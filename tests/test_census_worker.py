"""🧑‍🏭 Sayım worker modu — DB'siz servis ana uygulamadan kiralar, HL'ye kendi sorar, geri yollar.

Pinlenenler:
  • /api/census/lease: WORKER_TOKEN yoksa 404, yanlışsa 401; doğruysa sıradaki n hesap bakiye
    sırasıyla kiralanır (leased_ts/lease_worker), ikinci kira kalanları alır, üçüncü boş; tur yoksa /
    sayım kapalıysa boş + neden; süresi dolan kira yeniden dağıtılır
  • /api/census/ingest: aynı yazıcılar (addr_positions/hl_positions/positions_current, otorite yalnız
    dex), bozuk state hata sayılır ve silmez, bilinmeyen dex/adres reddedilir; hesap sayılmış olur,
    kira düşer; census_workers kv (aktif worker), ana uygulamanın yerel hızı census_rpm_local
  • worker döngüsü (sahte oturum → ASGI): kirala → kendi sondası (toplu/tek) → sorgula → POST;
    boş kira → bekleme; ana uygulama düşerse hata notu, çökmez; health payload
  • ana tur worker'ın kiraladığı/bitirdiği hesapları atlar
  • bağlantı: Dockerfile ROLE dalı, env-only alanlar (EDITABLE'da YOK), census_rpm_local künyesi,
    .env/README
"""
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-census-worker.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app import worker  # noqa: E402
from app.config import EDITABLE_FIELDS, Config, get_config  # noqa: E402
from app.radar import census  # noqa: E402
from test_census import DATA, L1, L2, L3, L4, L5, X, Y, Client, _fresh, _rows  # noqa: E402

TOKEN = "s3cret-worker"


async def call(app, method: str, path: str, query: str = "", body: dict | None = None,
               headers: dict | None = None) -> tuple[int, object]:
    raw = json.dumps(body).encode() if body is not None else b""
    hdrs = [(b"host", b"test")]
    if body is not None:
        hdrs.append((b"content-type", b"application/json"))
        hdrs.append((b"content-length", str(len(raw)).encode()))
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), v.encode()))
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "root_path": "", "headers": hdrs, "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    status, chunks = {}, []

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))
    await app(scope, receive, send)
    text = b"".join(chunks).decode("utf-8", "replace")
    try:
        return status.get("code"), json.loads(text)
    except ValueError:
        return status.get("code"), text


async def _app(cfg):
    from app.main import app
    app.state.cfg, app.state.client, app.state.session = cfg, None, None
    app.state.bot, app.state.notifier = None, None
    return app


class Resp:
    def __init__(self, status, data):
        self.status, self.data = status, data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.data

    async def text(self):
        return json.dumps(self.data)


class Sess:
    """aiohttp oturumu taklidi: GET/POST'u ASGI uygulamasına yönlendirir."""

    def __init__(self, app, base="http://main"):
        self.app, self.base, self.calls, self.down = app, base, [], False

    def _path(self, url):
        assert url.startswith(self.base), url
        return url[len(self.base):]

    def get(self, url, params=None, headers=None, timeout=None):
        return self._do("GET", url, params, headers, None)

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        return self._do("POST", url, params, headers, json)

    def _do(self, method, url, params, headers, body):
        if self.down:
            raise RuntimeError("bağlantı yok")
        q = "&".join(f"{k}={v}" for k, v in (params or {}).items())
        self.calls.append((method, self._path(url), q))

        class Ctx:
            async def __aenter__(_s):
                st, data = await call(self.app, method, self._path(url), q, body, headers)
                return Resp(st, data)

            async def __aexit__(_s, *a):
                return False
        return Ctx()


async def _setup(batch=True):
    cfg, t = await _fresh()
    cfg.worker_token = TOKEN
    cfg.census_rpm_local = 100
    cli = Client(batch=batch)
    await census.refresh_universe(cfg, cli)
    # süren tur (ana uygulama run_pass'ı başlatmış gibi)
    await dbm.kv_set(census.STATE_KV, {"pass_ts": t, "mode": "batch" if batch else "single", "finished": False,
                                       "total": 7, "done": 0, "ts": t})
    app = await _app(cfg)
    return cfg, t, cli, app


