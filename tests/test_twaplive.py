"""⏳ Canlı TWAP radarı — "INJ'e $2M TWAP" alarmı.

Pinlenenler:
  • aynı saniyedeki parçalar tek dilim; tabanın altındaki dilimler sayılır, fills'e yazılmaz
  • düzenli 30 sn dizisi → hız/gün, HL düzeni; düzensiz → yok; HL jitter canlı eşikle geçer, arşivle geçmez
  • kapı: hacme oran (A) / mutlak (B) / hacim yok-bayat → yalnız B; mm/vault elenir
  • kripto → CRYPTO_CHAT_ID (boşsa gönderilmez), hisse → ana sohbet; sessiz saatte tek özet kaydı
  • bekleme + restart (alerts_log), ilerleme basamağı 30 dk kilidi, bitiş notu, geri yükleme
  • budama/tavan; collector kancası fırlatmaz; arşiv upsert canlı kolonları korur; sayfa; bağlantı
"""
import asyncio
import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twaplive.db")
from app import db as dbm
from app.config import Config
from app.hl import universe as uni
from app.notify import Notifier
from app.radar import twap as twapmod
from app.radar import twaplive as tl
from app.telegram import format as fmt

A, B, C = ("0x" + c * 40 for c in "abc")


class Bot:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, text, chat_id=None):
        if self.ok:
            self.sent.append((chat_id, text))
        return self.ok


def _cfg(chat="-100"):
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.crypto_chat_id = chat
    cfg.notify_twap = True
    cfg.twap_live_enabled = True
    cfg.twap_alert_min_usd = 100_000
    cfg.twap_alert_rate_pct = 20
    cfg.twap_alert_big_usd = 5_000_000
    cfg.twap_alert_min_slices = 20
    cfg.twap_alert_cooldown = 21600
    cfg.twap_alert_progress = True
    cfg.twap_alert_end_note = True
    cfg.twap_live_window_min = 240
    cfg.twap_min_usd = 5_000_000
    cfg.twap_window_h = 12
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "tw.db"))
    tl.REG.clear()


def slices(coin, addr, side, n, gap=30, ntl=2200.0, px=12.3, t0=None, jitter=0.0, size_jitter=0.0,
           seed=1, taker=1):
    """n dilim: t0'dan itibaren `gap` sn arayla; jitter oranı ±uniform."""
    r = random.Random(seed)
    t0 = t0 if t0 is not None else dbm.now() - (n - 1) * gap - 5
    t = t0
    last = t0
    for i in range(n):
        g = gap * (1 + r.uniform(-jitter, jitter)) if jitter else gap
        t = t0 + i * gap if not jitter else last + g
        last = t
        v = ntl * (1 + r.uniform(-size_jitter, size_jitter)) if size_jitter else ntl
        tl.observe(coin, addr, side, px, v / px, v, int(t), taker)
    return int(last)


async def set_vol(coin, v, ts=None, mark=12.3):
    cur = await dbm.kv_get(uni.MAIN_CTX_KV) or {"c": {}}
    c = dict(cur.get("c") or {})
    c[coin] = {"m": mark, "oi": 1, "f": 0, "v": v, "p": None}
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": c, "ts": ts or dbm.now()})


async def _alerts(kind):
    async with dbm.db() as c:
        cur = await c.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind=?", (kind,))
        return (await cur.fetchone())["n"]


async def _runs():
    async with dbm.db() as c:
        cur = await c.execute("SELECT * FROM twap_runs ORDER BY first_ts")
        return [dict(r) for r in await cur.fetchall()]


