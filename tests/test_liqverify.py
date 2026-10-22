"""🎯 Liq doğrulama + keşif şeridi ve /kriptoliq tazelik / boyut süzgeci.

Kullanıcı (22.10, /kriptoliq ekranı): "sitedeki kripto liqlere bakıyorum hepsi eski basıyorum öyle bir poz yok veya liqi
çok farklı" · "tüm pozlarda en yakın liq arama yeri çok iyi çalışmalı ve poz boyutu filtreleyebilmeliyim 500k+ 1m$ vs
gibi" · "hem güvenilir olacak hem de poz kaçırmayacak".
Ölçüm (canlı HL): ekrandaki 7 satırın çözülebilen 6'sı bayattı (günler önce kapanmış / likide olmuş / liq %9 kaymış).

Kararlar: yakın-liq satırları 10 dk (≤ %5) / 30 dk (%5–10) / liq'i geçilmişse 1 dk sonra yeniden okunur; son 15 dk'da
büyük işlem yapan adres okunur; sayfa doğrulama penceresinden eski satırı gizler ve sayar; boyut süzgeci pozisyon başına.

Pinlenenler:
  • due_verify: önce liq'i geçilmiş, sonra en yakın; taze / uzak / taban altı / HIP-3 aday değil
  • read: boş yanıt pozisyonu KAPATIR (census.write_main), bozuk yanıt kapatmaz, hata sayılır
  • tick: dakika payı, doğrulama keşiften önce, kota doluysa okumaz
  • due_discover: ≥ taban, ana dex kripto (':' ve '@' yok), işlemden sonra okunduysa yok
  • page: bayat gizlenir + sayılır (bayat=1 gösterir), boyut süzgeci, poz görünümü en yakından, BTC sayfada, kapsama
  • kablolama: _spawn, health, /tani satırı, README, .env.example, rota süzgeçleri
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-liqverify.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.radar import cryptoliq as cl, liqverify as lv  # noqa: E402

A = lambda i: "0x" + f"{i:040x}"  # noqa: E731
MARKS = {"HYPE": 40.0, "BTC": 100_000.0, "SOL": 200.0}


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "lv.db"))
    lv.LAST_READ.clear()
    for k in lv.STATS:
        lv.STATS[k] = 0
    await dbm.kv_set("main_dex_ctx", {"c": {c: {"m": m, "oi": 1_000_000.0} for c, m in MARKS.items()},
                                      "ts": dbm.now()})


async def _pos(coin, addr, side, ntl, liq, age, t=None):
    t = t or dbm.now()
    async with dbm.db() as c:
        await c.execute(
            "INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts)"
            " VALUES(?,?,'',?,1,1,5,?,0,?,?)", (coin, addr, side, liq, ntl, t - age))
        await c.commit()


async def _fill(coin, addr, ntl, age):
    async with dbm.db() as c:
        await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts,taker) VALUES(?,?,?,'buy',1,1,?,?,1)",
                        (coin, f"{coin}{addr}{age}", addr, ntl, dbm.now() - age))
        await c.commit()


class Client:
    def __init__(self, resp=None, free=500):
        self.calls, self.resp, self.free = [], resp, free

    async def clearinghouse(self, user, dex="", priority=None, stats=None, retries=4):
        self.calls.append((user, dex, priority, retries))
        r = self.resp(user) if callable(self.resp) else self.resp
        if isinstance(r, Exception):
            raise r
        return r

    def usage(self):
        return {"free": self.free}


EMPTY = {"assetPositions": [], "marginSummary": {"accountValue": "1000"}}


def test_due_verify_order_and_cadence():
    async def run():
        await _fresh()
        t = dbm.now()
        await _pos("HYPE", A(1), "long", 600_000, 39.0, 11 * 60, t)      # %2.5, 11 dk → aday
        await _pos("HYPE", A(2), "long", 600_000, 39.5, 5 * 60, t)       # %1.25, 5 dk → taze, değil
        await _pos("SOL", A(3), "short", 600_000, 214.0, 20 * 60, t)     # %7, 20 dk → değil (30 dk)
        await _pos("SOL", A(4), "short", 600_000, 214.0, 35 * 60, t)     # %7, 35 dk → aday
        await _pos("HYPE", A(5), "long", 600_000, 41.0, 2 * 60, t)       # liq'i geçilmiş, 2 dk → İLK
        await _pos("HYPE", A(6), "long", 600_000, 30.0, 99 * 3600, t)    # %25 → değil
        await _pos("HYPE", A(7), "long", 100_000, 39.0, 99 * 3600, t)    # taban altı → değil
        await _pos("xyz:AAPL", A(8), "long", 900_000, 190.0, 99 * 3600, t)   # HIP-3 → değil
        await _pos("BTC", A(9), "short", 5_000_000, 101_000.0, 3600, t)  # %1, BTC → aday (sayfa BTC'yi gösterir)
        due = await lv.due_verify(Config(), MARKS, t)
        assert due == [A(5), A(9), A(1), A(4)], due
        lv.LAST_READ[A(1)] = t - 60                                       # az önce okundu (hata da olsa)
        assert A(1) not in await lv.due_verify(Config(), MARKS, t)
        print("✅ sıra) liq'i geçilmiş önce, sonra en yakın; taze / uzak / taban altı / HIP-3 aday değil")
    asyncio.run(run())


def test_read_closes_and_bad_response_keeps():
    async def run():
        await _fresh()
        t = dbm.now()
        await _pos("HYPE", A(1), "long", 600_000, 39.0, 3600, t)
        await _pos("HYPE", A(2), "long", 600_000, 39.0, 3600, t)
        # bozuk yanıt: kapatmaz, hata sayılır, 1 dk sonra yeniden aday
        cfg = Config()
        assert not await lv.read(cfg, Client({"error": "x"}), A(2))
        assert not await lv.read(cfg, Client(RuntimeError("429")), A(2))
        assert lv.STATS["errors"] == 2
        async with dbm.db() as c:
            r = await (await c.execute("SELECT closed_ts FROM addr_positions WHERE address=?", (A(2),))).fetchone()
        assert r["closed_ts"] is None, "bozuk yanıt kayıt kapatmaz"
        # boş yanıt: pozisyon kapandı damgası
        cli = Client(EMPTY)
        assert await lv.read(cfg, cli, A(1))
        assert cli.calls == [(A(1), "", "low", 1)], cli.calls
        async with dbm.db() as c:
            r = await (await c.execute("SELECT closed_ts FROM addr_positions WHERE address=?", (A(1),))).fetchone()
        assert r["closed_ts"], "kapanmış pozisyon damgalanır"
        assert lv.STATS["closed_seen"] == 1 and lv.STATS["reads"] == 1
        # açık pozisyon: liq ve ts güncellenir
        live = {"assetPositions": [{"position": {"coin": "HYPE", "szi": "100", "positionValue": "700000",
                                                 "entryPx": "41", "liquidationPx": "30.5", "unrealizedPnl": "0",
                                                 "leverage": {"value": 5}}}]}
        assert await lv.read(cfg, Client(live), A(2))
        async with dbm.db() as c:
            r = await (await c.execute("SELECT liq_px, ts, closed_ts FROM addr_positions WHERE address=?",
                                       (A(2),))).fetchone()
        assert abs(r["liq_px"] - 30.5) < 1e-9 and r["ts"] >= t and r["closed_ts"] is None, dict(r)
        print("✅ okuma) boş yanıt kapatır, bozuk yanıt kapatmaz (hata sayılır), açık pozisyonun liq'i tazelenir")
    asyncio.run(run())


def test_tick_budget_order_and_busy():
    async def run():
        await _fresh()
        t = dbm.now()
        for i in range(1, 6):
            await _pos("HYPE", A(i), "long", 600_000, 39.0 + i * 0.01, 3600, t)
        await _fill("SOL", A(40), 80_000, 60)
        cfg = Config()
        cfg.liq_verify_rpm = 12                                          # 15 sn'lik turda 3 okuma
        cli = Client(EMPTY)
        done = await lv.tick(cfg, cli, MARKS, t)
        assert len(cli.calls) == 3 and len(done["verify"]) == 3 and not done["discover"], done
        done = await lv.tick(cfg, cli, MARKS, t)
        assert len(done["verify"]) == 2 and done["discover"] == [A(40)], "doğrulama önce, keşif kalan payla"
        # kota dolu: okuma yok
        await _pos("HYPE", A(30), "long", 600_000, 39.2, 3600, t)
        busy = Client(EMPTY, free=5)
        done = await lv.tick(cfg, busy, MARKS, t)
        assert busy.calls == [] and lv.STATS["skipped_busy"] == 1
        print("✅ tur) dakika payı, doğrulama keşiften önce, kota doluysa okumaz")
    asyncio.run(run())


def test_due_discover():
    async def run():
        await _fresh()
        await _fill("SOL", A(1), 80_000, 60)
        await _fill("SOL", A(2), 300_000, 120)
        await _fill("SOL", A(3), 20_000, 60)                 # taban altı
        await _fill("xyz:AAPL", A(4), 900_000, 60)           # HIP-3
        await _fill("@107", A(5), 900_000, 60)               # spot
        await _fill("SOL", A(6), 900_000, 20 * 60)           # pencere dışı
        await _fill("SOL", A(7), 90_000, 300)
        lv.LAST_READ[A(7)] = dbm.now() - 100                 # işlemden sonra okundu
        assert await lv.due_discover(Config(), dbm.now()) == [A(2), A(1)]
        print("✅ keşif) son 15 dk ≥ $50K ana dex kripto, büyükten; HIP-3 / spot / okunmuş yok")
    asyncio.run(run())


def test_page_freshness_size_view():
    async def run():
        await _fresh()
        t = dbm.now()
        await _pos("HYPE", A(1), "long", 600_000, 39.0, 5 * 60, t)        # taze %2.5
        await _pos("HYPE", A(2), "long", 2_500_000, 39.6, 3 * 86400, t)   # BAYAT %1 (ekrandaki vaka)
        await _pos("HYPE", A(3), "long", 1_200_000, 38.5, 60, t)          # taze %3.75
        await _pos("BTC", A(4), "short", 5_000_000, 101_000.0, 60, t)     # BTC sayfada
        await _pos("SOL", A(5), "short", 300_000, 214.0, 40 * 60, t)      # %7, 40 dk → taze (45 dk)
        cfg = Config()
        out = await cl.page(cfg, dist_pct=10.0, view="poz", t=t)
        addrs = [p["address"] for p in out["positions"]]
        assert A(2) not in addrs and out["n_stale"] == 1 and abs(out["stale_usd"] - 2_500_000) < 1, out["n_stale"]
        assert addrs == [A(4), A(1), A(3), A(5)], addrs                  # en yakından
        assert all(p["fresh"] for p in out["positions"])
        # bayat=1 → gösterilir, işaretli
        out = await cl.page(cfg, dist_pct=10.0, view="poz", show_stale=True, t=t)
        st = [p for p in out["positions"] if p["address"] == A(2)]
        assert st and not st[0]["fresh"] and st[0]["age"] >= 3 * 86400
        # boyut: 1M+ → küçükler düşer
        out = await cl.page(cfg, dist_pct=10.0, view="poz", min_pos=1_000_000, t=t)
        assert [p["address"] for p in out["positions"]] == [A(4), A(3)], out["positions"]
        assert out["min_pos"] == 1_000_000 and 200_000 in out["sizes"]
        # band görünümü: yaş + kapsama (OI 1M coin × fiyat)
        out = await cl.page(cfg, dist_pct=10.0, t=t)
        hype = [r for r in out["rows"] if r["coin"] == "HYPE"][0]
        assert hype["age"] is not None and hype["age"] <= 5 * 60 and not hype["stale"], hype
        assert abs(hype["cov"] - 1_800_000 / 40_000_000) < 1e-9, hype["cov"]  # bayat (3 gün) kapsamaya girmez
        print("✅ sayfa) bayat gizlenir ve sayılır, bayat=1 gösterir; boyut süzgeci; poz görünümü en yakından;"
              " BTC var; kapsama OI'ye göre")
    asyncio.run(run())


def test_routes_and_wiring():
    async def run():
        from test_routes_smoke import _fresh as app_fresh, get
        app = await app_fresh()
        await _fresh()
        t = dbm.now()
        await _pos("HYPE", A(1), "long", 600_000, 39.0, 60, t)
        await _pos("HYPE", A(2), "long", 3_000_000, 39.6, 3 * 86400, t)
        st, body = await get(app, "/kriptoliq", b"gorunum=poz")
        assert st == 200 and "en yakın liq" in body and "doğrulanmadı" in body and "Onları da göster" in body, body[-600:]
        assert A(1) in body and A(2) not in body.split("<table")[1]
        st, body = await get(app, "/kriptoliq", b"gorunum=poz&bayat=1")
        assert st == 200 and A(2) in body.split("<table")[1]
        st, body = await get(app, "/kriptoliq", b"boyut=1000000&gorunum=poz")
        assert st == 200 and "Bu süzgeçle liq" in body
        st, body = await get(app, "/kriptoliq", b"boyut=123&gorunum=zzz")
        assert st == 200 and "HYPE" in body
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert '_spawn("liqverify"' in rd("app", "main.py")
        from app import health
        assert "liqverify" in health.limits(Config()) and "liqverify" in health.periods(Config())
        assert "_lv.diag_line" in rd("app", "diag.py")
        line = await lv.diag_line(Config())
        assert line.startswith("🎯 liq doğrulama"), line
        readme, env = rd("README.md"), rd(".env.example")
        for k in ("LIQ_VERIFY_ENABLED", "LIQ_VERIFY_RPM", "LIQ_DISCOVER_MIN_USD"):
            assert k in readme and k in env, k
        assert "Kör nokta, açıkça:" in readme.split("### 🎯 Liq doğrulama")[1][:3000]
        print("✅ kablolama) rota süzgeçleri (boyut/görünüm/bayat), _spawn, sağlık, /tani, README, .env.example")
    asyncio.run(run())
