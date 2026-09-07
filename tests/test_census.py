"""🗳 Sayım (census) v2 — tanıdığımız HER hesabın defteri, ana dex + kripto dex'ler, tur tur.

Pinlenenler:
  • evren: leaderboard (bakiye ≥ taban, HAFTALIK HACİM ŞARTI YOK, tekrarsız, küçük harf) ∪
    addresses (bakiyesiyle) ∪ fills; sıra bakiye azalan (NULL sonda); leaderboard LB_TTL içinde
    yeniden çekilmez, birleştirme UNI_TTL içinde yinelenmez
  • toplu mod: sonda (2 adres) → batchClearinghouseStates; ana dex herkeste, para yalnız bakiyesi
    ≥ hip3 tabanındakilerde; addr_positions/hl_positions/positions_current yazımı, otorite yalnız
    sorgulanan dex (xyz satırı yaşar, bayat para silinir, bayat ana dex kapanır), bozuk yanıt hata
    sayılır ve SİLMEZ, pozisyonsuz hesap addresses'a girmez; state/stats; /tani; kapsama notu
  • üst üste toplu hata → tek moda düşer; zorla sonda → toplu geri gelir
  • tek tek mod: 4xx sondası → single (nedeni kayıtlı); istek sırası; 429 → hız yarıya; sessizlikte
    geri; kapatınca durur, yeniden açınca AYNI turu kaldığı yerden sürer (bozuk yanıt yeniden denenir)
  • bağlantı: ayar künyeleri (eski crypto_dex_census_enabled YOK), varsayılanlar (açık, 250/dk, $100,
    parti 50, HL_MAX_RPM 550), _spawn, health, istemci payload'ı + 429 sayacı, README/.env
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
X, Y = "0x" + "a" * 40, "0x" + "b" * 40


def lb_row(addr, av, week_vlm=None):
    r = {"ethAddress": addr.upper() if addr == L2 else addr, "accountValue": str(av)}
    if week_vlm is not None:
        r["windowPerformances"] = [["day", {"vlm": "1"}], ["week", {"vlm": str(week_vlm)}]]
    return r


def pos(coin, ntl, side=1):
    return {"position": {"coin": coin, "szi": str(100 * side), "positionValue": str(ntl), "entryPx": "0.2",
                         "leverage": {"value": 3}, "liquidationPx": "0.13", "unrealizedPnl": "1"}}


DATA = {"leaderboardRows": [lb_row(L1, 5000, 10), lb_row(L2, 2000), lb_row(L3, 50, 10), lb_row(L4, 3000, 0),
                            lb_row(L5, 1500, 5), lb_row(L1, 5000, 10), {"accountValue": "x"}, "çöp",
                            {"ethAddress": "deadbeef", "accountValue": "9"}]}


class Client:
    """Sahte HL: leaderboard + tek/toplu defter. L2 her dex'te bozuk yanıt (None)."""

    def __init__(self, data=DATA, batch=True, fail_batch=0):
        self.data, self.batch_ok, self.fail_batch = data, batch, fail_batch
        self.calls, self.batch_calls, self.lb_calls = [], [], 0
        self.n_429 = 0
        self.trip_429: set[str] = set()
        self.main = {L1: [("PUMP", 5_200_000), ("BTC", 60_000_000)], L4: [("PUMP", 36_000)], X: [("HYPE", 1000)]}
        self.hip3 = {(L1, "para"): [("para:ANSEM", 1500)]}
        self.av = {L1: 7777, L4: 3001}

    async def leaderboard(self):
        self.lb_calls += 1
        return self.data

    def _state(self, addr, dex):
        if addr == L2:
            return None
        rows = self.main.get(addr, []) if dex == "" else self.hip3.get((addr, dex), [])
        return {"assetPositions": [pos(c, n) for c, n in rows],
                "marginSummary": {"accountValue": str(self.av.get(addr, 1))}}

    async def clearinghouse(self, addr, dex=""):
        self.calls.append((addr, dex))
        if addr in self.trip_429 and dex == "":
            self.n_429 += 1
        return self._state(addr, dex)

    async def batch_clearinghouse(self, users, dex=""):
        self.batch_calls.append((tuple(users), dex))
        if not self.batch_ok:
            raise RuntimeError("HL info batchClearinghouseStates HTTP 422: unknown type")
        if self.fail_batch > 0:
            self.fail_batch -= 1
            raise RuntimeError("HL info batchClearinghouseStates HTTP 500: boom")
        return [self._state(u, dex) for u in users]


