"""📊 /hareket — son 5 dakikanın en büyük fiyat hareketleri.

Kullanıcı isteği: "sitemize son 5dk en büyük hareketler şeyi koyalım, propr'da
listeli olan şeyler olsun, 24 saatlik hacmi 1M$+ olan." (Hacim eşiği 24 SAATLİK,
kovanın kendi hacmi değil; 1dk istenmedi, 5dk yeterli.)

NEDEN `/hacim` YETMİYOR: o sekme *hacmin* kendi 24 saatlik normalini kırmasına
bakıyor ve yalnız rekor kıran kovayı `vol_events`'e yazıyor. Bir coin %8 oynayıp
rekor kırmazsa oraya HİÇ girmiyor — "en çok oynayan kim" oradan cevaplanamaz.

Veri kaynağı yeni istek gerektirmiyor: hacim radarları zaten PROPR ∩ dex
evrenini 5dk mumlarla tarıyor ve 24s hacmi elinde tutuyor; artık her turda son
kapanmış kovayı `movers5m:*` kv'sine de yazıyorlar.

Pinlenenler:
  • `last_bucket` saf: devam eden mum atlanır; `find_record` onu kullanır (tek kaynak)
  • tarama, rekor KIRMAYAN coini de kv'ye yazar (gerileme: vol_events yazmaz)
  • `movers.page` ağa çıkmaz; 24s hacim tabanı süzer; |%| sıralar; piyasa süzgeci
  • rota 200, geçersiz querystring varsayılana düşer, boş durum dürüst
  • künye kova saatini ve ön süzgeç sayısını söyler — sayfa "şu an" demez
"""
import asyncio
import inspect
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-movers.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import cryptovol as cv, movers  # noqa: E402

BUCKET = 300


def candles(n=20, last_o=100.0, last_c=106.0, last_v=1000.0, base_ts=None, vol=50.0):
    """n kapanmış kova + 1 devam eden. Son kapanmış kova açılış→kapanış %+6."""
    t0 = (base_ts if base_ts is not None else 1_700_000_000)
    out = [{"t": t0 + i * BUCKET, "o": 100.0, "h": 101.0, "l": 99.0, "c": 100.0, "v": vol}
           for i in range(n - 1)]
    out.append({"t": t0 + (n - 1) * BUCKET, "o": last_o, "h": max(last_o, last_c),
                "l": min(last_o, last_c), "c": last_c, "v": last_v})
    out.append({"t": t0 + n * BUCKET, "o": last_c, "h": 999.0, "l": 0.1,   # DEVAM EDEN
                "c": 999.0, "v": 9e9})
    return out, t0 + n * BUCKET          # ref_ts: son kova henüz kapanmadı


def test_last_bucket_is_pure_and_shared():
    cs, ref = candles()
    b = cv.last_bucket(cs, ref)
    assert b and b["bucket_ts"] == cs[-2]["t"], "devam eden mum ATLANIR"
    assert abs(b["chg_pct"] - 6.0) < 1e-9 and b["open_px"] == 100.0 and b["px"] == 106.0
    assert abs(b["notional"] - 1000.0 * 106.0) < 1e-6 and b["n_closed"] == 20
    assert cv.last_bucket(cs[:3], ref) is None, "en az MIN_BUCKETS kova gerekir"
    # find_record AYNI kovayı kullanıyor (tek kaynak): rekor kapısı üstte
    rec = cv.find_record(cs, ref)
    assert rec and rec["bucket_ts"] == b["bucket_ts"] and rec["chg_pct"] == b["chg_pct"]
    assert rec["px"] == b["px"] and rec["notional"] == b["notional"]
    # rekor DEĞİLSE find_record None döner ama last_bucket yine satır verir
    cs2, ref2 = candles(last_v=1.0)
    assert cv.find_record(cs2, ref2) is None
    assert cv.last_bucket(cs2, ref2)["chg_pct"] == b["chg_pct"], \
        "rekor kırmayan hareket de ölçülür — /hareket'in varlık sebebi"
    print("✅ saf) last_bucket devam eden mumu atlar; find_record onu kullanır (tek kaynak);"
          " rekor kırmayan hareket yine ölçülür")


