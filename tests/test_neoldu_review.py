"""🔎 "Ne oldu?" — inceleme düzeltmeleri (18.10, çok-ajanlı inceleme; her bulgu yeniden üretildi).

Kullanıcı (18.10): "ne oldu? sekmesi neden eksik veri çekiyor ?"

Kararlar:
  • Kalan parça satırı ('rest' öneki) düzenlilik ölçümünü ve satır sayan ölçüleri bozmaz: aynı saniye tek dilim
    (twap.merge_slices), mm etiketi / taker oranı / uzmanlar 'rest' satırını saymaz.
  • Bant kopukken ya da coin pencerenin içinde dinlenmeye başlamışken "BÜTÜN işlemler / $0 / işlem görülmedi"
    denmez (ölçülü dil: veri yoksa bilinmiyor); alarm özeti de söyler.
  • Formun dakikaya yuvarlanmış "son 15 dk"sı yine banttan gelir (tolerans + varsayılan pencerede boş form).
  • Ters yazılmış saatler sıralanır; "gg.aa" ocakta geçen yıl; gelecekteki pencere söylenir.
  • 'PURR/USDC' gibi '@'siz spot çifti spot sayılır; kayıt kapalı sınıfın alarm metni; OI "silindi" gerekçesi.

Pinlenenler: aşağıdaki testler.
"""
import asyncio
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-neoldu-review.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import Config, get_config  # noqa: E402
from app.radar import forensics as fx  # noqa: E402
from app.radar import tape  # noqa: E402

A, B, C = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40


def _trade(coin, tid, usd, buyer, seller, ts, side="B", px=40.0):
    return {"coin": coin, "side": side, "px": str(px), "sz": str(usd / px), "time": ts * 1000,
            "hash": "0x" + "1" * 64, "tid": tid, "users": [buyer, seller]}


class _Live:
    def __init__(self, **kw):
        self.down_log, self.down_since, self.last_msg_at, self.connected = [], 0.0, time.time(), True
        self.valid_coins, self.valid_since = set(), {}
        self.__dict__.update(kw)


def test_rest_rows_keep_twap_and_mm():
    """Düzenli TWAP (her 6 dilimden biri 2 parçaya bölünmüş) yine düzenli; N süpürme N sayılır (mm değil)."""
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "r.db"))
        from app.hl import collector as colm
        from app.radar import scorer, twap
        old = (tape.STARTED, dict(tape.HWM))
        T = dbm.now() - 4000
        tape.STARTED = T - 1000
        tape.HWM.pop("HYPE", None)
        tape.HWM.pop("xyz:TSLA", None)
        try:
            col = colm.Collector(Config(), session=None)
            col.crypto_coins = {"HYPE"}
            for i in range(120):                          # $6.2K her 30 sn; her 6. dilim $5.3K + $0.9K
                ts = T + i * 30
                msg = ([_trade("HYPE", f"{i}a", 5300, A, C, ts), _trade("HYPE", f"{i}b", 900, A, B, ts)]
                       if i % 6 == 0 else [_trade("HYPE", f"{i}a", 6200, A, C, ts)])
                await col._handle(json.dumps({"channel": "trades", "data": msg}))
            async with dbm.db() as c:
                cur = await c.execute("SELECT ts, sz, notional, taker, tid FROM fills WHERE address=? AND side='buy'"
                                      " ORDER BY ts", (A,))
                rows = [dict(r) for r in await cur.fetchall()]
            assert len(rows) == 140 and sum(1 for r in rows if r["tid"].startswith("rest")) == 20, len(rows)
            assert twap.detect(rows) is None, "birleştirmeden 0 sn aralıklar düzeni bozar (kök neden)"
            d = twap.detect(twap.merge_slices(rows))
            assert d and d["n"] == 120 and d["cv_gap"] < 0.01, d
            assert await scorer._twap_fills("HYPE", A, "long") == 120
            # mm: 42 süpürme ($6K + $500 kalan), 41 alış 1 satış → 84 satır ama 42 sayılır
            for i in range(42):
                ts = T + 5000 + i * 60
                side = "B" if i else "A"
                b_, s_ = (B, C) if side == "B" else (C, B)
                await col._handle(json.dumps({"channel": "trades", "data": [
                    _trade("xyz:TSLA", f"m{i}a", 6000, b_, s_, ts, side), _trade("xyz:TSLA", f"m{i}b", 500, b_, A, ts, side)]}))
            cfg = Config()
            ent = await scorer._detect_entity(cfg, None, B, 0)
            assert ent is None, ent
        finally:
            tape.STARTED = old[0]
            tape.HWM.clear(); tape.HWM.update(old[1])
    asyncio.run(run())
    print("✅ rest) bölünmüş dilimli TWAP birleştirince düzenli (120 dilim, _twap_fills 120); 42 süpürme mm sayılmaz")


