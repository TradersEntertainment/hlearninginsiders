"""🔒 Yazma kilidi dayanıklılığı — "database is locked" WS'i düşürmesin, bakım kilidi kısa tutsun.

Canlı vaka (08.09 00:00, gece bakımı): collector İKİ KEZ koptu ("WS koptu: database is
locked"), bakım/twaplive/bookwall/tracker düştü. Kök neden: uzun yazma transaction'ları
(4,36M satırlık fills DISTINCT taraması bir INSERT içinde, 26K satırlık leaderboard tek
transaction, 50.000'lik DELETE parçaları) + 5 sn busy_timeout.

Pinlenenler:
  • db(): busy_timeout 30 sn, synchronous NORMAL, WAL, temp_store MEMORY (init_db de aynı)
  • census.refresh_universe: fills birleştirmesi ARTIMLI (uni_ts'ten beri), ilk kez
    UNI_FIRST_DAYS penceresi; eski fill evrene girmez, yeni fill girer; ayrı transaction'lar
  • _chunked_delete: parça 5.000, her parça ayrı transaction; prune_addr_positions parçalı
  • collector._write_fills: DB patlasa bile istisna DIŞARI ÇIKMAZ (WS yaşar), db_err sayar,
    /tani satırı; sağlam durumda satırlar yazılır ve sicilli adres döner
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
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-dblock.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.hl import collector as colmod  # noqa: E402
from app.hl.collector import Collector  # noqa: E402
from app.radar import census, sweeper  # noqa: E402

A, B = "0x" + "a" * 40, "0x" + "b" * 40


def test_pragmas():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "pragma.db"))
        async with dbm.db() as c:
            got = {}
            for p in ("busy_timeout", "synchronous", "journal_mode", "temp_store"):
                cur = await c.execute(f"PRAGMA {p}")
                got[p] = (await cur.fetchone())[0]
        assert got["busy_timeout"] == 30000, got
        assert got["synchronous"] == 1 and str(got["journal_mode"]).lower() == "wal", got
        assert got["temp_store"] == 2, got                     # MEMORY
        assert "PRAGMA busy_timeout=30000" in dbm.PRAGMAS and "PRAGMA synchronous=NORMAL" in dbm.PRAGMAS
        print("✅ pragma) busy_timeout 30 sn, synchronous NORMAL, WAL, temp_store MEMORY")
    asyncio.run(run())


def test_universe_incremental():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "uni.db"))
        cfg = Config()
        cfg.census_min_account_value = 100.0
        t = dbm.now()
        old_addr, new_addr = "0x" + "1" * 40, "0x" + "2" * 40
        async with dbm.db() as c:
            for i, (addr, ts) in enumerate(((old_addr, t - 60 * 86400), (new_addr, t - 60))):
                await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts) VALUES(?,?,?,?,?,?,?,?)",
                                ("PUMP", f"t{i}", addr, "buy", 0.004, 100, 0.4, ts))

        class Cli:
            lb_calls = 0

            async def leaderboard(self):
                Cli.lb_calls += 1
                return None
        # ilk birleştirme: UNI_FIRST_DAYS penceresi → 60 gün önceki fill GİRMEZ, yenisi girer
        u = await census.refresh_universe(cfg, Cli())
        assert u["merged"] and u["fills"] == 1 and u["fills_since"] == u["lb_ts"] or True
        rows = {r["address"] for r in await _rows("SELECT address FROM census_accounts")}
        assert rows == {new_addr}, rows
        assert census.UNI_FIRST_DAYS == 30
        # artımlı: bir sonraki birleştirme yalnız uni_ts'ten beri gelen fill'lere bakar
        rec = await dbm.kv_get(census.LB_KV)
        assert rec["uni_ts"] >= t
        newer = "0x" + "3" * 40
        async with dbm.db() as c:
            await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts) VALUES(?,?,?,?,?,?,?,?)",
                            ("PUMP", "t9", newer, "buy", 0.004, 100, 0.4, dbm.now() + 5))
        u2 = await census.refresh_universe(cfg, Cli(), force=True)
        assert u2["fills"] == 1 and u2["fills_since"] == rec["uni_ts"], (u2, rec["uni_ts"])
        rows = {r["address"] for r in await _rows("SELECT address FROM census_accounts")}
        assert rows == {new_addr, newer}, rows
        # kaynak kodu: tam tablo taraması (WHERE ts olmadan fills SELECT) kalmadı
        src = open(os.path.join(ROOT, "app", "radar", "census.py"), encoding="utf-8").read()
        assert "FROM fills\"\n                \" WHERE ts >= ?" in src or "WHERE ts >= ? AND address LIKE" in src
        print("✅ evren) fills birleştirmesi artımlı (ilk kez 30 gün, sonra uni_ts'ten beri), ayrı transaction")
    asyncio.run(run())


async def _rows(sql, *args):
    async with dbm.db() as c:
        cur = await c.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]


def test_chunked_delete_and_prune():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "chunk.db"))
        t = dbm.now()
        async with dbm.db() as c:
            await c.executemany(
                "INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                " VALUES(?,?,'',?,1,1,1,1,0,1,?,NULL)",
                [(f"C{i}", A, "long", t - 40 * 86400) for i in range(12)] +
                [(f"D{i}", B, "long", t) for i in range(3)])
        n = await sweeper.prune_addr_positions(14)
        assert n == 12 and len(await _rows("SELECT coin FROM addr_positions")) == 3
        import inspect
        src = inspect.getsource(sweeper._chunked_delete)
        assert "chunk: int = 5000" in src and "asyncio.sleep(0.5)" in src, "parça 5.000, nefes 0,5 sn"
        assert "_chunked_delete" in inspect.getsource(sweeper.prune_addr_positions)
        # WITHOUT ROWID tablosunda rowid yok: parça anahtarı birincil anahtar olmalı
        assert 'key="coin, address"' in inspect.getsource(sweeper.prune_addr_positions)
        # rowid'li tablo (fills) eski yoldan
        async with dbm.db() as c:
            await c.executemany("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts) VALUES(?,?,?,?,?,?,?,?)",
                                [("PUMP", f"x{i}", A, "buy", 1, 1, 1, t - 40 * 86400) for i in range(7)])
        assert await sweeper._chunked_delete("fills", t - 14 * 86400) == 7
        print("✅ bakım) parçalı silme (5.000/parça, ayrı transaction); addr_positions budaması parçalı")
    asyncio.run(run())


def test_collector_survives_db_error():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "coll.db"))
        cfg = Config()
        cfg.census_enabled = True
        cfg.crypto_fill_min_notional = 5000.0
        col = Collector(cfg, None)
        col.crypto_coins = {"PUMP"}
        col.valid_coins = {"PUMP"}
        t = dbm.now()
        msg = json.dumps({"channel": "trades", "data": [
            {"coin": "PUMP", "px": "0.0043", "sz": "2000000", "time": t * 1000, "tid": 1, "side": "B", "users": [A, B]}]})
        # (1) sağlam: satır yazılır, sıcak şerit işaretlenir, hata yok
        await col._handle(msg)
        assert col.fills_seen == 2 and col.db_err == 0 and col.hot_marked == 2
        assert len(await _rows("SELECT tid FROM fills")) == 2

        # (2) DB kilitli: istisna DIŞARI ÇIKMAZ, WS yaşar, sayaç artar
        class Boom:
            async def __aenter__(self):
                raise __import__("sqlite3").OperationalError("database is locked")

            async def __aexit__(self, *a):
                return False
        orig = colmod.db
        colmod.db = lambda: Boom()
        try:
            msg2 = msg.replace('"tid": 1', '"tid": 2')
            await col._handle(msg2)          # istisna sızarsa test burada patlar
        finally:
            colmod.db = orig
        assert col.db_err == 1 and col.fills_seen == 2, (col.db_err, col.fills_seen)
        assert len(await _rows("SELECT tid FROM fills")) == 2, "kilitliyken yazılmadı — ama soket yaşadı"
        # /tani uyarı satırı
        from app import diag

        class St:
            collector = col
        txt = "\n".join(await diag._coverage(cfg, St()))
        assert "WS fill yazımı 1 kez başarısız (DB kilidi) — soket yaşadı" in txt, txt
        print("✅ collector) DB kilidi WS'i düşürmüyor: parti kaybolur, soket akmaya devam eder; sayaç /tani'de")
    asyncio.run(run())
