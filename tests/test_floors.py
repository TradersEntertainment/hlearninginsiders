"""💵 Kripto dex tabanları — fill $1K / pozisyon $1K (para:ANSEM).

Pinlenenler:
  • assets.fill_floor_for / position_floor_for: kripto dex → yeni tabanlar (ayar), diğerleri
    → min_fill_notional / min_position_notional; kripto dex kapalıysa hisse tabanı
  • sweeper._parse_equity_positions: sayı ya da çağrılabilir taban (`_pos_floor(cfg)`);
    $2K para:ANSEM kalır, $2K xyz:TSLA ve $500 para:MEME düşer
  • collector.fill_floor: kripto dex / hisse / ana dex kripto üç dal; _current_coins kümeyi kurar
  • scanner.scan: toz süzgeci coin sınıfına göre ($500 düşer, $1500 kalır)
  • routes._coin_data satır tavanı 400; ayar/env/README bağlantısı; .env'de CRYPTO_DEXES tek
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-floors.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config, get_config  # noqa: E402
from app.radar import sweeper  # noqa: E402


def rd(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


def _cfg() -> Config:
    cfg = Config()
    cfg.equity_dexes, cfg.crypto_dexes = ["xyz"], ["para"]
    cfg.min_fill_notional, cfg.min_position_notional = 5000.0, 10000.0
    cfg.crypto_dex_fill_min_notional = cfg.crypto_dex_min_position_notional = 1000.0
    cfg.crypto_fill_min_notional = 5000.0
    g = get_config()
    g.equity_dexes, g.crypto_dexes = ["xyz"], ["para"]
    assets.set_crypto_dex_symbols(["ANSEM", "MEME"])
    return cfg


def pos(coin, ntl, szi=100.0):
    return {"position": {"coin": coin, "szi": str(szi), "positionValue": str(ntl), "entryPx": "0.2",
                         "leverage": {"value": 3}, "liquidationPx": "0.13", "unrealizedPnl": "1"}}


def test_floor_helpers():
    cfg = _cfg()
    assert assets.fill_floor_for(cfg, "para:ANSEM") == 1000 and assets.fill_floor_for(cfg, "ANSEM") == 1000
    assert assets.fill_floor_for(cfg, "xyz:TSLA") == 5000 and assets.fill_floor_for(cfg, "HYPE") == 5000
    assert assets.position_floor_for(cfg, "para:MEME") == 1000 and assets.position_floor_for(cfg, "MEME") == 1000
    assert assets.position_floor_for(cfg, "xyz:TSLA") == 10000 and assets.position_floor_for(cfg, "TSLA") == 10000
    cfg.crypto_dex_fill_min_notional, cfg.crypto_dex_min_position_notional = 250, 500
    assert assets.fill_floor_for(cfg, "para:ANSEM") == 250 and assets.position_floor_for(cfg, "para:ANSEM") == 500
    c2 = _cfg()
    c2.crypto_dexes = []
    assert assets.fill_floor_for(c2, "para:ANSEM") == 5000 and assets.position_floor_for(c2, "para:ANSEM") == 10000, \
        "kripto dex kapalıysa hisse tabanı"
    print("✅ taban) kripto dex $1K (ayar), hisse $5K/$10K, kapalıysa hisse tabanı")


def test_parse_callable_floor():
    cfg = _cfg()
    resp = {"main": {"assetPositions": []},
            "xyz": {"assetPositions": [pos("xyz:TSLA", 2000)]},
            "para": {"assetPositions": [pos("para:ANSEM", 2000), pos("para:MEME", 500)]}}
    coin_set = {"xyz:TSLA", "para:ANSEM", "para:MEME"}
    sym_map = {"TSLA": "xyz:TSLA", "ANSEM": "para:ANSEM", "MEME": "para:MEME"}
    out, valid = sweeper._parse_equity_positions(resp, coin_set, sym_map, sweeper._pos_floor(cfg))
    assert valid and set(out) == {"para:ANSEM"} and out["para:ANSEM"]["notional"] == 2000, out
    out2, _ = sweeper._parse_equity_positions(resp, coin_set, sym_map, 1000)
    assert set(out2) == {"xyz:TSLA", "para:ANSEM"}, "sayı tabanı hâlâ çalışır"
    out3, valid3 = sweeper._parse_equity_positions({}, coin_set, sym_map, sweeper._pos_floor(cfg))
    assert out3 == {} and valid3 is False
    print("✅ ayrıştırma) çağrılabilir taban: $2K para kalır, $2K hisse ve $500 para düşer; sayı uyumlu")


def test_collector_and_scanner():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "floors.db"))
        cfg = _cfg()
        cfg.crypto_watch_top = 0
        cfg.leaderboard_top = 0
        cfg.fills_lookback_days = 30
        cfg.scan_max_candidates = 600
        from app.hl.collector import Collector
        col = Collector(cfg, None)
        async with dbm.db() as c:
            for coin, dex, sym in (("xyz:SNDK", "xyz", "SNDK"), ("para:ANSEM", "para", "ANSEM")):
                await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
        coins = await col._current_coins()
        assert set(coins) == {"xyz:SNDK", "para:ANSEM"} and col.crypto_dex_coins == {"para:ANSEM"}, col.crypto_dex_coins
        col.crypto_coins = {"HYPE"}
        assert col.fill_floor("para:ANSEM") == 1000 and col.fill_floor("xyz:SNDK") == 5000 and col.fill_floor("HYPE") == 5000
        cfg.crypto_fill_min_notional = 0
        assert col.fill_floor("HYPE") == float("inf"), "0 = ana dex kripto kaydı kapalı"
        # scanner.scan: toz süzgeci coin sınıfına göre
        from app.radar import scanner
        A, B = "0x" + "a" * 40, "0x" + "b" * 40
        async with dbm.db() as c:
            for i, (addr, ntl) in enumerate(((A, 1500.0), (B, 500.0))):
                await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts,taker)"
                                " VALUES(?,?,?,?,?,?,?,?,?)", ("para:ANSEM", f"t{i}", addr, "buy", 0.2, 1, ntl, dbm.now(), 1))
        holdings = {A: 1500.0, B: 500.0}

        class Client:
            def __init__(self):
                self.calls = []

            async def leaderboard(self):
                return None

            async def clearinghouse(self, addr, dex=""):
                self.calls.append((addr, dex))
                return {"assetPositions": [pos("para:ANSEM", holdings[addr])],
                        "marginSummary": {"accountValue": "5000"}}
        cli = Client()
        found = await scanner.scan(cfg, cli, "para:ANSEM", "para")
        assert sorted(cli.calls) == sorted([(A, "para"), (B, "para")]), cli.calls
        assert [f["address"] for f in found] == [A], found
        async with dbm.db() as c:
            cur = await c.execute("SELECT address, notional FROM positions_current WHERE coin='para:ANSEM'")
            rows = {r["address"]: r["notional"] for r in await cur.fetchall()}
        assert rows == {A: 1500.0}, "$500 para pozisyonu toz, $1500 kalır"
        print("✅ collector/scanner) kripto dex kümesi tickers'tan; üç dal; para taraması $500 düşürür, $1500 yazar")
    asyncio.run(run())


def test_wiring():
    c = Config()
    for f in ("crypto_dex_fill_min_notional", "crypto_dex_min_position_notional"):
        e = EDITABLE_FIELDS[f]
        assert e["type"] == "float" and e["group"] == "Skorlama eşikleri" and all(e.get(x) for x in ("label", "desc")), f
        assert hasattr(c, f)
    if not os.getenv("CRYPTO_DEX_FILL_MIN_NOTIONAL"):
        assert c.crypto_dex_fill_min_notional == 1000 and c.crypto_dex_min_position_notional == 1000
    env = rd(".env.example")
    assert env.count("CRYPTO_DEX_FILL_MIN_NOTIONAL=") == 1 and env.count("CRYPTO_DEX_MIN_POSITION_NOTIONAL=") == 1
    assert env.count("\nCRYPTO_DEXES=") == 1 and env.endswith("\n"), "çift CRYPTO_DEXES temizlendi"
    assert "CRYPTO_DEX_FILL_MIN_NOTIONAL" in rd("README.md")
    src = rd("app", "web", "routes.py")
    assert "COIN_ROWS_MAX = 400" in src and src.count("LIMIT ?\"\"\"") == 2 and "LIMIT 100" not in src.split("def _coin_data")[1].split("def ")[0]
    assert "position_floor_for" in rd("app", "radar", "scanner.py") and "_pos_floor(cfg)" in rd("app", "radar", "sweeper.py")
    print("✅ bağlantı) iki ayar Skorlama eşikleri grubunda, vars. $1K; env/README; satır tavanı 400")