async def _fresh(enabled=True):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "census.db"))
    cfg = Config()
    cfg.equity_dexes, cfg.crypto_dexes = ["xyz"], ["para"]
    cfg.census_enabled, cfg.census_min_account_value, cfg.census_hip3_min_account_value = enabled, 100.0, 1000.0
    cfg.census_rpm, cfg.census_batch_size = 250, 50
    cfg.min_position_notional, cfg.crypto_dex_min_position_notional = 10000.0, 1000.0
    cfg.telegram_chat_id = "111"
    g = get_config()
    g.equity_dexes, g.crypto_dexes = ["xyz"], ["para"]
    assets.set_crypto_dex_symbols(["ANSEM", "MEME"])
    t = dbm.now()
    async with dbm.db() as c:
        for coin, dex, sym in (("xyz:SNDK", "xyz", "SNDK"), ("para:ANSEM", "para", "ANSEM"), ("para:MEME", "para", "MEME")):
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
        for coin, addr in (("xyz:SNDK", L1), ("para:MEME", L5), ("para:MEME", L2)):
            await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px) VALUES(?,?,?,?,?,?,?)",
                            (coin, addr, t - 500, "long", 1, 5000.0, 0.1))
        for coin, addr in (("HYPE", L5), ("HYPE", L2)):
            await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", (coin, addr, "", "long", 1, 40, 2, 30, 0, 9000.0, t - 500))
        await c.execute("INSERT INTO addresses(address, first_seen, account_value) VALUES(?,?,?)", (L3, t - 900, 80.0))
        await c.execute("INSERT INTO addresses(address, first_seen) VALUES(?,?)", (X, t - 900))
        await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts) VALUES(?,?,?,?,?,?,?,?)",
                        ("PUMP", "t1", Y, "buy", 0.004, 100, 0.4, t - 100))
    return cfg, t


async def _rows(sql, *args):
    async with dbm.db() as c:
        cur = await c.execute(sql, args)
        return [dict(r) for r in await cur.fetchall()]


def test_universe():
    async def run():
        cfg, t = await _fresh()
        cli = Client()
        u = await census.refresh_universe(cfg, cli)
        assert u["lb"] == 5 and u["addr"] == 2 and u["fills"] == 1 and u["total"] == 7 and u["merged"], u
        order = await census.pass_order(t + 1)
        assert [a for a, _ in order[:5]] == [L1, L4, L2, L5, L3], order
        assert {a for a, _ in order[5:]} == {X, Y} and all(v is None for _, v in order[5:]), order
        rows = {r["address"]: r for r in await _rows("SELECT * FROM census_accounts")}
        assert rows[L4]["account_value"] == 3000 and rows[L4]["src"] == "lb", "haftalık hacim 0 olan hesap da evrende"
        assert rows[L3]["account_value"] == 80 and rows[L3]["src"] == "addr" and rows[Y]["src"] == "fills"
        assert L2 in rows and "deadbeef" not in rows and all(a == a.lower() for a in rows)
        # TTL içinde: leaderboard yeniden çekilmez, birleştirme yinelenmez; zorla → çekilir
        u2 = await census.refresh_universe(cfg, cli)
        assert cli.lb_calls == 1 and u2["lb"] == 0 and not u2["merged"] and u2["total"] == 7, u2
        u3 = await census.refresh_universe(cfg, cli, force=True)
        assert cli.lb_calls == 2 and u3["lb"] == 5 and u3["merged"] and u3["addr"] == 0
        # leaderboard düşerse evren kalır, hata notu
        u4 = await census.refresh_universe(cfg, Client(data=None), force=True)
        assert u4.get("lb_err") == "leaderboard alınamadı" and u4["total"] == 7
        assert list(census.lb_rows(None, 0)) == [] and list(census.lb_rows({"x": 1}, 0)) == []
        print("✅ evren) leaderboard ≥ taban (hacim şartı yok) ∪ addresses ∪ fills; bakiye azalan; TTL; lb düşerse kalır")
    asyncio.run(run())


