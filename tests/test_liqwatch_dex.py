"""🎯 Liq radarı dex budaması + arşiv görevleri düşük şerit.

Pinlenenler:
  • _dex_map: ana dex herkeste; HIP-3 dex'i yalnız positions_current / liq_watch'ta o dex'te
    satırı olan hesapta; liq_watch_all_dexes → herkeste tüm dex'ler; izlenmeyen dex'ler girmez
  • run_cycle: adres başına budanmış dex listesiyle clearinghouse_all; liqwatch_stats (istek sayısı)
    + /tani satırı
  • bars.loop / hourstats.refresh_loop düşük şeritte (PRIORITY.set("low")); config künyesi
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-liqwatch-dex.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import liqwatch  # noqa: E402

A1, A2, A3, A4 = ("0x" + ch * 40 for ch in "1234")


class Client:
    def __init__(self):
        self.calls = []

    async def leaderboard(self):
        return None

    async def meta_and_ctxs(self, dex=""):
        return [{"universe": [{"name": "xyz:SNDK" if dex == "xyz" else ("para:ANSEM" if dex == "para" else "HYPE")}]},
                [{"markPx": "10"}]]

    async def clearinghouse_all(self, addr, dexes):
        self.calls.append((addr, tuple(dexes)))
        return {(d or "main"): {"assetPositions": []} for d in dexes}


def test_dex_map_and_cycle():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "lw.db"))
        cfg = Config()
        cfg.equity_dexes, cfg.crypto_dexes = ["xyz"], ["para"]
        cfg.liq_watch_top_accounts, cfg.liq_watch_min_notional = 300, 1000.0
        t = dbm.now()
        async with dbm.db() as c:
            await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px) VALUES(?,?,?,?,?,?,?)",
                            ("xyz:SNDK", A1, t, "long", 1, 5000.0, 9.0))
            await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px) VALUES(?,?,?,?,?,?,?)",
                            ("para:ANSEM", A2, t, "long", 1, 5000.0, 9.0))
            await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px) VALUES(?,?,?,?,?,?,?)",
                            ("abc:FOO", A2, t, "long", 1, 5000.0, 9.0))          # izlenmeyen dex → girmez
            await c.execute("INSERT INTO liq_watch(address,coin,side,notional,liq_px,stage,last_dist,updated_ts)"
                            " VALUES(?,?,?,?,?,?,?,?)", (A3, "para:MEME", "long", 5000.0, 9.0, 0, 5.0, t))
            await c.execute("INSERT INTO addresses(address, first_seen, watchlist) VALUES(?,?,1)", (A4, t))
        m = await liqwatch._dex_map(cfg, [A1, A2, A3, A4])
        assert m == {A1: ["", "xyz"], A2: ["", "para"], A3: ["", "para"], A4: [""]}, m
        cfg.liq_watch_all_dexes = True
        assert liqwatch._dex_map is not None and (await liqwatch._dex_map(cfg, [A1, A4])) == {A1: ["", "xyz", "para"], A4: ["", "xyz", "para"]}
        cfg.liq_watch_all_dexes = False
        assert await liqwatch._dex_map(cfg, []) == {}
        # tur: adaylar (izlenen A3, watchlist A4, ≥ taban A1/A2) budanmış dex'lerle sorgulanır
        cli = Client()
        await liqwatch.run_cycle(cfg, cli, None)
        calls = dict(cli.calls)
        assert calls == {A1: ("", "xyz"), A2: ("", "para"), A3: ("", "para"), A4: ("",)}, calls
        lw = await dbm.kv_get("liqwatch_stats")
        assert lw["addrs"] == 4 and lw["requests"] == 7 and lw["ok"] == 4 and lw["pruned"] is True, lw
        from app import diag
        txt = await diag.report(cfg, None)
        assert "liq radarı: 4 hesap · 7 dex sorgusu (budama açık) · 4 yanıt" in txt, txt
        # bağlantı: arşiv görevleri düşük şerit; ayar künyesi
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert 'PRIORITY.set("low")' in rd("app", "radar", "bars.py") and 'PRIORITY.set("low")' in rd("app", "radar", "hourstats.py")
        e = EDITABLE_FIELDS["liq_watch_all_dexes"]
        assert e["group"] == "Likidasyon radarı" and all(e.get(x) for x in ("type", "label", "desc")) and Config().liq_watch_all_dexes is False
        print("✅ liq radarı) dex budaması: ana dex herkeste, HIP-3 yalnız pozisyonu/izi olanda; stats + /tani; arşiv düşük şerit")
    asyncio.run(run())
