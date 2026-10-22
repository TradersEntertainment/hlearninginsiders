"""⚡ Web paneli hızı — sayfa açılışındaki ağır işlerin sonucu değiştirmeden hızlandırılması.

Kullanıcı (22.10): "websitesi çok ağır açılıyor ya".
Ölçüm (canlı boyutta sentetik DB: fills 10M, asset_metrics 6,5M satır): / 1,6 sn (soğuk 4 sn), /twap 1,7 sn (soğuk
5 sn), /devler 0,85 sn, /funding 0,97 sn. Nedenler: coin başına en yeni metrik `MAX(ts) … GROUP BY coin` ile bütün
tabloyu tarıyordu (≈600 ms, / sayfasında iki kez); /twap her açılışta fills'i iki kez `LIKE '%:%'` ile sayıyordu;
/funding coin başına iki ayrı bağlantı açıyordu (301 bağlantı); sayfalar sıkıştırmasız gidiyordu.
Sonra: / 0,3 sn (soğuk 0,7), /twap 0,04 sn, /devler 0,24 sn, /funding 0,15 sn.

Pinlenenler:
  • LATEST_METRICS_TS eski GROUP BY ile AYNI (coin, en yeni ts) kümesini verir; plan tam tarama değil
  • twap.coverage istekte fills'e dokunmaz (kv); hesaplanmadıysa "hesaplanıyor", hesaplanınca kripto / hisse ayrımı doğru
  • /funding tek bağlantı; gzip açık, canlı akış (text/event-stream) sıkıştırılmaz
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-webperf.db")
from app import db as dbm  # noqa: E402


async def _seed():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "wp.db"))
    t = dbm.now()
    async with dbm.db() as c:
        for i, coin in enumerate(("BTC", "HYPE", "xyz:CRWD", "xyz:NVDA", "para:ANSEM")):
            for k in range(5 + i):
                await c.execute("INSERT INTO asset_metrics(coin, ts, mark_px, oi, funding, day_volume)"
                                " VALUES(?,?,?,?,?,?)", (coin, t - k * 180 - i, 100 + k, 10, 0.0001, 1e6))
        for i, coin in enumerate(("BTC", "BTC", "xyz:CRWD")):
            await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts,taker) VALUES(?,?,?,?,1,1,1,?,1)",
                            (coin, f"t{i}", "0xa", "buy", t - 1000 * (i + 1)))
        await c.commit()
    return t


def test_latest_metrics_same_as_group_by():
    async def run():
        await _seed()
        async with dbm.db() as c:
            old = await (await c.execute("SELECT coin, MAX(ts) mts FROM asset_metrics GROUP BY coin")).fetchall()
            new = await (await c.execute("SELECT coin, mts FROM " + dbm.LATEST_METRICS_TS + " q")).fetchall()
            assert sorted(map(tuple, old)) == sorted(map(tuple, new)) and len(new) == 5, (old, new)
            plan = " ".join(str(tuple(r)) for r in await (await c.execute(
                "EXPLAIN QUERY PLAN SELECT a.coin FROM asset_metrics a JOIN " + dbm.LATEST_METRICS_TS
                + " b ON a.coin=b.coin AND a.ts=b.mts")).fetchall())
            assert "SCAN asset_metrics" not in plan, plan
            empty = await (await c.execute("SELECT coin FROM " + dbm.LATEST_METRICS_TS + " q WHERE 0")).fetchall()
            assert empty == []
        from app.radar import lowvol
        got = await lowvol.latest_metrics_all()
        assert got["BTC"]["mark_px"] == 100 and set(got) == {"BTC", "HYPE", "xyz:CRWD", "xyz:NVDA", "para:ANSEM"}
    asyncio.run(run())
    print("✅ son metrik) skip-scan eski GROUP BY ile aynı küme; tam tarama yok")


def test_twap_coverage_from_kv():
    async def run():
        t = await _seed()
        from app.radar import twap
        cov = await twap.coverage()
        assert cov.get("pending") and cov["crypto"]["n"] is None, cov        # henüz hesaplanmadı: "boş" denmez
        await twap.compute_coverage()
        cov = await twap.coverage()
        assert cov["crypto"]["n"] == 2 and cov["equity"]["n"] == 1 and not cov.get("pending"), cov
        assert cov["crypto"]["a"] == t - 2000 and cov["equity"]["a"] == t - 3000
        from app.radar import sweeper
        await dbm.kv_set(twap.COVERAGE_KV, None)
        await sweeper.refresh_fills_count()
        assert (await twap.coverage())["crypto"]["n"] == 2, "arka plan sayacı kapsamı da yazar"
    asyncio.run(run())
    print("✅ /twap kapsamı) istekte fills taranmaz (kv); hesaplanmadıysa 'hesaplanıyor'")


def test_funding_single_connection_and_gzip():
    async def run():
        await _seed()
        from app.radar import funding
        n = [0]
        orig = dbm.db
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def counted():
            n[0] += 1
            async with orig() as c:
                yield c
        funding.db = counted
        try:
            await funding.ranking()
        finally:
            funding.db = orig
        assert n[0] <= 3, n                       # eskiden coin başına 2 bağlantı (metrics içinde) + üstte
        from app.main import app
        mws = [m.cls.__name__ for m in app.user_middleware]
        assert "GZipMiddleware" in mws, mws
        from starlette.middleware import gzip as g
        assert "text/event-stream" in g.DEFAULT_EXCLUDED_CONTENT_TYPES
    asyncio.run(run())
    print("✅ /funding tek bağlantı; gzip açık, canlı akış sıkıştırılmaz")