def test_batch_pass():
    async def run():
        cfg, t = await _fresh()
        cli = Client()
        delays = []

        async def sleep(d):
            delays.append(round(d, 3))
        st = await census.run_pass(cfg, cli, sleep=sleep)
        # sonda (2 adres, ana dex) → toplu; ana dex tek parti (7 hesap), para yalnız bakiyesi ≥ $1K olanlar
        assert cli.batch_calls[0] == ((L1, L4), "") and cli.batch_calls[1][1] == "" and cli.batch_calls[2] == ((L1, L4, L2, L5), "para"), cli.batch_calls
        assert list(cli.batch_calls[1][0][:5]) == [L1, L4, L2, L5, L3] and set(cli.batch_calls[1][0][5:]) == {X, Y}
        assert cli.calls == [] and delays == [0.24, 0.24]
        assert (await dbm.kv_get(census.MODE_KV))["mode"] == "batch"
        assert st["mode"] == "batch" and st["accounts"] == 6 and st["ok"] == 6 and st["err"] == 2 and st["found"] == 5, st
        assert st["requests"] == 2 and st["n_429"] == 0 and not st["fell_back"] and st["universe"]["total"] == 7
        # ana dex yazımı: her boyut addr_positions, kademe üstü hl_positions, bayat ana dex satırı kapanır
        ap = {(r["coin"], r["address"]): r for r in await _rows("SELECT * FROM addr_positions")}
        assert ap[("PUMP", L1)]["notional"] == 5_200_000 and ap[("PUMP", L1)]["dex"] == "" and ap[("PUMP", L1)]["closed_ts"] is None
        assert ap[("BTC", L1)]["closed_ts"] is None and ap[("PUMP", L4)]["notional"] == 36_000 and ap[("HYPE", X)]["notional"] == 1000
        assert ap[("HYPE", L5)]["closed_ts"] is not None, "L5 artık tutmuyor → kapandı damgası"
        assert ap[("HYPE", L2)]["closed_ts"] is None, "L2 bozuk yanıt → dokunulmadı"
        hl = {(r["coin"], r["address"]): r for r in await _rows("SELECT * FROM hl_positions")}
        assert ("BTC", L1) in hl and hl[("BTC", L1)]["closed_ts"] is None and ("PUMP", L1) not in hl, hl.keys()
        # para yazımı: otorite yalnız para — xyz satırı yaşar, bayat para silinir, bozuk yanıt silmez
        pc = {(r["coin"], r["address"]): r["notional"] for r in await _rows("SELECT coin,address,notional FROM positions_current")}
        assert pc == {("xyz:SNDK", L1): 5000.0, ("para:ANSEM", L1): 1500.0, ("para:MEME", L2): 5000.0}, pc
        # sayım tablosu + addresses: pozisyonsuz hesap havuza girmez, bakiye ölçümden
        ca = {r["address"]: r for r in await _rows("SELECT * FROM census_accounts")}
        assert ca[L1]["scanned_ts"] and ca[L1]["positions"] == 2 and ca[L1]["account_value"] == 7777
        assert ca[L2]["scanned_ts"] is None and ca[Y]["scanned_ts"] and ca[Y]["positions"] == 0 and ca[L5]["positions"] == 0
        ad = {r["address"]: r for r in await _rows("SELECT * FROM addresses")}
        assert set(ad) == {L3, X, L1, L4}, set(ad)
        assert ad[L1]["account_value"] == 7777 and ad[L1]["probed_ts"] and ad[X]["probed_ts"]
        # state/stats/ilerleme/tani/kapsama
        s = await dbm.kv_get(census.STATE_KV)
        assert s["finished"] is True and s["done"] == 6 and s["total"] == 7 and s["pass_ts"] >= t
        p = await census.progress()
        assert p["running"] is False and p["mode"] == "batch" and p["text"].startswith("sayım tam (toplu, tur "), p
        from app import diag
        txt = await diag.report(cfg, None)
        line = next((ln.strip() for ln in txt.splitlines() if ln.strip().startswith("sayım (census)")), "")
        assert line.startswith("sayım (census): toplu · son tur ") and "6 hesap → 5 poz (2 hata)" in line, line
        assert "evren: leaderboard 5 hesap (bakiye ≥ $100)" in line, line
        from app.radar import coverage as cov
        from app.telegram import format as fmt
        c = await cov.coverage("PUMP", "crypto", cfg, ctx={"c": {"PUMP": {"m": 0.004, "oi": 2_618_000_000}}})
        assert abs(c["pct_long"] - 50.0) < 0.01 and c["census"]["text"].startswith("sayım tam"), c
        assert "(havuz / HL OI) · sayım tam (toplu" in fmt.coverage_ctx(c)
        cfg.census_enabled = False
        assert (await cov.coverage("PUMP", "crypto", cfg))["census"] is None
        assert "sayım (census): kapalı (census_enabled=0)" in await diag.report(cfg, None)
        cfg.census_enabled = True
        # ikinci tur: yeni pass_ts, leaderboard TTL içinde çekilmez, aynı hesaplar yeniden
        cli2 = Client()
        st2 = await census.run_pass(cfg, cli2, sleep=sleep)
        assert cli2.lb_calls == 0 and st2["accounts"] == 6 and cli2.batch_calls[0][1] == "" and len(cli2.batch_calls) == 2, cli2.batch_calls
        # üst üste 3 toplu hata → tek moda düşer (parti 2): kalan hesaplar tek tek sorgulanır
        cfg.census_batch_size = 2
        cli3 = Client(fail_batch=3)
        st3 = await census.run_pass(cfg, cli3, sleep=sleep)
        assert st3["fell_back"] and st3["mode"] == "single" and cli3.calls, (st3, cli3.calls)
        assert (await dbm.kv_get(census.MODE_KV))["mode"] == "single" and "üst üste" in (await dbm.kv_get(census.MODE_KV))["why"]
        line = next((ln.strip() for ln in (await diag.report(cfg, None)).splitlines() if ln.strip().startswith("sayım (census)")), "")
        assert "toplu→tek düştü" in line and line.startswith("sayım (census): tek tek (tur içinde üst üste toplu hata)"), line
        # zorla sonda → toplu geri (toplu hata bitti)
        assert await census.probe_mode(cfg, cli3, [L1, L4], force=True) == "batch"
        print("✅ toplu tur) sonda → batchClearinghouseStates; ana dex herkes, para ≥ $1K; yazım/otorite; havuz şişmez; state/tani/kapsama; 3 hata → tek")
    asyncio.run(run())


