"""🌾 Hasat sondası — recentTrades ile görülen adresin defterine hemen bak (coinin dex'inde).

Pinlenenler:
  • harvest_trades: kripto dex coinleri (para) kendi imleciyle her tur, genel rotasyon 6/tur
    (imleç ilerler); fill tabanı sınıfa göre ($1K para, $5K hisse)
  • _harvest_probe: adaylar = son 24 saatte fill'i olan, pozisyonu bilinmeyen, probed_ts taze olmayan
    adresler, büyük fill önce; tur tavanı; istekler coinin dex'iyle; pozisyon yazılır; aynı çift
    6 saat yeniden sondalanmaz; sıraya kalanlar sayılır; cap=0 → istek yok
  • harvest_stats her tur; /tani satırı
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-harvest.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config, get_config  # noqa: E402
from app.radar import sweeper  # noqa: E402

A, B, C, D, E, F, G, H, K, P = ("0x" + ch * 40 for ch in "abcdefghkp")
S1, S2, S3, S4 = ("0x" + ch * 40 for ch in "1234")


def trade(tid, users, px, sz, ts):
    return {"px": str(px), "sz": str(sz), "time": ts * 1000, "tid": tid, "users": list(users), "side": "B"}


def pos(coin, ntl):
    return {"position": {"coin": coin, "szi": "100", "positionValue": str(ntl), "entryPx": "0.2",
                         "leverage": {"value": 3}, "liquidationPx": "0.13", "unrealizedPnl": "1"}}


class Client:
    def __init__(self, ts):
        self.ts, self.calls, self.trades_calls = ts, [], []
        self.holdings = {A: ("para:ANSEM", 1500.0), E: ("para:ANSEM", 5000.0), F: ("para:ANSEM", 2000.0),
                         G: ("para:MEME", 1200.0), S3: ("xyz:SNDK", 20000.0)}
        self.trades = {
            "para:ANSEM": [trade("t1", (A, B), 0.2, 7500, ts - 100),      # $1.5K ≥ $1K
                           trade("t2", (C, D), 0.2, 3000, ts - 90),       # $600 < $1K → yok
                           trade("t3", (K, E), 0.2, 15000, ts - 80),      # $3K; K zaten sahip
                           trade("t4", (P, F), 0.2, 10000, ts - 70)],     # $2K; P taze probed_ts
            "para:MEME": [trade("t5", (G, H), 0.1, 12000, ts - 60)],      # $1.2K
            "xyz:SNDK": [trade("t6", (S1, S2), 100, 40, ts - 50),        # $4K < $5K → yok
                         trade("t7", (S3, S4), 100, 60, ts - 40)],       # $6K
        }

    async def recent_trades(self, coin):
        self.trades_calls.append(coin)
        return self.trades.get(coin, [])

    async def clearinghouse(self, addr, dex=""):
        self.calls.append((addr, dex))
        h = self.holdings.get(addr)
        return {"assetPositions": [pos(*h)] if h else [], "marginSummary": {"accountValue": "5000"}}

    async def leaderboard(self):
        return None


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "harvest.db"))
    sweeper._harvest_probed.clear()
    cfg = Config()
    cfg.equity_dexes, cfg.crypto_dexes = ["xyz"], ["para"]
    cfg.min_fill_notional, cfg.min_position_notional = 5000.0, 10000.0
    cfg.crypto_dex_fill_min_notional = cfg.crypto_dex_min_position_notional = 1000.0
    cfg.harvest_probe_max = 2
    cfg.fills_lookback_days, cfg.fills_retention_days = 30, 14
    cfg.telegram_chat_id = "111"
    g = get_config()
    g.equity_dexes, g.crypto_dexes = ["xyz"], ["para"]
    assets.set_crypto_dex_symbols(["ANSEM", "MEME"])
    t = dbm.now()
    async with dbm.db() as c:
        rows = [("para:ANSEM", "para", "ANSEM"), ("para:MEME", "para", "MEME"), ("xyz:SNDK", "xyz", "SNDK")]
        rows += [(f"xyz:T{i}", "xyz", f"T{i}") for i in range(1, 9)]
        for coin, dex, sym in rows:
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
        await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px) VALUES(?,?,?,?,?,?,?)",
                        ("para:ANSEM", K, t, "long", 1, 3000.0, 0.13))
        await c.execute("INSERT INTO addresses(address, first_seen, probed_ts) VALUES(?,?,?)", (P, t, t - 60))
    return cfg, t


def test_harvest_probe():
    async def run():
        cfg, t = await _fresh()
        cli = Client(t)
        added = await sweeper.harvest_trades(cfg, cli)
        # rotasyon: kripto dex ikisi + genel dilim (xyz:SNDK, T1..T5); imleçler
        assert cli.trades_calls[:2] == ["para:ANSEM", "para:MEME"] and cli.trades_calls[2:] == \
            ["xyz:SNDK", "xyz:T1", "xyz:T2", "xyz:T3", "xyz:T4", "xyz:T5"], cli.trades_calls
        assert await dbm.kv_get("harvest_cursor") == 6 and await dbm.kv_get("harvest_cursor_cdex") == 0
        assert added == 10, added                       # 6 ANSEM + 2 MEME + 2 SNDK ($600 ve $4K düştü)
        async with dbm.db() as c:
            cur = await c.execute("SELECT DISTINCT address FROM fills WHERE coin='para:ANSEM'")
            assert {r["address"] for r in await cur.fetchall()} == {A, B, K, E, P, F}
        # sonda (tavan 2): ANSEM adayları büyük fill önce — E($3K), F($2K) sondalanır; A/B($1.5K),
        # MEME (G,H) ve SNDK (S3,S4) sıraya kalır. K zaten sahip, P'nin tam defteri taze (probed_ts).
        assert sorted(cli.calls) == sorted([(E, "para"), (F, "para")]), cli.calls
        hv = await dbm.kv_get("harvest_stats")
        assert hv["probed"] == 2 and hv["found"] == 2 and hv["probe_skipped"] == 6 and hv["probe_err"] == 0, hv
        assert hv["cdex"] == 2 and hv["cycle_added"] == 10 and hv["total"] == 10 and "ANSEM" in hv["coins"]
        async with dbm.db() as c:
            cur = await c.execute("SELECT address, notional FROM positions_current WHERE coin='para:ANSEM'")
            rows = {r["address"]: r["notional"] for r in await cur.fetchall()}
            cur = await c.execute("SELECT COUNT(*) n FROM scans")
            n_scans = (await cur.fetchone())["n"]
        assert rows == {K: 3000.0, E: 5000.0, F: 2000.0}, rows
        assert n_scans == 0, "kısmi tarama scans damgası atmaz"
        # 2. tur (tavan 4): aynı işlemler (fill eklenmez); ANSEM'de A,B, MEME'de G,H sondalanır,
        # SNDK (S3,S4) sıraya kalır; genel imleç 3'e sarar
        cfg.harvest_probe_max = 4
        cli.calls.clear()
        added = await sweeper.harvest_trades(cfg, cli)
        assert added == 0 and await dbm.kv_get("harvest_cursor") == 3
        assert sorted(cli.calls) == sorted([(A, "para"), (B, "para"), (G, "para"), (H, "para")]), cli.calls
        hv = await dbm.kv_get("harvest_stats")
        assert hv["probed"] == 4 and hv["found"] == 2 and hv["probe_skipped"] == 2 and hv["total"] == 10, hv
        # 3. tur: rotasyonda SNDK yok (T3..T8); ANSEM/MEME'de herkes ya sahip ya işaretli → istek yok
        cli.calls.clear()
        await sweeper.harvest_trades(cfg, cli)
        assert cli.calls == [] and (await dbm.kv_get("harvest_stats"))["probed"] == 0
        assert await dbm.kv_get("harvest_cursor") == 0
        # 4. tur: SNDK yeniden sırada → S3,S4 xyz dex'inde; S3 $20K yazılır
        await sweeper.harvest_trades(cfg, cli)
        assert sorted(cli.calls) == sorted([(S3, "xyz"), (S4, "xyz")]), cli.calls
        async with dbm.db() as c:
            cur = await c.execute("SELECT coin, address, notional FROM positions_current WHERE address IN (?,?,?)", (A, G, S3))
            assert {(r["coin"], r["address"], r["notional"]) for r in await cur.fetchall()} == \
                {("para:ANSEM", A, 1500.0), ("para:MEME", G, 1200.0), ("xyz:SNDK", S3, 20000.0)}
        # işaret süresi dolunca yeniden sondalanır (B, H); S4 hâlâ işaretli
        sweeper._harvest_probed[("para:ANSEM", B)] = t - sweeper.HARVEST_PROBE_TTL - 1
        sweeper._harvest_probed[("para:MEME", H)] = t - sweeper.HARVEST_PROBE_TTL - 1
        cli.calls.clear()
        await sweeper.harvest_trades(cfg, cli)
        assert sorted(cli.calls) == sorted([(B, "para"), (H, "para")]), cli.calls
        assert sweeper._harvest_probed[("para:ANSEM", B)] >= t
        # cap=0 → sonda kapalı
        cfg.harvest_probe_max = 0
        sweeper._harvest_probed.clear()
        cli.calls.clear()
        await sweeper.harvest_trades(cfg, cli)
        assert cli.calls == [] and (await dbm.kv_get("harvest_stats"))["probed"] == 0
        # /tani satırı (tavan 4, sondalanacak kimse yok: B/H/S4 yeniden işaretlenir)
        cfg.harvest_probe_max = 4
        cli.calls.clear()
        await sweeper.harvest_trades(cfg, cli)
        from app import diag
        txt = await diag.report(cfg, None)
        line = next((ln for ln in txt.splitlines() if ln.strip().startswith("işlem hasadı:")), "")
        assert "10 fill REST'ten toplandı · bu tur +0 · sonda" in line and "kripto dex: 2 coin/tur" in line and "önce" in line, line
        assert f"sonda {len(cli.calls)} adres" in line
        # bağlantı
        e = EDITABLE_FIELDS["harvest_probe_max"]
        assert e["type"] == "int" and e["group"] == "Tarama & performans" and e["desc"] and hasattr(Config(), "harvest_probe_max")
        if not os.getenv("HARVEST_PROBE_MAX"):
            assert Config().harvest_probe_max == 40
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "HARVEST_PROBE_MAX=40" in rd(".env.example") and "Hasat sondası" in rd("README.md")
        print("✅ hasat sondası) para her tur + rotasyon; sınıf tabanı; büyük fill önce; tavan/sıraya kaldı; dex doğru; işaret TTL; cap=0; /tani")
    asyncio.run(run())