def test_tape_outage_and_late_subscribe():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "o.db"))
        from app.hl import collector as colm
        from app.telegram import format as fmt
        now = dbm.now()
        old = (tape.STARTED, dict(tape.REG), dict(tape.HWM), colm.LIVE)
        tape.STARTED = now - 5000
        for c in ("PUMP", "NEWC"):
            tape.REG.pop(c, None)
            tape.HWM.pop(c, None)
        try:
            # (a) akış 700 sn'dir kopuk, bantta işlem yok
            colm.LIVE = _Live(down_since=float(now - 700), connected=False, valid_coins={"PUMP", "NEWC"})
            rep = await fx.report("PUMP", now - 900, now)
            assert rep["source"] == "tape" and rep["tape_unknown"] and not rep["tape_full"], rep["source"]
            br = await fx.window_brief(get_config(), None, "PUMP", now - 300, now, probe=False)
            assert br["empty_reason"] == "stream_gap", br["empty_reason"]
            txt = "\n".join(fmt.what_happened(br))
            assert "işlem görülmedi" not in txt and "bilinmiyor" in txt, txt
            # (b) coin pencerenin içinde dinlenmeye başladı
            colm.LIVE = _Live(valid_coins={"PUMP", "NEWC"}, valid_since={"NEWC": float(now - 185)})
            for i in range(3):
                tape.observe("NEWC", "B", A, C, 1.0, 1000, now - 180 + i * 30, tid=f"n{i}")
            rep = await fx.report("NEWC", now - 900, now)
            assert rep["source"] == "tape" and not rep["tape_full"] and rep["sub_since"] == now - 185, rep
            br = await fx.window_brief(get_config(), None, "NEWC", now - 900, now, probe=False)
            assert br["partial"] and "eksik olabilir" in "\n".join(fmt.what_happened(br))
        finally:
            tape.STARTED = old[0]
            tape.REG.clear(); tape.REG.update(old[1])
            tape.HWM.clear(); tape.HWM.update(old[2])
            colm.LIVE = old[3]
    asyncio.run(run())
    print("✅ bant eksik) akış kopukken 'işlem görülmedi' değil 'bilinmiyor' (sayfa + alarm); pencere içinde dinlenmeye"
          " başlayan coin 'tam' sayılmaz, alarm satırı 'eksik olabilir' der")


