"""🧪 İlk dalga kurallar, planlı bakışlar ve teslim (Faz 5–7).

Pinlenenler:
  • türetilmiş kurallar: akış ailesinin olayı DONMUŞ süzgeçten geçerse aynı anda kural olayı doğar;
    follow/fade/−işaret yönleri; coin başına birimde bir kez; ayarlı taban eşiği aşınca türemez;
    09:30 ET kovası hisse kuralına girmez
  • ilk dalga: her ileri kural geçerli, yuvası var, α = LAB_ALPHA/K kayıtta; log aileleri yuvasız
  • planlı bakış: yalnız kapanmış kümeler; eşik aşılınca BİR kez (UNIQUE), güçlü etki → gecti + sahibe
    onay sorusu, etkisiz → devam; durum yalnız burada değişir; ölçülemeyen payı > %20 geçemez
  • onay: yalnız sahip, yalnız 'gecti' durumunda; canlı kuralın YENİ olayı ölçülü mesajla sahibe
    (public=False, açık chat_id); onaysız kural mesaj atmaz; app/lab wake içe aktarmaz
  • canlıda düşüş: onay sonrası 20 kümede anlamlı negatif → durdu + tek mesaj
"""
import asyncio
import importlib.util
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-labrules.db")
from app import db as dbm  # noqa: E402
from app.lab import deliver, looks, registry, specs  # noqa: E402

