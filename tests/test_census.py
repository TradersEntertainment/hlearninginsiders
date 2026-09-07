"""🗳 Kripto dex sayımı (census) — varsayılan kapalı; açıkken leaderboard'daki her hesabın defteri
kripto dex'te, günde bir.

Pinlenenler:
  • rows_above: bakiye tabanı, haftalık hacim 0 elenir, hacim bilgisi yoksa kalır, tekrarsız, bakiye azalan
  • due: kapalı → asla; açık → hiç koşmamış / gün değişmiş / yarım kalmış
  • run: istek sayısı = uygun adres × dex, gecikme 60/rpm, para satırı yazılır, aynı adresin xyz satırı
    yaşar, tutmayanın bayat para satırı silinir, bozuk yanıt hata sayılır ve silmez; state/stats;
    kripto dex yok / leaderboard yok → atlandı
  • /tani satırları; bağlantı (_spawn, health, EDITABLE, env, README)
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-census.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config, get_config  # noqa: E402
from app.radar import census  # noqa: E402

L1, L2, L3, L4, L5 = ("0x" + ch * 40 for ch in "12345")


def lb_row(addr, av, week_vlm=None):
    r = {"ethAddress": addr.upper() if addr == L2 else addr, "accountValue": str(av)}
    if week_vlm is not None:
        r["windowPerformances"] = [["day", {"vlm": "1"}], ["week", {"vlm": str(week_vlm)}]]
    return r


def pos(coin, ntl):
    return {"position": {"coin": coin, "szi": "100", "positionValue": str(ntl), "entryPx": "0.2",
                         "leverage": {"value": 3}, "liquidationPx": "0.13", "unrealizedPnl": "1"}}


class Client:
    def __init__(self, data):
        self.data, self.calls = data, []
        self.holdings = {L1: [("para:ANSEM", 1500.0)], L5: []}

    async def leaderboard(self):
        return self.data

    async def clearinghouse(self, addr, dex=""):
        self.calls.append((addr, dex))
        if addr == L2:
            return None                        # bozuk yanıt
        return {"assetPositions": [pos(c, n) for c, n in self.holdings.get(addr, [])],
                "marginSummary": {"accountValue": "1"}}


DATA = {"leaderboardRows": [lb_row(L1, 5000, 10), lb_row(L2, 2000), lb_row(L3, 500, 10), lb_row(L4, 3000, 0),
                            lb_row(L5, 1500, 5), lb_row(L1, 5000, 10), {"accountValue": "x"}, "çöp"]}


async def _fresh(enabled=True, crypto=("para",)):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "census.db"))
    cfg = Config()
    cfg.equity_dexes, cfg.crypto_dexes = ["xyz"], list(crypto)
    cfg.crypto_dex_census_enabled, cfg.census_min_account_value, cfg.census_rpm = enabled, 1000.0, 120
    cfg.min_position_notional, cfg.crypto_dex_min_position_notional = 10000.0, 1000.0
    cfg.telegram_chat_id = "111"
    g = get_config()
    g.equity_dexes, g.crypto_dexes = ["xyz"], list(crypto)
    assets.set_crypto_dex_symbols(["ANSEM", "MEME"])
    t = dbm.now()
    async with dbm.db() as c:
        for coin, dex, sym in (("xyz:SNDK", "xyz", "SNDK"), ("para:ANSEM", "para", "ANSEM"), ("para:MEME", "para", "MEME")):
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
        for coin, addr in (("xyz:SNDK", L1), ("para:MEME", L5), ("para:MEME", L2)):
            await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px) VALUES(?,?,?,?,?,?,?)",
                            (coin, addr, t - 500, "long", 1, 5000.0, 0.1))
    return cfg, t


def test_rows_and_due():
    assert census.rows_above(DATA, 1000) == [L1, L2, L5], census.rows_above(DATA, 1000)
    assert census.rows_above(DATA, 3000) == [L1] and census.rows_above(None, 0) == [] and census.rows_above({"x": 1}, 0) == []
    cfg = Config()
    cfg.crypto_dex_census_enabled = False
    assert census.due(cfg, None, "2026-09-07") is False
    cfg.crypto_dex_census_enabled = True
    assert census.due(cfg, None, "2026-09-07") is True and census.due(cfg, {}, "2026-09-07") is True
    assert census.due(cfg, {"day": "2026-09-07", "finished": True}, "2026-09-07") is False
    assert census.due(cfg, {"day": "2026-09-06", "finished": True}, "2026-09-07") is True
    assert census.due(cfg, {"day": "2026-09-07", "finished": False, "dex": "para"}, "2026-09-07") is True
    print("✅ sayım adayları/zamanı) taban, hacim 0 elenir, tekrarsız, azalan; kapalı → asla; gün/yarım kalmış")


def test_run_and_diag():
    async def run():
        cfg, t = await _fresh()
        cli = Client(DATA)
        delays = []

        async def sleep(d):
            delays.append(d)
        st = await census.run(cfg, cli, sleep=sleep)
        assert cli.calls == [(L1, "para"), (L2, "para"), (L5, "para")], cli.calls
        assert delays == [0.5, 0.5, 0.5]
        assert st["dexes"] == {"para": {"n": 3, "ok": 2, "found": 1, "err": 1, "min": st["dexes"]["para"]["min"]}}, st
        async with dbm.db() as c:
            cur = await c.execute("SELECT coin, address, notional FROM positions_current ORDER BY coin, address")
            rows = {(r["coin"], r["address"]): r["notional"] for r in await cur.fetchall()}
        assert rows == {("para:ANSEM", L1): 1500.0, ("xyz:SNDK", L1): 5000.0, ("para:MEME", L2): 5000.0}, rows
        # L1'in xyz satırı yaşadı (yalnız para otoritesi); L5'in bayat para satırı silindi; L2 bozuk yanıt → dokunulmadı
        state = await dbm.kv_get(census.STATE_KV)
        assert state["finished"] is True and state["day"] == census.today_key() and state["finished_dexes"] == ["para"]
        assert census.due(cfg, state, census.today_key()) is False
        # /tani: bitti özeti
        from app import diag
        txt = await diag.report(cfg, None)
        line = next((ln for ln in txt.splitlines() if ln.strip().startswith("sayım")), "")
        assert f"sayım: {census.today_key()} para 3 adres → 1 poz (1 hata)" in line and "dk" in line, line
        # kapalı
        cfg.crypto_dex_census_enabled = False
        txt = await diag.report(cfg, None)
        assert "sayım (census): kapalı (crypto_dex_census_enabled=0)" in txt
        # kripto dex yok → atlandı, istek yok
        cfg2, _ = await _fresh(crypto=())
        cli2 = Client(DATA)
        st2 = await census.run(cfg2, cli2)
        assert cli2.calls == [] and "kripto dex yok" in st2["skipped"] and (await dbm.kv_get(census.STATE_KV))["finished"] is True
        # leaderboard yok → atlandı, bitmedi (sonraki tikte yeniden dener)
        cfg3, _ = await _fresh()
        cli3 = Client(None)
        st3 = await census.run(cfg3, cli3)
        assert cli3.calls == [] and st3["skipped"] == "leaderboard alınamadı"
        assert census.due(cfg3, await dbm.kv_get(census.STATE_KV), census.today_key()) is True
        txt = await diag.report(cfg3, None)
        assert "sayım: atlandı — leaderboard alınamadı" in txt
        # bağlantı
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        c = Config()
        for f in ("crypto_dex_census_enabled", "census_min_account_value", "census_rpm"):
            e = EDITABLE_FIELDS[f]
            assert e["group"] == "Tarama & performans" and all(e.get(x) for x in ("type", "label", "desc")) and hasattr(c, f), f
        if not os.getenv("CRYPTO_DEX_CENSUS_ENABLED"):
            assert c.crypto_dex_census_enabled is False and c.census_rpm == 60 and c.census_min_account_value == 1000
        assert '_spawn("census"' in rd("app", "main.py")
        from app.health import limits, periods
        assert limits(c)["census"] == 3600 and periods(c)["census"] == 600
        assert "CRYPTO_DEX_CENSUS_ENABLED=0" in rd(".env.example") and "Kripto dex sayımı (census" in rd("README.md")
        print("✅ sayım) istek = uygun adres × dex, 60/rpm; para yazılır, xyz yaşar, bayat silinir, bozuk yanıt silmez; state/stats; /tani; bağlantı")
    asyncio.run(run())