def test_single_pass_429_stop_resume():
    async def run():
        cfg, t = await _fresh()
        cli = Client(batch=False)
        cli.trip_429 = {L4}
        delays = []
        stop_after = [None]

        async def sleep(d):
            delays.append(round(d, 3))
            if stop_after[0] is not None and len(delays) >= stop_after[0]:
                cfg.census_enabled = False
        st = await census.run_pass(cfg, cli, sleep=sleep)
        md = await dbm.kv_get(census.MODE_KV)
        assert md["mode"] == "single" and "HTTP 422" in md["why"] and len(cli.batch_calls) == 1, md
        main = [a for a, d in cli.calls if d == ""]
        para = [a for a, d in cli.calls if d == "para"]
        assert main[:5] == [L1, L4, L2, L5, L3] and set(main[5:]) == {X, Y} and para == [L1, L4, L2, L5], cli.calls
        # 429 (L4'ün isteğinde) → sonraki gecikmeler iki kat
        assert delays[:3] == [0.24, 0.24, 0.48] and all(d == 0.48 for d in delays[2:]) and st["n_429"] == 1, delays
        assert st["mode"] == "single" and st["accounts"] == 6 and st["err"] == 2 and st["found"] == 5, st
        line = next((ln.strip() for ln in (await __import__("app.diag", fromlist=["report"]).report(cfg, None)).splitlines()
                     if ln.strip().startswith("sayım (census)")), "")
        assert line.startswith("sayım (census): tek tek (RuntimeError: HL info batchClearinghouseStates HTTP 422") and "429: 1" in line, line
        # hız geri: sessizlikte bir kademe
        clock = [0.0]
        pace = census.Pace(250, cli, clock=lambda: clock[0])
        cli.n_429 += 1
        pace.observe()
        assert pace.factor == 0.5 and abs(pace.delay() - 0.48) < 1e-9
        for _ in range(5):
            cli.n_429 += 1
            pace.observe()
        assert pace.factor == 0.125, "en az 1/8"
        clock[0] = census.SLOW_RECOVER_SEC + 1
        pace.observe()
        assert pace.factor == 0.25 and pace.n_429 == 6
        # kapatınca durur; yeniden açınca AYNI tur kaldığı yerden (bozuk L2 yeniden denenir)
        cfg2, _ = await _fresh()
        cli2 = Client(batch=False)
        delays.clear()
        stop_after[0] = 3
        cfg = cfg2
        st2 = await census.run_pass(cfg, cli2, sleep=sleep)
        assert st2.get("stopped") == "kapatıldı" and st2["finished"] is False and [a for a, _ in cli2.calls] == [L1, L4, L2], cli2.calls
        s = await dbm.kv_get(census.STATE_KV)
        assert s["finished"] is False and s["done"] == 2 and s["stopped"] == "kapatıldı"
        assert (await census.progress())["running"] is True and "sayım %29" in (await census.progress())["text"]
        stop_after[0] = None
        cfg.census_enabled = True
        line = next((ln.strip() for ln in (await __import__("app.diag", fromlist=["report"]).report(cfg, None)).splitlines()
                     if ln.strip().startswith("sayım (census)")), "")
        assert "sürüyor %29 (2/7 hesap)" in line, line
        cli3 = Client(batch=False)
        st3 = await census.run_pass(cfg, cli3, sleep=sleep)
        main3 = [a for a, d in cli3.calls if d == ""]
        assert main3[:3] == [L2, L5, L3] and set(main3[3:]) == {X, Y} and st3["resumed"] is True, cli3.calls
        assert st3["accounts"] == 6 and (await dbm.kv_get(census.STATE_KV))["pass_ts"] == s["pass_ts"]
        # kapalıyken tur koşmaz (loop enabled bakar) — run_pass doğrudan çağrılsa da hesap sorgulamaz
        cfg.census_enabled = False
        cli4 = Client(batch=False)
        st4 = await census.run_pass(cfg, cli4, sleep=sleep)
        assert cli4.calls == [] and st4.get("stopped") == "kapatıldı"
        print("✅ tek tek) 4xx sondası → single; sıra; 429 → hız yarıya, sessizlikte geri; kapat/devam aynı tur; kapalıyken istek yok")
    asyncio.run(run())


