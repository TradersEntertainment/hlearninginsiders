"""🧭 Derin keşif: kripto dex (para) yalnız ona dokunan adreslerde sorgulanır; yanıt otoritesi
sorgulanan dex'lerle sınırlı.

Pinlenenler:
  • crypto_touch_set: para fill'i / positions_current / açık addr_positions / açık hl_positions / watchlist
  • sweep_batch: dokunanlar ("", "xyz", "para"), diğerleri ("", "xyz"); crypto_dexes boşsa herkes base
  • yazıcı kapsamı: _upsert_address / _upsert_addr_pos / _upsert_hl dexes=["", "xyz"] ile para satırına
    dokunmaz, xyz satırını siler/kapatır; dexes=None eski davranış; _dex_clause
  • _adaptive_batch(per_addr=2.2) partisi per_addr=3'ten büyük; sweep_stats crypto_addrs/per_addr
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-sweepdex.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import Config, get_config  # noqa: E402
from app.radar import sweeper  # noqa: E402

A, B, C, D, E = ("0x" + ch * 40 for ch in "abcde")
W, X = "0x" + "f" * 40, "0x" + "9" * 40


def pos(coin, ntl):
    return {"position": {"coin": coin, "szi": "100", "positionValue": str(ntl), "entryPx": "0.2",
                         "leverage": {"value": 3}, "liquidationPx": "0.13", "unrealizedPnl": "1"}}


class Client:
    def __init__(self, holdings=None):
        self.calls, self.holdings = [], holdings or {}

    def usage(self):
        return {"rpm": 0, "max": 350, "free": 350, "window": 60}

    async def leaderboard(self):
        return None

    async def clearinghouse_all(self, addr, dexes):
        self.calls.append((addr, tuple(dexes)))
        out = {}
        for d in dexes:
            aps = [pos(c, n) for c, n in self.holdings.get(addr, []) if (c.split(":")[0] if ":" in c else "") == d]
            out[d or "main"] = {"assetPositions": aps, "marginSummary": {"accountValue": "5000"}}
        return out


async def _fresh(crypto=("para",)):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "sweepdex.db"))
    cfg = Config()
    cfg.equity_dexes, cfg.crypto_dexes = ["xyz"], list(crypto)
    cfg.min_position_notional, cfg.crypto_dex_min_position_notional = 10000.0, 1000.0
    cfg.sweep_catchup, cfg.sweep_batch_size, cfg.sweep_interval_sec = False, 40, 90
    cfg.fills_lookback_days, cfg.fills_retention_days = 30, 14
    cfg.sweep_leaderboard_top = 1500
    g = get_config()
    g.equity_dexes, g.crypto_dexes = ["xyz"], list(crypto)
    assets.set_crypto_dex_symbols(["ANSEM", "MEME"])
    t = dbm.now()
    async with dbm.db() as c:
        for coin, dex, sym in (("xyz:SNDK", "xyz", "SNDK"), ("para:ANSEM", "para", "ANSEM"), ("para:MEME", "para", "MEME")):
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
        await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts,taker) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("para:ANSEM", "t1", A, "buy", 0.2, 10000, 2000.0, t - 100, 1))
        await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts,taker) VALUES(?,?,?,?,?,?,?,?,?)",
                        ("xyz:SNDK", "t2", C, "buy", 100, 100, 10000.0, t - 100, 1))
        await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px) VALUES(?,?,?,?,?,?,?)",
                        ("para:ANSEM", B, t - 500, "long", 1, 2000.0, 0.13))
        await c.execute("INSERT INTO hl_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,"
                        "first_seen_ts,peak_notional,peak_ts,closed_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                        ("para:MEME", D, "para", "long", 1, 0.1, 3, 0.05, 0, 2_000_000.0, t - 500, t - 500, 2_000_000.0, t - 500))
        await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", ("para:MEME", E, "para", "long", 1, 0.1, 3, 0.05, 0, 900.0, t - 500))
        await c.execute("INSERT INTO addresses(address, first_seen, watchlist) VALUES(?,?,1)", (W, t))
    await dbm.kv_set("leaderboard", {"ts": t, "addrs": [D, E]})
    return cfg, t


def test_touch_set_and_sweep():
    async def run():
        cfg, t = await _fresh()
        assert await sweeper.crypto_touch_set(cfg) == {A, B, D, E, W}
        cli = Client({A: [("para:ANSEM", 1500.0)], B: [("para:ANSEM", 2500.0)], C: [("xyz:SNDK", 20000.0)]})
        res = await sweeper.sweep_batch(cfg, cli)
        calls = dict(cli.calls)
        assert set(calls) == {A, B, C, D, E, W}, calls
        for a in (A, B, D, E, W):
            assert calls[a] == ("", "xyz", "para"), (a, calls[a])
        assert calls[C] == ("", "xyz"), calls[C]
        assert res["ok"] == 6 and res["err"] == 0
        sw = await dbm.kv_get("sweep_stats")
        assert sw["crypto_addrs"] == 5 and abs(sw["per_addr"] - (2 + 5 / 6)) < 0.01, sw
        async with dbm.db() as c:
            cur = await c.execute("SELECT coin, address, notional FROM positions_current ORDER BY address")
            rows = {(r["coin"], r["address"]): r["notional"] for r in await cur.fetchall()}
        assert rows == {("para:ANSEM", A): 1500.0, ("para:ANSEM", B): 2500.0, ("xyz:SNDK", C): 20000.0}, rows
        # kripto dex kapalı → herkes base
        cfg2, _ = await _fresh(crypto=())
        assert await sweeper.crypto_touch_set(cfg2) == set()
        cli2 = Client()
        await sweeper.sweep_batch(cfg2, cli2)
        assert cli2.calls and all(dx == ("", "xyz") for _a, dx in cli2.calls), cli2.calls
        print("✅ derin keşif) para yalnız dokunanlarda (fill/poz/arşiv/watchlist), diğerleri 2 dex; stats; kapalıysa herkes base")
    asyncio.run(run())


def test_writer_scope_and_batch():
    async def run():
        cfg, t = await _fresh()
        assert sweeper._dex_clause(None) == ("", []) and sweeper._dex_clause([]) == (" AND 0", [])
        assert sweeper._dex_clause(["", "xyz"]) == (" AND (instr(coin, ':') = 0 OR coin LIKE ?)", ["xyz:%"])

        async def seed():
            async with dbm.db() as c:
                await c.execute("DELETE FROM positions_current WHERE address=?", (X,))
                await c.execute("DELETE FROM addr_positions WHERE address=?", (X,))
                await c.execute("DELETE FROM hl_positions WHERE address=?", (X,))
                for coin in ("para:ANSEM", "xyz:SNDK", "HYPE"):
                    await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px) VALUES(?,?,?,?,?,?,?)",
                                    (coin, X, t, "long", 1, 5000.0, 0.13))
                    await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", (coin, X, coin.split(":")[0] if ":" in coin else "", "long", 1, 1, 3, 1, 0, 5000.0, t))
                    await c.execute("INSERT INTO hl_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,"
                                    "first_seen_ts,peak_notional,peak_ts,closed_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                                    (coin, X, "", "long", 1, 1, 3, 1, 0, 5000.0, t, t, 5000.0, t))

        async def state():
            async with dbm.db() as c:
                cur = await c.execute("SELECT coin FROM positions_current WHERE address=?", (X,))
                pc = {r["coin"] for r in await cur.fetchall()}
                cur = await c.execute("SELECT coin FROM addr_positions WHERE address=? AND closed_ts IS NULL", (X,))
                ap = {r["coin"] for r in await cur.fetchall()}
                cur = await c.execute("SELECT coin FROM hl_positions WHERE address=? AND closed_ts IS NULL", (X,))
                hl = {r["coin"] for r in await cur.fetchall()}
            return pc, ap, hl

        # kapsamlı: para sorgulanmadı → para satırı yaşar; ana dex + xyz silinir/kapanır
        await seed()
        await sweeper._upsert_address(X, {}, t + 1, dexes=["", "xyz"])
        await sweeper._upsert_addr_pos(X, {}, t + 1, dexes=["", "xyz"])
        await sweeper._upsert_hl(X, {}, t + 1, held=set(), dexes=["", "xyz"])
        assert await state() == ({"para:ANSEM"}, {"para:ANSEM"}, {"para:ANSEM"}), await state()
        # kısmi pozisyon listesiyle: xyz tutuluyor, HYPE kapanır, para dokunulmaz
        await seed()
        keep = {"xyz:SNDK": {"side": "long", "szi": 1, "entry_px": 1, "leverage": 3, "liq_px": 1, "upnl": 0,
                             "notional": 5000.0, "dex": "xyz"}}
        await sweeper._upsert_address(X, keep, t + 2, dexes=["", "xyz"])
        await sweeper._upsert_addr_pos(X, keep, t + 2, dexes=["", "xyz"])
        await sweeper._upsert_hl(X, keep, t + 2, held={"xyz:SNDK"}, dexes=["", "xyz"])
        assert await state() == ({"para:ANSEM", "xyz:SNDK"},) * 3, await state()
        # dexes=None → eski davranış: hepsi silinir/kapanır
        await seed()
        await sweeper._upsert_address(X, {}, t + 3)
        await sweeper._upsert_addr_pos(X, {}, t + 3)
        await sweeper._upsert_hl(X, {}, t + 3, held=set())
        assert await state() == (set(), set(), set())
        # yalnız para sorgulandı (sayım yolu): xyz ve ana dex satırları yaşar
        await seed()
        await sweeper._upsert_address(X, {}, t + 4, dexes=["para"])
        assert (await state())[0] == {"xyz:SNDK", "HYPE"}
        # parti: kesirli per_addr partiyi büyütür
        cfg.sweep_catchup = True
        cli = Client()
        n3, _ = sweeper._adaptive_batch(cfg, cli, per_addr=3)
        n22, _ = sweeper._adaptive_batch(cfg, cli, per_addr=2.2)
        n0, _ = sweeper._adaptive_batch(cfg, cli)
        assert n22 > n3 and n0 == n3 and n3 == int(350 * 0.85 * 1.5 / 3), (n3, n22, n0)
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "kripto dex sorgusu" in rd("app", "diag.py") and "yalnız ona dokunmuş adreslerde" in rd("README.md")
        print("✅ yazıcı kapsamı) para sorgulanmadan para satırı silinmez/kapanmaz; None eski davranış; kesirli parti; bağlantı")
    asyncio.run(run())