# ------------------------------------------------ 1) sayaç
def test_observe_coalesce_and_prune():
    tl.REG.clear()
    t = 1_000_000
    for i, (sz, px) in enumerate(((100, 12.30), (50, 12.31), (30, 12.32), (20, 12.33))):
        tl.observe("INJ", A, "buy", px, sz, sz * px, t + (1 if i else 0), 1)
    run = tl.REG.runs[("INJ", A, "buy")]
    assert run.n == 1 and len(run.slices) == 1, "aynı 2 sn içindeki parçalar tek dilim"
    ts0, sz0, ntl0, px0 = run.slices[0]
    assert abs(sz0 - 200) < 1e-9 and abs(ntl0 - sum(s * p for s, p in ((100, 12.30), (50, 12.31), (30, 12.32), (20, 12.33)))) < 1e-6
    assert abs(px0 - ntl0 / sz0) < 1e-9, "ağırlıklı fiyat"
    tl.observe("INJ", A, "buy", 12.34, 100, 1234, t + 30, 0)
    assert run.n == 2 and run.known_n == 2 and run.tk_n == 1 and run.last_ts == t + 30 and run.px_last == 12.34
    # budama: tekil işlem (5 dilimden az) 5 dk'da, dizi 30 dk'da; bildirilmiş süren tur pencereye takılmaz
    tl.REG.clear()
    slices("INJ", A, "buy", 30, t0=t)                                   # dizi: son dilim t+870
    tl.observe("BTC", B, "sell", 100000, 0.01, 1000, t + 870, 1)        # tekil işlem
    assert tl.REG.prune(t + 870 + 301, 4 * 3600) == 1 and ("BTC", B, "sell") not in tl.REG.runs
    assert tl.REG.prune(t + 870 + 1801, 4 * 3600) == 1 and not tl.REG.runs
    slices("INJ", A, "buy", 30, t0=t)
    run = tl.REG.runs[("INJ", A, "buy")]
    run.alerted_ts = t
    assert tl.REG.prune(run.last_ts + 60, 60) == 0, "bildirilmiş tur pencere aşımıyla düşmez"
    run.alerted_ts = None
    assert tl.REG.prune(run.last_ts + 60, 60) == 1, "bildirilmemiş dizi pencereyi aşınca düşer"
    # tavan
    old = tl.MAX_KEYS
    tl.MAX_KEYS = 50
    try:
        for i in range(70):
            tl.observe("BTC", f"0x{i:040x}", "buy", 1.0, 1, 1, t + i, 1)
        assert len(tl.REG.runs) <= 50
    finally:
        tl.MAX_KEYS = old
        tl.REG.clear()
    print("✅ sayaç) 2 sn birleştirme, ağırlıklı fiyat, taker sayacı, kademeli budama, tavan")