def test_batch_states_and_wiring():
    a, b = L1, L2
    ok = [{"assetPositions": []}, None]
    assert census._batch_states(ok, [a, b]) == ok
    assert census._batch_states({L1.upper(): ok[0], L2: ok[0]}, [a, b]) == [ok[0], ok[0]], "adres→state sözlüğü de kabul"
    assert census._batch_states([ok[0]], [a, b]) is None and census._batch_states([ok[0], {"x": 1}], [a, b]) is None
    assert census._batch_states({"assetPositions": []}, [a]) is None and census._batch_states("x", [a]) is None
    # istemci: payload + 429 sayacı (sahte oturum)
    from app.hl.client import HLClient

    class Resp:
        def __init__(self, status, body):
            self.status, self.body = status, body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return self.body

        async def text(self):
            return "x"

    class Sess:
        def __init__(self):
            self.posts, self.queue = [], []

        def post(self, url, json=None, timeout=None):
            self.posts.append((url, json))
            return self.queue.pop(0) if self.queue else Resp(200, [])

    async def run():
        sess = Sess()
        cli = HLClient(sess, "https://x/", "https://lb", min_interval=0)
        sess.queue = [Resp(429, None), Resp(200, [{"assetPositions": []}])]
        out = await cli.batch_clearinghouse([L1], "para")
        assert out == [{"assetPositions": []}] and cli.n_429 == 1
        assert sess.posts[-1] == ("https://x/info", {"type": "batchClearinghouseStates", "users": [L1], "dex": "para"}), sess.posts
        await cli.batch_clearinghouse([L1, L2])
        assert sess.posts[-1][1] == {"type": "batchClearinghouseStates", "users": [L1, L2]}
    asyncio.run(run())
    rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
    c = Config()
    for f in ("census_enabled", "census_min_account_value", "census_hip3_min_account_value", "census_rpm", "census_batch_size"):
        e = EDITABLE_FIELDS[f]
        assert e["group"] == "Tarama & performans" and all(e.get(x) for x in ("type", "label", "desc")) and hasattr(c, f), f
    assert "crypto_dex_census_enabled" not in EDITABLE_FIELDS and not hasattr(c, "crypto_dex_census_enabled")
    if not any(os.getenv(k) for k in ("CENSUS_ENABLED", "CENSUS_RPM", "CENSUS_MIN_ACCOUNT_VALUE", "CENSUS_BATCH_SIZE", "HL_MAX_RPM")):
        assert c.census_enabled is True and c.census_rpm == 250 and c.census_min_account_value == 100
        assert c.census_hip3_min_account_value == 1000 and c.census_batch_size == 50 and c.hl_max_rpm == 550
    assert '_spawn("census"' in rd("app", "main.py")
    from app.health import limits, periods
    assert limits(c)["census"] == 3600 and periods(c)["census"] == 600
    env = rd(".env.example")
    assert "CENSUS_ENABLED=1" in env and "CENSUS_RPM=250" in env and "HL_MAX_RPM=550" in env and "CENSUS_BATCH_SIZE=50" in env
    assert "CRYPTO_DEX_CENSUS_ENABLED" not in env
    assert "batchClearinghouseStates" in rd("README.md") and "Sayım (census" in rd("README.md")
    print("✅ bağlantı) toplu yanıt şekilleri; istemci payload + 429 sayacı; ayar künyeleri/varsayılanlar; _spawn/health; .env/README")
