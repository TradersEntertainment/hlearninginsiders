"""🔎 Coin taraması: coin'in kendi trader'ları önce, scans sayımı, adres listesiyle kısmi tarama.

Pinlenenler:
  • candidates: known → coinin trader'ları (en son işlem önce) → watchlist → leaderboard; tavan
  • scan(addrs=[…], stamp=False): yalnız o adresler, coinin dex'iyle, scans damgası yok;
    sahip yazılır (first_seen_ts korunur), tutmayan silinir, taranmayan dokunulmaz,
    kripto dex tabanı ($500 düşer, $1500 kalır)
  • tam tarama scans.n_addrs/n_found yazar; başlangıç damgası sayaçları NULL'lamaz
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-scanner.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import Config, get_config  # noqa: E402
from app.radar import scanner  # noqa: E402

A, B, C, D = ("0x" + ch * 40 for ch in "abcd")
K1, P1, P2, W1, L1 = ("0x" + ch * 40 for ch in "12345")


def pos(coin, ntl, szi=100.0):
    return {"position": {"coin": coin, "szi": str(szi), "positionValue": str(ntl), "entryPx": "0.2",
                         "leverage": {"value": 3}, "liquidationPx": "0.13", "unrealizedPnl": "1"}}


class Client:
    def __init__(self, holdings):
        self.holdings, self.calls = holdings, []

    async def leaderboard(self):
        return None                                   # kv önbelleği kullanılır

    async def clearinghouse(self, addr, dex=""):
        self.calls.append((addr, dex))
        h = self.holdings.get(addr)
        if h == "err":
            raise RuntimeError("HL 500")
        return {"assetPositions": [pos("para:ANSEM", h)] if h else [],
                "marginSummary": {"accountValue": "5000"}}


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "scanner.db"))
    cfg = Config()
    cfg.equity_dexes, cfg.crypto_dexes = ["xyz"], ["para"]
    cfg.min_fill_notional, cfg.min_position_notional = 5000.0, 10000.0
    cfg.crypto_dex_fill_min_notional = cfg.crypto_dex_min_position_notional = 1000.0
    cfg.fills_lookback_days, cfg.fills_retention_days = 30, 14
    cfg.leaderboard_top, cfg.scan_max_candidates = 500, 600
    g = get_config()
    g.equity_dexes, g.crypto_dexes = ["xyz"], ["para"]
    assets.set_crypto_dex_symbols(["ANSEM"])
    async with dbm.db() as c:
        await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('para:ANSEM','para','ANSEM')")
    await dbm.kv_set("leaderboard", {"ts": dbm.now(), "addrs": [L1]})
    return cfg


async def _fill(coin, addr, ntl, ts, tid):
    async with dbm.db() as c:
        await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts,taker) VALUES(?,?,?,?,?,?,?,?,?)",
                        (coin, tid, addr, "buy", 0.2, 1, ntl, ts, 1))


async def _poscur(coin, addr, ntl, ts, first_seen=None):
    async with dbm.db() as c:
        await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px,first_seen_ts)"
                        " VALUES(?,?,?,?,?,?,?,?)", (coin, addr, ts, "long", 1, ntl, 0.13, first_seen))


def test_candidates_order():
    async def run():
        cfg = await _fresh()
        t = dbm.now()
        await _fill("para:ANSEM", P1, 2000, t - 3600, "f1")
        await _fill("para:ANSEM", P2, 1200, t - 60, "f2")
        await _fill("para:ANSEM", W1, 1200, t - 7200, "f3")     # watchlist'te de var → tek kez, trader sırasında
        await _poscur("para:ANSEM", K1, 3000, t)
        async with dbm.db() as c:
            await c.execute("INSERT INTO addresses(address, first_seen, watchlist) VALUES(?,?,1)", (W1, t))
        order = await scanner.candidates(cfg, Client({}), "para:ANSEM")
        assert order == [K1, P2, P1, W1, L1], order
        cfg.scan_max_candidates = 3
        assert await scanner.candidates(cfg, Client({}), "para:ANSEM") == [K1, P2, P1]
        print("✅ adaylar) sahipler → coinin trader'ları (yeni önce) → watchlist → leaderboard; tavan")
    asyncio.run(run())


def test_partial_and_full_scan():
    async def run():
        cfg = await _fresh()
        t = dbm.now()
        await _poscur("para:ANSEM", A, 900.0, t - 9000, first_seen=1000)   # sahip, eski first_seen
        await _poscur("para:ANSEM", B, 2000.0, t - 9000)                    # artık tutmuyor
        await _poscur("para:ANSEM", C, 4000.0, t - 9000)                    # taranmayacak
        cli = Client({A: 1500.0, B: None, D: 500.0})
        found = await scanner.scan(cfg, cli, "para:ANSEM", "para", addrs=[A, B, D, A.upper()], stamp=False)
        assert sorted(cli.calls) == sorted([(A, "para"), (B, "para"), (D, "para")]), cli.calls
        assert [f["address"] for f in found] == [A] and found[0]["first_seen_ts"] == 1000, found
        async with dbm.db() as c:
            cur = await c.execute("SELECT address, notional, first_seen_ts FROM positions_current WHERE coin='para:ANSEM'")
            rows = {r["address"]: (r["notional"], r["first_seen_ts"]) for r in await cur.fetchall()}
            cur = await c.execute("SELECT * FROM scans")
            scans = await cur.fetchall()
        assert rows == {A: (1500.0, 1000), C: (4000.0, None)}, rows
        assert not scans, "kısmi tarama scans damgası atmaz"
        # tam tarama: adaylar (sahipler A, C + leaderboard L1), sayaçlar yazılır
        cli = Client({A: 1500.0, C: 4000.0, L1: "err"})
        found = await scanner.scan(cfg, cli, "para:ANSEM", "para")
        assert {a for a, _ in cli.calls} == {A, C, L1}
        async with dbm.db() as c:
            cur = await c.execute("SELECT ts, n_addrs, n_found FROM scans WHERE coin='para:ANSEM'")
            r = dict(await cur.fetchone())
        assert r["ts"] >= t and r["n_addrs"] == 2 and r["n_found"] == 2, r    # L1 hata → sayılmaz
        # ikinci tam tarama sayaçları korur (INSERT OR REPLACE değil)
        src = open(os.path.join(ROOT, "app", "radar", "scanner.py"), encoding="utf-8").read()
        assert "INSERT OR REPLACE INTO scans" not in src and "ON CONFLICT(coin) DO UPDATE SET ts=excluded.ts" in src
        cli = Client({A: 1500.0, C: 4000.0, L1: None})
        await scanner.scan(cfg, cli, "para:ANSEM", "para")
        async with dbm.db() as c:
            cur = await c.execute("SELECT n_addrs, n_found FROM scans WHERE coin='para:ANSEM'")
            r = dict(await cur.fetchone())
        assert r == {"n_addrs": 3, "n_found": 2}, r
        # şema/migrasyon bağlantısı
        db_src = open(os.path.join(ROOT, "app", "db.py"), encoding="utf-8").read()
        assert "ALTER TABLE scans ADD COLUMN n_addrs INTEGER" in db_src and "n_found INTEGER" in db_src
        assert '"scan_info": scan_info' in open(os.path.join(ROOT, "app", "web", "routes.py"), encoding="utf-8").read()
        print("✅ tarama) kısmi: yalnız verilen adresler, para dex'i, damga yok, sahip/tutmayan/taranmayan; tam: n_addrs/n_found")
    asyncio.run(run())