def test_tape_window_clamp_and_boundary():
    old = (tape.STARTED, dict(tape.REG), dict(tape.HWM))
    now = (int(time.time()) // 60) * 60 + 37            # dakikanın 37. saniyesi
    tape.STARTED = now - 5000
    tape.REG.pop("ZEC", None)
    tape.HWM.pop("ZEC", None)
    try:
        for i, ts in enumerate((now - 880, now - 400, now - 20)):
            tape.observe("ZEC", "B", A, B, 10.0, 100, ts, tid=f"z{i}")
        t0 = ((now - 900) // 60) * 60                   # formun dakikaya yuvarladığı başlangıç (37 sn önde)
        w = tape.window("ZEC", t0, now, now)
        assert w is not None and w["n"] == 3 and w["lo"] > t0, w
        assert tape.window("ZEC", now - 900 - 200, now, now) is None, "tolerans 90 sn"
        # canlı kanadı: t1 kova sınırında, şimdi t1+4 → t1'de başlayan kova sayılmaz
        t1 = ((now - 100) // 30) * 30
        tape.observe("ZEC", "B", A, B, 10.0, 50_000, t1 + 2, tid="late")
        w2 = tape.window("ZEC", t1 - 300, t1, t1 + 4)
        assert w2 and w2["buy"] < 50_000 * 10, w2["buy"]
    finally:
        tape.STARTED = old[0]
        tape.REG.clear(); tape.REG.update(old[1])
        tape.HWM.clear(); tape.HWM.update(old[2])
    print("✅ bant penceresi) dakikaya yuvarlanmış 'son 15 dk' bantta (kapsam yazılır), 90 sn'den fazlası değil;"
          " kova sınırındaki t1'den sonraki işlem sayılmaz")


def test_route_default_form_and_spot():
    async def run():
        import test_neoldu as tn
        from test_routes_smoke import get
        await tn._fresh()
        from app.main import app
        app.state.cfg, app.state.client, app.state.session = get_config(), None, None
        app.state.bot, app.state.notifier = None, None
        now = dbm.now()
        old = (tape.STARTED, dict(tape.REG), dict(tape.HWM))
        tape.STARTED = now - 5000
        tape.REG.pop("PUMP", None)
        tape.HWM.pop("PUMP", None)
        try:
            await dbm.kv_set("ws_universe", {"crypto": ["PUMP"], "spot": ["@107", "PURR/USDC"], "ts": now})
            assets.set_spot_names({"@107": "HYPE/USDC", "PURR/USDC": "PURR/USDC"})
            for i in range(20):
                tape.observe("PUMP", "B", tn._addr(i + 1), C, 0.004, 10_000, now - 600 + i * 20, tid=f"p{i}")
            st, body = await get(app, "/neoldu", b"sym=PUMP")
            assert st == 200 and "Kaynak: canlı bant" in body and 'name="t0" class="sq" size="11" placeholder="18:25" value=""' in body
            st, body = await get(app, "/neoldu", b"sym=PUMP&t0=&t1=")       # boş formu yeniden gönder
            assert "Kaynak: canlı bant" in body, "yeniden gönderilen form da bant"
            st, body = await get(app, "/neoldu", b"sym=PURR%2FUSDC")
            assert st == 200 and "bulunamadı" not in body and "· spot" in body and "dinlemiyoruz" not in body, \
                body[body.find("<h2>"):][:500]
            st, body = await get(app, "/neoldu", b"sym=JEFF%2FUSDC")
            assert "izlediğimiz ilk 2 spot çiftinin arasında yok" in body and "Yazım hatası" not in body
        finally:
            tape.STARTED = old[0]
            tape.REG.clear(); tape.REG.update(old[1])
            tape.HWM.clear(); tape.HWM.update(old[2])
    asyncio.run(run())
    print("✅ rota) varsayılan pencerede form boş, yeniden gönderilince de bant; PURR/USDC spot ve dinleniyor;"
          " önbellek dışı spot çifti 'yazım hatası' denmeden söylenir")


def test_parse_reversed_and_year():
    from datetime import datetime
    from app.web.routes import TR, _parse_window

    class R:
        def __init__(self, **q):
            self.query_params = q
    at = lambda y, mo, d, h, m: int(datetime(y, mo, d, h, m, tzinfo=TR).timestamp())   # noqa: E731
    now = at(2026, 10, 18, 18, 0)
    assert _parse_window(R(t0="10:00", t1="09:00"), now)[:2] == (at(2026, 10, 18, 9, 0), at(2026, 10, 18, 10, 0))
    assert _parse_window(R(t0="18:35", t1="18:25"), at(2026, 10, 18, 22, 30))[:2] == \
        (at(2026, 10, 18, 18, 25), at(2026, 10, 18, 18, 35))
    jan = at(2027, 1, 3, 12, 0)
    assert _parse_window(R(t0="28.12 18:00", t1="28.12 18:30"), jan)[:2] == (at(2026, 12, 28, 18, 0), at(2026, 12, 28, 18, 30))
    t0, t1, note = _parse_window(R(t0="19.10 10:00", t1="19.10 10:15"), now)
    assert "gelecekte" in note and "şimdiye çekildi" not in note, note
    print("✅ zaman) ters saatler sıralanır (10:00→09:00 = 09:00–10:00); ocakta '28.12' geçen yıl; gelecek pencere söylenir")


def test_oi_purged_and_recording_off():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "p.db"))
        from app.telegram import format as fmt
        now = dbm.now()
        async with dbm.db() as c:
            await c.execute("INSERT INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume) VALUES(?,?,?,?,?,?)",
                            ("xyz:TSLA", now - 44 * 86400, 400, 1000, 0, 1))
            await c.commit()
        rep = await fx.report("xyz:TSLA", now - 50 * 86400, now - 50 * 86400 + 600)
        assert "silindi" in rep["oi"]["why"], rep["oi"]
        lines = fmt.what_happened({"verdict": "belirsiz", "why": "", "n_addr": 0, "empty_reason": "recording_off",
                                   "floor": None})
        assert any("kaydı kapalı" in x for x in lines) and not any("- üstü" in x for x in lines), lines
    asyncio.run(run())
    print("✅ OI / kayıt kapalı) 45 günden eski pencerede OI 'silindi'; kayıt kapalı sınıfın alarm satırı doğru")


def test_round2_neoldu():
    """2. tur: 20:00 → 03:00 gece yarısını aşar (ters değil); açılıştan ÖNCE biten pencere 'pencerenin içinde
    yeniden başladı' demez; gelecekteki pencere 'kısa' denmez; bandın başı kesikse 'BÜTÜN' / tam sayılmaz; kayıtta
    pencere içinde açılış → alarm 'kopuktu'; coin sonradan dinlendiyse 'sub_late'; bant bilinmiyorsa künye 0 demez."""
    from datetime import datetime
    from app.web.routes import TR, _parse_window

    class R:
        def __init__(self, **q):
            self.query_params = q
    at = lambda d, h, m: int(datetime(2026, 10, d, h, m, tzinfo=TR).timestamp())   # noqa: E731
    now = at(18, 12, 0)
    assert _parse_window(R(t0="20:00", t1="03:00"), now)[:2] == (at(17, 21, 0), at(18, 3, 0))

    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "r2.db"))
        from app.hl import collector as colm
        from app.telegram import format as fmt
        n = dbm.now()
        old = (tape.STARTED, dict(tape.REG), dict(tape.HWM), colm.LIVE)
        try:
            colm.LIVE = _Live(valid_coins={"PUMP"})
            await dbm.kv_set("ws_universe", {"crypto": ["PUMP"], "spot": [], "ts": n})
            tape.STARTED = n - 120
            rep = await fx.report("PUMP", n - 600, n - 300)
            assert rep["tape_why"] == "before_boot", rep["tape_why"]
            rep = await fx.report("PUMP", n + 600, n + 900)
            assert rep["tape_why"] == "future"
            # kayıt yolu, pencere içinde açılış, satır yok → alarm "kopuktu"
            async with dbm.db() as c:
                await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts,taker) VALUES"
                                "('PUMP','x',?,'buy',1,9000,9000,?,1)", (A, n - 86400))
                await c.commit()
            br = await fx.window_brief(get_config(), None, "PUMP", n - 300, n, probe=False)
            assert br["empty_reason"] == "stream_gap", br["empty_reason"]
            # bandın başı kesik: tam sayılmaz
            tape.STARTED = n - 5000
            tape.REG.pop("PUMP", None)
            tape.HWM.pop("PUMP", None)
            for i in range(30):
                tape.observe("PUMP", "B", A, B, 1.0, 1000, n - 880 + i * 29, tid=f"q{i}")
            rep = await fx.report("PUMP", n - 960, n)
            assert rep["source"] == "tape" and rep["head_cut"] and not rep["tape_full"], (rep["head_cut"], rep["tape_full"])
            # coin sonradan dinlendi, işlem yok → sub_late
            colm.LIVE = _Live(valid_coins={"NEWC"}, valid_since={"NEWC": float(n - 100)})
            await dbm.kv_set("ws_universe", {"crypto": ["NEWC"], "spot": [], "ts": n})
            tape.REG.pop("NEWC", None)
            br = await fx.window_brief(get_config(), None, "NEWC", n - 300, n, probe=False)
            assert br["empty_reason"] == "sub_late" and any("dinlenmeye başladı" in x for x in fmt.what_happened(br))
        finally:
            tape.STARTED = old[0]
            tape.REG.clear(); tape.REG.update(old[1])
            tape.HWM.clear(); tape.HWM.update(old[2])
            colm.LIVE = old[3]
    asyncio.run(run())
    tpl = open(os.path.join(ROOT, "app/web/templates/neoldu.html"), encoding="utf-8").read()
    assert "Bu pencerenin işlemleri <b>bilinmiyor</b>" in tpl and "adresler bilinmiyor" in tpl
    print("✅ 2. tur) 20:00→03:00 gece yarısı; açılış öncesi / gelecek pencere doğru neden; baş kesik tam değil;"
          " açılış içeren pencere 'kopuktu'; sonradan dinlenen coin 'sub_late'; künye 0 demez")


