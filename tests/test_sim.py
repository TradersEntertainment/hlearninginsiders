"""🧪 SİM — kâğıt üstü liq simülasyonu: plan, mum adımı, ücret/bakiye, kanca,
tick, sıfırlama, bağlantı, sayfa/mesaj.

Pinlenenler:
  • ön bacak balinanın tersi; hedef zincir sonu (defter yoksa liq fiyatı, ters bacak yok);
    stop %10; 2x × kullanılabilir bakiye; "hedef çok yakın" / "bakiye bağlı" / "sonda teyidi yok" atlanır
  • mum adımı: liq kesişimi → liq_ts; stop; hedef; aynı mumda ikisi → stop (both)
  • hedefte dolunca AYNI fiyattan ters bacak (maker), bakiye bileşik; sonraki mumdan izlenir
  • 30 dk kuralı (iğne gelmedi), balina patlamadı, ters bacak ömrü; tez bozuldu (watch 'close')
  • Telegram: SIM_CHAT_ID'ye; kanal boşsa gönderim yok ama defter ilerler; düşerse fail:sim, işlem geri alınmaz
  • tick: açık işlem yoksa istek yok; oluşan mum yeniden okunur, çift kapanış yok; mum hatası → taze mark; bayat → ertele
  • cryptoliq.scan gerçek akış: kademe 3 → out["sim"]==1; gönderim düşse de açılır
  • sıfırlama: run+1, açıklar 'reset', bakiye başa; sayfa güncel tur / tüm turlar
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-sim.db")
from app import db as dbm
from app.config import Config
from app.hl import universe as uni
from app.notify import Notifier
from app.radar import sim
from app.telegram import format as fmt

A, B, C = ("0x" + c * 40 for c in "abc")
HYPE = 40.0


def _cfg(chat="-200"):
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.crypto_chat_id = "-100"
    cfg.sim_chat_id = chat
    cfg.notify_sim = True
    cfg.sim_enabled = True
    cfg.sim_start_balance = 10_000.0
    cfg.sim_leverage = 2.0
    cfg.sim_margin_pct = 100.0
    cfg.sim_stop_pct = 10.0
    cfg.sim_min_tp_pct = 0.2
    cfg.sim_max_tp_pct = 0
    cfg.sim_after_liq_min = 30
    cfg.sim_pre_max_min = 360
    cfg.sim_post_tp_pct = 0.75
    cfg.sim_post_stop_pct = 10.0
    cfg.sim_post_max_min = 120
    cfg.sim_fee_taker_pct = 0.045
    cfg.sim_fee_maker_pct = 0.015
    cfg.sim_poll_sec = 60
    cfg.crypto_liq_min_usd = 500_000
    cfg.crypto_liq_dist_pct, cfg.crypto_liq_dist2_pct, cfg.crypto_liq_dist3_pct = 2.5, 1.0, 0.5
    cfg.crypto_liq_cooldown = 3600
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    return cfg


async def set_marks(marks, ts=None):
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {k: {"m": v, "oi": 1, "f": 0, "v": 1, "p": None}
                                             for k, v in marks.items()}, "ts": ts or dbm.now()})


async def _fresh(marks=None):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "sim.db"))
    sim._lock = None                             # önceki döngünün kilidi sızmasın
    from app.radar import sweeper
    sweeper._probe_sem = None
    await set_marks(marks or {"HYPE": HYPE, "PUMP": 0.0032})


def whale(side="short", ntl=1_300_000, liq=None, verified=True, addr=A, coin="HYPE", mark=HYPE):
    liq = liq or (mark * 1.004 if side == "short" else mark * 0.996)
    return {"address": addr, "side": side, "notional": float(ntl), "liq_px": liq, "coin": coin,
            "dist": abs(liq - mark) / mark * 100, "mark": mark, "need": 3, "sent": 2,
            "verified": verified, "leverage": 10.0, "entry_px": mark, "ts": dbm.now(), "entity": None}


def casc(end=HYPE * 1.024, no_book=False, exhausted=False):
    return {"direction": "up", "end_px": end, "total_usd": 17_400_000, "n_pos": 3, "no_book": no_book,
            "exhausted": exhausted, "coarse": False, "start_px": HYPE * 1.004, "move_pct": 2.4}


def cand(t, o, h, l, c=None):
    return {"t": t, "o": o, "h": h, "l": l, "c": c if c is not None else o}


def raw(cands):
    return [{"t": c["t"] * 1000, "o": str(c["o"]), "h": str(c["h"]), "l": str(c["l"]), "c": str(c["c"]), "v": "1"}
            for c in cands]


class Bot:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, text, chat_id=None):
        if self.ok:
            self.sent.append((chat_id, text))
        return self.ok


class Client:
    """Sahte HL: yalnız mumlar (pencereye bakmaz — test ne verirse). `err` → istek düşer."""

    def __init__(self, cands=None, err=False):
        self.cands, self.err, self.calls = cands or [], err, 0

    async def candles(self, coin, interval, start_ms, end_ms):
        self.calls += 1
        assert interval == "1m"
        if self.err:
            raise RuntimeError("HL 500")
        return raw(self.cands)

    async def meta_and_ctxs(self, dex=""):
        raise RuntimeError("ağ yok")           # fiyat yalnız kv'den


async def _rows(status=None):
    async with dbm.db() as c:
        cur = await c.execute("SELECT * FROM sim_trades" + (f" WHERE status='{status}'" if status else "")
                              + " ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def _alerts(kind):
    async with dbm.db() as c:
        cur = await c.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind=?", (kind,))
        return (await cur.fetchone())["n"]


# ------------------------------------------------ 1) ön bacak planı
def test_plan_leg1():
    cfg = _cfg()
    w = whale("short")                                          # liq 40.16
    p = sim.plan_leg1(cfg, "HYPE", HYPE, w, casc(), 10_000, 10_000)
    assert p["side"] == "long" and p["entry_px"] == HYPE and p["tp_src"] == "cascade"
    assert abs(p["tp_px"] - 40.96) < 1e-9 and abs(p["stop_px"] - 36.0) < 1e-9
    assert p["margin"] == 10_000 and p["notional"] == 20_000 and abs(p["qty"] - 500) < 1e-9
    assert abs(p["fee_usd"] - 9.0) < 1e-9, "giriş ücreti taker %0,045 × $20K"
    assert p["whale_addr"] == A and p["whale_liq_px"] == w["liq_px"] and p["casc_end_px"] == casc()["end_px"]
    assert p["note"] is None and "3 poz" in p["casc_note"]
    p2 = sim.plan_leg1(cfg, "HYPE", HYPE, w, casc(no_book=True), 10_000, 10_000)
    assert p2["tp_src"] == "liq" and p2["tp_px"] == w["liq_px"] and "defter yok" in p2["note"]
    assert sim.plan_leg1(cfg, "HYPE", HYPE, w, None, 10_000, 10_000)["tp_src"] == "liq"
    assert sim.plan_leg1(cfg, "HYPE", HYPE, w, casc(end=40.1), 10_000, 10_000)["tp_src"] == "liq", \
        "zincir sonu liq'in altındaysa tutarsız → liq fiyatı"
    cfg.sim_max_tp_pct = 1.0
    p3 = sim.plan_leg1(cfg, "HYPE", HYPE, w, casc(), 10_000, 10_000)
    assert p3["tp_src"] == "capped" and abs(p3["tp_px"] - 40.4) < 1e-9 and "kırpıldı" in p3["note"]
    cfg.sim_max_tp_pct = 0
    assert "hedef çok yakın" in sim.plan_leg1(cfg, "HYPE", HYPE, whale(liq=40.02), casc(end=40.04), 10_000, 10_000)["skip"]
    assert "bakiye bağlı" in sim.plan_leg1(cfg, "HYPE", HYPE, w, casc(), 300, 10_000)["skip"]
    assert sim.plan_leg1(cfg, "HYPE", HYPE, whale(verified=False), casc(), 10_000, 10_000)["skip"] == "sonda teyidi yok"
    assert sim.plan_leg1(cfg, "HYPE", None, w, casc(), 10_000, 10_000)["skip"] == "fiyat yok"
    wl = whale("long")                                          # liq 39.84
    p4 = sim.plan_leg1(cfg, "HYPE", HYPE, wl, casc(end=HYPE * 0.975), 5_000, 10_000)
    assert p4["side"] == "short" and abs(p4["stop_px"] - 44.0) < 1e-9 and abs(p4["tp_px"] - 39.0) < 1e-9
    assert p4["margin"] == 5_000 and p4["notional"] == 10_000
    # ters bacak planı
    parent = {**p, "id": 7, "coin": "HYPE"}
    q = sim.plan_leg2(cfg, parent, 40.96, 10_467.928, 10_467.928)
    assert q["side"] == "short" and q["leg"] == 2 and q["entry_px"] == 40.96 and q["parent_id"] == 7
    assert abs(q["tp_px"] - 40.96 * 0.9925) < 1e-9 and abs(q["stop_px"] - 40.96 * 1.1) < 1e-9
    assert abs(q["notional"] - 2 * 10_467.928) < 1e-6 and abs(q["fee_usd"] - q["notional"] * 0.00015) < 1e-9
    assert sim.plan_leg2(cfg, {**parent, "tp_src": "liq"}, 40.96, 10_000, 10_000) is None, "defter yok → ters bacak yok"
    assert sim.plan_leg2(cfg, parent, 40.96, 100, 10_000) is None, "bakiye yok"
    print("✅ plan) balinanın tersi, 2x × bakiye, hedef zincir sonu / liq / kırpık, atlanma nedenleri, ters bacak")


# ------------------------------------------------ 2) ücret / bakiye / istatistik (saf)
def test_pnl_fees():
    t = {"side": "long", "qty": 500.0, "entry_px": 40.0, "fee_usd": 9.0, "margin": 10_000.0}
    net, fee_total, pct = sim.pnl_of(t, 40.96, 0.015)
    assert abs(net - 467.928) < 1e-9 and abs(fee_total - 12.072) < 1e-9 and abs(pct - 4.67928) < 1e-9
    s = {"side": "short", "qty": 500.0, "entry_px": 40.0, "fee_usd": 9.0, "margin": 10_000.0}
    net2, _, _ = sim.pnl_of(s, 44.0, 0.045)                    # stop: -2000 - 9 - 9.9
    assert abs(net2 - (-2000 - 9 - 9.9)) < 1e-9
    assert sim.sizing(10_000, 100, 2, 40) == (10_000, 20_000, 500) and sim.sizing(10_000, 50, 2, 40)[0] == 5_000
    closed = [{"pnl_usd": 467.9, "fee_usd": 12.1, "leg": 1, "exit_reason": "tp", "coin": "HYPE", "exit_ts": 10, "id": 1},
              {"pnl_usd": -2018.9, "fee_usd": 18.9, "leg": 1, "exit_reason": "stop", "coin": "PUMP", "exit_ts": 20, "id": 2},
              {"pnl_usd": 150.0, "fee_usd": 6.3, "leg": 2, "exit_reason": "tp", "coin": "HYPE", "exit_ts": 15, "id": 3}]
    st = sim.stats(closed, 10_000, 8_599.0)
    assert st["n"] == 3 and st["wins"] == 2 and st["losses"] == 1 and abs(st["hit_rate"] - 200 / 3) < 1e-9
    assert st["best"]["coin"] == "HYPE" and st["worst"]["pnl_usd"] == -2018.9 and st["by_leg"][2]["n"] == 1
    assert abs(st["pnl_pct"] - (-14.01)) < 1e-9 and st["reasons"] == {"tp": 2, "stop": 1}
    pts = sim.curve(closed, 10_000, 0)
    assert [round(p[1], 1) for p in pts] == [10_000, 10_467.9, 10_617.9, 8_599.0], "kapanış sırasıyla bileşik"
    svg = sim.svg_curve(pts, 10_000)
    assert svg.startswith("<svg") and svg.count("<circle") == 3 and "var(--red)" in svg and "<title>" in svg
    assert sim.svg_curve(pts[:1], 10_000) == "" and sim.stats([], 10_000, 10_000)["hit_rate"] is None
    print("✅ ücret) iki taraf notional üzerinden; pnl_pct marjine göre; istatistik/eğri/SVG")


# ------------------------------------------------ 3) mum adımı (saf)
def test_step_and_rules():
    cfg = _cfg()
    base = {"leg": 1, "side": "long", "stop_px": 36.0, "tp_px": 40.96, "whale_side": "short",
            "whale_liq_px": 40.16, "entry_ts": 1_000_000}
    t = dict(base)
    assert sim.step(t, cand(1000, 40.0, 40.1, 39.9)) is None and t.get("liq_ts") is None and t["hi_px"] == 40.1
    assert sim.step(t, cand(1060, 40.1, 40.3, 40.0)) is None and t["liq_ts"] == 1060, "liq kesildi, hedef yok"
    ev = sim.step(t, cand(1120, 40.2, 41.0, 40.1))
    assert ev == {"reason": "tp", "px": 40.96, "ts": 1120, "both": False} and t["hi_px"] == 41.0
    t2 = dict(base)
    ev = sim.step(t2, cand(1000, 40.0, 41.5, 35.5))
    assert ev["reason"] == "stop" and ev["both"] is True and ev["px"] == 36.0, "aynı mumda ikisi → stop"
    t3 = dict(base)
    assert sim.step(t3, cand(1000, 40.0, 40.1, 35.9))["reason"] == "stop"
    # canlı liq fiyatı (teminat eklendi, liq yukarı kaydı) kesişimi belirler
    t4 = dict(base)
    assert sim.step(t4, cand(1000, 40.0, 40.3, 39.9), live_liq_px=40.5) is None and not t4.get("liq_ts")
    # short taraf simetrik
    s = {"leg": 1, "side": "short", "stop_px": 44.0, "tp_px": 39.0, "whale_side": "long", "whale_liq_px": 39.84}
    assert sim.step(s, cand(1000, 40.0, 40.2, 38.9))["reason"] == "tp" and s["liq_ts"] == 1000
    assert sim.step(dict(s), cand(1000, 40.0, 44.2, 39.9))["reason"] == "stop"
    # ters bacakta liq kesişimi aranmaz
    l2 = {"leg": 2, "side": "short", "stop_px": 45.0, "tp_px": 40.65, "whale_side": "short", "whale_liq_px": 40.16}
    assert sim.step(l2, cand(1000, 41.0, 41.1, 40.9)) is None and "liq_ts" not in l2
    # süre kuralları
    now = 2_000_000
    assert sim.expire({"leg": 1, "entry_ts": now - 100, "liq_ts": now - 1801}, now, cfg)["reason"] == "timeout"
    assert "iğne" in sim.expire({"leg": 1, "entry_ts": now - 100, "liq_ts": now - 1801}, now, cfg)["why"]
    assert sim.expire({"leg": 1, "entry_ts": now - 100, "liq_ts": now - 1799}, now, cfg) is None
    assert "patlamadı" in sim.expire({"leg": 1, "entry_ts": now - 360 * 60}, now, cfg)["why"]
    assert "ters bacak" in sim.expire({"leg": 2, "entry_ts": now - 7200}, now, cfg)["why"]
    assert sim.expire({"leg": 2, "entry_ts": now - 7199}, now, cfg) is None
    # balina ne yaptı
    t5 = {"leg": 1}
    assert sim.whale_event(t5, {"closed_ts": 5, "closed_kind": "liq"}) is None and t5["liq_ts"] == 5
    assert sim.whale_event({"leg": 1}, {"closed_ts": 5, "closed_kind": "close"})["reason"] == "void"
    assert sim.whale_event({"leg": 1}, {"closed_ts": 5, "closed_kind": "unknown"})["reason"] == "void"
    assert sim.whale_event({"leg": 1, "liq_ts": 3}, {"closed_ts": 5, "closed_kind": "unknown"}) is None
    assert sim.whale_event({"leg": 1}, {"closed_ts": None}) is None and sim.whale_event({"leg": 2}, {"closed_ts": 5, "closed_kind": "close"}) is None
    # mum seçimi: tamamen kapanmış eski mumlar dışarıda, oluşan mum içeride
    cs = [cand(900, 1, 1, 1), cand(960, 1, 1, 1), cand(1020, 1, 1, 1)]
    assert [c["t"] for c in sim.select_candles(cs, 1020, 900)] == [1020]
    assert [c["t"] for c in sim.select_candles(cs, 0, 950)] == [900, 960, 1020], "giriş dakikası dahil"
    assert [c["t"] for c in sim.select_candles(cs, 0, 960)] == [960, 1020], "tam kapanmış mum dışarıda"
    print("✅ adım) liq kesişimi → liq_ts; stop/hedef; aynı mumda stop; canlı liq; süre ve balina kuralları; mum seçimi")


# ------------------------------------------------ 4) kanca → tick → ters bacak (uçtan uca, sahte mumlar)
def test_flow_tp_then_leg2():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        cli = Client()
        assert await sim.on_signal(cfg, cli, notifier, "HYPE", HYPE, [whale()], casc()) is True
        rows = await _rows()
        assert len(rows) == 1 and rows[0]["status"] == "open" and rows[0]["side"] == "long" and rows[0]["run"] == 1
        tid = rows[0]["id"]
        assert bot.sent[0][0] == "-200" and "SİM AÇILDI" in bot.sent[0][1] and "HYPE" in bot.sent[0][1]
        assert "hedef <b>40.96</b>" in bot.sent[0][1] and "SHORT</b> açılır" in bot.sent[0][1]
        assert await sim.on_signal(cfg, cli, notifier, "HYPE", HYPE, [whale()], casc()) is False
        assert len(await _rows()) == 1 and len(bot.sent) == 1, "açık işlem varken tekrar sinyal sessiz"
        # PUMP sinyali: bakiye bağlı → atlanır (1 saatte tek satır)
        pw = whale(addr=B, coin="PUMP", mark=0.0032, liq=0.0032 * 1.004, ntl=800_000)
        assert await sim.on_signal(cfg, cli, notifier, "PUMP", 0.0032, [pw], None) is False
        assert await sim.on_signal(cfg, cli, notifier, "PUMP", 0.0032, [pw], None) is False
        sk = await _rows("skipped")
        assert len(sk) == 1 and "bakiye bağlı" in sk[0]["skip_reason"] and sk[0]["coin"] == "PUMP"
        # zaman: giriş 10 dk önce; mumlar liq'i kesip hedefe uzanıyor
        now = dbm.now()
        t0 = now - 600 - (now - 600) % 60
        await sim._set(tid, entry_ts=t0, last_eval_ts=t0)
        cli.cands = [cand(t0, 40.0, 40.05, 39.95), cand(t0 + 60, 40.0, 40.30, 39.9, 40.2),
                     cand(t0 + 120, 40.2, 41.0, 40.1, 40.8)]
        out = await sim.tick(cfg, cli, notifier)
        assert out["closed"] == 1 and out["opened2"] == 1 and cli.calls == 1 and out["open"] == 1, out
        c1 = (await sim.closed_trades(1))[0]
        assert c1["exit_reason"] == "tp" and abs(c1["exit_px"] - 40.96) < 1e-9 and c1["liq_ts"] == t0 + 60
        assert abs(c1["pnl_usd"] - 467.928) < 1e-6 and abs(c1["fee_usd"] - 12.072) < 1e-6
        assert c1["hi_px"] == 41.0 and c1["lo_px"] == 39.9 and c1["exit_ts"] == t0 + 180
        acc = await sim.account(cfg)
        assert abs(acc["balance"] - 10_467.928) < 1e-6, "bakiye bileşik"
        leg2 = (await sim.open_trades())[0]
        assert leg2["leg"] == 2 and leg2["side"] == "short" and abs(leg2["entry_px"] - 40.96) < 1e-9
        assert leg2["parent_id"] == tid and leg2["entry_src"] == "tp" and leg2["tp_src"] == "pct"
        assert abs(leg2["margin"] - 10_467.928) < 1e-6 and leg2["last_eval_ts"] == t0 + 180, "iğne mumu yeniden okunmaz"
        assert abs(leg2["tp_px"] - 40.96 * 0.9925) < 1e-9 and leg2["liq_ts"] == t0 + 60
        msg = bot.sent[1][1]
        assert "SİM KAPANDI" in msg and "✅ hedef" in msg and "SHORT açıldı</b> @ 40.96" in msg and "+$468" in msg
        assert f"bakiye <b>{fmt.usd(10_467.928)}</b>" in msg and "isabet %100" in msg
        # aynı mumlar yeniden gelirse ters bacak kapanmaz (iğne mumu dışarıda), çift kapanış yok
        out = await sim.tick(cfg, cli, notifier)
        assert out["closed"] == 0 and len(await _rows("closed")) == 1
        # geri çekilme (şimdiki dakikanın mumu): hedef 40.6528 → low 40.5
        now = dbm.now()
        cli.cands = [cand(now - now % 60, 40.9, 40.95, 40.5, 40.7)]
        out = await sim.tick(cfg, cli, notifier)
        assert out["closed"] == 1 and out["opened2"] == 0 and out["open"] == 0
        c2 = (await sim.closed_trades(1))[0]
        assert c2["leg"] == 2 and c2["exit_reason"] == "tp" and c2["pnl_usd"] > 0
        qty = leg2["qty"]
        exp = qty * (40.96 - 40.96 * 0.9925) - leg2["fee_usd"] - qty * 40.96 * 0.9925 * 0.00015
        assert abs(c2["pnl_usd"] - exp) < 1e-6
        acc = await sim.account(cfg)
        assert abs(acc["balance"] - (10_467.928 + exp)) < 1e-6
        assert "ters bacak" in bot.sent[2][1] and "✅ hedef" in bot.sent[2][1]
        # açık işlem yok → istek yok
        calls = cli.calls
        out = await sim.tick(cfg, cli, notifier)
        assert cli.calls == calls and out["open"] == 0
        st = await dbm.kv_get("sim_stats")
        assert st["opened"] == 1 and st["skipped_signals"] == 1 and st["balance"] == acc["balance"]
        print("✅ akış) sinyal → ön bacak → liq kesildi → hedefte kapandı → aynı fiyattan ters bacak → geri çekilmede kapandı; bakiye bileşik")
    asyncio.run(run())


# ------------------------------------------------ 5) stop / tez bozuldu / süre
def test_flow_stop_void_timeout():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        cli = Client()
        assert await sim.on_signal(cfg, cli, notifier, "HYPE", HYPE, [whale()], casc())
        tid = (await _rows())[0]["id"]
        now = dbm.now()
        t0 = now - 600 - (now - 600) % 60
        await sim._set(tid, entry_ts=t0, last_eval_ts=t0)
        # a) aynı mumda hedef de stop da → stop, ters bacak yok, not düşer
        cli.cands = [cand(t0 + 60, 40.0, 41.5, 35.5, 36.5)]
        out = await sim.tick(cfg, cli, notifier)
        c = (await sim.closed_trades(1))[0]
        assert out["closed"] == 1 and out["opened2"] == 0 and c["exit_reason"] == "stop" and c["exit_px"] == 36.0
        assert "stop sayıldı" in c["note"] and abs(c["pnl_usd"] - (-2000 - 9 - 18000 * 0.00045)) < 1e-6
        assert "🛑 stop" in bot.sent[-1][1] and "stop sayıldı" in bot.sent[-1][1] and not await sim.open_trades()
        # b) tez bozuldu: balina liq olmadan kapattı (watch 'close') → void, piyasadan (son mum kapanışı)
        assert await sim.on_signal(cfg, cli, notifier, "HYPE", HYPE, [whale()], casc())
        tid = (await sim.open_trades())[0]["id"]
        await sim._set(tid, entry_ts=t0, last_eval_ts=t0)
        async with dbm.db() as conn:
            await conn.execute(
                "INSERT INTO cryptoliq_watch(coin,address,side,notional,liq_px,stage,first_ts,updated_ts,"
                "closed_ts,closed_kind) VALUES('HYPE',?,'short',1300000,?,3,?,?,?,'close')",
                (A, HYPE * 1.004, t0, now, now - 30))
        cli.cands = [cand(now - 60 - (now - 60) % 60, 40.0, 40.05, 39.95, 39.98)]
        out = await sim.tick(cfg, cli, notifier)
        c = (await sim.closed_trades(1))[0]
        assert c["exit_reason"] == "void" and abs(c["exit_px"] - 39.98) < 1e-9 and "tez bozuldu" in c["note"]
        assert "🚫 tez bozuldu" in bot.sent[-1][1] and out["opened2"] == 0
        async with dbm.db() as conn:
            await conn.execute("DELETE FROM cryptoliq_watch")
        # c) liq geldi, iğne 30 dk içinde hedefe uzanmadı → timeout, ters bacak yok
        assert await sim.on_signal(cfg, cli, notifier, "HYPE", HYPE, [whale()], casc())
        tid = (await sim.open_trades())[0]["id"]
        await sim._set(tid, entry_ts=now - 3000, last_eval_ts=now - 60, liq_ts=now - 1801)
        out = await sim.tick(cfg, cli, notifier)
        c = (await sim.closed_trades(1))[0]
        assert c["exit_reason"] == "timeout" and "iğne" in c["note"] and out["opened2"] == 0
        assert "⏱ süre doldu" in bot.sent[-1][1]
        # d) balina hiç patlamadı (6 saat) → timeout "liq gelmedi"
        assert await sim.on_signal(cfg, cli, notifier, "HYPE", HYPE, [whale()], casc())
        tid = (await sim.open_trades())[0]["id"]
        await sim._set(tid, entry_ts=now - 361 * 60, last_eval_ts=now - 60)
        await sim.tick(cfg, cli, notifier)
        assert "patlamadı" in (await sim.closed_trades(1))[0]["note"]
        # e) ters bacak süresi
        acc = await sim.account(cfg)
        leg2 = sim.plan_leg2(cfg, {"id": 99, "coin": "HYPE", "side": "long", "tp_src": "cascade"}, 40.96,
                             acc["balance"], acc["balance"])
        leg2.update(run=1, status="open", entry_ts=now - 7300, last_eval_ts=now - 60, created_ts=now - 7300)
        await sim._insert(leg2)
        cli.cands = [cand(now - 60 - (now - 60) % 60, 40.96, 40.97, 40.95, 40.96)]   # ne hedef ne stop
        out = await sim.tick(cfg, cli, notifier)
        c = (await sim.closed_trades(1))[0]
        assert c["leg"] == 2 and c["exit_reason"] == "timeout" and "ters bacak" in c["note"]
        print("✅ kurallar) aynı mumda stop; tez bozuldu (watch close) piyasadan; 30 dk iğne; 6 s liq gelmedi; ters bacak ömrü")
    asyncio.run(run())


# ------------------------------------------------ 6) kanca kapıları / Telegram
def test_on_signal_gates():
    async def run():
        await _fresh()
        bot = Bot()
        cli = Client()
        # kapalı / kademe 3 değil / küçük → hiçbir şey
        cfg = _cfg(); cfg.sim_enabled = False
        assert await sim.on_signal(cfg, cli, Notifier(cfg, bot), "HYPE", HYPE, [whale()], casc()) is False
        cfg = _cfg()
        w2 = whale(); w2["need"] = 2
        assert await sim.on_signal(cfg, cli, Notifier(cfg, bot), "HYPE", HYPE, [w2], casc()) is False
        assert await sim.on_signal(cfg, cli, Notifier(cfg, bot), "HYPE", HYPE, [whale(ntl=400_000)], casc()) is False
        assert await _rows() == [] and bot.sent == []
        # doğrulanmamış → atlanan satırı (1 saatte tek)
        assert await sim.on_signal(cfg, cli, Notifier(cfg, bot), "HYPE", HYPE, [whale(verified=False)], casc()) is False
        assert await sim.on_signal(cfg, cli, Notifier(cfg, bot), "HYPE", HYPE, [whale(verified=False)], casc()) is False
        sk = await _rows("skipped")
        assert len(sk) == 1 and sk[0]["skip_reason"] == "sonda teyidi yok" and sk[0]["whale_addr"] == A
        # kanal boş → gönderim yok, satır var, fail:sim yok
        cfg = _cfg(chat="")
        assert await sim.on_signal(cfg, cli, Notifier(cfg, bot), "HYPE", HYPE, [whale()], casc()) is True
        assert bot.sent == [] and len(await _rows("open")) == 1 and await _alerts("fail:sim") == 0
        st = await dbm.kv_get("sim_stats")
        assert st["opened"] == 1 and st.get("sent", 0) == 0
        # bot düşerse: defter ilerler, fail:sim kaydı
        await _fresh()
        cfg = _cfg()
        bad = Bot(ok=False)
        assert await sim.on_signal(cfg, cli, Notifier(cfg, bad), "HYPE", HYPE, [whale()], casc()) is True
        assert len(await _rows("open")) == 1 and await _alerts("fail:sim") == 1 and await _alerts("sent:sim") == 0
        # bildirim kapalıysa hiç denemez
        await _fresh()
        cfg = _cfg(); cfg.notify_sim = False
        ok_bot = Bot()
        assert await sim.on_signal(cfg, cli, Notifier(cfg, ok_bot), "HYPE", HYPE, [whale()], casc()) is True
        assert ok_bot.sent == [] and await _alerts("fail:sim") == 0
        assert sim.gate(cfg, Notifier(cfg, ok_bot)) == "bildirim kapalı (notify_sim)"
        assert sim.gate(_cfg(chat=""), None) == "SIM_CHAT_ID yok" and sim.gate(_cfg(), None) == "bot yok"
        c2 = _cfg(); c2.crypto_chat_id = ""
        assert "CRYPTO_CHAT_ID" in sim.source_gate(c2) and sim.source_gate(_cfg()) == ""
        print("✅ kapı) kapalı/kademe/boyut sessiz; doğrulanmamış tek atlanma; kanal boş → gönderim yok ama defter; düşerse fail:sim")
    asyncio.run(run())


# ------------------------------------------------ 7) tick: mum yok → mark; bayat → ertele; damga
def test_tick_candles():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        cli = Client(err=True)
        assert await sim.on_signal(cfg, cli, notifier, "HYPE", HYPE, [whale()], casc())
        tid = (await _rows())[0]["id"]
        now = dbm.now()
        await sim._set(tid, entry_ts=now - 300, last_eval_ts=now - 300)
        out = await sim.tick(cfg, cli, notifier)
        assert out["candle_err"] == 1 and out["mark_used"] == 1 and out["deferred"] == 0 and out["closed"] == 0
        t = (await _rows())[0]
        assert t["last_eval_ts"] >= now and t["status"] == "open", "taze kv fiyatı düz mum sayıldı, damga ilerledi"
        await set_marks({"HYPE": HYPE}, ts=now - 700)             # bayat
        out = await sim.tick(cfg, cli, notifier)
        assert out["deferred"] == 1 and (await _rows())[0]["last_eval_ts"] == t["last_eval_ts"], "ertelendi, damga aynı"
        await set_marks({"HYPE": 30.0})                            # taze ama stop altı → mark ile stop
        out = await sim.tick(cfg, cli, notifier)
        assert out["closed"] == 1 and (await sim.closed_trades(1))[0]["exit_reason"] == "stop"
        # istek sayısı = açık işlem: iki coin
        await _fresh({"HYPE": HYPE, "SOL": 170.0})
        cfg.sim_margin_pct = 50
        cli = Client(cands=[])
        assert await sim.on_signal(cfg, cli, notifier, "HYPE", HYPE, [whale()], casc())
        sw = whale(addr=B, coin="SOL", mark=170.0, liq=170.0 * 1.004, ntl=900_000)
        assert await sim.on_signal(cfg, cli, notifier, "SOL", 170.0, [sw], None)
        opens = await sim.open_trades()
        assert len(opens) == 2 and opens[0]["margin"] == 5_000 and opens[1]["margin"] == 2_500, "yarı marjin bileşik"
        cli.err = True
        await sim.tick(cfg, cli, notifier)
        assert cli.calls == 2
        print("✅ tick) mum hatası → taze fiyat; bayat → ertele; damga; işlem başına 1 istek; marjin payı")
    asyncio.run(run())


# ------------------------------------------------ 8) cryptoliq.scan gerçek akış
def test_scan_hook():
    class ScanClient:
        def __init__(self, rows):
            self.rows = rows
            self.calls = 0

        async def meta_and_ctxs(self, dex=""):
            return [{"universe": [{"name": "PUMP"}]},
                    [{"markPx": "0.0032", "openInterest": "1", "funding": "0", "dayNtlVlm": "1"}]]

        async def clearinghouse_all(self, addr, dexes):
            aps = [{"position": {"coin": p["coin"], "szi": str(-p["notional"] / 0.0032),
                                 "positionValue": str(p["notional"]), "liquidationPx": str(p["liq_px"]),
                                 "entryPx": "0.0032", "leverage": {"value": "10"}, "unrealizedPnl": "0"}}
                   for p in self.rows if p["address"] == addr]
            return {"main": {"assetPositions": aps, "marginSummary": {"accountValue": "1000000"}},
                    "xyz": {"assetPositions": []}}

        async def user_fills_by_time(self, addr, start_ms, end_ms=None):
            return []

        async def candles(self, coin, interval, start_ms, end_ms):
            self.calls += 1
            raise RuntimeError("mum yok")

        async def l2_book(self, coin, n_sig_figs=None):
            m = 0.0032
            return {"levels": [[{"px": str(m * 0.995), "sz": "5e8"}],
                               [{"px": str(m * 1.006), "sz": "5e8"}, {"px": str(m * 1.02), "sz": "5e8"}]]}

    async def run():
        from app.radar import cryptoliq as cl
        await _fresh()
        rows = [{"coin": "PUMP", "address": A, "side": "short", "notional": 1_200_000.0,
                 "liq_px": 0.0032 * 1.003, "entry_px": 0.0032, "leverage": 10, "ts": dbm.now()}]
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:SNDK','SNDK')")
            for p in rows:
                await c.execute(
                    "INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,"
                    "upnl,notional,ts,closed_ts) VALUES(?,?,'',?,1,?,?,?,0,?,?,NULL)",
                    (p["coin"], p["address"], p["side"], p["entry_px"], p["leverage"], p["liq_px"],
                     p["notional"], p["ts"]))
        cfg = _cfg()
        cfg.crypto_liq_chart = False
        bot = Bot(ok=False)                                    # alarm DÜŞSE de sim açılır
        cli = ScanClient(rows)
        out = await cl.scan(cfg, cli, Notifier(cfg, bot))
        assert out["candidates"] == 1 and out["failed"] == 1 and out["sim"] == 1, out
        r = await _rows("open")
        assert len(r) == 1 and r[0]["coin"] == "PUMP" and r[0]["side"] == "long" and r[0]["tp_src"] == "cascade"
        assert r[0]["whale_addr"] == A and abs(r[0]["whale_liq_px"] - 0.0032 * 1.003) < 1e-12
        out = await cl.scan(cfg, cli, Notifier(cfg, bot))
        assert out["sim"] == 0 and len(await _rows()) == 1, "açık işlem varken tekrar sinyal sessiz"
        cfg.sim_enabled = False
        await _fresh()
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:SNDK','SNDK')")
            for p in rows:
                await c.execute(
                    "INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,"
                    "upnl,notional,ts,closed_ts) VALUES(?,?,'',?,1,?,?,?,0,?,?,NULL)",
                    (p["coin"], p["address"], p["side"], p["entry_px"], p["leverage"], p["liq_px"],
                     p["notional"], p["ts"]))
        out = await cl.scan(cfg, cli, Notifier(cfg, Bot()))
        assert out["alerted"] == 1 and out["sim"] == 0 and await _rows() == []
        print("✅ kanca) kademe 3 taraması sim açar (alarm düşse de); tekrar sessiz; sim kapalıyken alarm yine gider")
    asyncio.run(run())


# ------------------------------------------------ 9) sıfırlama / sayfa / özet / render
def test_reset_page_render():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        cli = Client()
        assert await sim.on_signal(cfg, cli, notifier, "HYPE", HYPE, [whale()], casc())
        await set_marks({"HYPE": 40.4})
        s = await sim.page(cfg)
        assert s["cur_run"] == 1 and len(s["opens"]) == 1 and s["opens"][0]["live"]["usd"] > 0
        assert abs(s["equity"] - (10_000 + s["opens"][0]["live"]["usd"])) < 1e-9 and s["svg"] == ""
        assert s["gate"] == "" and s["source_gate"] == "" and s["stats"]["n"] == 0
        txt = fmt.sim_summary(await sim.summary(cfg))
        assert "🧪 <b>SİM</b>" in txt and "açık: <b>HYPE</b>" in txt and "şimdi 40.40" in txt and "liq'i bekleniyor" in txt
        from app.web.routes import templates
        html = templates.env.get_template("sim.html").render(
            request=type("R", (), {"url": type("U", (), {"path": "/sim"})()})(), k="", is_admin=True,
            has_pw=True, s=s, cfg=cfg)
        from app.web.routes import _usd
        assert "Liq simülasyonu" in html and 'id="sim-reset"' in html and _usd(10_000) in html
        assert "balina liq&#39;i bekleniyor" in html
        # sıfırla: açık 'reset' ile kv fiyatından kapanır, tur 2, bakiye başa
        res = await sim.reset(cfg, notifier)
        assert res["run"] == 2 and res["closed"] == 1 and res["balance"] == 10_000
        c = (await sim.closed_trades(None))[0]
        assert c["exit_reason"] == "reset" and abs(c["exit_px"] - 40.4) < 1e-9 and c["run"] == 1
        acc = await sim.account(cfg)
        assert acc["run"] == 2 and acc["balance"] == 10_000 and "SİM SIFIRLANDI" in bot.sent[-1][1]
        s2 = await sim.page(cfg)
        assert s2["closed_total"] == 0 and s2["cur_run"] == 2 and s2["opens"] == []
        s3 = await sim.page(cfg, "all")
        assert s3["closed_total"] == 1 and s3["run"] is None and s3["stats"]["pnl_pct"] is None
        s4 = await sim.page(cfg, "1")
        assert s4["closed_total"] == 1 and s4["run"] == 1 and s4["svg"].startswith("<svg")
        html = templates.env.get_template("sim.html").render(
            request=type("R", (), {"url": type("U", (), {"path": "/sim"})()})(), k="?key=x", is_admin=False,
            has_pw=True, s=s3, cfg=cfg)
        assert "tüm turlar" in html and "🔄 sıfırlama" in html and 'href="/sim?key=x"' in html and 'id="sim-reset"' not in html
        # boş/uyarı dalları
        cfg2 = _cfg(chat=""); cfg2.crypto_chat_id = ""
        s5 = await sim.page(cfg2)
        html = templates.env.get_template("sim.html").render(
            request=type("R", (), {"url": type("U", (), {"path": "/sim"})()})(), k="", is_admin=False,
            has_pw=False, s=s5, cfg=cfg2)
        assert "SIM_CHAT_ID yok" in html and "Sinyal kaynağı kapalı" in html and "Açık işlem yok" in html
        txt = fmt.sim_summary(await sim.summary(cfg2))
        assert "⚠️ Telegram: SIM_CHAT_ID yok" in txt and "sinyal kaynağı kapalı" in txt
        # mesaj şablonları
        t = {"coin": "HYPE", "side": "long", "leverage": 2, "leg": 1, "entry_px": 40.0, "tp_px": 40.96, "stop_px": 36.0,
             "qty": 500, "notional": 20000, "margin": 10000, "tp_src": "liq", "whale_addr": A, "whale_side": "short",
             "whale_notional": 1.3e6, "whale_liq_px": 40.16, "note": "defter yok → hedef liq fiyatı, ters bacak yok"}
        m = fmt.sim_opened(t, {"balance": 10000, "run": 1}, cfg)
        assert "ters bacak YOK" in m and "defter yok" in m and "liq fiyatı (defter yok)" in m
        m2 = fmt.sim_closed({**t, "exit_reason": "tp", "exit_px": 40.96, "pnl_usd": 467.9, "pnl_pct": 4.7,
                             "fee_usd": 12.1, "entry_ts": 0, "exit_ts": 900}, {"balance": 10467.9, "start_balance": 10000}, cfg)
        assert "ters bacak açılmadı: hedef zincir sonu değildi" in m2 and "15 dk" in m2
        # Telegram /sim: ana sohbet ve SİM kanalı (kendi kanalı), yabancı sohbet sessiz
        from app.telegram.bot import TelegramBot
        tb = TelegramBot(cfg, None, cli, {})
        sent = []

        async def fake_send(text, chat_id=None):
            sent.append((chat_id, text))
            return True
        tb.send = fake_send
        await tb._handle_update({"channel_post": {"chat": {"id": -200}, "text": "/sim"}})
        await tb._handle_update({"message": {"chat": {"id": 111}, "text": "/sim"}})
        await tb._handle_update({"message": {"chat": {"id": 999}, "text": "/sim"}})
        assert [c for c, _ in sent] == ["-200", "111"] and all("🧪 <b>SİM</b>" in t for _, t in sent)
        print("✅ sıfırlama/sayfa) kv fiyatından reset, tur 2, bakiye başa; sayfa güncel tur / tüm turlar; render; özet; uyarılar; /sim komutu")
    asyncio.run(run())


# ------------------------------------------------ 10) bağlantı
def test_wiring():
    from app.config import EDITABLE_FIELDS
    c = Config()
    fields = ("sim_enabled", "sim_start_balance", "sim_leverage", "sim_margin_pct", "sim_stop_pct",
              "sim_min_tp_pct", "sim_max_tp_pct", "sim_after_liq_min", "sim_pre_max_min", "sim_post_tp_pct",
              "sim_post_stop_pct", "sim_post_max_min", "sim_fee_taker_pct", "sim_fee_maker_pct", "sim_poll_sec",
              "notify_sim")
    for f in fields:
        assert f in EDITABLE_FIELDS and hasattr(c, f), f
        assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
    assert "sim_chat_id" not in EDITABLE_FIELDS and hasattr(c, "sim_chat_id"), "chat id env-only"
    assert c.sim_start_balance == 10_000 and c.sim_leverage == 2 and c.sim_stop_pct == 10 and c.sim_after_liq_min == 30
    assert c.sim_post_tp_pct == 0.75 and c.sim_post_stop_pct == 10 and c.sim_max_tp_pct == 0 and c.notify_sim is True
    from app.health import limits, periods
    assert "sim" in limits(c) and "sim" in periods(c)
    from app.notify import KINDS
    assert KINDS["sim"][0] == "notify_sim" and KINDS["sim"][2] == "high"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rd = lambda *p: open(os.path.join(root, *p), encoding="utf-8").read()  # noqa: E731
    assert '_spawn("sim"' in rd("app", "main.py") and "sim_trades" in rd("app", "db.py")
    assert "sim.on_signal" in rd("app", "radar", "cryptoliq.py")
    assert "nl('/sim'" in rd("app", "web", "templates", "base.html")
    assert '@router.get("/sim")' in rd("app", "web", "routes.py") and '"/sim/sifirla"' in rd("app", "web", "routes.py")
    assert 'getattr(self.cfg, "sim_chat_id", "")' in rd("app", "telegram", "bot.py") and "_cmd_sim" in rd("app", "telegram", "bot.py")
    assert "SIM_CHAT_ID" in rd(".env.example") and "## Simülasyon" in rd("README.md")
    assert '"sim_stats"' in rd("app", "diag.py") and "/sim —" in fmt.help_text()
    print("✅ bağlantı) 16 ayar künyeli, chat id env-only, bekçi/tip/döngü/kanca/sayfa/bot/belge yerinde")
