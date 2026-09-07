"""🧊 Süpürücü soğuk kuyruk — en uzun süredir uğranmayan önce, imleçsiz.

Pinlenenler:
  • build_pools soğuk sırası: hiç uğranmayan (probed_ts NULL) önce, aralarında son işlem yapan
    önce; uğranan (probed_ts) arkaya düşer — yeni trader tur sarmasını beklemez
  • _cold_head: kuyruğun başından n adres; COLD_RETRY_SEC içinde denenmiş atlanır (isteği
    patlayan adres başta takılı kalmasın); süresi geçen damga temizlenir
  • sweep_cursor_cold kv artık yazılmaz/okunmaz
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-sweep-cold.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.radar import sweeper  # noqa: E402

A, B, C, D = ("0x" + ch * 40 for ch in "abcd")


class NoLB:
    async def leaderboard(self):
        return None


def test_cold_order_and_head():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cold.db"))
        cfg = Config()
        cfg.fills_lookback_days = 30
        cfg.sweep_leaderboard_top = 0
        t = dbm.now()
        async with dbm.db() as c:
            for i, (addr, fts) in enumerate(((A, t - 10), (B, t - 2 * 86400), (C, t - 5), (D, t - 3600))):
                await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts) VALUES(?,?,?,?,?,?,?,?)",
                                ("PUMP", f"t{i}", addr, "buy", 0.004, 100, 1000.0, fts))
            await c.execute("INSERT INTO addresses(address, first_seen, probed_ts) VALUES(?,?,?)", (A, t - 900, t - 3600))
            await c.execute("INSERT INTO addresses(address, first_seen, probed_ts) VALUES(?,?,?)", (D, t - 900, t - 7200))
            await c.execute("INSERT INTO addresses(address, first_seen) VALUES(?,?)", (B, t - 900))
        hot, cold = await sweeper.build_pools(cfg, NoLB())
        # C (hiç uğranmadı, en yeni işlem) → B (hiç uğranmadı, eski işlem) → D (2 sa önce uğrandı) → A (1 sa önce)
        assert hot == [] and cold == [C, B, D, A], cold
        # baş: 2 adres; aynı saat içinde yeniden alınmazlar; süresi geçince alınır
        sweeper._cold_tried.clear()
        assert sweeper._cold_head(cold, 2, t) == [C, B]
        assert sweeper._cold_head(cold, 2, t + 10) == [D, A], "denenenler atlanır"
        assert sweeper._cold_head(cold, 2, t + 20) == []
        assert sweeper._cold_head(cold, 3, t + sweeper.COLD_RETRY_SEC) == [C, B], "t'deki damgalar doldu, t+10'dakiler değil"
        assert sweeper._cold_head(cold, 3, t + sweeper.COLD_RETRY_SEC + 10) == [D, A], "damga süresi doldu"
        assert sweeper._cold_head([], 5, t) == [] and sweeper._cold_head(cold, 0, t) == []
        src = open(os.path.join(ROOT, "app", "radar", "sweeper.py"), encoding="utf-8").read()
        assert "sweep_cursor_cold" not in src and "_cold_head(cold, n_cold" in src
        sweeper._cold_tried.clear()            # modül durumu: sonraki testler (sweep_dex) etkilenmesin
        print("✅ soğuk kuyruk) uğranmayan önce (yeni işlem önce), uğranan arkaya; baş imleçsiz; deneme damgası; kv imleci yok")
    asyncio.run(run())