def test_scan_writes_movers_snapshot():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "mv.db"))
        cfg = Config()
        cfg.crypto_chat_id = ""
        ts = dbm.now()
        # Son mum ŞU ANKİ kovaya denk gelsin ki gerçekten "devam eden" olsun;
        # aksi hâlde o da kapanır ve son kapanmış kova yanlış seçilir.
        t0 = (ts // BUCKET) * BUCKET - 20 * BUCKET
        # HYPE rekor kırar, QUIET kırmaz (hacmi sabit) — ikisi de kv'ye girmeli
        big, _ = candles(last_v=9999.0, base_ts=t0)
        flat, _ = candles(last_o=100.0, last_c=97.0, last_v=1.0, base_ts=t0)

        class Cli:
            async def candles(self, coin, interval, a, b):
                return [{"t": c["t"] * 1000, "T": (c["t"] + BUCKET) * 1000, "o": c["o"],
                         "h": c["h"], "l": c["l"], "c": c["c"], "v": c["v"], "n": 5}
                        for c in (big if coin == "HYPE" else flat)]

        async def _uni(_cfg, _client):
            return ["HYPE", "QUIET"], {"HYPE": 240e6, "QUIET": 400_000.0}
        cv.universe, orig = _uni, cv.universe
        try:
            out = await cv.scan(cfg, Cli(), None)
        finally:
            cv.universe = orig
        rec = await dbm.kv_get(f"{cv.MOVERS_KV}:crypto") or {}
        got = {r["coin"]: r for r in rec.get("rows") or []}
        assert set(got) == {"HYPE", "QUIET"}, got
        assert abs(got["QUIET"]["chg_pct"] + 3.0) < 1e-6, "rekor kırmayan da yazıldı"
        assert got["QUIET"]["day_vol"] == 400_000.0 and got["HYPE"]["day_vol"] == 240e6
        assert rec["n_coins"] == 2 and rec["ts"] >= ts
        # GERİLEME: vol_events yalnız rekoru tutar — hareket listesi oradan çıkmaz
        async with dbm.db() as c:
            cur = await c.execute("SELECT coin FROM vol_events")
            ev = [r["coin"] for r in await cur.fetchall()]
        assert ev == ["HYPE"] and out["events"] == 1, ev
        print("✅ tarama) rekor kırmayan coin de movers kv'sine yazılır (vol_events yazmaz);"
              " 24s hacim satırda")
    asyncio.run(run())


def test_page_filters_and_order():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "mp.db"))
        cfg, ts = Config(), dbm.now()

        def row(coin, sym, chg, dv, ntl=500_000.0):
            return {"coin": coin, "symbol": sym, "chg_pct": chg, "day_vol": dv,
                    "notional": ntl, "px": 100.0, "open_px": 100.0,
                    "bucket_ts": ts - 300, "vol": 5.0, "n_closed": 288}
        await dbm.kv_set(f"{cv.MOVERS_KV}:crypto", {
            "ts": ts - 60, "n_coins": 104, "n_prefiltered": 31, "n_nodata": 2,
            "rows": [row("HYPE", "HYPE", 6.2, 240e6), row("PUMP", "PUMP", -4.1, 60e6),
                     row("TINY", "TINY", 18.0, 400_000.0)]})
        await dbm.kv_set(f"{cv.MOVERS_KV}:equity", {
            "ts": ts - 200, "n_coins": 83, "n_prefiltered": 44, "n_nodata": 5,
            "rows": [row("xyz:NVDA", "NVDA", -5.4, 12e6)]})

        assert "client" not in inspect.signature(movers.page).parameters, \
            "sayfa HL'ye istek atmamalı — veri hacim radarının turundan gelir"
        p = await movers.page(cfg)
        assert [r["symbol"] for r in p["rows"]] == ["HYPE", "NVDA", "PUMP"], p["rows"]
        assert p["n_below_vol"] == 1, "TINY 24s $400K → $1M tabanının altında"
        assert "TINY" not in [r["symbol"] for r in p["rows"]]
        assert p["n_up"] == 1 and p["n_down"] == 2
        assert p["age"] == max(p["meta"]["kripto"]["age"], p["meta"]["hisse"]["age"]), \
            "en ESKİ tur belirleyici — sayfa en kötü yaşı söyler"
        assert p["meta"]["kripto"]["n_prefiltered"] == 31
        # süzgeçler
        assert {r["kind"] for r in (await movers.page(cfg, market="kripto"))["rows"]} == {"kripto"}
        assert {r["kind"] for r in (await movers.page(cfg, market="hisse"))["rows"]} == {"hisse"}
        assert len((await movers.page(cfg, min_day_vol=0))["rows"]) == 4, "taban yok → TINY girer"
        assert [r["symbol"] for r in (await movers.page(cfg, min_day_vol=0))["rows"]][0] == "TINY"
        assert not (await movers.page(cfg, min_day_vol=1e9))["rows"]
        cfg.movers_min_chg_pct = 5.0
        p2 = await movers.page(cfg)
        assert [r["symbol"] for r in p2["rows"]] == ["HYPE", "NVDA"] and p2["n_below_chg"] == 1
        cfg.movers_min_chg_pct = 0
        assert len((await movers.page(cfg, limit=1))["rows"]) == 1
        assert (await movers.page(cfg, limit=1))["n_all"] == 3, "tavan satırı gizler, sayı künyede"
        # kv hiç yoksa çökmez, dürüstçe boş
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "mp2.db"))
        e = await movers.page(cfg)
        assert e["rows"] == [] and e["age"] is None and e["meta"]["kripto"]["ts"] == 0
        print("✅ sayfa) |%| sıralı, 24s hacim tabanı süzer, piyasa/limit/min%% çalışır;"
              " ağa çıkmaz; kv yoksa dürüstçe boş")
    asyncio.run(run())


