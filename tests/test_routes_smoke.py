"""🌐 Rota dumanı — sayfalar ASGI uygulaması üzerinden GERÇEKTEN açılıyor mu.

Şablon testleri Jinja'yı doğrudan render ediyor; rota gövdesindeki bir NameError
(routes.py'de `assets` import'u eksikti → her coin sayfası 500) hiçbir testte
yakalanmıyordu. Burada uygulama httpx'siz, ham ASGI çağrısıyla sürülür:
lifespan koşmaz, app.state elle kurulur, arka plan döngüsü yok.

Pinlenenler: /, /t/<HIP-3 hisse> (kapsama kutusu), /t/<ana dex kripto>, /t/<yok>,
/whale/<adres>, /tani — hepsi 200, gövdede beklenen metin, hiçbiri 500 değil.
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-routes.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import Config, get_config  # noqa: E402
from app.hl import universe as uni  # noqa: E402

ADDR = "0x" + "a" * 40


async def get(app, path: str, query: bytes = b"") -> tuple[int, str]:
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query,
             "root_path": "", "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1),
             "server": ("127.0.0.1", 80)}
    status, body = {}, []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]
        elif msg["type"] == "http.response.body":
            body.append(msg.get("body", b""))
    await app(scope, receive, send)
    return status.get("code"), b"".join(body).decode("utf-8", "replace")


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "routes.db"))
    cfg = Config()
    cfg.equity_dexes, cfg.crypto_dexes = ["xyz"], ["para"]
    cfg.scan_stale_min = 10
    g = get_config()
    g.equity_dexes, g.crypto_dexes = ["xyz"], ["para"]
    assets.set_crypto_dex_symbols(["ANSEM"])
    t = dbm.now()
    async with dbm.db() as c:
        for coin, dex, sym in (("xyz:SNDK", "xyz", "SNDK"), ("para:ANSEM", "para", "ANSEM")):
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
            await c.execute("INSERT INTO scans(coin,ts,n_addrs,n_found) VALUES(?,?,?,?)", (coin, t, 12, 3))   # taze → kick yok
        await c.execute("INSERT INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume) VALUES(?,?,?,?,?,?)",
                        ("para:ANSEM", t - 60, 0.2, 10_000_000, 0.0, 1.0))
        await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,entry_px,leverage,notional,liq_px,upnl)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?)", ("para:ANSEM", ADDR, t - 30, "long", 100, 0.2, 3, 240_000.0, 0.13, 1.0))
        await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", ("HYPE", ADDR, "", "long", 1, 40, 5, 30, 0, 300_000.0, t - 30))
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HYPE": {"m": 40.0, "oi": 50_000, "v": 1.0}}, "ts": t})
    from app.main import app
    app.state.cfg, app.state.client, app.state.session = cfg, None, None
    app.state.bot, app.state.notifier = None, None
    return app


def test_pages_open():
    async def run():
        app = await _fresh()
        st, body = await get(app, "/")
        assert st == 200, st
        st, body = await get(app, "/t/ANSEM")
        assert st == 200 and "Kapsama (havuz / HL OI)" in body and "L %12 · S %0" in body and "para dex kripto" in body, (st, body[-500:])
        assert "12 adres → 3 poz" in body
        st, body = await get(app, "/t/HYPE")
        assert st == 200 and "ana dex kripto" in body and "L %15" in body, (st, body[-500:])
        st, body = await get(app, "/t/XXXX")
        assert st == 200 and "bulunamad" in body
        st, body = await get(app, f"/whale/{ADDR}")
        assert st == 200, st
        st, body = await get(app, "/tani", b"full=1")
        assert st == 200 and "kapsama (havuz/HL OI" in body and ("işlem hasadı" in body or "sayım (census)" in body), (st, body[:300])
        print("✅ rotalar) /, /t/ANSEM (kapsama kutusu, para rozeti, tarama sayımı), /t/HYPE, /t/XXXX, /whale, /tani → 200")
    asyncio.run(run())
