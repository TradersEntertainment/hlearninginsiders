"""🧪 Strateji laboratuvarı iskeleti (Faz 2–3) — kayıt, kuyruk, veri, çözücü, döngü, kablolama.

Pinlenenler:
  • metrics_hourly: saatlik özet bellekte toplanır, saat dolunca TEK satır (oraclePx, premium, funding
    ortalaması, OI$); ham tablo büyümez; eksik alan özeti düşürmez
  • lab_candles: kapanmış mum kapanmamış sürümüyle ezilmez; pencere isteği yalnız eksik kuyruğu çeker;
    bütçe izin vermezse None (eksik veriyle ölçülmez); bars kopyası yalnız 1h ve yalnız evren
  • kayıt: yeni kural yuva + α = LAB_ALPHA/K dondurulur; 'log' kuralı yuva/α almaz; tanım/ayar/maliyet
    hash'i değişirse emekli; K bütçesi dolarsa emekli; koddan kalkan emekli; durum izi lab_tests'te
  • emit: senkron (G/Ç yok), kapalı/bilinmeyen kural reddedilir, gün tavanı ve taşma SAYILIR,
    özellik 512 bayta kırpılır; flush aynı tetiği bir kez yazar
  • çözücü: giriş = karar + gecikme sonrası ilk mumun AÇILIŞI; ufuk çıkışı vade sonrası ilk açılış;
    piyasa düzeltmesi (β·kıyas); maliyet (seans içi hisse); h=0 TP içinden geçişte; işlem yoksa
    'ölçülemedi' (tutmadı değil); tüm ufuklar bitince olay 'done'
  • döngü: bir tur kayıt → boşalt → çöz → dolum; kapalıyken emit reddedilir; app/lab wake içe aktarmaz
"""
import ast
import asyncio
import inspect
import os
import sys
import tempfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-labcore.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.lab import data, registry, resolver, specs  # noqa: E402
from app.lab import loop as lab_loop  # noqa: E402

T = int(datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc).timestamp())    # Salı 10:00 ET (seans içi)


async def _fresh(name="labcore.db"):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), name))
    data._COVER.clear()
    data._SHORT.clear()
    data._UNIV.update(ts=0, coins={})
    registry._Q.clear()
    registry.ACTIVE.clear()
    registry._DAY.clear()
    registry._SEEN.clear()
    registry.CAP_DROPS.clear()
    for k in registry.STATS:
        registry.STATS[k] = 0
    cfg = Config()
    cfg.lab_k_budget, cfg.lab_alpha, cfg.lab_epoch = 4, 0.05, 1
    return cfg


def px(k):            # test hissesi: dakikada +0.01
    return 100 + 0.01 * k


def bx(k):            # kıyas: dakikada +%0.005
    return 1000 * (1 + 0.00005 * k)