def test_lease_and_ingest_api():
    async def run():
        cfg, t, cli, app = await _setup()
        # jeton yoksa uç yok; yanlışsa 401; doğruysa kira
        cfg.worker_token = ""
        assert (await call(app, "GET", "/api/census/lease", "worker=w1&n=3"))[0] == 404
        cfg.worker_token = TOKEN
        assert (await call(app, "GET", "/api/census/lease", "worker=w1&n=3&token=nope"))[0] == 401
        st, body = await call(app, "GET", "/api/census/lease", "worker=w1&n=3", headers={"X-Worker-Token": TOKEN})
        assert st == 200 and [x["a"] for x in body["accounts"]] == [L1, L4, L2] and body["pass_ts"] == t, body
        assert body["dexes"] == ["", "para"] and body["hip3_floor"] == 1000 and body["lease_sec"] == 600 and body["wait"] == 0
        rows = {r["address"]: r for r in await _rows("SELECT * FROM census_accounts")}
        assert rows[L1]["lease_worker"] == "w1" and rows[L1]["leased_ts"] >= t and rows[L5]["leased_ts"] is None
        st, body2 = await call(app, "GET", "/api/census/lease", f"worker=w2&n=10&token={TOKEN}")
        assert [x["a"] for x in body2["accounts"]][:2] == [L5, L3] and {x["a"] for x in body2["accounts"]} == {L5, L3, X, Y}
        st, body3 = await call(app, "GET", "/api/census/lease", f"worker=w2&n=10&token={TOKEN}")
        assert body3["accounts"] == [] and body3["wait"] == 30 and body3["pass_ts"] == t
        ws = await dbm.kv_get(census.WORKERS_KV)
        assert ws["w1"]["leased"] == 3 and ws["w2"]["leased"] == 4 and await census.workers_active() == ["w1", "w2"]
        assert await census.effective_rpm(cfg) == 100, "worker aktifken ana uygulama yerel hıza iner"
        # süresi dolan kira yeniden dağıtılır
        async with dbm.db() as c:
            await c.execute("UPDATE census_accounts SET leased_ts=? WHERE address=?", (t - census.LEASE_SEC - 1, L5))
        st, body4 = await call(app, "GET", "/api/census/lease", f"worker=w3&n=10&token={TOKEN}")
        assert [x["a"] for x in body4["accounts"]] == [L5], body4
        # ingest: w1 sonuçları (L1 ana+para, L4 ana, L2 bozuk) — aynı yazıcılar, otorite yalnız dex
        res = [{"a": L1, "dex": "", "state": worker.trim_state(cli._state(L1, ""))},
               {"a": L1, "dex": "para", "state": worker.trim_state(cli._state(L1, "para"))},
               {"a": L4, "dex": "", "state": worker.trim_state(cli._state(L4, ""))},
               {"a": L2, "dex": "", "state": None},
               {"a": L4.upper(), "dex": "xyz", "state": {"assetPositions": []}},      # bilinmeyen dex → red
               {"a": "0xkısa", "dex": "", "state": {"assetPositions": []}}, "çöp"]      # bozuk satır → red
        st, out = await call(app, "POST", "/api/census/ingest", f"worker=w1&token={TOKEN}", {"pass_ts": t, "results": res})
        assert st == 200 and out == {"ok": True, "ok_n": 2, "err": 4, "found": 4, "scanned": 2}, out
        ap = {(r["coin"], r["address"]): r for r in await _rows("SELECT * FROM addr_positions")}
        assert ap[("PUMP", L1)]["notional"] == 5_200_000 and ap[("PUMP", L4)]["notional"] == 36_000 and ap[("HYPE", L2)]["closed_ts"] is None
        assert ("BTC", L1) in {(r["coin"], r["address"]) for r in await _rows("SELECT coin,address FROM hl_positions")}
        pc = {(r["coin"], r["address"]): r["notional"] for r in await _rows("SELECT coin,address,notional FROM positions_current")}
        assert pc == {("xyz:SNDK", L1): 5000.0, ("para:ANSEM", L1): 1500.0, ("para:MEME", L5): 5000.0, ("para:MEME", L2): 5000.0}, pc
        rows = {r["address"]: r for r in await _rows("SELECT * FROM census_accounts")}
        assert rows[L1]["scanned_ts"] >= t and rows[L1]["leased_ts"] is None and rows[L1]["positions"] == 2 and rows[L1]["account_value"] == 7777
        assert rows[L2]["scanned_ts"] is None and rows[L2]["lease_worker"] == "w1", "bozuk → sayılmadı, kira sürüyor (süresi dolunca yeniden)"
        ws = await dbm.kv_get(census.WORKERS_KV)
        assert ws["w1"]["ingested"] == 2 and ws["w1"]["err"] == 4
        # gövde bozuksa 400; tur yoksa / sayım kapalıysa kira boş
        assert (await call(app, "POST", "/api/census/ingest", f"token={TOKEN}", ["x"]))[0] == 400
        await dbm.kv_set(census.STATE_KV, {"pass_ts": t, "finished": True})
        st, b5 = await call(app, "GET", "/api/census/lease", f"worker=w1&token={TOKEN}")
        assert b5["accounts"] == [] and b5["why"] == "süren tur yok" and b5["pass_ts"] is None
        cfg.census_enabled = False
        st, b6 = await call(app, "GET", "/api/census/lease", f"worker=w1&token={TOKEN}")
        assert "kapalı" in b6["why"]
        cfg.census_enabled = True
        # /tani worker satırı
        from app import diag
        line = next((ln.strip() for ln in (await diag.report(cfg, None)).splitlines() if ln.strip().startswith("sayım (census)")), "")
        assert "worker: 3 aktif/3 bilinen" in line and "teslim 2 hesap" in line and "yerel hız 100/dk" in line, line
        print("✅ worker API) 404/401/kira sırası/ikinci kira/boş; kira süresi; ingest aynı yazıcılar + red; kv; yerel hız; /tani")
    asyncio.run(run())


