"""🧪 Çözücü sağlamlığı (08.10 bağımsız inceleme bulguları).

Pinlenenler:
  • bir coinin HL hatası turu düşürmez: öteki coinler ölçülür; art arda 6 turda o coinin işi "veri alınamadı"
  • kıyas bütçe yüzünden okunamadıysa sonuç YAZILMAZ (bekler); kıyas okundu ama işlem yoksa "ölçülemedi"
    (net'siz "done" asla); forward_clusters net'siz "done"ı da ölçülemeyen sayar
  • olay KAYITTA DONMUŞ kendi sürümünün tanımıyla ölçülür; yeni sürüm çıkış kuralını kaldırsa da tur çökmez
  • kapanışta (iptal) kuyruktaki karar anı kayıtları yazılır; flush iptalde partiyi geri koyar
  • iş birikince çözücü her turda koşar, derin dolum yalnız iş bitince
"""
import asyncio
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-labres.db")
from app import db as dbm  # noqa: E402
from app.lab import data, looks, registry, resolver  # noqa: E402
from app.lab import loop as lab_loop  # noqa: E402

_spec = importlib.util.spec_from_file_location("lab_core_x", os.path.join(HERE, "test_lab_core.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
T = core.T


async def _rows(sql, args=()):
    async with dbm.db() as c:
        return [dict(r) for r in await (await c.execute(sql, args)).fetchall()]


class Poison(core.Client):
    """Bir coin için HL hatası (kaldırılmış varlık: candleSnapshot HTTP 500)."""

    def __init__(self, raw, bad):
        super().__init__(raw)
        self.bad = bad

    async def candles(self, coin, interval, start_ms, end_ms):
        if coin == self.bad:
            raise RuntimeError("HL info candleSnapshot HTTP 500: null")
        return await super().candles(coin, interval, start_ms, end_ms)


def test_poison_coin_isolated_then_given_up():
    async def run():
        cfg = await core._fresh("res1.db")
        await registry.sync(cfg, ts=T - 86400, rules=[core._rule("A", exit=None, horizons_s=[900], primary_h=900)])
        for c in ("xyz:GONE", "xyz:AAA"):
            registry.emit("A", c, T, side=1, trig_key=c)
        await registry.flush(ts=T + 5)
        raw = {("xyz:AAA", "1m"): core.series(core.px, T - 3600, T + 4 * 3600),
               ("xyz:XYZ100", "1m"): core.series(core.bx, T - 3600, T + 4 * 3600)}
        cl = Poison(raw, "xyz:GONE")
        b = data.Budget(per_min=100_000)
        out = await resolver.resolve_due(cl, b, T + 3600)
        assert out["errors"] >= 1 and out["outcomes"]["done"] == 1, out
        ev = {r["coin"]: r for r in await _rows("SELECT * FROM strat_events")}
        assert ev["xyz:AAA"]["status"] == "done" and ev["xyz:GONE"]["status"] == "pending", "öteki coin ölçüldü"
        for i in range(resolver.FAIL_MAX):
            await resolver.resolve_due(cl, b, T + 3600 + 60 * (i + 1))
        ev = {r["coin"]: r for r in await _rows("SELECT * FROM strat_events")}
        assert ev["xyz:GONE"]["status"] == "unresolvable" and "veri alınamadı" in ev["xyz:GONE"]["status_note"]
    asyncio.run(run())
    print("✅ çözücü) bir coinin HL hatası turu düşürmez; art arda 6 turda 'veri alınamadı'")


class BenchDeny(core.Client):
    def __init__(self, raw):
        super().__init__(raw)
        self.deny = True


def test_bench_denied_waits_and_empty_bench_unresolvable():
    async def run():
        cfg = await core._fresh("res2.db")
        await registry.sync(cfg, ts=T - 86400, rules=[core._rule("A", exit=None, horizons_s=[900], primary_h=900)])
        registry.emit("A", "xyz:AAA", T, side=1, trig_key="a")
        registry.emit("A", "xyz:BBB", T, side=1, trig_key="b")
        await registry.flush(ts=T + 5)
        raw = {("xyz:AAA", "1m"): core.series(core.px, T - 3600, T + 4 * 3600),
               ("xyz:BBB", "1m"): core.series(core.px, T - 3600, T + 4 * 3600)}
        cl = core.Client(raw)
        await resolver.resolve_due(cl, data.Budget(per_min=100_000), T + 300)   # girişler (kıyas istenmez)
        # kıyas penceresi bütçeye takılsın: kıyas coininde HL çağrısını reddeden bütçe
        b = data.Budget(per_min=100_000)
        real = data.ensure_window

        async def deny_bench(client, coin, tf, t0, t1, budget=None, now_ts=None):
            if coin == "xyz:XYZ100":
                return None
            return await real(client, coin, tf, t0, t1, budget, now_ts)
        data.ensure_window = deny_bench
        try:
            out = await resolver.resolve_due(cl, b, T + 3 * 3600)               # geç ama bütçe → bekle
        finally:
            data.ensure_window = real
        oc = await _rows("SELECT status, net FROM strat_outcomes")
        assert all(o["status"] == "open" for o in oc) and out["outcomes"]["wait"] == 2, (oc, out)
        # kıyas okundu ama işlem yok (boş seri) → ölçülemedi, net'siz done değil
        out = await resolver.resolve_due(cl, b, T + 3 * 3600 + 60)
        oc = await _rows("SELECT status, exit_reason, net FROM strat_outcomes")
        assert all(o["status"] == "unresolvable" and o["exit_reason"] == "kıyasta işlem yok" for o in oc), oc
        # forward_clusters: eski kayıtlardaki net'siz "done" da ölçülemeyen sayılır
        async with dbm.db() as c:
            await c.execute("UPDATE strat_outcomes SET status='done', net=NULL")
            await c.execute("UPDATE strat_events SET status='done'")
        fc = await looks.forward_clusters("A", 1, registry.ACTIVE["A"]["spec"], T - 86400, T + 5 * 86400)
        assert fc["unres"] == 2 and fc["unres_share"] == 1.0 and fc["n_ev"] == 0, fc
    asyncio.run(run())
    print("✅ kıyas) bütçe → bekler; kıyasta işlem yok → ölçülemedi; net'siz done ölçülemeyen sayılır")


def test_event_measured_with_its_own_version():
    async def run():
        cfg = await core._fresh("res3.db")
        cfg.lab_k_budget = 10
        v1 = core._rule("X", horizons_s=[900], primary_h=900, exit={"tp": 0.5, "sl": 0.5, "timeout": 1800})
        await registry.sync(cfg, ts=T - 86400, rules=[v1])
        registry.emit("X", "xyz:AAA", T, side=1, trig_key="x1")
        await registry.flush(ts=T + 5)
        raw = {("xyz:AAA", "1m"): core.series(core.px, T - 3600, T + 4 * 3600),
               ("xyz:XYZ100", "1m"): core.series(core.bx, T - 3600, T + 4 * 3600)}
        cl = core.Client(raw)
        await resolver.resolve_due(cl, data.Budget(per_min=100_000), T + 300)
        v2 = core._rule("X", ver=2, exit=None, horizons_s=[3600], primary_h=3600)
        await registry.sync(cfg, ts=T + 400, rules=[v2])                       # v1 koddan kalktı
        out = await resolver.resolve_due(cl, data.Budget(per_min=100_000), T + 3 * 3600)
        oc = {o["h"]: o for o in await _rows("SELECT * FROM strat_outcomes")}
        assert set(oc) == {0, 900}, "v1 olayının ufukları v1'den (v2'nin 3600'ü değil)"
        assert oc[0]["status"] == "done" and oc[0]["exit_reason"] == "timeout" and oc[900]["status"] == "done"
        assert out["errors"] == 0
    asyncio.run(run())
    print("✅ sürüm) olay kendi donmuş tanımıyla ölçülür; yeni sürüm çıkış kuralını kaldırsa da tur çökmez")


def test_shutdown_flush_and_cancel_requeue():
    async def run():
        cfg = await core._fresh("res4.db")
        await registry.sync(cfg, ts=T, rules=[core._rule("A", max_events_day=100)])
        for i in range(25):
            registry.emit("A", f"xyz:C{i}", T + i, side=1, trig_key=str(i))
        assert registry.pending() == 25
        assert await lab_loop.shutdown_flush() == 25 and registry.pending() == 0
        assert len(await _rows("SELECT id FROM strat_events")) == 25
        # iptal: parti geri konur
        registry.emit("A", "xyz:Z", T + 99, side=1, trig_key="z")
        real = dbm.db

        class Boom:
            async def __aenter__(self):
                raise asyncio.CancelledError

            async def __aexit__(self, *a):
                return False
        registry.db = lambda: Boom()
        try:
            try:
                await registry.flush(ts=T + 100)
            except asyncio.CancelledError:
                pass
        finally:
            registry.db = real
        assert registry.pending() == 1, "iptalde parti kaybolmaz"
        src = open(os.path.join(ROOT, "app/main.py"), encoding="utf-8").read()
        assert "shutdown_flush()" in src
    asyncio.run(run())
    print("✅ kapanış) kuyruk yazılır; iptalde parti geri konur; lifespan kapanışta boşaltır")


def test_loop_prioritises_resolver_backlog():
    async def run():
        cfg = await core._fresh("res5.db")
        st = lab_loop.State()
        calls = []
        real_r, real_d = resolver.resolve_due, data.deep_fill_step

        async def fake_resolve(client, budget, t):
            calls.append(("r", t))
            return {"entries": {}, "outcomes": {}, "errors": 0, "backlog": 5 if len(calls) < 3 else 0}

        async def fake_deep(client, budget, coins, t, max_n=1):
            calls.append(("d", t))
            return {"filled": 0, "left": 0, "done": 0, "err": ""}
        resolver.resolve_due, data.deep_fill_step = fake_resolve, fake_deep
        try:
            for i in range(5):
                await lab_loop.step(cfg, core.Client({}), data.Budget(10_000), st, T + 30 * i)
        finally:
            resolver.resolve_due, data.deep_fill_step = real_r, real_d
        kinds = [k for k, _ in calls]
        assert kinds[:3] == ["r", "r", "r"], kinds
        assert "d" in kinds and kinds.index("d") >= 3, "derin dolum yalnız birikim bitince"
    asyncio.run(run())
    print("✅ döngü) iş birikince çözücü her turda; derin dolum yalnız birikim bitince")