def test_round3():
    """3. tur: bant başı tam bir kova kesilse de 'tam' sayılmaz; kayıt yolunda dinlenmeyen coin 'kopuktu' değil
    'dinlemiyoruz' der; grafik cevabı bayat önbellekten geldiyse istemci boşluğu 'dolmaz' diye kilitlemez."""
    old = (tape.STARTED, dict(tape.REG), dict(tape.HWM))
    now = (int(time.time()) // 30) * 30
    tape.STARTED = now - 5000
    tape.REG.pop("ZEC", None)
    tape.HWM.pop("ZEC", None)
    try:
        for i in range(40):
            tape.observe("ZEC", "B", A, B, 10.0, 100, now - 890 + i * 20, tid=f"r{i}")
        w = tape.window("ZEC", now - 930, now, now)     # politika başı now-900: tam 30 sn kesik
        assert w and w["head_cut"] == 30 and not w["full"], (w and w["head_cut"], w and w["full"])
    finally:
        tape.STARTED = old[0]
        tape.REG.clear(); tape.REG.update(old[1])
        tape.HWM.clear(); tape.HWM.update(old[2])

    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "r3.db"))
        from app.hl import collector as colm
        n = dbm.now()
        o = (tape.STARTED, colm.LIVE)
        try:
            colm.LIVE = _Live(valid_coins={"BTC"}, down_log=[(float(n - 200), float(n - 100))])
            tape.STARTED = n + 10_000                   # bant yolu kapalı: kayıt yolu
            br = await fx.window_brief(get_config(), None, "ZRO", n - 300, n, probe=False)
            assert br["empty_reason"] == "not_listening", br["empty_reason"]
            br = await fx.window_brief(get_config(), None, "BTC", n - 300, n, probe=False)
            assert br["empty_reason"] == "stream_gap", br["empty_reason"]
        finally:
            tape.STARTED, colm.LIVE = o
    asyncio.run(run())
    base = open(os.path.join(ROOT, "app/web/templates/base.html"), encoding="utf-8").read()
    routes = open(os.path.join(ROOT, "app/web/routes.py"), encoding="utf-8").read()
    assert "!PXCHART.refreshing" in base and '"refreshing": refreshing' in routes
    print("✅ 3. tur) 30 sn kesik baş tam değil; kayıt yolunda dinlenmeyen coin 'dinlemiyoruz', kopukluk 'kopuktu';"
          " bayat grafik cevabı boşluğu kilitlemez")


if __name__ == "__main__":
    test_rest_rows_keep_twap_and_mm()
    test_tape_outage_and_late_subscribe()
    test_tape_window_clamp_and_boundary()
    test_route_default_form_and_spot()
    test_parse_reversed_and_year()
    test_oi_purged_and_recording_off()
    test_round2_neoldu()
    test_round3()