def test_worker_loop_and_main_pass_skip():
    async def run():
        # (1) toplu: worker kendi sondasını yapar, 7 hesabı tek kirada alır, 2+1 isteğe sığdırır, POST eder
        cfg, t, cli, app = await _setup(batch=True)
        wcfg = Config()
        wcfg.main_url, wcfg.worker_token, wcfg.worker_name = "http://main", TOKEN, "wA"
        wcfg.census_rpm, wcfg.census_batch_size, wcfg.worker_lease_n = 250, 50, 500
        sess = Sess(app)
        delays = []

        async def sleep(d):
            delays.append(round(d, 3))
        hl = Client(batch=True)
        n = await worker.run(wcfg, sess, hl, sleep=sleep, once=True)
        assert n == 7 and hl.batch_calls[0] == ((L1, L4), "") and len(hl.batch_calls) == 3 and hl.calls == [], hl.batch_calls
        assert [c[:2] for c in sess.calls] == [("GET", "/api/census/lease"), ("POST", "/api/census/ingest")], sess.calls
        assert worker.STATS["mode"] == "batch" and worker.STATS["accounts"] == 7 and worker.STATS["ok"] == 6 and worker.STATS["err"] == 2
        assert worker.STATS["requests"] == 2 and delays == [0.24, 0.24] and worker.STATS["last_error"] == ""
        rows = {r["address"]: r for r in await _rows("SELECT * FROM census_accounts")}
        assert sum(1 for r in rows.values() if r["scanned_ts"]) == 6 and rows[L2]["scanned_ts"] is None
        assert all(r["leased_ts"] is None for a, r in rows.items() if a != L2) and rows[L2]["lease_worker"] == "wA"
        ap = {(r["coin"], r["address"]) for r in await _rows("SELECT coin,address FROM addr_positions WHERE closed_ts IS NULL")}
        assert ap == {("PUMP", L1), ("BTC", L1), ("PUMP", L4), ("HYPE", X), ("HYPE", L2)}, ap
        assert (await dbm.kv_get(census.WORKERS_KV))["wA"]["ingested"] == 6
        # ikinci kira boş → 0, bekleme 30
        assert await worker.run(wcfg, sess, hl, sleep=sleep, once=True) == 0
        # ana uygulama düşerse: hata notu, çökmez
        sess.down = True
        assert await worker.run(wcfg, sess, hl, sleep=sleep, once=True) == 0 and "bağlantı yok" in worker.STATS["last_error"]
        sess.down = False
        h = worker.health_payload()
        assert h["ok"] is True and h["role"] == "census-worker" and h["stats"]["accounts"] == 7 and h["stats"]["ok"] == 6 and "uptime_sec" in h
        assert worker.trim_state({"assetPositions": [1], "marginSummary": {"accountValue": "5", "x": 1}, "junk": 1}) == \
            {"assetPositions": [1], "marginSummary": {"accountValue": "5"}}
        assert worker.trim_state(None) is None and worker.trim_state({"x": 1}) is None
        # (2) tek tek: worker sondası 422 → single; ana tur worker'ın kiraladığını ATLAR, kalanı sayar
        cfg, t, cli, app = await _setup(batch=False)
        sess = Sess(app)
        wcfg.worker_lease_n = 3
        hl2 = Client(batch=False)
        st, lease = await call(app, "GET", "/api/census/lease", f"worker=wB&n=3&token={TOKEN}")
        assert [x["a"] for x in lease["accounts"]] == [L1, L4, L2]
        cli_main = Client(batch=False)
        stats = await census.run_pass(cfg, cli_main, sleep=sleep)
        main_calls = [a for a, d in cli_main.calls if d == ""]
        assert set(main_calls) == {L5, L3, X, Y} and L1 not in main_calls, cli_main.calls
        assert stats["mode"] == "single" and stats["workers"] == 1 and stats["accounts"] == 4, stats
        # worker sonra teslim eder → hesaplar sayılmış olur (tur bitmiş olsa da veri taze)
        n = await worker.run(wcfg, sess, hl2, sleep=sleep, once=True)      # kira: kalan yok (tur bitti) → 0
        assert n == 0
        st, out = await call(app, "POST", "/api/census/ingest", f"worker=wB&token={TOKEN}",
                             {"pass_ts": lease["pass_ts"], "results": [{"a": L1, "dex": "", "state": worker.trim_state(hl2._state(L1, ""))}]})
        assert out["ok_n"] == 1
        rows = {r["address"]: r for r in await _rows("SELECT * FROM census_accounts")}
        assert rows[L1]["scanned_ts"] and rows[L1]["leased_ts"] is None and rows[L4]["lease_worker"] == "wB"
        print("✅ worker döngüsü) kendi sondası; toplu 2 istek/7 hesap; POST; boş kira; ana düşse çökmez; health; ana tur kiralıyı atlar")
    asyncio.run(run())