# ------------------------------------------------ 2) ölçüm + kapı (saf)
def test_measure_and_gate():
    tl.REG.clear()
    cfg = _cfg()
    now = dbm.now()
    last = slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
    m = tl.measure(tl.REG.runs[("INJ", A, "buy")])
    assert m and m["n"] == 50 and abs(m["total"] - 110_000) < 1e-6 and m["native_like"]
    assert abs(m["rate_day"] - 2200 / 30 * 86400) < 1e-3, m["rate_day"]         # $6.336M/gün
    assert m["median_gap"] == 30 and m["taker_pct"] == 100 and m["dur"] == 49 * 30
    assert tl.gate(cfg, m, 9.9e6, now, now) == "ratio", "hacmin %64'ü/gün"
    assert tl.gate(cfg, m, 2e9, now, now) == "", "BTC hacminde %0,3 — kapı kapalı"
    assert tl.gate(cfg, m, None, None, now) == "", "hacim yok → yalnız mutlak kapı"
    assert tl.gate(cfg, m, 9.9e6, now - 3601, now) == "", "bayat hacim → oran kapısı yok"
    assert tl.gate(cfg, {**m, "total": 5_100_000}, None, None, now) == "big"
    assert tl.gate(cfg, {**m, "n": 19}, 9.9e6, now, now) == "" and tl.gate(cfg, {**m, "dur": 500}, 9.9e6, now, now) == ""
    # 42 dilim = $92.4K < $100K tabanı → henüz değil
    tl.REG.clear()
    slices("INJ", A, "buy", 42, t0=now - 41 * 30 - 5)
    assert tl.gate(cfg, tl.measure(tl.REG.runs[("INJ", A, "buy")]), 9.9e6, now, now) == ""
    # düzensiz aralık → ölçüm yok
    tl.REG.clear()
    r = random.Random(3)
    t = now - 5000
    for i in range(40):
        t += r.randint(10, 200)
        tl.observe("INJ", B, "buy", 12.3, 100, 1230, t, 1)
    assert tl.measure(tl.REG.runs[("INJ", B, "buy")]) is None
    # HL randomize: ±%70 aralık, ±%80 boyut → canlı eşikle düzenli, arşiv (0.35) eşiğiyle değil
    tl.REG.clear()
    slices("INJ", C, "sell", 60, jitter=0.7, size_jitter=0.8, seed=5, t0=now - 60 * 30)
    run = tl.REG.runs[("INJ", C, "sell")]
    m2 = tl.measure(run)
    assert m2 is not None and 0.35 <= m2["cv_gap"] < 0.5 and 0.35 <= m2["cv_size"] < 0.6, (m2["cv_gap"], m2["cv_size"])
    rows = [{"ts": t, "sz": s, "notional": n} for (t, s, n, _) in run.slices]
    assert twapmod.detect(rows) is None, "arşiv eşiği bunu görmezdi"
    assert tl.next_progress_step(type("R", (), {"progress_step": 0, "alert_total": 92_000, "total": 1_100_000})()) == 1e6
    assert tl.next_progress_step(type("R", (), {"progress_step": 1e6, "alert_total": 92_000, "total": 1_100_000})()) is None
    print("✅ ölçüm) hız/gün, HL düzeni, oran/mutlak/bayat kapıları, jitter canlıda geçer arşivde geçmez")


# ------------------------------------------------ 3) collector kancası: taban altı dilimler sayılır, fills boş
def test_collector_hook():
    async def run():
        await _fresh()
        from app.hl.collector import Collector
        cfg = _cfg()
        cfg.crypto_fill_min_notional = 5000
        col = Collector(cfg, None)
        col.valid_coins = {"INJ"}
        col.crypto_coins = {"INJ"}
        now = dbm.now()
        data = [{"coin": "INJ", "side": "B", "px": "12.3", "sz": "100", "time": (now - 60) * 1000,
                 "tid": "t1", "users": [A, B]},
                {"coin": "INJ", "side": "B", "px": "12.31", "sz": "80", "time": (now - 60) * 1000 + 300,
                 "tid": "t2", "users": [A, C]},
                {"coin": "INJ", "side": "B", "px": "x", "sz": "bad", "time": now * 1000, "tid": "t3", "users": [A, B]},
                {"coin": "INJ", "side": "B", "px": "12.32", "sz": "100", "time": (now - 30) * 1000,
                 "tid": "t4", "users": [A, B]}]
        await col._handle(json.dumps({"channel": "trades", "data": data}))
        run_a = tl.REG.runs[("INJ", A, "buy")]
        assert run_a.n == 2 and abs(run_a.total - (1230 + 984.8 + 1232)) < 1e-6, "alıcı: aynı saniye birleşti, bozuk kayıt atlandı"
        assert run_a.tk_n == 2, "agresör alıcı → taker"
        assert tl.REG.runs[("INJ", B, "sell")].n == 2 and tl.REG.runs[("INJ", C, "sell")].n == 1
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) n FROM fills")
            assert (await cur.fetchone())["n"] == 0, "tabanın altı fills'e yazılmaz"
        assert tl.REG.errors == 0
        print("✅ kanca) her trade adres bazında sayıldı, taban altı fills'e yazılmadı, bozuk kayıt fırlatmadı")
    asyncio.run(run())