_spec = importlib.util.spec_from_file_location("lab_core_r", os.path.join(HERE, "test_lab_core.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
T = core.T
ET = ZoneInfo("America/New_York")


async def _prod(name):
    cfg = await core._fresh(name)
    cfg.lab_k_budget = 40
    out = await registry.sync(cfg, ts=T - 86400)
    return cfg, out


def _q(rid):
    return [e for e in registry._Q if e["rule_id"] == rid]


def test_first_wave_registered():
    async def run():
        cfg, out = await _prod("rules0.db")
        slotted = [r for r in specs.RULES if r["evidence"] != "log"]
        assert out["slots_used"] == len(slotted) and not out["retired"]
        oru = [r for r in slotted if r.get("custom") == "oru"]
        assert len(oru) == 2 and all(r["evidence"] == "frozen_backtest" for r in oru)
        fwd = [r for r in slotted if not r.get("custom")]
        assert len(fwd) >= 12
        async with dbm.db() as c:
            rows = {r["rule_id"]: dict(r) for r in await (await c.execute("SELECT * FROM lab_rules")).fetchall()}
        for r in fwd:
            row = rows[r["id"]]
            assert row["status"] == "kagit" and row["slot"] and abs(row["alpha"] - 0.05 / 40) < 1e-15, row
            assert r["src"] in {x["id"] for x in specs.RULES if x["evidence"] == "log"}
            assert not specs.validate(r)
        assert all(rows[r["id"]]["slot"] is None for r in specs.RULES if r["evidence"] == "log")
        assert len({rows[r["id"]]["slot"] for r in slotted}) == len(slotted), "yuvalar tekil"
        assert all(rows[r["id"]]["status"] == "aday" for r in oru), "denetim: Aşama A bekliyor"
        sha = registry.spec_sha(fwd[0])
        changed = dict(fwd[0], where=fwd[0]["where"] + [["usd", ">=", 1]])
        assert registry.spec_sha(changed) != sha, "süzgeç hash'e girer"
    asyncio.run(run())
    print("✅ ilk dalga) ileri kurallar yuvalı, α = 0.05/40 kayıtta; log aileleri yuvasız; süzgeç hash'te")


def test_derivation():
    async def run():
        cfg, _ = await _prod("rules1.db")
        feat = {"usd": 1_200_000, "kat": 3.1, "chg": 0.8, "kova": T - 300, "piyasa": "crypto", "taban": 50_000}
        registry.emit("LOG-VOL", "SOL", T, side=1, px_ref=150.0, trig_key="b1", features=feat)
        f, t = _q("HAC-KRP-F"), _q("HAC-KRP-T")
        assert len(f) == 1 and len(t) == 1 and f[0]["side"] == 1 and t[0]["side"] == -1
        assert f[0]["trig_key"] == str(T // (4 * 3600)) and f[0]["ts_decision"] == T
        assert not _q("HAC-HSS-F"), "kripto olayı hisse kuralına girmez"
        registry.emit("LOG-VOL", "SOL", T + 600, side=-1, trig_key="b2", features=feat)
        assert len(_q("HAC-KRP-F")) == 1, "aynı 4 saatlik birimde coin başına bir kez"
        assert len(_q("LOG-VOL")) == 2, "aile kaydı yine de iki"
        registry.emit("LOG-VOL", "ETH", T, side=1, trig_key="b3", features={**feat, "usd": 900_000})
        registry.emit("LOG-VOL", "BNB", T, side=1, trig_key="b4", features={**feat, "taban": 2_000_000})
        assert {e["coin"] for e in _q("HAC-KRP-F")} == {"SOL"}, "eşik altı ve yüksek ayarlı taban türemez"
        # hisse: 09:30–09:35 ET kovası hariç
        d = datetime(2026, 10, 6, 9, 30, tzinfo=ET)
        k930 = int(d.timestamp())
        heq = {**feat, "piyasa": "equity", "kova": k930}
        registry.emit("LOG-VOL", "xyz:NVDA", k930 + 360, side=1, trig_key="e1", features=heq)
        registry.emit("LOG-VOL", "xyz:AMD", k930 + 660, side=1, trig_key="e2", features={**heq, "kova": k930 + 300})
        assert {e["coin"] for e in _q("HAC-HSS-F")} == {"xyz:AMD"}
        # funding: ödeyen tarafın tersine
        registry.emit("LOG-ANOM", "xyz:SNDK", T, side=0, trig_key="a1", features={"fund": 0.0007})
        registry.emit("LOG-ANOM", "xyz:MU", T, side=0, trig_key="a2", features={"fund": -0.0006})
        registry.emit("LOG-ANOM", "xyz:CBRS", T, side=0, trig_key="a3", features={"fund": 0.0001})
        got = {e["coin"]: e["side"] for e in _q("ANO-FUND-T")}
        assert got == {"xyz:SNDK": -1, "xyz:MU": 1}, got
        # açılış: yalnız ilk 30 dk listesinin ilk 5'i
        for i, c in enumerate(("xyz:A1", "xyz:A2", "xyz:A6")):
            registry.emit("LOG-OPEN", c, T, side=-1, trig_key="2026-10-06:30",
                          features={"dk": 30, "sira": (1, 5, 6)[i], "chg": -2.0})
        registry.emit("LOG-OPEN", "xyz:A9", T, side=1, trig_key="2026-10-06:5", features={"dk": 5, "sira": 1})
        assert {e["coin"] for e in _q("ACI-12-F")} == {"xyz:A1", "xyz:A2"}
        assert all(e["side"] == 1 for e in _q("ACI-16-T")), "fade: düşene yukarı"
        # TWAP: SABİT eşikler (radarın ayarlı kapısı değil) + o anki ön süzgeç ayarı
        tw = {"st": "activated", "plan": 2_000_000, "left": 1_500_000, "vol_pct": 8.0, "lk_min": 50_000,
              "min_sl": 10, "kapi": "order_small"}
        registry.emit("LOG-TWAP", "xyz:SNDK", T, side=1, trig_key="0xa:1", features=tw)
        registry.emit("LOG-TWAP", "xyz:MU", T, side=1, trig_key="0xb:1", features={**tw, "plan": 600_000, "kapi": "ok"})
        registry.emit("LOG-TWAP", "xyz:AMD", T, side=1, trig_key="0xc:1", features={**tw, "lk_min": 200_000})
        assert {e["coin"] for e in _q("AKI-TWAP-F")} == {"xyz:SNDK"}, "radar kapısı değil sabit eşik; ayar yükselince türemez"
        assert specs.side_of("-sign:x", 1, {"x": "metin"}) == 0 and specs.side_of("follow", 0, {}) == 0
        assert not specs.match([["yok", "==", 1]], {}, T), "eksik alan eşleşmez"
        wrote = await registry.flush(ts=T + 30)
        assert wrote == len([1 for _ in range(wrote)]) and wrote > 10
    asyncio.run(run())
    print("✅ türetme) donmuş süzgeç; follow/fade/−işaret; birimde bir kez; eşik/taban/09:30 kovası; açılış ilk 5")


async def _seed_outcomes(rid, ver, nets, start, width, primary_h, coin_prefix="xyz:C", status="done"):
    """Doğrudan çözülmüş olay + birincil ufuk sonucu yaz (küme başına bir olay)."""
    async with dbm.db() as c:
        for i, net in enumerate(nets):
            ts = start + i * width + 60
            cur = await c.execute(
                "INSERT INTO strat_events(rule_id, rule_ver, origin, coin, klass, ts_decision, trig_key, side,"
                " latency_s, cluster, status, entry_ts, entry_px) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, ver, "live", f"{coin_prefix}{i % 7}", "hisse", ts, f"k{i}", 1, 60, ts // width,
                 "done" if status == "done" else "unresolvable", ts + 60, 100.0))
            await c.execute("INSERT INTO strat_outcomes(event_id, h, due_ts, status, net, exit_reason)"
                            " VALUES(?,?,?,?,?,?)", (cur.lastrowid, primary_h, ts + 60 + primary_h,
                                                     status, net if status == "done" else None, "timeout"))


def _rule(rid, **kw):
    return core._rule(rid, exit=None, horizons_s=[3600], primary_h=3600, looks=[0.5, 1.0], n_max=40,
                      max_events_day=50, **kw)


def test_scheduled_looks_pass_and_once():
    async def run():
        cfg = await core._fresh("rules2.db")
        cfg.lab_k_budget, cfg.lab_alpha = 2, 0.10                     # kural başına α = 0.05 — davranış testi
        await registry.sync(cfg, ts=T, rules=[_rule("GOOD"), _rule("FLAT")])
        rng = np.random.default_rng(1)
        await _seed_outcomes("GOOD", 1, list(0.01 + rng.normal(0, 0.004, 25)), T, 86400, 3600)
        await _seed_outcomes("FLAT", 1, list(rng.normal(0, 0.004, 25)), T, 86400, 3600)
        passed = []

        async def on_pass(rec):
            passed.append(rec)
        now_ts = T + 40 * 86400
        out = await looks.run(now_ts, on_pass=on_pass)
        by = {r["rule_id"]: r for r in out}
        assert by["GOOD"]["look"] == 1 and by["GOOD"]["n_c"] == 25, by
        assert registry.ACTIVE["GOOD"]["status"] in ("gecti", "kagit")
        assert by["FLAT"]["decision"] in ("devam", "emekli") and registry.ACTIVE.get("FLAT", {}).get("status") != "gecti"
        assert (by["GOOD"]["decision"] == "gecti") == bool(passed)
        again = await looks.run(now_ts + 3600, on_pass=on_pass)
        assert not [r for r in again if r["rule_id"] == "GOOD" and r["look"] == 1], "aynı bakış iki kez yok"
        async with dbm.db() as c:
            n = (await (await c.execute("SELECT COUNT(*) n FROM lab_tests WHERE kind='look' AND rule_id='GOOD'"))
                 .fetchone())["n"]
        assert n == 1
        # kapanmamış kümeler sayılmaz: aynı veri, şimdi = son olaydan hemen sonra
        fc = await looks.forward_clusters("FLAT", 1, registry.ACTIVE.get("FLAT", {"spec": _rule("FLAT")})["spec"],
                                          T, T + 10 * 86400)
        assert len(fc["xc"]) == 10, "11. gün kümesi sürüyor (kapanış + ufuk + pay geçmedi) — sayılmaz"
    asyncio.run(run())
    print("✅ planlı bakış) eşikte bir kez; güçlü etki gecti + onay sorusu; düz veri geçmez; kapanmamış küme yok")


def test_unresolvable_guard_blocks():
    async def run():
        cfg = await core._fresh("rules3.db")
        cfg.lab_k_budget = 1
        await registry.sync(cfg, ts=T, rules=[_rule("U")])
        rng = np.random.default_rng(9)
        await _seed_outcomes("U", 1, list(0.02 + rng.normal(0, 0.004, 25)), T, 86400, 3600)
        await _seed_outcomes("U", 1, [0] * 10, T + 30 * 86400, 86400, 3600, coin_prefix="xyz:X", status="unres")
        out = await looks.run(T + 80 * 86400)
        rec = next(r for r in out if r["rule_id"] == "U")
        assert rec["decision"] != "gecti" and "ölçülemeyen" in rec["why"], rec
    asyncio.run(run())
    print("✅ koruma) ölçülemeyen payı > %20 → geçemez")


class _Note:
    def __init__(self):
        self.sent = []

    async def send(self, kind, text, **kw):
        self.sent.append((kind, text, kw))
        return True


def test_approval_and_live_delivery():
    async def run():
        from app.telegram.bot import TelegramBot
        cfg = await core._fresh("rules4.db")
        cfg.lab_k_budget, cfg.telegram_chat_id, cfg.strat_chat_id = 1, "111", ""
        await registry.sync(cfg, ts=T, rules=[_rule("G")])
        note = _Note()
        rec = {"rule_id": "G", "ver": 1, "look": 1, "decision": "gecti", "p": 1e-4, "alpha_k": 0.005,
               "n_c": 21, "n_ev": 30, "mean": 0.004}
        assert await deliver.on_pass(note, cfg, rec)
        kind, text, kw = note.sent[-1]
        assert kind == "strat" and kw["public"] is False and kw["chat_id"] == "111"
        assert kw["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "lab:ok:G:1"
        assert "onay bekliyor" in text and "arama yok" in text
        # onaysız (kagit) kural: olay mesajı YOK
        registry.emit("G", "xyz:SNDK", T + 100, side=1, trig_key="x1")
        await registry.flush(ts=T + 130)
        assert registry.take_live() == [] and await deliver.send_live(note, cfg, []) == 0
        # bot: yabancı basamaz; sahip 'kagit' kuralı onaylayamaz; gecti → canli
        bot = TelegramBot(cfg, None, None, {})
        answers = []

        async def fake_answer(cq_id, text="", alert=False):
            answers.append(text)
            return True

        async def fake_edit(chat, mid, kb):
            return True
        bot.answer_callback, bot.edit_reply_markup = fake_answer, fake_edit

        def cq(data, chat=111, uid=111, ctype="private"):
            return {"callback_query": {"id": "q", "data": data, "from": {"id": uid},
                                       "message": {"message_id": 5, "chat": {"id": chat, "type": ctype}}}}
        await bot._handle_update(cq("lab:ok:G:1"))
        assert "onay yalnız kapıyı geçmiş kural" in answers[-1] and registry.ACTIVE["G"]["status"] == "kagit"
        await registry.set_status("G", 1, "gecti", "bakış 1", ts=T + 200)
        await bot._handle_update(cq("lab:ok:G:1", chat=-555, uid=42, ctype="supergroup"))
        assert "yalnız sahibi" in answers[-1] and registry.ACTIVE["G"]["status"] == "gecti"
        await bot._handle_update(cq("lab:ok:G:1"))
        assert registry.ACTIVE["G"]["status"] == "canli" and "canlı" in answers[-1]
        async with dbm.db() as c:
            r = dict(await (await c.execute("SELECT status, approved_ts FROM lab_rules WHERE rule_id='G'")).fetchone())
            trail = [dict(x) for x in await (await c.execute(
                "SELECT decision, payload FROM lab_tests WHERE rule_id='G' AND kind='status' ORDER BY id")).fetchall()]
        assert r["status"] == "canli" and r["approved_ts"] and "tg:111" in trail[-1]["payload"]
        # canlı: YENİ olay ölçülü mesajla; tekrar eden olay ikinci kez gitmez
        registry.emit("G", "xyz:SNDK", T + 300, side=-1, px_ref=50.0, trig_key="x2")
        registry.emit("G", "xyz:SNDK", T + 300, side=-1, trig_key="x1")           # DB'de zaten var
        await registry.flush(ts=T + 330)
        live = registry.take_live()
        assert [e["trig_key"] for e in live] == ["x2"]
        n0 = len(note.sent)
        assert await deliver.send_live(note, cfg, live) == 1
        kind, text, kw = note.sent[n0]
        assert kind == "strat" and kw["public"] is False and kw["chat_id"] == "111" and "tetiklendi" in text
        assert "SNDK" in text and "▼ aşağı yön" in text and "Kural karnesi" in text
        bad = re.compile(r"işlem aç|\bal\b|\bsat\b|ihtimal|tavsiye ed|fırsat|kesin")
        assert not bad.search(text), text
        cfg.strat_chat_id = "-777"
        assert deliver.target(cfg) == "-777"
        from app.notify import KINDS, PUBLIC_KINDS
        assert "strat" in KINDS and "strat" not in PUBLIC_KINDS
    asyncio.run(run())
    print("✅ teslim) onay sorusu yalnız sahibe; onaysız mesaj yok; yalnız sahip + yalnız gecti; canlı yeni olay"
          " ölçülü mesaj, tekrar yok; public=False; strat herkese açık değil")


def test_live_demotion():
    async def run():
        cfg = await core._fresh("rules5.db")
        cfg.lab_k_budget = 1
        await registry.sync(cfg, ts=T, rules=[_rule("L")])
        await registry.set_status("L", 1, "canli", "test", ts=T)
        rng = np.random.default_rng(3)
        await _seed_outcomes("L", 1, list(-0.01 + rng.normal(0, 0.003, 22)), T + 3600, 86400, 3600)
        stopped = []

        async def on_stop(rec):
            stopped.append(rec)
        await looks.run(T + 40 * 86400, on_stop=on_stop)
        assert registry.ACTIVE["L"]["status"] == "durdu" and stopped and stopped[0]["n_c"] >= 20
        note = _Note()
        cfg.telegram_chat_id = "111"
        assert await deliver.on_stop(note, cfg, stopped[0]) and "durduruldu" in note.sent[-1][1]
    asyncio.run(run())
    print("✅ canlıda düşüş) onay sonrası 20 kümede anlamlı negatif → durdu + tek mesaj")


def test_reoffer_unit_src_and_ladder():
    """İnceleme (08.10): türetilmiş kural kaynağın İLK görülüşüyle sınırlı değil (sonradan aşırılaşan
    funding yakalanır); pozisyon başına birim (src) yeniden görülmede ikinci olay üretmez; gün tavanına
    takılan kaynak türetmeyi engellemez; kripto liq aşaması lab'ın SABİT basamaklarıyla."""
    async def run():
        cfg, _ = await _prod("rules6.db")
        registry.emit("LOG-ANOM", "SOL", T + 60, side=0, trig_key=str((T + 60) // 86400), features={"fund": 0.0002})
        assert not _q("ANO-FUND-T")
        registry.emit("LOG-ANOM", "SOL", T + 3600, side=0, trig_key=str((T + 3600) // 86400), features={"fund": 0.0008})
        got = _q("ANO-FUND-T")
        assert len(got) == 1 and got[0]["ts_decision"] == T + 3600 and got[0]["side"] == -1, "aynı gün sonradan aşırılaşan"
        assert len(_q("LOG-ANOM")) == 1, "aile kaydı gün başına bir"
        nb = {"usd": 2_000_000, "age_s": 3600}
        registry.emit("LOG-NEWBIG", "xyz:NVDA", T, side=1, trig_key="0xa:100", features=nb)
        registry.emit("LOG-NEWBIG", "xyz:NVDA", T + 86400 + 60, side=1, trig_key="0xa:100", features={**nb, "age_s": 5 * 3600})
        assert [e["trig_key"] for e in _q("AKI-NEWBIG-F")] == ["0xa:100"], "aynı pozisyon bir kez (birim = kaynak)"
        await registry.flush(ts=T + 90000)
        registry._SEEN.clear()                                          # yeniden başlama
        await registry.sync(cfg, ts=T + 90000)
        registry.emit("LOG-NEWBIG", "xyz:NVDA", T + 90000, side=1, trig_key="0xa:100", features=nb)
        assert not _q("AKI-NEWBIG-F"), "yeniden başlamada da (son 2 günden tohumlanan bellek)"
        # gün tavanına takılan kaynak da türetir; tavan sayısı tekrarla şişmez
        cap = registry.ACTIVE["LOG-VOL"]["spec"]["max_events_day"]
        day = (T + 5 * 86400) // 86400 * 86400
        registry._DAY[("LOG-VOL", day // 86400)] = cap
        feat = {"usd": 1_500_000, "chg": 0.5, "kova": day, "piyasa": "crypto", "taban": 50_000}
        registry.emit("LOG-VOL", "DOGE", day + 600, side=1, trig_key="z1", features=feat)
        registry.emit("LOG-VOL", "DOGE", day + 700, side=1, trig_key="z1", features=feat)
        assert _q("HAC-KRP-F") and registry.CAP_DROPS.get("LOG-VOL") == 1, registry.CAP_DROPS
        # kripto liq: radarın d3'ü %0.3'e çekilse de lab basamağı %0.5'te
        from app.radar import cryptoliq
        cryptoliq._LAB_ST.clear()
        by = {"ETH": [{"address": "0xw", "side": "long", "dist": 0.45, "notional": 900_000, "mark": 3000.0,
                       "leverage": 20, "liq_px": 2986.5}]}
        cryptoliq._lab_stages(by, T, 2.5, 1.0, 0.3, 500_000)
        ev = [e for e in _q("LOG-CLIQ") if e["coin"] == "ETH"]
        assert ev and '"asama":3' in ev[0]["features"] and _q("LIQ-S3-F") and _q("LIQ-S3-F")[0]["side"] == -1
    asyncio.run(run())
    print("✅ inceleme düzeltmeleri) sonradan aşırılaşan funding; pozisyon başına bir; yeniden başlamada tekrar"
          " yok; tavana takılan kaynak türetir; kripto liq sabit basamak")



def test_lost_approval_prompt_is_resent_and_card_not_cut():
    async def run():
        from app.telegram import format as fmt
        from app.lab import ui
        cfg = await core._fresh("rules7.db")
        cfg.lab_k_budget, cfg.telegram_chat_id = 1, "111"
        await registry.sync(cfg, ts=T, rules=[_rule("P")])
        async with dbm.db() as c:                                    # geçen bakış kaydı + durum
            await c.execute("INSERT INTO lab_tests(ts, rule_id, ver, kind, look_no, n_events, n_clusters, est, p,"
                            " alpha_k, decision) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (T, "P", 1, "look", 1, 30, 22, 0.004, 1e-4, 0.005, "gecti"))
        await registry.set_status("P", 1, "gecti", "bakış 1", ts=T)

        class Down(_Note):
            async def send(self, kind, text, **kw):
                return False
        assert await deliver.resend_pending(Down(), cfg, T) == 0               # teslim düştü: işaret yok
        note = _Note()
        assert await deliver.resend_pending(note, cfg, T + 3600) == 1 and "onay bekliyor" in note.sent[-1][1]
        assert await deliver.resend_pending(note, cfg, T + 7200) == 0, "teslim edilen bir daha sorulmaz"
        # /strat kartı: kesilmez, dipnot ve bağlantı her zaman sonda
        ui._CACHE.update(ts=0, v=None)
        v = await ui.overview(T + 10)
        many = {**v, "rules": [dict(v["rules"][0], rule_id=f"R{i:02d}") for i in range(40)]}
        card = fmt.strat_card(many, base_url="https://x.test")
        assert card.rstrip().endswith("</i>") and "strateji asla aramaz" in card and "/lab" in card
        assert "+10 kural daha" in card
    asyncio.run(run())
    print("✅ onay) teslim edilemeyen onay sorusu saatlik yeniden; teslim edilen tekrar sorulmaz; /strat kartı"
          " kesilmez")