def series(fn, t0, t1, tf=60, base=T):
    out = []
    for t in range(t0 // tf * tf, t1, tf):
        k = (t - base) // 60
        o, c = fn(k), fn(k + 1)
        out.append({"t": t * 1000, "o": str(o), "h": str(max(o, c)), "l": str(min(o, c)), "c": str(c),
                    "v": "10", "n": 5})
    return out


class Client:
    def __init__(self, raw_by_coin=None, weight=0, paused=0):
        self.raw = raw_by_coin or {}
        self.calls = []
        self.weight, self.paused = weight, paused

    async def candles(self, coin, interval, start_ms, end_ms):
        self.calls.append((coin, interval, start_ms // 1000, end_ms // 1000))
        return [x for x in self.raw.get((coin, interval), []) if start_ms <= x["t"] <= end_ms]

    def usage(self):
        return {"weight": self.weight, "weight_max": 1200}

    def low_paused(self):
        return self.paused


def test_hourly_metrics_accumulator():
    from app.radar import metrics
    acc, done = None, None
    rows = []
    for i, (ts, prem) in enumerate([(T + 10, 0.001), (T + 1800, None), (T + 3599, 0.003), (T + 3600, 0.002)]):
        acc, done = metrics.hour_acc(acc, ts, 100.0 + i, 99.0 + i, prem, 0.00001 * (i + 1), 5e6 + i, 1e8)
        if done:
            rows.append(done)
    assert len(rows) == 1, "saat dolunca tek satır"
    h, mark_c, oracle_c, prem_avg, fund_avg, oi_c, vol_c, n = rows[0]
    assert h == T and n == 3 and mark_c == 102.0 and oracle_c == 101.0 and oi_c == 5e6 + 2
    assert abs(prem_avg - 0.002) < 1e-12, "eksik premium ortalamaya girmez"
    assert abs(fund_avg - 0.00002) < 1e-12
    assert acc["h"] == T + 3600 and acc["n"] == 1

    async def run():
        cfg = await _fresh("lab_mh.db")
        cfg.crypto_metrics_enabled = True
        cfg.equity_dexes, cfg.crypto_dexes = [], []
        metrics._HOURLY.clear()

        class MC:
            async def meta_and_ctxs(self, dex):
                if dex:
                    return [{"universe": []}, []]
                return [{"universe": [{"name": "BTC"}]},
                        [{"markPx": "60000", "openInterest": "10", "funding": "0.0000125", "dayNtlVlm": "1e9",
                          "oraclePx": "59990", "premium": "0.0001"}]]
        real = metrics.now
        try:
            for t in (T + 5, T + 65, T + 3605):
                metrics.now = lambda t=t: t
                await metrics.poll_metrics(cfg, MC())
        finally:
            metrics.now = real
        async with dbm.db() as c:
            got = [dict(r) for r in await (await c.execute("SELECT * FROM metrics_hourly")).fetchall()]
            raw = (await (await c.execute("SELECT COUNT(*) n FROM asset_metrics")).fetchone())["n"]
        assert raw == 3 and len(got) == 1, (raw, got)
        g = got[0]
        assert g["coin"] == "BTC" and g["ts"] == T and g["n"] == 2 and g["oracle_c"] == 59990.0
        assert abs(g["oi_usd_c"] - 600000.0) < 1e-6 and abs(g["premium_avg"] - 0.0001) < 1e-12
    asyncio.run(run())
    print("✅ saatlik metrik) bellekte toplanır, saat dolunca tek satır; oraclePx/premium/funding/OI$; ham tablo aynı")


def test_candle_cache_window_budget_tee():
    async def run():
        await _fresh("lab_cc.db")
        rows = data.parse(series(px, T, T + 600), 60, T + 300)
        assert [r["closed"] for r in rows][:3] == [True, True, True] and rows[-1]["closed"] is False
        assert data.parse([{"t": (T + 30) * 1000, "o": 1, "h": 1, "l": 1, "c": 1}], 60, T + 999) == [], "ızgara dışı"
        await data.upsert("xyz:AAA", 60, rows)
        bad = [{**rows[0], "c": 1.0, "closed": False}]
        await data.upsert("xyz:AAA", 60, bad)
        got = await data.candles("xyz:AAA", 60, T, T)
        assert got and got[0]["c"] == rows[0]["c"], "kapanmış mum kapanmamışla ezilmez"

        now_ts = T + 3 * 3600
        cl = Client({("xyz:BBB", "1m"): series(px, T - 3600, now_ts)})
        b = data.Budget(per_min=10_000)
        r1 = await data.ensure_window(cl, "xyz:BBB", 60, T, T + 3600, b, now_ts)
        assert len(r1) == 61 and len(cl.calls) == 1 and b.spent_total > 20
        r2 = await data.ensure_window(cl, "xyz:BBB", 60, T + 600, T + 1200, b, now_ts)
        assert len(r2) == 11 and len(cl.calls) == 1, "kapsanan aralık yeniden çekilmez"
        await data.ensure_window(cl, "xyz:BBB", 60, T, T + 2 * 3600, b, now_ts)
        assert len(cl.calls) == 2 and cl.calls[1][2] >= T + 3600 - 120, ("yalnız eksik kuyruk", cl.calls)
        tight = data.Budget(per_min=10)
        assert await data.ensure_window(cl, "yyy:CCC", 60, T, T + 600, tight, now_ts) is None and tight.denied == 1
        busy = Client({}, weight=900)
        assert await data.ensure_window(busy, "xyz:DDD", 60, T, T + 600, data.Budget(10_000), now_ts) is None, \
            "paylaşılan pencere yarıdan doluysa bekler"
        data._UNIV.update(ts=now_ts, coins={"xyz:EEE": "hisse"})
        raw_h = series(px, T - 10 * 3600, T, tf=3600)
        assert await data.tee_from_bars("xyz:EEE", "1h", raw_h, T) == 10
        assert await data.tee_from_bars("xyz:EEE", "15m", raw_h, T) == 0, "15m kopyalanmaz"
        assert await data.tee_from_bars("xyz:ZZZ", "1h", raw_h, T) == 0, "evren dışı kopyalanmaz"
        assert data.bench_of("BTC", "kripto") is None and data.bench_of("ETH", "kripto") == "BTC"
        assert data.bench_of("xyz:NVDA", "hisse") == "xyz:XYZ100" and data.bench_of("xyz:GOLD", "endeks") is None
        assert data.bench_of("xyz:SP500", "endeks") == "xyz:XYZ100"
        n = await data.prune(T + 30 * 86400)
        assert n >= 61 and not await data.candles("xyz:BBB", 60, 0, 2 ** 40), "1m 7 günde budanır"
        assert await data.candles("xyz:EEE", 3600, 0, 2 ** 40), "1h süresiz"
    asyncio.run(run())
    print("✅ mum önbelleği) kapanmış ezilmez; yalnız eksik kuyruk çekilir; bütçe/paylaşılan pencere → erteler;"
          " bars kopyası yalnız 1h+evren; kıyas eşlemesi; 1m budanır, 1h kalır")


def _rule(rid, evidence="forward", ver=1, **kw):
    r = {"id": rid, "ver": ver, "family": "test", "title": f"deneme {rid}", "evidence": evidence,
         "metric": "net", "horizons_s": [900, 3600], "primary_h": 3600,
         "exit": {"tp": 0.001, "sl": 0.002, "timeout": 3600}, "latency_s": 60, "cluster_s": 86400,
         "looks": [0.5, 1.0], "n_max": 40, "min_g": 20, "bt_alpha_share": 0.25, "max_events_day": 3,
         "cfg_keys": []}
    r.update(kw)
    return r


def test_registry_sync_slots_hash():
    async def run():
        cfg = await _fresh("lab_reg.db")
        rules = [_rule("A"), _rule("B", "frozen_backtest"), _rule("L", "log"), _rule("C"), _rule("D"),
                 _rule("E")]
        out = await registry.sync(cfg, ts=T, rules=rules)
        async with dbm.db() as c:
            rows = {r["rule_id"]: dict(r) for r in await (await c.execute("SELECT * FROM lab_rules")).fetchall()}
        assert rows["A"]["slot"] == 1 and abs(rows["A"]["alpha"] - 0.05 / 4) < 1e-15 and rows["A"]["status"] == "kagit"
        assert rows["B"]["status"] == "aday" and rows["B"]["slot"] == 2
        assert rows["L"]["slot"] is None and rows["L"]["alpha"] == 0 and rows["L"]["status"] == "kagit"
        assert rows["E"]["status"] == "emekli" and "K bütçesi" in rows["E"]["status_note"], "5. yuva yok (K=4)"
        assert set(registry.ACTIVE) == {"A", "B", "L", "C", "D"} and out["slots_used"] == 4
        # α kayıtta donar: K değişse de eski kuralın α'sı aynı
        cfg.lab_k_budget = 100
        await registry.sync(cfg, ts=T + 10, rules=rules)
        async with dbm.db() as c:
            a = dict(await (await c.execute("SELECT alpha, registered_ts FROM lab_rules WHERE rule_id='A'")).fetchone())
        assert abs(a["alpha"] - 0.05 / 4) < 1e-15 and a["registered_ts"] == T
        # tanım değişti → emekli; yeni sürüm yeni yuva
        rules2 = [_rule("A", horizons_s=[900]), _rule("B", "frozen_backtest"), _rule("L", "log"),
                  _rule("C"), _rule("A", ver=2)]
        out2 = await registry.sync(cfg, ts=T + 20, rules=rules2)
        assert "A/v1" in out2["retired"] and "D/v1" in out2["retired"] and "A/v2" in out2["new"], out2
        assert registry.ACTIVE["A"]["ver"] == 2 and "D" not in registry.ACTIVE
        # bağlı ayar hash'e girer
        r3 = [_rule("S", cfg_keys=["sticky_min_usd"])]
        await registry.sync(cfg, ts=T + 30, rules=r3)
        assert "S" in registry.ACTIVE
        cfg.sticky_min_usd = 123.0
        out3 = await registry.sync(cfg, ts=T + 40, rules=r3)
        assert "S/v1" in out3["retired"] and "S" not in registry.ACTIVE, "ayar değişti → emekli"
        async with dbm.db() as c:
            n = (await (await c.execute("SELECT COUNT(*) n FROM lab_tests WHERE kind='status'")).fetchone())["n"]
        assert n >= 10, "durum izi lab_tests'te"
        await registry.set_status("A", 2, "canli", "sahip onayı", by="sahip", ts=T + 50)
        async with dbm.db() as c:
            r = dict(await (await c.execute("SELECT status, approved_ts FROM lab_rules WHERE rule_id='A' AND ver=2"))
                     .fetchone())
        assert r == {"status": "canli", "approved_ts": T + 50}
        sha_a = registry.spec_sha(_rule("A"))
        assert sha_a == registry.spec_sha(dict(reversed(list(_rule("A").items())))), "anahtar sırası fark etmez"
    asyncio.run(run())
    print("✅ kayıt) yuva + α kayıtta donar; log kuralı yuvasız; K dolunca emekli; tanım/ayar hash'i → emekli,"
          " yeni sürüm yeni yuva; koddan kalkan emekli; onay izi")


def test_emit_sync_cap_overflow_flush():
    async def run():
        cfg = await _fresh("lab_emit.db")
        await registry.sync(cfg, ts=T, rules=[_rule("A")])
        assert not inspect.iscoroutinefunction(registry.emit), "emit senkron"
        assert registry.emit("YOK", "BTC", T) is False and registry.STATS["rejected"] == 1
        for i in range(6):
            registry.emit("A", "xyz:AAA", T + i, side=1, px_ref=100.0, trig_key=f"k{i}",
                          features={"usd": 1e6, "uzun": "x" * 2000})
        assert registry.pending() == 3 and registry.STATS["cap_drop"] == 3 and registry.CAP_DROPS["A"] == 3
        assert registry.emit("A", "xyz:AAA", T + 9, side=1, trig_key="k0") is False and registry.STATS["seen"] == 1, \
            "aynı tetik bellekte tekil — gün tavanını tekrarlar doldurmaz"
        assert registry.emit("A", "xyz:AAA", T + 86400, side=-1) is True, "ertesi gün tavan sıfır"
        ev = registry._Q[0]
        assert len(ev["features"].encode()) <= registry.FEATURES_MAX and "_kirpildi" in ev["features"]
        assert ev["latency_s"] == 60 and ev["cluster"] == T // 86400
        registry._SEEN.clear()                                   # süreç yeniden başladı: bellek boş
        registry._DAY.clear()
        registry.emit("A", "xyz:AAA", T + 50, side=1, trig_key="k0")
        wrote = await registry.flush(ts=T + 100)
        assert wrote == 4 and registry.STATS["dup"] == 1, "aynı tetik anahtarı DB'de de bir kez"
        async with dbm.db() as c:
            rows = [dict(r) for r in await (await c.execute("SELECT * FROM strat_events ORDER BY id")).fetchall()]
        assert rows[0]["klass"] == "hisse" and rows[0]["bench"] == "xyz:XYZ100" and rows[0]["status"] == "pending"
        old = registry.QUEUE_MAX
        try:
            registry._Q = __import__("collections").deque(maxlen=2)
            registry.QUEUE_MAX = 2
            for i in range(3):
                registry.emit("A", f"C{i}", T + 5 * 86400, trig_key=str(i))
            assert registry.STATS["overflow"] == 1 and len(registry._Q) == 2
        finally:
            registry.QUEUE_MAX = old
            registry._Q = __import__("collections").deque(maxlen=old)
    asyncio.run(run())
    print("✅ emit) senkron; bilinmeyen reddedilir; gün tavanı + taşma sayılır; özellik 512 bayt; flush tekil")


def test_resolver_entry_exit_adjust_cost():
    async def run():
        cfg = await _fresh("lab_res.db")
        await registry.sync(cfg, ts=T - 86400, rules=[_rule("A")])
        registry.emit("A", "xyz:TEST", T, side=1, px_ref=100.0, trig_key="t1")
        registry.emit("A", "xyz:EMPTY", T, side=1, trig_key="t2")
        await registry.flush(ts=T + 5)
        cl = Client({("xyz:TEST", "1m"): series(px, T - 3600, T + 4 * 3600),
                     ("xyz:XYZ100", "1m"): series(bx, T - 3600, T + 4 * 3600)})
        b = data.Budget(per_min=100_000)
        out = await resolver.resolve_due(cl, b, T + 2 * 3600)
        assert out["entries"]["opened"] == 1 and out["entries"]["unres"] == 1, out
        async with dbm.db() as c:
            ev = {r["coin"]: dict(r) for r in await (await c.execute("SELECT * FROM strat_events")).fetchall()}
            oc = {r["h"]: dict(r) for r in await (await c.execute("SELECT * FROM strat_outcomes")).fetchall()}
        e = ev["xyz:TEST"]
        assert e["entry_ts"] == T + 60 and abs(e["entry_px"] - px(1)) < 1e-9 and e["entry_src"] == "1m"
        assert e["beta"] == 1.0, "geçmiş 1h yok → β 1"
        assert ev["xyz:EMPTY"]["status"] == "unresolvable" and "işlem yok" in ev["xyz:EMPTY"]["status_note"]
        o15 = oc[900]
        assert o15["exit_ts"] == T + 960 and abs(o15["exit_px"] - px(16)) < 1e-9
        r_raw = px(16) / px(1) - 1
        r_b = bx(16) / bx(1) - 1
        assert abs(o15["ret_raw"] - r_raw) < 1e-12 and abs(o15["ret_bench"] - r_b) < 1e-12
        assert abs(o15["ret_adj"] - (r_raw - r_b)) < 1e-12
        assert abs(o15["cost"] - 0.0028) < 1e-12, "hisse seans içi: ücret 0.0018 + kayma 0.0010, saat sınırı yok"
        assert abs(o15["net"] - (r_raw - r_b - 0.0028)) < 1e-12
        assert o15["mae"] == 0.0 and o15["mfe"] > 0
        o0 = oc[0]
        assert o0["status"] == "done" and o0["exit_reason"] == "tp" and o0["exit_ts"] == T + 660
        assert abs(o0["exit_px"] - px(1) * 1.001) < 1e-9, "TP seviyesinden (içinden geçti)"
        o60 = oc[3600]
        assert o60["status"] == "done" and abs(o60["cost"] - (0.0028 + 0.00002)) < 1e-12, \
            "bir saat sınırı geçildi, metrics_hourly yok → en kötü funding sınırı"
        async with dbm.db() as c:
            await c.execute("INSERT INTO metrics_hourly(coin, ts, funding_avg) VALUES('xyz:TEST', ?, -0.0001)", (T,))
        assert await resolver.funding_list("xyz:TEST", T + 60, T + 3660) == [-0.0001]
        assert await resolver.funding_list("xyz:TEST", T + 60, T + 960) == [], "saat sınırı yok → funding yok"
        assert resolver.off_hours("hisse", T, T + 600) is False and resolver.off_hours("kripto", T - 86400 * 2, T) is False
        assert resolver.off_hours("hisse", T - 6 * 3600, T) is True, "04:00 ET seans dışı"
        async with dbm.db() as c:
            st = (await (await c.execute("SELECT status FROM strat_events WHERE coin='xyz:TEST'")).fetchone())["status"]
        assert st == "done", "tüm ufuklar sonuçlandı"
    asyncio.run(run())
    print("✅ çözücü) giriş karar+gecikme sonrası ilk açılış; ufuk vade sonrası açılış; β·kıyas düzeltmesi;"
          " seans içi maliyet; TP içinden geçişte seviyeden; işlemsiz → ölçülemedi; olay done")


def test_resolver_waits_and_side_zero():
    async def run():
        cfg = await _fresh("lab_res2.db")
        await registry.sync(cfg, ts=T - 86400, rules=[_rule("A", exit=None, horizons_s=[900], primary_h=900)])
        registry.emit("A", "BTC", T, side=0, trig_key="z")
        await registry.flush(ts=T + 5)
        cl = Client({("BTC", "1m"): series(px, T - 3600, T + 600)})
        b = data.Budget(per_min=100_000)
        out = await resolver.resolve_due(cl, b, T + 600)
        assert out["entries"]["opened"] == 1 and out["outcomes"]["done"] == 0, out
        async with dbm.db() as c:
            rows = [dict(r) for r in await (await c.execute("SELECT * FROM strat_outcomes")).fetchall()]
        assert [r["h"] for r in rows] == [900], "yönsüz olayda h=0 (çıkış kuralı) yok"
        out = await resolver.resolve_due(Client({}), data.Budget(per_min=10), T + 1100)
        assert out["outcomes"]["wait"] == 1, "bütçe yok → bekler, uydurmaz"
    asyncio.run(run())
    print("✅ çözücü) vade gelmeden ölçmez; bütçe yokken bekler; yönsüz olayda birim işlem satırı yok")


def test_loop_step_and_disabled():
    async def run():
        cfg = await _fresh("lab_loop.db")
        st = lab_loop.State()
        b = data.Budget(per_min=100_000)
        await lab_loop.step(cfg, Client({}), b, st, T)
        assert st.synced and set(registry.ACTIVE) == {r["id"] for r in specs.RULES}
        assert all(r["evidence"] == "log" for r in specs.RULES), "ilk dalga yalnız kayıt (yuva harcamaz)"
        stats = await dbm.kv_get(lab_loop.STATS_KV)
        assert stats["reg"]["active"] == len(specs.RULES) and stats["ts"] == T
        assert registry.emit("LOG-VOL", "BTC", T, side=1) is True
        await lab_loop.step(cfg, Client({}), b, st, T + 30)
        assert registry.pending() == 0
        cfg.lab_enabled = False
        await lab_loop.step(cfg, Client({}), b, st, T + 60)
        assert registry.emit("LOG-VOL", "BTC", T + 61, side=1) is False, "kapalı → emit reddedilir"
        cfg.lab_enabled = True
        await lab_loop.step(cfg, Client({}), b, st, T + 90)
        assert registry.emit("LOG-VOL", "BTC", T + 91, side=1) is True
    asyncio.run(run())
    print("✅ döngü) tur: kayıt → boşalt → çöz → dolum → durum kv; kapalıyken emit reddedilir, açınca sürer")


def test_beta_grid_and_cost_class():
    r, rb = resolver.hourly_returns({0: 100.0, 3600: 101.0, 10800: 102.0}, {0: 10.0, 3600: 10.0, 7200: 11.0, 10800: 11.0})
    assert len(r) == len(rb) == 3 and r[1] == 0.0 and abs(rb[1] - 0.1) < 1e-12, "işlemsiz saat önceki kapanışla"
    assert resolver.hourly_returns({}, {0: 1.0}) == ([], [])
    assert resolver.cost_class("para:ANSEM", "kripto") == "hip3_kripto" and resolver.cost_class("BTC", "kripto") == "kripto"
    from app.lab import costs
    c = costs.cost(resolver.cost_class("para:ANSEM", "kripto"), False, 600, 1, [])
    assert abs(c["fee"] - costs.COSTS["hisse"]["fee_rt"]) < 1e-15, "HIP-3 kripto ana dex ücretiyle değil"
    print("✅ β ızgarası bitişik (işlemsiz saat 0 getiri); HIP-3 kripto HIP-3 yer tutucusuyla ücretlenir")


def test_every_log_family_has_an_emit_site():
    """Her akış ailesinin kodda bir kayıt noktası var; kayıt noktaları senkron ve korunaklı (try)."""
    import glob
    src = {f: open(f, encoding="utf-8").read() for f in glob.glob(os.path.join(ROOT, "app", "**", "*.py"),
                                                                    recursive=True) if "/lab/" not in f}
    for r in specs.RULES:
        sites = [f for f, s in src.items() if f'"{r["id"]}"' in s]
        assert sites, f"{r['id']} için kayıt noktası yok"
    for name in ("autoscan.py", "collector.py", "twaplive.py", "stickywall.py", "anomaly.py", "cryptovol.py",
                 "openmove.py", "cryptoliq.py"):
        f = next(f for f in src if f.endswith("/" + name))
        assert "lab.registry import emit" in src[f], name
    assert all(r["ver"] >= 2 and r["latency_s"] <= 60 for r in specs.RULES), "görme anı + tepki süresi"
    print("✅ kayıt noktaları) 8 akış ailesinin her biri kodda; v2 gecikme = tepki süresi")


def test_lab_never_imports_wake_and_wiring():
    lab_dir = os.path.join(ROOT, "app", "lab")
    for f in sorted(os.listdir(lab_dir)):
        if not f.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(lab_dir, f), encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [a.name for a in node.names]
            assert not any("wake" in (n or "") for n in names), f"app/lab/{f} wake içe aktarıyor — strateji ARAMAZ"
    main = open(os.path.join(ROOT, "app", "main.py"), encoding="utf-8").read()
    assert '_spawn("lab"' in main
    from app import health
    from app.telegram import format as fmt
    cfg = Config()
    assert health.limits(cfg)["lab"] >= 600 and health.periods(cfg)["lab"] == 30 and "lab" in fmt.TASK_TR
    sw = open(os.path.join(ROOT, "app", "radar", "sweeper.py"), encoding="utf-8").read()
    for t in ("strat_events", "strat_outcomes", "lab_tests", "lab_rules", "metrics_hourly"):
        assert f'_chunked_delete("{t}"' not in sw, f"{t} budanmaz"
    bars = open(os.path.join(ROOT, "app", "radar", "bars.py"), encoding="utf-8").read()
    assert "tee_from_bars" in bars
    from app.config import EDITABLE_FIELDS
    assert "lab_enabled" in EDITABLE_FIELDS and "seans_archive_all" in EDITABLE_FIELDS
    assert "lab_alpha" not in EDITABLE_FIELDS and "lab_k_budget" not in EDITABLE_FIELDS, "α/K yalnız env"
    print("✅ kablolama) app/lab wake içe aktarmaz; spawn, sağlık, görev adı; lab tabloları budanmaz;"
          " bars kopyası; α/K sayfadan oynatılamaz")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