def test_route_and_wiring():
    async def run():
        from test_routes_smoke import _fresh, get
        app = await _fresh()
        ts = dbm.now()
        await dbm.kv_set(f"{cv.MOVERS_KV}:crypto", {
            "ts": ts - 60, "n_coins": 104, "n_prefiltered": 31, "n_nodata": 0,
            "rows": [{"coin": "HYPE", "symbol": "HYPE", "chg_pct": 6.2, "day_vol": 240e6,
                      "notional": 1.8e6, "px": 40.0, "open_px": 37.7,
                      "bucket_ts": ts - 300, "vol": 45000.0, "n_closed": 288}]})
        for q, expect in ((b"", "HYPE"), (b"piyasa=hisse", "hareket yok"),
                          (b"taban=25000000.0", "HYPE"),
                          (b"piyasa=zzz&taban=99", "HYPE")):
            st, body = await get(app, "/hareket", q)
            assert st == 200 and expect in body, (q, st, body[-400:])
        st, body = await get(app, "/hareket")
        assert "+6.20%" in body and "/hacim" in body, "yön işaretli, kardeş sekmeye bağ"
        assert "tahmin yok" in body.lower() and "şu an" in body, "tazelik dürüstlüğü"
        assert "31 sembol canlı akış ön süzgeciyle atlandı" in body, "kör nokta künyede"
        assert 'data-refresh="60"' in body, "45 altına inilmez (base.html 45'e çeker)"
        # Yüzde hücresi ASCII '-' kullanmalı: base.html'in parseCell'i U+2212'yi
        # anlamıyor, sütun sıralaması sessizce bozulurdu. base.html'in KENDİ CSS
        # yorumunda U+2212 geçiyor — o yüzden render edilen sayfa değil ŞABLON sınanır.
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "\u2212" not in rd("app", "web", "templates", "hareket.html")
        assert "/hareket" in rd("app", "web", "templates", "base.html"), "nav sekmesi"
        assert "/hareket" in rd("app", "web", "templates", "hacim.html"), "çapraz bağ"
        assert "/hareket" in rd("README.md") and "MOVERS_MIN_DAY_VOL=" in rd(".env.example")
        for f in ("movers_min_day_vol", "movers_max_rows", "movers_min_chg_pct"):
            assert f in EDITABLE_FIELDS and hasattr(Config(), f), f
            assert EDITABLE_FIELDS[f]["group"] == "Hareket", f
            assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
        assert Config().movers_min_day_vol == 1_000_000.0, "kullanıcı kuralı: 24s ≥ $1M"
        print("✅ rota) /hareket 200, süzgeçler querystring'den, geçersiz değer güvenli;"
              " boş durum dürüst; nav + çapraz bağ + README + 3 ayar künyeli")
    asyncio.run(run())