# ------------------------------------------------ 4) alarm: kripto kanalı, dedupe, restart
def test_evaluate_alert_crypto():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        now = dbm.now()
        await set_vol("INJ", 9.9e6)
        slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["alerted"] == 1 and out["cands"] == 1 and out["regular"] == 1 and out["no_chat"] == 0, out
        assert len(bot.sent) == 1 and bot.sent[0][0] == "-100"
        txt = bot.sent[0][1]
        assert "⏳ <b>TWAP</b> · <b>INJ</b> · 🟢 ALIŞ · 🐘 düşük hacimli kripto" in txt
        assert "50 dilim" in txt and "her 30 sn" in txt and "%64" in txt and "HL TWAP emri" in txt, txt
        assert "pozisyon: bilinmiyor" in txt and "%100 taker" in txt
        assert await _alerts("twap") == 2 and await _alerts("sent:twap") == 1, "işaret + metin"
        rows = await _runs()
        assert len(rows) == 1 and rows[0]["src"] == "live" and rows[0]["alerted_ts"] and rows[0]["day_volume"] == 9.9e6
        assert rows[0]["n_slices"] == 50 and abs(rows[0]["rate_day"] - 2200 / 30 * 86400) < 1e-3
        # ikinci tur: tekrar yok
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["alerted"] == 0 and len(bot.sent) == 1
        # restart: registry boş, aynı dilimler → bekleme DB'de, tekrar yok
        tl.REG.clear()
        slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["alerted"] == 0 and len(bot.sent) == 1 and tl.REG.runs[("INJ", A, "buy")].alerted_ts
        # entity mm → elenir
        tl.REG.clear()
        await _fresh()
        await set_vol("INJ", 9.9e6)
        async with dbm.db() as c:
            await c.execute("INSERT INTO addresses(address,first_seen,entity) VALUES(?,?,'mm')", (B, now))
        slices("INJ", B, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["skipped_mm"] == 1 and out["alerted"] == 0
        # sayfa satırı: recent() rate_pct ve src
        print("✅ alarm) kripto kanalına tek mesaj, işaret+metin kaydı, twap_runs canlı satırı, restart'ta tekrar yok, mm elenir")
    asyncio.run(run())


# ------------------------------------------------ 5) kanal boş / hisse ana sohbet / sessiz saat
def test_routing_and_quiet():
    async def run():
        await _fresh()
        cfg = _cfg(chat="")
        bot = Bot()
        notifier = Notifier(cfg, bot)
        now = dbm.now()
        await set_vol("INJ", 9.9e6)
        slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["no_chat"] == 1 and out["alerted"] == 0 and bot.sent == [] and await _alerts("twap") == 0
        assert (await _runs())[0]["alerted_ts"], "kanal yokken de tur bildirilmiş sayılır (sayfada görünür)"
        # hisse: asset_metrics hacmi → ana sohbet
        await _fresh()
        cfg = _cfg(chat="")
        bot = Bot()
        notifier = Notifier(cfg, bot)
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:UANSEM','UANSEM')")
            await c.execute("INSERT INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume)"
                            " VALUES('xyz:UANSEM',?,0.2268,1e7,0.0001,4.1e6)", (now,))
        slices("xyz:UANSEM", B, "sell", 50, ntl=13_475, px=0.2268, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["alerted"] == 1 and bot.sent[0][0] is None, "hisse → ana sohbet"
        assert "UANSEM" in bot.sent[0][1] and "🔴 SATIŞ" in bot.sent[0][1] and "düşük hacimli hisse" in bot.sent[0][1]
        # sessiz saat (ana sohbet): tek özet kaydı, tekrar yok, hata sayılmaz
        await _fresh()
        cfg = _cfg(chat="")
        cfg.quiet_start_hour, cfg.quiet_end_hour = 0, 24
        cfg.quiet_allow_high = False
        bot = Bot()
        notifier = Notifier(cfg, bot)
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:UANSEM','UANSEM')")
            await c.execute("INSERT INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume)"
                            " VALUES('xyz:UANSEM',?,0.2268,1e7,0.0001,4.1e6)", (now,))
        slices("xyz:UANSEM", B, "sell", 50, ntl=13_475, px=0.2268, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, client=None)
        assert bot.sent == [] and out["failed"] == 0 and out.get("quiet") == 1
        assert await _alerts("quiet:twap") == 1 and await _alerts("twap") == 1
        await tl.evaluate(cfg, notifier, client=None)
        tl.REG.clear()
        slices("xyz:UANSEM", B, "sell", 50, ntl=13_475, px=0.2268, t0=now - 49 * 30 - 5)
        await tl.evaluate(cfg, notifier, client=None)
        assert await _alerts("quiet:twap") == 1, "işaret gönderimden önce → sessiz saatte tek özet kaydı"
        print("✅ yönlendirme) kanal boş → gönderim yok; hisse → ana sohbet; sessiz saatte tek özet, hata değil")
    asyncio.run(run())


# ------------------------------------------------ 6) ilerleme + bitiş + geri yükleme
def test_progress_end_rehydrate():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        now = dbm.now()
        await set_vol("INJ", 9.9e6)
        last = slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["alerted"] == 1 and len(bot.sent) == 1, out
        run = tl.REG.runs[("INJ", A, "buy")]
        # toplam $250K'yı geçince ilerleme notu (tek); aynı 30 dk içinde sonraki basamak sessiz
        slices("INJ", A, "buy", 70, t0=last + 30)                      # 120 dilim ≈ $264K
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["progress"] == 1 and "TWAP sürüyor" in bot.sent[-1][1] and fmt.usd(250e3) in bot.sent[-1][1], bot.sent[-1][1]
        assert run.progress_step == 250e3 and (await _runs())[0]["n_slices"] == 120
        slices("INJ", A, "buy", 120, t0=last + 71 * 30)                # 240 dilim ≈ $528K
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["progress"] == 0 and len(bot.sent) == 2, "30 dk kilidi"
        run.progress_ts = now - 1801
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["progress"] == 1 and fmt.usd(500e3) in bot.sent[-1][1]
        # bitiş: son dilimden 5+ dk geçti → 🏁 notu, ended_ts; tekrar yok
        run.last_ts = now - 400
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["ended"] == 1 and "🏁 <b>TWAP bitti</b>" in bot.sent[-1][1] and "24s hacmin" in bot.sent[-1][1]
        assert run.ended_ts and (await _runs())[0]["ended_ts"]
        n_sent = len(bot.sent)
        await tl.evaluate(cfg, notifier, client=None)
        assert len(bot.sent) == n_sent and await _alerts("twap_end") == 1
        # geri yükleme: DB'de bildirilmiş-bitmemiş satır, registry boş → bitiş notu gider
        await _fresh()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        async with dbm.db() as c:
            await c.execute(
                "INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,avg_slice,avg_gap,"
                "cv_gap,cv_size,taker_pct,ts,day_volume,rate_day,src,alerted_ts,px_first,px_last,sz_total)"
                " VALUES('INJ',?,'buy',?,?,300,660000,2200,30,0.05,0.1,100,?,9.9e6,6.3e6,'live',?,12.3,12.5,53000)",
                (A, now - 9400, now - 400, now - 400, now - 5000))
        assert await tl._rehydrate(4 * 3600) == 1
        out = await tl.evaluate(cfg, notifier, client=None)
        assert out["ended"] == 1 and "TWAP bitti" in bot.sent[-1][1] and fmt.usd(660_000) in bot.sent[-1][1]
        assert (await _runs())[0]["ended_ts"]
        print("✅ takip) ilerleme basamağı ve 30 dk kilidi; bitiş notu; restart sonrası geri yükleme bitişi kaçırmaz")
    asyncio.run(run())


# ------------------------------------------------ 7) arşiv taraması canlı kolonları korur; recent tekilleştirir
def test_scan_upsert_and_recent():
    async def run():
        await _fresh()
        cfg = _cfg()
        now = dbm.now()
        t0 = now - 600
        run = tl.Run("INJ", A, "buy", t0, 12.3)
        run.n, run.total, run.last_ts, run.alerted_ts, run.day_volume, run.rate_day = 6, 6e6, t0 + 300, now - 100, 9.9e6, 5e8
        await tl.persist(run, {"avg_gap": 60, "cv_gap": 0.0, "cv_size": 0.0, "taker_pct": 100})
        async with dbm.db() as c:
            for i in range(6):
                await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts,taker)"
                                " VALUES('INJ',?,?,'buy',12.3,81300,1e6,?,1)", (f"f{i}", A, t0 + i * 60))
        out = await twapmod.scan(cfg)
        assert out["big"] == 1
        rows = await _runs()
        assert len(rows) == 1 and rows[0]["src"] == "live" and rows[0]["alerted_ts"] == now - 100
        assert rows[0]["day_volume"] == 9.9e6 and rows[0]["n_slices"] == 6 and rows[0]["last_ts"] == t0 + 300
        # farklı first_ts ile ikinci satır → recent tekilleştirir (çok dilimli kalır), rate_pct hesaplar
        run2 = tl.Run("INJ", A, "buy", t0 - 900, 12.3)
        run2.n, run2.total, run2.last_ts = 40, 88_000, t0 + 300
        await tl.persist(run2, {"avg_gap": 30, "cv_gap": 0.05, "cv_size": 0.1, "taker_pct": 100})
        rec = await twapmod.recent()
        assert len(rec) == 1 and rec[0]["n_slices"] == 40 and rec[0]["src"] == "live"
        assert rec[0]["rate_pct"] is None
        run.rate_day = 6.3e6
        await tl.persist(run, {"avg_gap": 60, "cv_gap": 0.0, "cv_size": 0.0, "taker_pct": 100})
        run2.n = 3
        await tl.persist(run2, {"avg_gap": 30, "cv_gap": 0.05, "cv_size": 0.1, "taker_pct": 100})
        rec = await twapmod.recent()
        assert rec[0]["n_slices"] == 6 and abs(rec[0]["rate_pct"] - 6.3e6 / 9.9e6 * 100) < 1e-6
        # sayfa render
        from app.web.routes import templates
        html = templates.env.get_template("twap.html").render(
            request=type("R", (), {"url": type("U", (), {"path": "/twap"})()})(), k="", is_admin=False, has_pw=False,
            rows=rec, st={}, cov={"crypto": {"n": 10, "a": now}, "equity": {"n": 5, "a": now}}, min_usd=5e6,
            window_h=12, crypto_floor=5000, eq_floor=5000, live={"ts": now, "keys": 12, "cands": 1, "regular": 1,
                                                                  "alerted": 1, "ended": 0, "no_chat": 0},
            ws={"crypto": ["INJ", "BTC"], "equity_n": 40}, alert_min=100_000, rate_pct=20, big_usd=5e6, live_on=True)
        assert "📡 canlı" in html and "24s hacim" in html and "%64" in html and "Canlı radar" in html
        assert "dinlenen <b>2</b> kripto + <b>40</b> hisse" in html and "12 dizi bellekte" in html
        print("✅ arşiv) upsert canlı kolonları korur; recent tekilleştirir + hız/gün; sayfa yeni kolonlarla")
    asyncio.run(run())