def test_wiring():
    rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
    d = rd("Dockerfile")
    assert 'ROLE' in d and 'census-worker' in d and 'python -m app.worker' in d and 'uvicorn app.main:app' in d
    line = next(ln for ln in d.splitlines() if ln.startswith("CMD"))
    cmd = json.loads(line[3:].strip())
    assert cmd[:2] == ["sh", "-c"] and "if [" in cmd[2] and "fi" in cmd[2]
    c = Config()
    for f in ("role", "worker_token", "main_url", "worker_name", "worker_lease_n"):
        assert hasattr(c, f) and f not in EDITABLE_FIELDS, f"{f} env-only kalmalı"
    e = EDITABLE_FIELDS["census_rpm_local"]
    assert e["group"] == "Tarama & performans" and all(e.get(x) for x in ("type", "label", "desc")) and c.census_rpm_local == 100
    if not os.getenv("WORKER_LEASE_N"):
        assert c.worker_lease_n == 500 and c.role == "" and c.worker_token == "" and c.main_url == ""
    env = rd(".env.example")
    for k in ("ROLE=", "MAIN_URL=", "WORKER_TOKEN=", "CENSUS_RPM_LOCAL=100", "WORKER_LEASE_N=500"):
        assert k in env, k
    r = rd("README.md")
    assert "ROLE=census-worker" in r and "/api/census/lease" in r and "WORKER_TOKEN" in r and "çıkış IP" in r
    print("✅ bağlantı) Dockerfile ROLE dalı (geçerli JSON CMD); env-only alanlar; census_rpm_local künyesi; .env/README")


def test_lease_hot_first():
    async def run():
        cfg, t, cli, app = await _setup()
        async with dbm.db() as c:
            await c.execute("UPDATE census_accounts SET hot_ts=? WHERE address=?", (t, Y))     # fills-only, bakiyesiz: normalde en son
        st, body = await call(app, "GET", "/api/census/lease", f"worker=w1&n=2&token={TOKEN}")
        assert [x["a"] for x in body["accounts"]] == [Y, L1], body["accounts"]
        print("✅ kira) sıcak hesap bakiye sırasının önüne geçer")
    asyncio.run(run())
