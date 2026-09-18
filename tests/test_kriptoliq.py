"""💥 /kriptoliq — kripto liq kümeleri ayrı sayfada; /saldiri'ye sınıf süzgeci.

Kullanıcı ekran görüntüsüyle geldi: liq attack radarı baştan aşağı TradFi (SP500,
XYZ100, GOLD, CL, JPY, EUR, AAPL…) ve "TradFi'leri çıkarıp kriptoya bakmak
istiyorum filtreyle" dedi. Kök neden filtre unutulması DEĞİL: radarın tarama
evreni yapısal olarak TradFi'dir —

    scan() → _equity_positions() → positions_current JOIN tickers
           → is_crypto_dex(coin) olanı ELER ("dayanak piyasa kapanmaz, tez yok")
    tickers → yalnız refresh_universe yazar (equity_dexes + crypto_dexes);
              ANA DEX KRİPTOSU (BTC, HYPE, PUMP) oraya HİÇ girmez

yani TradFi süzülse sayfa BOŞ kalırdı. Kullanıcı kararı: radar TradFi'de kalsın,
kripto AYRI SAYFADA gösterilsin — yeni tarama yok, eldeki veri sayfalaştırılsın.

Pinlenenler:
  • `cryptoliq.page` AĞA ÇIKMAZ: yalnız addr_positions + main_dex_ctx kv
  • satır = coin-yön başına EN YAKIN ANLAMLI band (⭐ ile aynı kural)
  • mesafe ve yön süzgeçleri; rota geçersiz değerde varsayılana düşer
  • HIP-3 ve spot listeye girmez; yön sağlaması (long liq altta, short üstte)
  • /saldiri sınıf süzgeci `klass(COIN)` ile (symbol ile değil — 'SP500' öneksiz
    kalır ve yanlışlıkla 'kripto' döner)
  • sayfa başlığındaki kapı SON ADAYINKİ DEĞİL hisse kapısıdır (sızıntı hatası)
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-kriptoliq.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.radar import cryptoliq as cl, liqattack as la  # noqa: E402

A = lambda i: "0x" + f"{i:040x}"  # noqa: E731
MARK_H, MARK_P = 40.0, 0.0040


async def _seed(extra=()):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "kl.db"))
    t = dbm.now()
    rows = [("HYPE", A(i), "long", 300_000.0, 39.2 + i * 0.01) for i in range(6)]
    rows += [("PUMP", A(50 + i), "short", 400_000.0, 0.00412 + i * 0.00001) for i in range(4)]
    rows += [("HYPE", A(98), "short", 900_000.0, 43.5)]        # %8.75 → %5 penceresi dışı
    rows += [("xyz:AAPL", A(97), "long", 900_000.0, 190.0)]    # HIP-3: hiç girmez
    rows += list(extra)
    async with dbm.db() as c:
        for (coin, a, side, ntl, liq) in rows:
            await c.execute(
                "INSERT INTO addr_positions(coin,address,side,szi,entry_px,leverage,liq_px,"
                "upnl,notional,ts) VALUES(?,?,?,?,?,?,?,0,?,?)",
                (coin, a, side, ntl / 40, 40.0, 5, liq, ntl, t))
    await dbm.kv_set("main_dex_ctx", {"c": {"HYPE": {"m": MARK_H, "oi": 5e6},
                                            "PUMP": {"m": MARK_P, "oi": 2e9}}, "ts": t})
    return t


def test_page_is_pure_and_ranked():
    async def run():
        await _seed()
        out = await cl.page(Config(), dist_pct=5.0)
        rows = out["rows"]
        assert [(r["symbol"], r["side"]) for r in rows] == [("HYPE", "long"), ("PUMP", "short")], rows
        assert rows[0]["total"] > rows[1]["total"], "sıralama banddaki $'a göre"
        assert rows[0]["n"] == 6 and abs(rows[0]["total"] - 1_800_000) < 1
        assert abs(rows[0]["px_hi"] - 39.25) < 1e-6 and rows[0]["dist_lo"] < 2
        # HIP-3 ve pencere dışı satır listede YOK
        assert not [r for r in rows if r["symbol"] == "AAPL"], "HIP-3 hisse buraya girmez"
        assert out["n_coins"] == 2 and out["n_pos"] == 10 and out["mark_ts"]
        # ⭐ kuralıyla aynı kaynak: en yakın ANLAMLI band
        assert all(r["sig"] for r in rows)
        print("✅ sayfa) coin-yön başına en yakın anlamlı band, $'a göre sıralı;"
              " HIP-3 ve pencere dışı girmez")
    asyncio.run(run())


def test_no_network_and_filters():
    async def run():
        await _seed()
        cfg = Config()
        # AĞ YOK: client parametresi bile almıyor — imza iddiası
        import inspect
        assert "client" not in inspect.signature(cl.page).parameters, \
            "sayfa HL'ye istek atmamalı (kullanıcı: 'yeni tarama yok')"
        assert [r["side"] for r in (await cl.page(cfg, side="long"))["rows"]] == ["long"]
        assert [r["side"] for r in (await cl.page(cfg, side="short"))["rows"]] == ["short"]
        # mesafe daralınca %1.88'deki HYPE bandı da düşer
        assert not (await cl.page(cfg, dist_pct=1.0))["rows"]
        assert len((await cl.page(cfg, dist_pct=10.0))["rows"]) == 3, "%8.75'teki HYPE short girer"
        assert cl.PAGE_DISTS == (1.0, 2.5, 5.0, 10.0)
        print("✅ süzgeç) mesafe (%1/2.5/5/10) ve yön (long/short); sayfa ağa çıkmaz")
    asyncio.run(run())


def test_direction_sanity_and_empty():
    async def run():
        # Yön tutarsızlığı: long'un liq'i markın ÜSTÜNDE olamaz — sessizce atlanır
        await _seed(extra=[("HYPE", A(96), "long", 5_000_000.0, 41.0)])
        rows = (await cl.page(Config(), dist_pct=5.0))["rows"]
        assert all(r["total"] < 5_000_000 for r in rows), "tutarsız yön bandı şişiremez"
        # fiyat yoksa liste boş ve bu DÜRÜSTÇE boştur (çökmez)
        await dbm.kv_set("main_dex_ctx", {})
        out = await cl.page(Config(), dist_pct=5.0)
        assert out["rows"] == [] and out["n_coins"] == 0 and out["mark_ts"] == 0
        print("✅ sağlama) ters yönlü liq bandı şişirmez; fiyat yoksa liste dürüstçe boş")
    asyncio.run(run())


def test_saldiri_klass_filter_and_gate_leak():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "sl.db"))
        t = dbm.now()
        async with dbm.db() as c:
            for coin, sym in (("xyz:AAPL", "AAPL"), ("xyz:SP500", "SP500"), ("xyz:GOLD", "GOLD")):
                await c.execute("INSERT INTO tickers(coin,dex,symbol,name,max_leverage,listed_at)"
                                " VALUES(?,'xyz',?,?,5,?)", (coin, sym, sym, t))
                await c.execute(
                    "INSERT INTO liq_attack_candidates(coin,direction,ts,weekend_ts,mark,dist_pct,"
                    "liq_usd,cost_usd,score,book_thin,n_pos,target_px,hot,near_usd,zone_dist,"
                    "zone_liq,zone_score) VALUES(?,'down',?,?,100,2.0,5e6,1e6,5.0,0,3,98,1,2e6,2.0,2e6,5.0)",
                    (coin, t, t))
        cfg = Config()
        assert la.PAGE_KLASS == ("hepsi", "hisse", "endeks")
        full = await la.page(cfg)
        assert len(full["cands"]) == 3 and full["n_hidden"] == 0
        hisse = await la.page(cfg, sinif="hisse")
        assert [c["symbol"] for c in hisse["cands"]] == ["AAPL"], hisse["cands"]
        assert hisse["n_all"] == 3 and hisse["n_hidden"] == 2
        endeks = await la.page(cfg, sinif="endeks")
        assert sorted(c["symbol"] for c in endeks["cands"]) == ["GOLD", "SP500"]
        assert all(c["klass"] == "endeks" and c["big"] for c in endeks["cands"])
        # SIZINTI: başlıktaki kapı SON adayınki değil, HİSSE kapısı olmalı.
        # Eskiden `alert_dist` döngüde yeniden bağlanıp dönüşe sızıyordu → son satır
        # endeks olunca sayfa "≤%1 / $50M" yazıyordu.
        for pg in (full, endeks):
            assert pg["alert_dist"] == 2.0 and pg["alert_min"] == 1_000_000.0, pg["alert_dist"]
        assert endeks["big_dist"] == 1.0 and endeks["big_min"] == 50_000_000.0
        print("✅ /saldiri) sınıf süzgeci klass(COIN) ile; başlık kapısı son adaydan sızmıyor")
    asyncio.run(run())


def test_routes_and_wiring():
    async def run():
        from test_routes_smoke import _fresh, get
        app = await _fresh()
        await _seed()
        for q, expect in ((b"", "HYPE"), (b"yon=short", "PUMP"), (b"mesafe=1", "likidasyon yok"),
                          (b"yon=zzz&mesafe=999", "HYPE")):
            st, body = await get(app, "/kriptoliq", q)
            assert st == 200 and expect in body, (q, st, body[-400:])
        st, body = await get(app, "/kriptoliq")
        assert "/saldiri" in body and "oran da yok" in body, "farkını açıkça söyler"
        assert "spot" in body.lower() and "tahmin yok" in body.lower()
        st, _ = await get(app, "/saldiri", b"sinif=hisse")
        assert st == 200
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "/kriptoliq" in rd("app", "web", "templates", "base.html"), "nav sekmesi"
        assert "/kriptoliq" in rd("app", "web", "templates", "saldiri.html"), "çapraz bağ"
        assert "/kriptoliq" in rd("README.md")
        assert "bu hızla" not in rd("app", "web", "templates", "kriptoliq.html"), "tahmin yasağı"
        print("✅ rota) /kriptoliq 200, süzgeçler querystring'den, geçersiz değer güvenli;"
              " /saldiri?sinif= çalışıyor; nav + çapraz bağ + README")
    asyncio.run(run())