# ------------------------------------------------ 8) mesajlar + bağlantı
def test_messages_and_wiring():
    m = {"coin": "INJ", "address": A, "side": "buy", "n": 42, "total": 92_400, "sz_total": 7512, "first_ts": 0,
         "last_ts": 1260, "dur": 1260, "avg_slice": 2200, "avg_gap": 30, "median_gap": 30, "cv_gap": 0.05,
         "cv_size": 0.1, "rate_day": 6.336e6, "native_like": True, "taker_pct": 100, "avg_px": 12.36,
         "px_first": 12.34, "px_last": 12.44, "px_chg_pct": 0.81}
    t = fmt.twap_alert(m, {"day_vol": 9.9e6, "rate_pct": 64.0, "pos": {"side": "long", "notional": 1.2e6, "src": "canlı"},
                           "gate": "ratio", "klass": "kripto", "entity": ""})
    assert f"42 dilim × ~{fmt.usd(2200)}" in t and f"bu hızla 24 saatte <b>{fmt.usd(6.336e6)}</b> ≈ 24s hacmin <b>%64</b>'i" in t
    assert "📍 pozisyon: 🟢LONG <b>$1.2M</b> · canlı" in t and "+0.8%" in t and "yatırım tavsiyesi" in t
    t2 = fmt.twap_alert({**m, "native_like": False, "median_gap": 90}, {"day_vol": None, "rate_pct": None, "pos": None,
                                                                          "gate": "big", "klass": "hisse"})
    assert "💰 hacimden bağımsız büyük" in t2 and "Özel dilimleme (~2 dk aralık)" in t2 and "24s hacim bilinmiyor" in t2
    p = fmt.twap_progress(m, {"step": 250e3, "alert_total": 92_400, "rate_pct": 64.0})
    assert "TWAP sürüyor" in p and fmt.usd(250e3) in p and f"ilk bildirimde {fmt.usd(92_400)}" in p
    e = fmt.twap_end({**m, "total": 1_610_000, "sz_total": 337_720, "dur": 21_700}, {"day_vol": 9.9e6})
    assert "🏁 <b>TWAP bitti</b>" in e and fmt.usd(1_610_000) in e and "337.7K INJ" in e and "24s hacmin <b>%16</b>'i" in e
    assert fmt.qty_txt(7_010_000) == "7.01M" and fmt.qty_txt(337_720) == "337.7K" and fmt.qty_txt(12) == "12"
    # bağlantı
    from app.config import EDITABLE_FIELDS
    c = Config()
    for f in ("twap_live_enabled", "twap_alert_min_usd", "twap_alert_rate_pct", "twap_alert_big_usd",
              "twap_alert_min_slices", "twap_alert_cooldown", "twap_alert_progress", "twap_alert_end_note",
              "twap_live_window_min", "twap_live_eval_sec", "twap_min_usd", "twap_window_h", "twap_scan_sec"):
        assert f in EDITABLE_FIELDS and hasattr(c, f) and EDITABLE_FIELDS[f]["group"] == "TWAP radarı", f
        assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
    assert EDITABLE_FIELDS["notify_twap"]["group"] == "Bildirimler" and c.notify_twap is True
    assert c.twap_alert_min_usd == 100_000 and c.twap_alert_rate_pct == 20 and c.twap_alert_min_slices == 20
    from app.notify import KINDS
    assert KINDS["twap"][0] == "notify_twap" and KINDS["twap"][2] == "high"
    from app.health import limits, periods
    assert "twaplive" in limits(c) and "twaplive" in periods(c)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rd = lambda *p: open(os.path.join(root, *p), encoding="utf-8").read()  # noqa: E731
    assert '_spawn("twaplive"' in rd("app", "main.py") and "twaplive.observe(" in rd("app", "hl", "collector.py")
    assert "twap_runs ADD COLUMN src" in rd("app", "db.py") and '"twaplive_stats"' in rd("app", "diag.py")
    assert "Canlı TWAP alarmı" in rd("README.md") and fmt.TASK_TR.get("twaplive")
    print("✅ mesaj/bağlantı) alarm/ilerleme/bitiş metinleri; 13 ayar TWAP radarı grubunda; KINDS/health/spawn/kanca/migrasyon/README")
