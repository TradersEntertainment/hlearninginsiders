"""⏳ Canlı TWAP radarı — "INJ'e $2M TWAP" alarmı, EMİR VERİSİYLE (tahmin yok).

Pinlenenler:
  • aynı saniyedeki parçalar tek dilim; tabanın altındaki dilimler sayılır, fills'e yazılmaz
  • düzenli 30 sn dizisi → ölçüm; düzensiz → yok; HL jitter canlı eşikle geçer, arşivle geçmez
  • userTwapHistory ayrıştırma: coin/yön eşleşmesi, activated önce, plan/dolan/kalan/süre, bozuk şekil → []
  • kapı: emir ≥ $2M VE kalan ≥ $1M VE emir/24s hacim ≥ %20; emir yok / bitmiş (ADA vakası) / küçük /
    kalan az / hacim yok-bayat / BTC hacminde küçük → bildirim YOK; collector yoksa sorgu yok → YOK
  • sorgu: adres önbelleği (aynı adres iki coin → tek sorgu), tur başına tavan, başarısız sorgu sayılır
  • mesajda "bu hızla" / "24 saatte" YOK; emir satırı (plan, süre, başlangıç, kalan), doldu/kalan, dilimler
  • kripto → CRYPTO_CHAT_ID (boşsa gönderilmez), hisse → ana sohbet; sessiz saatte tek özet kaydı
  • bekleme + restart (alerts_log); ilerleme %50 (tek); 🏁 bitiş / ⛔ iptal gerçek tutarla; sorgu activated
    dediği sürece sessizlik bitiş sayılmaz; emir geçmişten düşerse gördüğümüz toplamla bitiş; geri yükleme
  • collector: userTwapHistory → bekleyen future dolar, trades akışı bozulmaz; abone ol/çık; zaman aşımı; paylaşım
  • arşiv upsert canlı kolonları korur; recent emir kolonları; sayfa; bağlantı (ayarlar $2M/$1M/%20)
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
SZ, MARK = 337_720, 12.3                      # hypurrscan örneği: 337.7K INJ ≈ $4.15M
PLAN = SZ * MARK
PCT = round(PLAN / 9.9e6 * 100)              # 24s hacmi $9.9M → %42


class Bot:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, text, chat_id=None):
        if self.ok:
            self.sent.append((chat_id, text))
        return self.ok


class FakeCol:
    """collector.fetch_twap_history taklidi: hazır snapshot (None = sorgu başarısız)."""

    def __init__(self, hist=None, raise_=False):
        self.hist, self.raise_, self.calls, self.twap_sample = hist, raise_, [], None

    async def fetch_twap_history(self, addr, timeout=6.0):
        self.calls.append(addr)
        if self.raise_:
            raise RuntimeError("ws koptu")
        return self.hist


def order(coin="INJ", side="B", sz=SZ, ex_sz=130_000, minutes=360, started=None, status="activated",
          ex_ntl=None, **extra):
    """HL userTwapHistory kaydı (sayılar HL'de string gelir, timestamp ms)."""
    started = started if started is not None else dbm.now() - 3030
    st = {"coin": coin, "side": side, "sz": str(sz), "executedSz": str(ex_sz),
          "executedNtl": str(ex_ntl if ex_ntl is not None else ex_sz * MARK), "minutes": minutes,
          "timestamp": started * 1000, "randomize": False, "reduceOnly": False, "user": A}
    st.update(extra)
    return {"state": st, "status": {"status": status}, "time": started * 1000}


def _cfg(chat="-100"):
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.crypto_chat_id = chat
    cfg.notify_twap = True
    cfg.twap_live_enabled = True
    cfg.twap_alert_min_usd = 2_000_000
    cfg.twap_alert_min_left_usd = 1_000_000
    cfg.twap_alert_vol_pct = 20
    cfg.twap_alert_big_usd = 0
    cfg.twap_alert_min_slices = 10
    cfg.twap_lookup_min_usd = 50_000
    cfg.twap_lookup_cooldown = 600
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
    tl._lookup_cache.clear()


def slices(coin, addr, side, n, gap=30, ntl=2200.0, px=MARK, t0=None, jitter=0.0, size_jitter=0.0,
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


async def set_vol(coin, v, ts=None, mark=MARK):
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


def _no_projection(txt):
    return "bu hızla" not in txt and "24 saatte" not in txt and "/gün" not in txt


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


# ------------------------------------------------ 2) ölçüm + snapshot ayrıştırma + kapı (saf)
def test_measure_parse_gate():
    tl.REG.clear()
    cfg = _cfg()
    now = dbm.now()
    slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
    m = tl.measure(tl.REG.runs[("INJ", A, "buy")])
    assert m and m["n"] == 50 and abs(m["total"] - 110_000) < 1e-6 and m["native_like"]
    assert m["median_gap"] == 30 and m["taker_pct"] == 100 and m["dur"] == 49 * 30
    # --- userTwapHistory: coin/yön süzgeci, activated önce, sayılar string, timestamp ms
    hist = [order(status="finished", started=now - 86400, ex_sz=SZ),       # eski bitmiş alış (ikinci sırada)
            order(side="A", sz=1000),                                       # satış → yön tutmaz
            order(coin="BTC", sz=10),                                        # başka coin
            order(started=now - 3030),                                       # aktif alış → ilk sırada
            {"state": "bozuk"}, {"state": {"coin": "INJ", "side": "B", "sz": "abc"}}, None, 7]
    os_ = tl.parse_twap_orders(hist, "INJ", "buy", MARK, now)
    assert [o["status"] for o in os_] == ["activated", "finished"], os_
    o = os_[0]
    assert o["planned_sz"] == SZ and abs(o["planned_usd"] - PLAN) < 1e-6 and o["minutes"] == 360
    assert abs(o["executed_usd"] - 130_000 * MARK) < 1e-6 and abs(o["remaining_usd"] - (SZ - 130_000) * MARK) < 1e-6
    assert abs(o["filled_pct"] - 130_000 / SZ * 100) < 1e-6 and o["started_ts"] == now - 3030
    assert o["left_sec"] == 360 * 60 - 3030 and o["reduce_only"] is False, o
    assert os_[1]["left_sec"] == 0 and abs(os_[1]["filled_pct"] - 100) < 1e-9
    # yön: 'A'/'Sell' satış, 'B'/'Buy' alış; fiyat yoksa dolan kısmın ortalaması
    assert len(tl.parse_twap_orders([order(side="A"), order(side="Sell"), order(side="Buy")], "INJ", "sell", MARK, now)) == 2
    assert len(tl.parse_twap_orders([order(side="Buy"), order(side="A")], "INJ", "buy", MARK, now)) == 1
    assert abs(tl.parse_twap_orders([order()], "INJ", "buy", None, now)[0]["planned_usd"] - PLAN) < 1e-6
    assert tl.parse_twap_orders([order(reduceOnly=True)], "INJ", "buy", MARK, now)[0]["reduce_only"] is True
    for bad in (None, "çöp", 42, {"history": []}, [None, 1, "x"], [{"state": None}], [{"status": {"status": "activated"}}]):
        assert tl.parse_twap_orders(bad, "INJ", "buy", MARK, now) == [], bad
    # --- kapı: emir ≥ $2M VE kalan ≥ $1M VE emir/hacim ≥ %20; taze hacim şart
    ok = tl.parse_twap_orders([order()], "INJ", "buy", MARK, now)[0]
    assert tl.order_gate(cfg, None, 9.9e6, now, now) == "no_order"
    assert tl.order_gate(cfg, {**ok, "status": "finished"}, 9.9e6, now, now) == "order_done", "ADA vakası: alış bitmiş"
    assert tl.order_gate(cfg, {**ok, "status": "terminated"}, 9.9e6, now, now) == "order_done"
    assert tl.order_gate(cfg, ok, 9.9e6, now, now) == "ok", "$4.15M emir, hacmin %42'si"
    assert tl.order_gate(cfg, ok, 2e9, now, now) == "vol_small", "BTC hacminde %0,2 — kapı kapalı"
    assert tl.order_gate(cfg, ok, None, None, now) == "no_vol", "hacim yok → tahmin yok → bildirim yok"
    assert tl.order_gate(cfg, ok, 9.9e6, now - 3601, now) == "no_vol", "bayat hacim"
    assert tl.order_gate(cfg, {**ok, "planned_usd": 1.5e6}, 9.9e6, now, now) == "order_small"
    assert tl.order_gate(cfg, {**ok, "remaining_usd": 600e3}, 9.9e6, now, now) == "order_left"
    cfg.twap_alert_big_usd = 3e6
    assert tl.order_gate(cfg, ok, None, None, now) == "big", "mutlak kapı açıksa hacim aranmaz"
    assert tl.order_gate(cfg, {**ok, "remaining_usd": 600e3}, None, None, now) == "order_left", "kalan azsa mutlak da geçmez"
    cfg.twap_alert_big_usd = 0
    # düzensiz aralık → ölçüm yok
    tl.REG.clear()
    r = random.Random(3)
    t = now - 5000
    for i in range(40):
        t += r.randint(10, 200)
        tl.observe("INJ", B, "buy", MARK, 100, 1230, t, 1)
    assert tl.measure(tl.REG.runs[("INJ", B, "buy")]) is None
    # HL randomize: ±%70 aralık, ±%80 boyut → canlı eşikle düzenli, arşiv (0.35) eşiğiyle değil
    tl.REG.clear()
    slices("INJ", C, "sell", 60, jitter=0.7, size_jitter=0.8, seed=5, t0=now - 60 * 30)
    run = tl.REG.runs[("INJ", C, "sell")]
    m2 = tl.measure(run)
    assert m2 is not None and 0.35 <= m2["cv_gap"] < 0.5 and 0.35 <= m2["cv_size"] < 0.6, (m2["cv_gap"], m2["cv_size"])
    rows = [{"ts": t, "sz": s, "notional": n} for (t, s, n, _) in run.slices]
    assert twapmod.detect(rows) is None, "arşiv eşiği bunu görmezdi"
    tl.REG.clear()
    print("✅ ölçüm) düzenlilik; snapshot ayrıştırma (yön/coin/sıra/bozuk şekil); kapı $2M·$1M·%20, bitmiş/hacimsiz geçmez")


# ------------------------------------------------ 3) collector: kanca + userTwapHistory sorgusu
def test_collector_hook_and_lookup():
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
        # userTwapHistory mesajı: bekleyen future dolar (adres küçük harfe indirgenir), ilk örnek saklanır
        loop_ = asyncio.get_running_loop()
        fut = loop_.create_future()
        col._twap_waiters[A] = fut
        await col._handle(json.dumps({"channel": "userTwapHistory",
                                      "data": {"user": A.upper(), "isSnapshot": True, "history": [order()]}}))
        assert fut.done() and fut.result()[0]["state"]["coin"] == "INJ"
        assert col.twap_sample and '"history"' in col.twap_sample and len(col.twap_sample) <= 600
        await col._handle(json.dumps({"channel": "subscriptionResponse", "data": {}}))
        await col._handle(json.dumps({"channel": "trades", "data": [
            {"coin": "INJ", "side": "B", "px": "12.33", "sz": "100", "time": now * 1000, "tid": "t5", "users": [A, B]}]}))
        assert run_a.n == 3, "emir mesajları trades akışını bozmaz"
        col._twap_waiters.clear()
        # user alanı yoksa ve tek bekleyen varsa ona gider; history listesi değilse []
        fut2 = loop_.create_future()
        col._twap_waiters["0x" + "9" * 40] = fut2
        col._on_twap_history({"isSnapshot": True, "history": "??"})
        assert fut2.done() and fut2.result() is None, "şekil uyumsuz → sorgu başarısız (emir yok DEĞİL)"
        col._twap_waiters.clear()
        # bağlı değil → None + hata sayacı (bildirim tahminle DEĞİL, gitmez)
        col._ws = None
        assert await col.fetch_twap_history(A) is None and col.twap_lookups_err == 1

        class WS:
            closed = False

            def __init__(self, deliver=True):
                self.sent, self.deliver = [], deliver

            async def send_json(self, msg):
                self.sent.append(msg)
                if msg["method"] == "subscribe" and self.deliver:
                    loop_.call_soon(col._on_twap_history, {"user": msg["subscription"]["user"],
                                                           "isSnapshot": True, "history": [order()]})
        # abone ol → snapshot → abonelikten çık; bekleyen temizlenir
        ws = col._ws = WS()
        hist = await col.fetch_twap_history(A.upper())
        assert hist and hist[0]["state"]["coin"] == "INJ" and col.twap_lookups_ok == 1
        assert [x["method"] for x in ws.sent] == ["subscribe", "unsubscribe"]
        assert ws.sent[0]["subscription"] == {"type": "userTwapHistory", "user": A} and not col._twap_waiters
        # zaman aşımı: None, sayaç, yine abonelikten çıkış
        ws = col._ws = WS(deliver=False)
        assert await col.fetch_twap_history(A, timeout=0.05) is None and col.twap_lookups_timeout == 1
        assert ws.sent[-1]["method"] == "unsubscribe" and not col._twap_waiters
        # eşzamanlı iki istek tek aboneliği paylaşır
        ws = col._ws = WS()
        r1, r2 = await asyncio.gather(col.fetch_twap_history(A), col.fetch_twap_history(A))
        assert r1 and r2 and sum(1 for x in ws.sent if x["method"] == "subscribe") == 1 and not col._twap_waiters
        print("✅ kanca) trade sayımı + fills boş; userTwapHistory future'ı doldurur; abone/çık, zaman aşımı, paylaşım")
    asyncio.run(run())


# ------------------------------------------------ 4) alarm: emir sorgusu → kripto kanalı, dedupe, restart
def test_evaluate_alert_crypto():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        now = dbm.now()
        await set_vol("INJ", 9.9e6)
        col = FakeCol([order(started=now - 3030)])
        col.twap_sample = '{"user": "0x..", "history": []}'
        slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["alerted"] == 1 and out["cands"] == 1 and out["regular"] == 1 and out["lookups"] == 1, out
        assert out["no_chat"] == 0 and out["sample"].startswith('{"user"') and col.calls == [A]
        assert out["best"]["coin"] == "INJ" and abs(out["best"]["planned"] - PLAN) < 1e-6 and out["best"]["status"] == "activated"
        assert len(bot.sent) == 1 and bot.sent[0][0] == "-100"
        txt = bot.sent[0][1]
        assert f"⏳ <b>TWAP</b> · <b>INJ</b> · 🟢 ALIŞ · 🐘 emir 24s hacmin <b>%{PCT}</b>'i" in txt, txt
        assert f"HL TWAP emri: <b>337.7K INJ ≈ {fmt.usd(PLAN)}</b> (bugünkü fiyatla) · 6 s 0 dk · " in txt, txt
        assert "'te başladı · <b>5 s 9 dk kaldı</b>" in txt, txt
        assert f"doldu <b>{fmt.usd(130_000 * MARK)}</b> (%38) · kalan ≈ {fmt.usd((SZ - 130_000) * MARK)}" in txt, txt
        assert f"gördüğümüz 50 dilim × ~{fmt.usd(2200)}, her 30 sn" in txt and "pozisyon: bilinmiyor" in txt, txt
        assert "24s hacim $9.9M" in txt and "%100 taker" in txt and _no_projection(txt), txt
        assert await _alerts("twap") == 1 and await _alerts("sent:twap") == 1, "metin (kripto yolunda işaret gönderimden SONRA)"
        rows = await _runs()
        r = rows[0]
        assert len(rows) == 1 and r["src"] == "live" and r["alerted_ts"] and r["day_volume"] == 9.9e6 and r["n_slices"] == 50
        assert abs(r["planned_usd"] - PLAN) < 1e-6 and r["planned_sz"] == SZ and r["order_status"] == "activated"
        assert r["order_ts"] == now - 3030 and r["order_min"] == 360 and r["lookup_ts"] and abs(r["executed_usd"] - 130_000 * MARK) < 1e-6
        st = await dbm.kv_get(tl.STATS_KV)
        assert st["alerted"] == 1 and st["lookups"] == 1 and st["best"]["coin"] == "INJ"
        # ikinci tur: tekrar yok, 10 dk dolmadan yeniden sorgu yok
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["alerted"] == 0 and len(bot.sent) == 1 and col.calls == [A]
        # restart: registry boş, aynı dilimler → bekleme DB'de; tekrar yok, sorgu bile yok
        tl.REG.clear()
        tl._lookup_cache.clear()
        slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["alerted"] == 0 and len(bot.sent) == 1 and tl.REG.runs[("INJ", A, "buy")].alerted_ts and col.calls == [A]
        # entity mm → sorgusuz elenir
        await _fresh()
        await set_vol("INJ", 9.9e6)
        async with dbm.db() as c:
            await c.execute("INSERT INTO addresses(address,first_seen,entity) VALUES(?,?,'mm')", (B, now))
        slices("INJ", B, "buy", 50, t0=now - 49 * 30 - 5)
        col = FakeCol([order()])
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["skipped_mm"] == 1 and out["alerted"] == 0 and col.calls == []
        # sorgu tabanı: 20 dilim = $44K < $50K → aday değil, sorgu yok
        await _fresh()
        await set_vol("INJ", 9.9e6)
        slices("INJ", A, "buy", 20, t0=now - 19 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["cands"] == 0 and col.calls == []
        print("✅ alarm) emir sorgusu → kripto kanalına tek mesaj (tahmin yok), işaret+metin, twap_runs emir kolonları, restart'ta tekrar yok, mm elenir")
    asyncio.run(run())


# ------------------------------------------------ 5) kapı nedenleri: bildirim YOK
def test_gate_reasons():
    async def run():
        now = dbm.now()
        cases = [
            ("bitmiş (ADA vakası)", FakeCol([order(status="finished", ex_sz=SZ)]), 9.9e6, "order_done"),
            ("iptal", FakeCol([order(status="terminated", ex_sz=20_000)]), 9.9e6, "order_done"),
            ("emir yok", FakeCol([]), 9.9e6, "no_order"),
            ("başka coinin emri", FakeCol([order(coin="SOL")]), 9.9e6, "no_order"),
            ("sorgu başarısız", FakeCol(None), 9.9e6, "lookup_fail"),
            ("sorgu istisna", FakeCol(None, raise_=True), 9.9e6, "lookup_fail"),
            ("emir $1.5M", FakeCol([order(sz=120_000, ex_sz=10_000)]), 9.9e6, "order_small"),
            ("kalan $587K", FakeCol([order(ex_sz=290_000)]), 9.9e6, "order_left"),
            ("hacim yok", FakeCol([order()]), None, "no_vol"),
            ("hacim bayat", FakeCol([order()]), -1, "no_vol"),
            ("BTC hacminde %0,2", FakeCol([order()]), 2e9, "vol_small"),
        ]
        for label, col, vol, reason in cases:
            await _fresh()
            cfg = _cfg()
            bot = Bot()
            notifier = Notifier(cfg, bot)
            if vol == -1:
                await set_vol("INJ", 9.9e6, ts=now - 3700)
            elif vol:
                await set_vol("INJ", vol)
            slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
            out = await tl.evaluate(cfg, notifier, collector=col)
            assert out["alerted"] == 0 and out[reason] == 1 and bot.sent == [] and col.calls == [A], (label, out)
            assert await _alerts("twap") == 0 and not await _runs(), label
            assert not tl.REG.runs[("INJ", A, "buy")].alerted_ts, label
        # collector yok → sorgu yok → bildirim yok (tahmin yok)
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        await set_vol("INJ", 9.9e6)
        slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=None)
        assert out["no_lookup"] == 1 and out["alerted"] == 0 and out["lookups"] == 0 and bot.sent == []
        # önbellek: aynı adres iki coinde → tek sorgu, iki bildirim
        await _fresh()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        await set_vol("INJ", 9.9e6)
        await set_vol("SOL", 9.9e6)
        col = FakeCol([order(), order(coin="SOL")])
        slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        slices("SOL", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["alerted"] == 2 and out["lookups"] == 1 and col.calls == [A], out
        assert {t[1].split("·")[1].strip() for t in bot.sent} == {"<b>INJ</b>", "<b>SOL</b>"}
        # tur başına sorgu tavanı: kalanlar bu turda sorgusuz (emir yok sayılır), sayaç
        await _fresh()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        await set_vol("INJ", 9.9e6)
        old = tl.LOOKUP_MAX_PER_EVAL
        tl.LOOKUP_MAX_PER_EVAL = 2
        try:
            for a in (A, B, C):
                slices("INJ", a, "buy", 50, t0=now - 49 * 30 - 5)
            col = FakeCol([])
            out = await tl.evaluate(cfg, notifier, collector=col)
            assert out["lookups"] == 2 and out["lookup_capped"] == 1 and out["no_order"] == 3 and len(col.calls) == 2, out
        finally:
            tl.LOOKUP_MAX_PER_EVAL = old
        print("✅ kapı) bitmiş/iptal/yok/başarısız/küçük/kalan az/hacimsiz/bayat/BTC → bildirim yok; collector yok → yok; önbellek; tavan")
    asyncio.run(run())


# ------------------------------------------------ 6) kanal boş / hisse ana sohbet / sessiz saat
def test_routing_and_quiet():
    async def run():
        await _fresh()
        cfg = _cfg(chat="")
        bot = Bot()
        notifier = Notifier(cfg, bot)
        now = dbm.now()
        await set_vol("INJ", 9.9e6)
        col = FakeCol([order()])
        slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["no_chat"] == 1 and out["alerted"] == 0 and bot.sent == [] and await _alerts("twap") == 0
        assert (await _runs())[0]["alerted_ts"] and (await _runs())[0]["planned_usd"], "kanal yokken de tur bildirilmiş sayılır (sayfada görünür)"
        # hisse: asset_metrics hacmi → ana sohbet; emir 20M UANSEM ≈ $4.5M = hacmin %111'i
        eq_hist = [order(coin="xyz:UANSEM", side="A", sz=20_000_000, ex_sz=5_000_000, ex_ntl=5_000_000 * 0.2268, minutes=60)]
        for quiet in (False, True):
            await _fresh()
            cfg = _cfg(chat="")
            if quiet:
                cfg.quiet_start_hour, cfg.quiet_end_hour = 0, 24
                cfg.quiet_allow_high = False
            bot = Bot()
            notifier = Notifier(cfg, bot)
            async with dbm.db() as c:
                await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:UANSEM','UANSEM')")
                await c.execute("INSERT INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume)"
                                " VALUES('xyz:UANSEM',?,0.2268,1e7,0.0001,4.1e6)", (now,))
            slices("xyz:UANSEM", B, "sell", 50, ntl=13_475, px=0.2268, t0=now - 49 * 30 - 5)
            out = await tl.evaluate(cfg, notifier, collector=FakeCol(eq_hist))
            if not quiet:
                assert out["alerted"] == 1 and bot.sent[0][0] is None, "hisse → ana sohbet"
                txt = bot.sent[0][1]
                pct = round(20_000_000 * 0.2268 / 4.1e6 * 100)
                assert "UANSEM" in txt and "🔴 SATIŞ" in txt and f"🐘 emir 24s hacmin <b>%{pct}</b>'i" in txt, txt
                assert f"HL TWAP emri: <b>20.00M UANSEM ≈ {fmt.usd(20_000_000 * 0.2268)}</b>" in txt and _no_projection(txt), txt
            else:
                # sessiz saat (ana sohbet): tek özet kaydı, tekrar yok, hata sayılmaz
                assert bot.sent == [] and out["failed"] == 0 and out.get("quiet") == 1, out
                assert await _alerts("quiet:twap") == 1 and await _alerts("twap") == 1
                await tl.evaluate(cfg, notifier, collector=FakeCol(eq_hist))
                tl.REG.clear()
                tl._lookup_cache.clear()
                slices("xyz:UANSEM", B, "sell", 50, ntl=13_475, px=0.2268, t0=now - 49 * 30 - 5)
                await tl.evaluate(cfg, notifier, collector=FakeCol(eq_hist))
                assert await _alerts("quiet:twap") == 1, "işaret gönderimden önce → sessiz saatte tek özet kaydı"
        print("✅ yönlendirme) kanal boş → gönderim yok; hisse → ana sohbet; sessiz saatte tek özet, hata değil")
    asyncio.run(run())


# ------------------------------------------------ 7) ilerleme %50, bitiş 🏁 / iptal ⛔, geri yükleme
def test_progress_end_rehydrate():
    async def run():
        await _fresh()
        cfg = _cfg()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        now = dbm.now()
        await set_vol("INJ", 9.9e6)
        col = FakeCol([order()])
        last = slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["alerted"] == 1 and len(bot.sent) == 1, out
        run = tl.REG.runs[("INJ", A, "buy")]
        # dilimler sürüyor, emir %38 → not yok; 10 dk dolmadan yeniden sorgu yok
        slices("INJ", A, "buy", 20, t0=last + 30)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["progress"] == 0 and out["ended"] == 0 and col.calls == [A]
        # 10 dk geçti → yeniden sorgu: emrin yarısı doldu → TEK ilerleme notu (gerçek tutarla)
        col.hist = [order(ex_sz=180_000)]
        run.lookup_ts = now - 601
        out = await tl.evaluate(cfg, notifier, collector=col)
        p = bot.sent[-1][1]
        assert out["progress"] == 1 and col.calls == [A, A] and "⏳ <b>TWAP yarılandı</b> · <b>INJ</b> · 🟢 ALIŞ" in p, p
        assert f"doldu <b>{fmt.usd(180_000 * MARK)}</b> / {fmt.usd(PLAN)} (%53)" in p and "kaldı" in p and _no_projection(p), p
        r = (await _runs())[0]
        assert run.half_ts and r["n_slices"] == 70 and abs(r["executed_usd"] - 180_000 * MARK) < 1e-6
        col.hist = [order(ex_sz=270_000)]
        run.lookup_ts = now - 601
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["progress"] == 0 and len(bot.sent) == 2, "%80'de ikinci not yok"
        # dilimler kesildi ama sorgu hâlâ activated → bitmiş SAYILMAZ (tahmin yok)
        run.last_ts = now - 400
        run.lookup_ts = now - 601
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["ended"] == 0 and not run.ended_ts and len(bot.sent) == 2
        # emir finished → 🏁 gerçek dolan / plan, 24s hacme oran; tekrar yok
        col.hist = [order(status="finished", ex_sz=SZ)]
        run.lookup_ts = now - 601
        out = await tl.evaluate(cfg, notifier, collector=col)
        e = bot.sent[-1][1]
        assert out["ended"] == 1 and "🏁 <b>TWAP bitti</b> · <b>INJ</b> · 🟢 ALIŞ" in e, e
        assert f"doldu <b>{fmt.usd(PLAN)}</b> / plan {fmt.usd(PLAN)} (%100)" in e and f"24s hacmin <b>%{PCT}</b>'i" in e and _no_projection(e), e
        r = (await _runs())[0]
        assert run.ended_ts and r["ended_ts"] and r["order_status"] == "finished"
        n_sent = len(bot.sent)
        await tl.evaluate(cfg, notifier, collector=col)
        assert len(bot.sent) == n_sent and await _alerts("twap_end") == 1
        # iptal: yeni tur (B) bildirildi, sonra terminated → ⛔ dolan / plan
        tl._lookup_cache.clear()
        col.hist = [order(ex_sz=50_000)]
        slices("INJ", B, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["alerted"] == 1, out
        rb = tl.REG.runs[("INJ", B, "buy")]
        col.hist = [order(status="terminated", ex_sz=50_000)]
        rb.lookup_ts = now - 601
        out = await tl.evaluate(cfg, notifier, collector=col)
        e = bot.sent[-1][1]
        assert out["ended"] == 1 and "⛔ <b>TWAP iptal edildi</b> · <b>INJ</b>" in e, e
        assert f"doldu <b>{fmt.usd(50_000 * MARK)}</b> / plan {fmt.usd(PLAN)} (%15)" in e, e
        # emir geçmişten düştü + dilimler kesildi → bitiş notu gördüğümüz toplamla (tahmin değil)
        tl._lookup_cache.clear()
        col.hist = [order()]
        slices("INJ", C, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["alerted"] == 1, out
        rc = tl.REG.runs[("INJ", C, "buy")]
        col.hist = []
        rc.lookup_ts = now - 601
        rc.last_ts = now - 400
        out = await tl.evaluate(cfg, notifier, collector=col)
        e = bot.sent[-1][1]
        assert out["ended"] == 1 and "🏁 <b>TWAP bitti</b>" in e and f"gördüğümüz toplam <b>{fmt.usd(110_000)}</b>" in e, e
        # geri yükleme: DB'de bildirilmiş-bitmemiş satır (emir kolonlarıyla), registry boş → sorgu finished → 🏁
        await _fresh()
        bot = Bot()
        notifier = Notifier(cfg, bot)
        await set_vol("INJ", 9.9e6)
        async with dbm.db() as c:
            await c.execute(
                "INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,avg_slice,avg_gap,"
                "cv_gap,cv_size,taker_pct,ts,day_volume,rate_day,src,alerted_ts,px_first,px_last,sz_total,"
                "planned_usd,planned_sz,executed_usd,remaining_usd,order_ts,order_min,order_status,lookup_ts)"
                " VALUES('INJ',?,'buy',?,?,300,660000,2200,30,0.05,0.1,100,?,9.9e6,6.3e6,'live',?,12.3,12.5,53000,"
                "?,?,1599000,?,?,360,'activated',?)",
                (A, now - 9400, now - 400, now - 400, now - 5000, PLAN, SZ, PLAN - 1599000, now - 9400, now - 5000))
        assert await tl._rehydrate(4 * 3600) == 1
        run = tl.REG.runs[("INJ", A, "buy")]
        assert run.order and run.order["status"] == "activated" and abs(run.order["planned_usd"] - PLAN) < 1e-6
        assert run.lookup_ts == now - 5000 and abs(run.order["filled_pct"] - 1599000 / PLAN * 100) < 1e-6
        col = FakeCol([order(status="finished", ex_sz=SZ, started=now - 9400)])
        out = await tl.evaluate(cfg, notifier, collector=col)
        e = bot.sent[-1][1]
        assert out["ended"] == 1 and col.calls == [A] and "TWAP bitti" in e and f"doldu <b>{fmt.usd(PLAN)}</b> / plan {fmt.usd(PLAN)}" in e, e
        assert (await _runs())[0]["ended_ts"] and (await _runs())[0]["order_status"] == "finished"
        print("✅ takip) yarı dolunca tek ilerleme; activated iken sessizlik bitiş değil; 🏁/⛔ gerçek tutar; emir düşerse gördüğümüz toplam; geri yükleme")
    asyncio.run(run())


# ------------------------------------------------ 8) arşiv taraması canlı kolonları korur; recent emir kolonları; sayfa
def test_scan_upsert_and_recent():
    async def run():
        await _fresh()
        cfg = _cfg()
        now = dbm.now()
        t0 = now - 600
        run = tl.Run("INJ", A, "buy", t0, MARK)
        run.n, run.total, run.last_ts, run.alerted_ts, run.day_volume, run.rate_day = 6, 6e6, t0 + 300, now - 100, 9.9e6, 5e8
        run.order = tl.parse_twap_orders([order(started=now - 3030)], "INJ", "buy", MARK, now)[0]
        run.lookup_ts = now - 100
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
        assert abs(rows[0]["planned_usd"] - PLAN) < 1e-6 and rows[0]["order_status"] == "activated", "arşiv emir kolonlarına dokunmaz"
        # farklı first_ts ile ikinci satır → recent tekilleştirir (çok dilimli kalır)
        run2 = tl.Run("INJ", A, "buy", t0 - 900, MARK)
        run2.n, run2.total, run2.last_ts = 40, 88_000, t0 + 300
        await tl.persist(run2, {"avg_gap": 30, "cv_gap": 0.05, "cv_size": 0.1, "taker_pct": 100})
        rec = await twapmod.recent()
        assert len(rec) == 1 and rec[0]["n_slices"] == 40 and rec[0]["src"] == "live" and rec[0]["order_pct"] is None
        run2.n = 3
        await tl.persist(run2, {"avg_gap": 30, "cv_gap": 0.05, "cv_size": 0.1, "taker_pct": 100})
        rec = await twapmod.recent()
        r = rec[0]
        assert r["n_slices"] == 6 and abs(r["order_pct"] - PLAN / 9.9e6 * 100) < 1e-6
        assert abs(r["filled_pct"] - 130_000 / SZ * 100) < 1e-6 and 18_500 <= r["left_sec"] <= 18_570, r["left_sec"]
        assert abs(r["remaining_usd"] - (SZ - 130_000) * MARK) < 1e-6
        # sayfa render: Emir / Doldu / Kalan kolonları, künye sorgu sayaçları, tahmin metni yok
        from app.web.routes import templates
        html = templates.env.get_template("twap.html").render(
            request=type("R", (), {"url": type("U", (), {"path": "/twap"})()})(), k="", is_admin=False, has_pw=False,
            rows=rec, st={}, cov={"crypto": {"n": 10, "a": now}, "equity": {"n": 5, "a": now}}, min_usd=5e6,
            window_h=12, crypto_floor=5000, eq_floor=5000,
            live={"ts": now, "keys": 12, "cands": 1, "regular": 1, "alerted": 1, "ended": 0, "no_chat": 0,
                  "lookups": 3, "lookup_fail": 1, "no_order": 2, "order_done": 1, "order_small": 1},
            ws={"crypto": ["INJ", "BTC"], "equity_n": 40}, alert_min=2e6, min_left=1e6, vol_pct=20, big_usd=0, live_on=True)
        assert "📡 canlı" in html and ">Emir<" in html and ">Doldu<" in html and ">Kalan<" in html and "tahmin yok" in html
        assert f"%{PCT}" in html and "%38" in html and "3 emir sorgusu" in html and "1 başarısız" in html
        assert "2 emir yok" in html and "1 bitmiş" in html and "1 eşik altı" in html and "12 dizi bellekte" in html
        assert "dinlenen <b>2</b> kripto + <b>40</b> hisse" in html and _no_projection(html) and "Hız/gün" not in html
        print("✅ arşiv) upsert canlı+emir kolonlarını korur; recent emir/doldu/kalan; sayfa yeni kolonlar, tahmin yok")
    asyncio.run(run())


# ------------------------------------------------ 9) mesajlar + bağlantı
def test_messages_and_wiring():
    now = dbm.now()
    m = {"coin": "INJ", "address": A, "side": "buy", "n": 42, "total": 92_400, "sz_total": 7512, "first_ts": 0,
         "last_ts": 1260, "dur": 1260, "avg_slice": 2200, "avg_gap": 30, "median_gap": 30, "cv_gap": 0.05,
         "cv_size": 0.1, "rate_day": 6.336e6, "native_like": True, "taker_pct": 100, "avg_px": 12.36,
         "px_first": 12.34, "px_last": 12.44, "px_chg_pct": 0.81}
    o = tl.parse_twap_orders([order(started=now - 3030)], "INJ", "buy", MARK, now)[0]
    t = fmt.twap_alert(m, {"order": o, "day_vol": 9.9e6, "vol_pct": PLAN / 9.9e6 * 100,
                           "pos": {"side": "long", "notional": 1.2e6, "src": "canlı"}, "gate": "ok", "klass": "kripto", "entity": ""})
    assert f"🐘 emir 24s hacmin <b>%{PCT}</b>'i" in t and f"HL TWAP emri: <b>337.7K INJ ≈ {fmt.usd(PLAN)}</b> (bugünkü fiyatla) · 6 s 0 dk · " in t, t
    assert "'te başladı · <b>5 s 9 dk kaldı</b>" in t and f"doldu <b>{fmt.usd(1_599_000)}</b> (%38) · kalan ≈ {fmt.usd(PLAN - 1_599_000)}" in t, t
    assert f"gördüğümüz 42 dilim × ~{fmt.usd(2200)}, her 30 sn" in t and "📍 pozisyon: 🟢LONG <b>$1.2M</b> · canlı" in t, t
    assert "+0.8%" in t and "24s hacim $9.9M" in t and "yatırım tavsiyesi" in t and _no_projection(t), t
    t2 = fmt.twap_alert({**m, "median_gap": 90}, {"order": {**o, "reduce_only": True, "left_sec": 0}, "day_vol": None,
                                                   "vol_pct": None, "pos": {"none": True, "src": "canlı"}, "gate": "big", "klass": "hisse"})
    assert "💰 hacimden bağımsız büyük" in t2 and "reduce-only (pozisyon kapatıyor)" in t2 and "<b>süresi doldu</b>" in t2, t2
    assert "açık perp pozisyonu yok" in t2 and "her 2 dk" in t2 and "24s hacim" not in t2
    p = fmt.twap_progress(m, {"order": {**o, "executed_usd": 2.2e6, "filled_pct": 53.0}})
    assert "⏳ <b>TWAP yarılandı</b>" in p and f"doldu <b>{fmt.usd(2.2e6)}</b> / {fmt.usd(PLAN)} (%53)" in p and "kaldı" in p and _no_projection(p), p
    e = fmt.twap_end(m, {"order": {**o, "status": "finished", "executed_usd": PLAN, "filled_pct": 100.0}, "day_vol": 9.9e6})
    assert "🏁 <b>TWAP bitti</b>" in e and f"doldu <b>{fmt.usd(PLAN)}</b> / plan {fmt.usd(PLAN)} (%100)" in e and f"24s hacmin <b>%{PCT}</b>'i" in e, e
    e2 = fmt.twap_end(m, {"order": {**o, "status": "terminated", "executed_usd": 615_000.0, "filled_pct": 14.8},
                          "day_vol": 9.9e6, "cancelled": True})
    assert "⛔ <b>TWAP iptal edildi</b>" in e2 and f"doldu <b>{fmt.usd(615_000)}</b> / plan" in e2 and "24s hacmin <b>%6</b>'i" in e2, e2
    e3 = fmt.twap_end({**m, "total": 1_610_000, "sz_total": 337_720, "dur": 21_700}, {"order": None, "day_vol": 9.9e6})
    assert f"gördüğümüz toplam <b>{fmt.usd(1_610_000)}</b> (337.7K INJ)" in e3 and "24s hacmin <b>%16</b>'i" in e3 and _no_projection(e3), e3
    assert fmt.qty_txt(7_010_000) == "7.01M" and fmt.qty_txt(337_720) == "337.7K" and fmt.qty_txt(12) == "12"
    # bağlantı
    from app.config import EDITABLE_FIELDS
    c = Config()
    for f in ("twap_live_enabled", "twap_alert_min_usd", "twap_alert_min_left_usd", "twap_alert_vol_pct",
              "twap_alert_vol_pct_major",
              "twap_alert_big_usd", "twap_alert_min_slices", "twap_lookup_min_usd", "twap_lookup_cooldown",
              "twap_alert_cooldown", "twap_alert_progress", "twap_alert_end_note", "twap_live_window_min",
              "twap_live_eval_sec", "twap_min_usd", "twap_window_h", "twap_scan_sec"):
        assert f in EDITABLE_FIELDS and hasattr(c, f) and EDITABLE_FIELDS[f]["group"] == "TWAP radarı", f
        assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
    assert "twap_alert_rate_pct" not in EDITABLE_FIELDS and not hasattr(c, "twap_alert_rate_pct")
    assert EDITABLE_FIELDS["notify_twap"]["group"] == "Bildirimler" and c.notify_twap is True
    if not os.getenv("TWAP_ALERT_MIN_USD"):
        assert c.twap_alert_min_usd == 1_000_000 and c.twap_alert_min_left_usd == 1_000_000
        assert c.twap_alert_vol_pct == 5 and c.twap_alert_vol_pct_major == 20
    assert c.twap_alert_big_usd == 0 and c.twap_alert_min_slices == 10 and c.twap_lookup_min_usd == 50_000 and c.twap_lookup_cooldown == 600
    from app.notify import KINDS
    assert KINDS["twap"][0] == "notify_twap" and KINDS["twap"][2] == "high"
    from app.health import limits, periods
    assert "twaplive" in limits(c) and "twaplive" in periods(c)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rd = lambda *p: open(os.path.join(root, *p), encoding="utf-8").read()  # noqa: E731
    assert "twaplive_loop(cfg, client, notifier, collector)" in rd("app", "main.py")
    colsrc = rd("app", "hl", "collector.py")
    assert "twaplive.observe(" in colsrc and '"userTwapHistory"' in colsrc and "def fetch_twap_history" in colsrc
    assert "twap_runs ADD COLUMN planned_usd" in rd("app", "db.py") and "twap_runs ADD COLUMN lookup_ts" in rd("app", "db.py")
    assert '"twaplive_stats"' in rd("app", "diag.py") and "emir sorgusu" in rd("app", "diag.py")
    assert "Canlı TWAP alarmı" in rd("README.md") and "userTwapHistory" in rd("README.md") and fmt.TASK_TR.get("twaplive")
    for p in (("app", "telegram", "format.py"), ("app", "web", "templates", "twap.html")):
        assert "bu hızla" not in rd(*p), p
    print("✅ mesaj/bağlantı) alarm/ilerleme/bitiş metinleri tahminsiz; 17 ayar TWAP radarı grubunda ($1M/$1M/%5, BTC/ETH %20); KINDS/health/spawn/kanca/migrasyon/README")


# ------------------------------------------------ 10) PUMP vakası: sınıfa göre hacim kuralı, $1M taban, teşhis
def test_pump_case_and_diag():
    async def run():
        now = dbm.now()
        await _fresh()
        cfg = _cfg()
        cfg.twap_alert_min_usd, cfg.twap_alert_vol_pct, cfg.twap_alert_vol_pct_major = 1_000_000, 5, 20
        bot = Bot()
        notifier = Notifier(cfg, bot)
        await set_vol("PUMP", 62e6, mark=0.003)
        col = FakeCol([order(coin="PUMP", sz=1_000_000_000, ex_sz=70_000_000, ex_ntl=210_000)])
        slices("PUMP", A, "buy", 50, ntl=4200, px=0.003, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["vol_small"] == 1 and out["alerted"] == 0 and bot.sent == [], out       # %4,8 < %5
        d = out["decisions"][0]
        assert d["reason"] == "vol_small" and d["coin"] == "PUMP" and d["addr"] == A and abs(d["planned"] - 3e6) < 1
        assert abs(d["vol_pct"] - 4.84) < 0.05 and d["pct_needed"] == 5 and d["need_usd"] == 3.1e6, d
        last = await dbm.kv_get(tl.LAST_KV)
        assert last and last[0]["reason"] == "vol_small" and last[0]["addr"] == A
        line = tl.decision_line(d)
        assert line == f"PUMP {fmt.short(A)} buy $3.0M emir, hacmin %4.8'i → hacme göre küçük (%5 gerekir)", line
        from app import diag
        txt = await diag.report(cfg, None)
        assert "son elenenler: PUMP" in txt and "hacme göre küçük (%5 gerekir)" in txt and "1 hacme göre küçük" in txt, txt
        # %4 → geçer, bildirilir (aynı emir); majör kuralı BTC'de %20 kalır
        cfg.twap_alert_vol_pct = 4
        tl._lookup_cache.clear()
        out = await tl.evaluate(cfg, notifier, collector=col)
        assert out["alerted"] == 1 and len(bot.sent) == 1 and "<b>PUMP</b>" in bot.sent[0][1], out
        assert out["decisions"][0]["reason"] == "alerted"
        await set_vol("BTC", 2e9, mark=100_000.0)
        slices("BTC", B, "buy", 50, ntl=60_000, px=100_000.0, t0=now - 49 * 30 - 5)
        col2 = FakeCol([order(coin="BTC", sz=30, ex_sz=1, ex_ntl=100_000)])       # $3M, hacmin %0,15'i
        bot.sent.clear()
        out = await tl.evaluate(cfg, notifier, collector=col2)
        assert out["vol_small"] == 1 and bot.sent == [] and tl.vol_pct_for(cfg, "BTC") == 20 and tl.vol_pct_for(cfg, "PUMP") == 4
        # $1M taban + kalan yarı kuralı: $1.2M emir, kalan $700K → geçer; kalan $400K → kalan az
        for ex, reason in ((40_650, "ok"), (65_041, "order_left")):
            o = tl.parse_twap_orders([order(sz=97_561, ex_sz=ex)], "INJ", "buy", MARK, now)[0]
            g = tl.order_gate(cfg, o, 9.9e6, now, now, coin="INJ")
            assert g == reason, (ex, g, o["remaining_usd"])
        assert tl.left_floor(cfg, 1.2e6) == 600_000 and tl.left_floor(cfg, 4e6) == 1_000_000
        # gönderim düşerse bekleme yanmaz: ikinci tur yeniden dener
        await _fresh()
        await set_vol("INJ", 9.9e6)
        bad, notifier_bad = Bot(ok=False), None
        notifier_bad = Notifier(cfg, bad)
        col3 = FakeCol([order()])
        slices("INJ", A, "buy", 50, t0=now - 49 * 30 - 5)
        out = await tl.evaluate(cfg, notifier_bad, collector=col3)
        assert out["failed"] == 1 and await _alerts("twap") == 0 and await _alerts("fail:twap") == 1
        assert tl.REG.runs[("INJ", A, "buy")].alerted_ts is None and out["decisions"][0]["reason"] == "failed"
        good = Bot()
        out = await tl.evaluate(cfg, Notifier(cfg, good), collector=col3)
        assert out["alerted"] == 1 and len(good.sent) == 1 and out["lookups"] == 0, "önbellekten, yeniden sorgu yok"
        print("✅ PUMP) %5 kuralı ($3M, hacmin %4,8'i → küçük; %4 → bildirim), BTC %20; $1M + kalan yarı; son kararlar; gönderim düşünce bekleme yanmaz")
    asyncio.run(run())


def test_volumes_fetch_and_twap_command():
    async def run():
        now = dbm.now()
        await _fresh()
        cfg = _cfg()
        cfg.twap_alert_min_usd, cfg.twap_alert_vol_pct = 1_000_000, 5
        # kv bayat (>30 dk) → client verilirse tek istekle tazelenir; verilmezse eski davranış
        await set_vol("INJ", 9.9e6, ts=now - 3700)

        class Cli:
            def __init__(self):
                self.calls = 0

            async def meta_and_ctxs(self, dex=""):
                self.calls += 1
                return [{"universe": [{"name": "INJ"}]},
                        [{"markPx": str(MARK), "openInterest": "1", "funding": "0", "dayNtlVlm": "12000000"}]]
        assert (await tl.volumes())["INJ"][1] == now - 3700
        cli = Cli()
        v = await tl.volumes(cli)
        assert v["INJ"][0] == 12e6 and v["INJ"][1] >= now - 5 and cli.calls == 1, v
        await tl.volumes(cli)
        assert cli.calls == 1, "taze kv → istek yok"
        # /twap komutu: adres / coin / özet
        await set_vol("PUMP", 62e6, mark=0.003)
        await dbm.kv_set("ws_universe", {"crypto": ["PUMP", "INJ"], "equity_n": 3, "ts": now})
        slices("PUMP", A, "buy", 50, ntl=4200, px=0.003, t0=now - 49 * 30 - 5)
        col = FakeCol([order(coin="PUMP", sz=1_000_000_000, ex_sz=70_000_000, ex_ntl=210_000),
                       order(coin="PUMP", sz=608_760_000, ex_sz=608_760_000, ex_ntl=1_826_000, status="finished")])
        await tl.evaluate(cfg, Notifier(cfg, Bot()), collector=col)
        from app.telegram.bot import TelegramBot
        tb = TelegramBot(cfg, None, None, {})
        tb.collector = col
        sent = []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append(text)
            return True
        tb.send = fake_send
        await tb._cmd_twap([A], "111")
        t = sent[-1]
        assert "Bellekteki diziler" in t and "PUMP" in t and "50 dilim" in t and "düzenli ✓" in t, t
        assert "HL TWAP emirleri (2 kayıt" in t and "PUMP BUY $3.0M" in t and "kalan $2.8M" in t and "activated" in t, t
        assert "kapı: hacme göre küçük — hacim $62.0M, emir hacmin %4.8'i (gerek %5, ≥ $3.1M)" in t, t
        assert "$1.8M" in t and "finished" in t and "kapı: emir bitmiş/iptal" in t and "Radarın son kararları" in t, t
        await tb._cmd_twap(["pump"], "111")
        t = sent[-1]
        assert "dinleniyor ✓" in t and "24s hacim $62.0M" in t and "bildirim için emir ≥ $3.1M" in t and "Bellekteki diziler (1)" in t, t
        await tb._cmd_twap(["ZZZ"], "111")
        assert "dinlenMİyor" in sent[-1] and "hacim bilinmiyor" in sent[-1]
        await tb._cmd_twap([], "111")
        assert "Son kararlar" in sent[-1] and "1 aday" in sent[-1] and "/twap 0xADRES" in sent[-1], sent[-1]
        # sorgu başarısız / collector yok / emir yok
        tb.collector = FakeCol(None)
        await tb._cmd_twap([B], "111")
        assert "sorgu başarısız" in sent[-1] and "yok (son 4 saatte" in sent[-1]
        tb.collector = None
        await tb._cmd_twap([B], "111")
        assert "collector/soket yok" in sent[-1]
        tb.collector = FakeCol([])
        await tb._cmd_twap([B], "111")
        assert "userTwapHistory boş" in sent[-1]
        # bağlantı
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rd = lambda *p: open(os.path.join(root, *p), encoding="utf-8").read()  # noqa: E731
        assert 'elif cmd == "twap":' in rd("app", "telegram", "bot.py") and "/twap 0x" in fmt.help_text()
        assert "CRYPTO_WATCH_TOP=120" in rd(".env.example") and "vol_pct_major" in rd("app", "web", "templates", "twap.html")
        assert "/twap" in rd("README.md") and "BTC/ETH" in rd("README.md")
        print("✅ hacim/komut) bayat kv → tek istek; /twap adres (emirler + kapı sayılarla) / coin (dinleme, gereken emir) / özet")
    asyncio.run(run())
