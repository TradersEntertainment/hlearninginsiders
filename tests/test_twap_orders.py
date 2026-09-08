"""📋 /twaplar sekmesi — görülen TÜM TWAP emirleri, spot dahil, planlanan boyuta göre.

Kullanıcı isteği: "spotlar dahil tüm twap orderları görebileceğimiz büyükten küçüğe
sıralamalı bi sekme". Üç şey gerekti:
  1. SPOT EVRENİ — `spotMetaAndAssetCtxs` ile hacimce ilk N spot çifti (@107 = PURR/USDC)
     WS'e abone edilir. Eskiden hiçbir spot işlemi görülmüyordu: evren yalnız perp'ti,
     `valid_coins` gerisini eliyordu.
  2. KAPIDAN GEÇMEYEN EMİRLER DE KAYDEDİLİR — eskiden `twap_runs` satırı yalnız Telegram'a
     düşen (ya da kanalı olmayan) emirler için yazılıyordu; sekme "emir defteri" olacaksa
     HL'de gerçek emir bulunan her tur yazılmalı. Bildirim alanları boş kalır.
  3. `twap.all_orders` — pencere/durum/piyasa süzgeçleri + planlanan boyuta göre sıralama.

Pinlenenler:
  • sıralama `planned_usd` (yoksa ölçülen `total`) azalan; `planned_known` bunu söyler
  • süzgeçler: pencere saat (0 = hepsi), durum hepsi/suren/bitmis, piyasa hepsi/perp/spot/hisse
  • spot satırı okunur adla ('@107' → 'PURR/USDC') ve `spot` bayrağıyla gelir
  • aynı emrin canlı + arşiv kopyası tekleşir (en çok dilimli kalır)
  • collector: spot coinleri evrene girer, kendi fill tabanı vardır, ana kanala ALARM ÜRETMEZ
  • rota /twaplar 200 döner ve süzgeçler querystring'den okunur; künye/ayarlar bağlı
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twaporders.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import twap  # noqa: E402

A, B, C, D = ("0x" + ch * 40 for ch in "abcd")


async def _seed():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "tw.db"))
    t = dbm.now()
    rows = [  # coin, adres, yön, first, last, n, total, gap, planned, executed, remaining, status, ended, src
        ("@107", A, "buy", t - 7200, t - 60, 180, 900_000, 30, 4_000_000, 900_000, 3_100_000, "activated", None, "live"),
        ("HYPE", B, "sell", t - 3600, t - 30, 110, 2_400_000, 30, 12_000_000, 2_400_000, 9_600_000, "activated", None, "live"),
        ("BTC", C, "buy", t - 3 * 86400, t - 3 * 86400 + 3600, 90, 5_000_000, 40,
         5_000_000, 5_000_000, 0, "finished", t - 3 * 86400 + 3600, "live"),
        ("xyz:SNDK", D, "buy", t - 1800, t - 120, 40, 300_000, 45, None, None, None, None, None, "fills"),
    ]
    async with dbm.db() as c:
        for (coin, a, sd, f, l, n, tot, gp, pu, eu, rem, st, en, src) in rows:
            await c.execute(
                "INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,avg_slice,avg_gap,"
                "cv_gap,cv_size,taker_pct,ts,src,planned_usd,executed_usd,remaining_usd,order_status,ended_ts,"
                "order_ts,order_min,day_volume) VALUES(?,?,?,?,?,?,?,?,?,0.1,0.1,50,?,?,?,?,?,?,?,?,240,?)",
                (coin, a, sd, f, l, n, tot, tot / n, gp, t, src, pu, eu, rem, st, en, f, 50e6))
    await dbm.kv_set("spot_top_coins", {"coins": ["@107"], "names": {"@107": "PURR/USDC"},
                                        "vols": {"@107": 3.2e6}, "want": 40, "ts": t})
    return t


def test_order_size_and_sort():
    async def run():
        await _seed()
        out = await twap.all_orders()
        assert [r["symbol"] for r in out] == ["HYPE", "BTC", "PURR/USDC", "SNDK"], out
        assert [int(r["size_usd"]) for r in out] == [12_000_000, 5_000_000, 4_000_000, 300_000]
        # planlanan bilinmiyorsa ÖLÇÜLEN kullanılır ve bu açıkça işaretlenir (tahmin yok)
        sn = next(r for r in out if r["symbol"] == "SNDK")
        assert sn["planned_known"] is False and sn["size_usd"] == 300_000.0 == sn["total"]
        assert all(r["planned_known"] for r in out if r["symbol"] != "SNDK")
        assert twap.order_size({"planned_usd": 0, "total": 7}) == 7
        assert twap.order_size({"planned_usd": 9, "total": 7}) == 9
        assert twap.order_size({}) == 0
        print("✅ sıralama) planlanan emir boyutuna göre büyükten küçüğe; planlanan yoksa ölçülen + işaret")
    asyncio.run(run())


def test_filters():
    async def run():
        await _seed()
        suren = await twap.all_orders(status="suren")
        assert "BTC" not in [r["symbol"] for r in suren] and suren, suren
        assert all(r["running"] for r in suren)
        bitmis = await twap.all_orders(status="bitmis")
        assert [r["symbol"] for r in bitmis] == ["BTC"] and not bitmis[0]["running"]
        assert [r["symbol"] for r in await twap.all_orders(market="spot")] == ["PURR/USDC"]
        assert [r["symbol"] for r in await twap.all_orders(market="hisse")] == ["SNDK"]
        perp = [r["symbol"] for r in await twap.all_orders(market="perp")]
        assert perp == ["HYPE", "BTC"], perp
        # pencere: 3 gün önceki BTC 24 saatlik pencerede yok, 'hepsi'de var
        assert "BTC" not in [r["symbol"] for r in await twap.all_orders(hours=24)]
        assert "BTC" in [r["symbol"] for r in await twap.all_orders(hours=0)]
        assert [h for h, _ in twap.WINDOWS] == [24, 72, 168, 720, 0]
        print("✅ süzgeç) pencere (0 = hepsi) · durum süren/bitmiş · piyasa perp/spot/hisse")
    asyncio.run(run())


def test_spot_row_and_dedupe():
    async def run():
        t = await _seed()
        sp = next(r for r in await twap.all_orders() if r["spot"])
        assert sp["symbol"] == "PURR/USDC" and sp["coin"] == "@107", sp
        assert sp["filled_pct"] is not None and 22 < sp["filled_pct"] < 23
        assert sp["left_sec"] is not None and sp["running"] is True
        assert not sp["alerted_ts"], "kapıdan geçmemiş emir de listede — bildirilmiş görünmez"
        # aynı emrin arşiv kopyası (farklı first_ts, daha az dilim) tekleşir
        async with dbm.db() as c:
            await c.execute(
                "INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,avg_slice,avg_gap,"
                "cv_gap,cv_size,taker_pct,ts,src) VALUES('@107',?,'buy',?,?,20,120000,6000,30,0.1,0.1,50,?,'fills')",
                (A, t - 7000, t - 60, t))
        rows = await twap.all_orders()
        spots = [r for r in rows if r["spot"]]
        assert len(spots) == 1 and spots[0]["n_slices"] == 180, spots
        print("✅ spot) '@107' → 'PURR/USDC', dolum/kalan/süre hesaplı; canlı+arşiv kopyası tekleşir")
    asyncio.run(run())


def test_collector_spot_universe():
    async def run():
        from app.hl.collector import Collector
        from app.hl.universe import SPOT_KV, top_spot_coins
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "sp.db"))

        class Cli:
            calls = 0

            async def spot_meta_and_ctxs(self):
                Cli.calls += 1
                return [{"tokens": [{"name": "PURR"}, {"name": "USDC"}, {"name": "HYPE"}],
                         "universe": [{"name": "@107", "tokens": [0, 1]},
                                      {"name": "@151", "tokens": [2, 1]},
                                      {"name": "@9", "tokens": [0, 1], "isDelisted": True}]},
                        [{"dayNtlVlm": "3200000"}, {"dayNtlVlm": "9900000"}, {"dayNtlVlm": "1"}]]
        coins, names, vols = await top_spot_coins(Cli(), 40)
        assert coins == ["@151", "@107"], "hacimce sıralı, delist edilen yok"
        assert names["@151"] == "HYPE/USDC" and names["@107"] == "PURR/USDC"
        assert vols["@151"] == 9_900_000.0
        assert (await dbm.kv_get(SPOT_KV))["names"]["@107"] == "PURR/USDC"
        await top_spot_coins(Cli(), 40)
        assert Cli.calls == 1, "kv taze: ikinci çağrı istek atmaz"

        cfg = Config()
        cfg.spot_watch_top = 40
        col = Collector(cfg, None, client=Cli())
        coins2 = await col._current_coins()
        assert "@151" in coins2 and "@107" in coins2 and col.spot_coins == {"@151", "@107"}
        assert (await dbm.kv_get("ws_universe"))["spot"] == ["@151", "@107"]
        # spot'un kendi kayıt tabanı var; kapatılabilir
        assert col.fill_floor("@107") == cfg.spot_fill_min_notional == 5000.0
        cfg.spot_fill_min_notional = 0
        assert col.fill_floor("@107") == float("inf"), "0 = spot kaydı kapalı"
        cfg.spot_enabled = False
        assert await col._spot_coins() == ([], {})
        # TWAP kapısı spot hacmini görebilsin (yoksa her spot emri 'no_vol'a düşerdi)
        from app.radar import twaplive
        assert (await twaplive.volumes())["@151"][0] == 9_900_000.0
        print("✅ evren) spotMeta → hacimce ilk N çift, kv önbellek, WS evreni, ayrı fill tabanı, TWAP hacmi")
    asyncio.run(run())


def test_route_and_wiring():
    async def run():
        from test_routes_smoke import _fresh, get
        app = await _fresh()
        await _seed()
        for q, expect in ((b"", "PURR/USDC"), (b"durum=bitmis", "BTC"),
                          (b"piyasa=spot", "PURR/USDC"), (b"pencere=24", "HYPE"),
                          (b"durum=zzz&piyasa=zzz&pencere=999", "TWAP emirleri")):
            st, body = await get(app, "/twaplar", q)
            assert st == 200 and expect in body, (q, st, body[-400:])
        st, body = await get(app, "/twaplar", b"piyasa=spot&durum=bitmis")
        assert st == 200 and "TWAP emri yok" in body, "boş durum dürüstçe söylenir"
        st, body = await get(app, "/twaplar")
        assert "büyükten küçüğe" in body and "tahmin yok" in body, "sayfa ne yaptığını söyler"
        assert "spot çifti" in body or "spot kapalı" in body, "dinlenen piyasalar künyesi"
        # künye: yeni ayarlar + nav girişi + .env
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        for f in ("spot_enabled", "spot_watch_top", "spot_fill_min_notional"):
            assert f in EDITABLE_FIELDS and hasattr(Config(), f), f
            assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
            assert EDITABLE_FIELDS[f]["group"] == "Spot", f
        assert "SPOT_WATCH_TOP=" in rd(".env.example")
        assert "/twaplar" in rd("app", "web", "templates", "base.html"), "nav sekmesi"
        assert "/twaplar" in rd("README.md")
        # tahmin yasağı bu sayfada da geçerli
        assert "bu hızla" not in rd("app", "web", "templates", "twaplar.html")
        print("✅ rota) /twaplar 200, süzgeçler querystring'den, geçersiz değer güvenli;"
              " boş durum dürüst; ayar künyesi + nav + README")
    asyncio.run(run())
