"""🧪 Örüntü denetimi (R3) — pattern_signals satırlarının dürüst, maliyet sonrası sonucu.

Pinlenenler:
  • aynı sorgu barı iki kez yazıldıysa tek sayılır; örtüşen pencereler inceltilir
  • "uyarılabilir" küme donmuş eşiklerle (n ≥ 20, |z| ≥ 2, |fark| ≥ 10) — ayardan bağımsız
  • giriş satır yazıldıktan SONRA kapanan ilk barın kapanışı (kayıtlı px değil); çıkış resolve_ts'te
    açılan barın kapanışı; yön farkın işareti; maliyet düşülür
  • ölçülemeyen satır "tutmadı" sayılmaz, ayrı sayılır; fiyatı olmayan satır da
  • Aşama A / ileri ayrımı satırın yazıldığı ana göre (since/until); vadesi dolmamış satır sayılmaz
  • piyasa yönü ödüllendirilmez: yükselen piyasada her şeye "yukarı" diyen tablo fark yönünde
    (p_up − taban) değerlendirilir
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-labaudit.db")
from app import db as dbm  # noqa: E402
from app.lab import audit, costs  # noqa: E402

H = 3600
T0 = 1_791_000_000 // H * H


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "audit.db"))


async def _bars(coin, tf, px_of, n, start=T0):
    async with dbm.db() as c:
        await c.executemany("INSERT INTO bars(coin, tf, ts, c, v) VALUES(?,?,?,?,0)",
                            [(coin, tf, start + i * H, px_of(i)) for i in range(n)])


async def _sig(coin, ts, horizon, last_ts, edge, z=2.5, n=30, status="hit", p_up=None, base=50.0):
    async with dbm.db() as c:
        await c.execute(
            "INSERT INTO pattern_signals(ts, coin, tf, win, horizon, n_match, p_up, base_up, edge, z, px, resolve_ts,"
            " status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ts, coin, "1h", 24, horizon, n, p_up if p_up is not None else base + edge, base, edge, z, 1.0,
             last_ts + horizon * H, status))


def test_dedupe_entry_exit_cost():
    async def run():
        await _fresh()
        await _bars("BTC", "1h", lambda i: 100.0 + i, 200)
        # sorgu barı T0+10h; satır 10h+40dk'da yazıldı → giriş 11h kapanışı (T0+11h barının açılışı 10h? hayır:
        # kapanışı ≥ ts olan ilk bar T0+10h barı (kapanış 11h) → fiyat 110)
        await _sig("BTC", T0 + 10 * H + 2400, 6, T0 + 10 * H, edge=+12)
        await _sig("BTC", T0 + 10 * H + 2500, 6, T0 + 10 * H, edge=+12)          # aynı sorgu barı: mükerrer
        await _sig("BTC", T0 + 12 * H + 100, 6, T0 + 12 * H, edge=+12)          # örtüşen pencere: incelir
        await _sig("BTC", T0 + 30 * H + 100, 6, T0 + 30 * H, edge=-15)          # ayrı pencere, aşağı yön
        await _sig("BTC", T0 + 50 * H + 100, 6, T0 + 50 * H, edge=+4)           # uyarılabilir değil (fark küçük)
        await _sig("BTC", T0 + 70 * H + 100, 6, T0 + 70 * H, edge=+20, status="unresolvable")
        spec = {"set": "alertable", "cluster_s": 2 * 86400}
        out = await audit.clusters(spec, ts=T0 + 199 * H)
        assert out["n_ev"] == 2 and out["unres"] == 1 and out["n_all"] == 3, out
        c = costs.cost("kripto", False, 6 * H, 1, None)["total"]
        up = (116 / 110 - 1) - c                       # giriş 10h barının kapanışı 110, çıkış 16h barının 116
        dn = -(136 / 130 - 1) - costs.cost("kripto", False, 6 * H, -1, None)["total"]
        got = sorted(out["xc"].tolist())
        exp = sorted([up, dn]) if len(got) == 2 else [(up + dn) / 2]
        assert all(abs(a - b) < 1e-12 for a, b in zip(got, exp)), (got, exp)
        allset = await audit.clusters({"set": "all", "cluster_s": 2 * 86400}, ts=T0 + 199 * H)
        assert allset["n_ev"] == 3, "küçük fark 'all' kümesinde"
        early = await audit.clusters(spec, ts=T0 + 17 * H)
        assert early["n_ev"] == 0, "vadesi dolmamış (resolve + 2 bar) sayılmaz"
        a_only = await audit.clusters(spec, until=T0 + 20 * H, ts=T0 + 199 * H)
        b_only = await audit.clusters(spec, since=T0 + 20 * H, ts=T0 + 199 * H)
        assert a_only["n_ev"] == 1 and b_only["n_ev"] == 1, "Aşama A / ileri ayrımı yazılma anına göre"
    asyncio.run(run())
    print("✅ denetim) mükerrer sorgu barı tek; örtüşen incelir; giriş yazıldıktan sonraki kapanış; çıkış vade"
          " barı; maliyet; ölçülemeyen ayrı; donmuş uyarı eşiği; A/ileri ayrımı")


def test_market_drift_not_rewarded():
    """Yükselen piyasa: p_up hep > 50 ama taban da yüksek → fark negatif olan satırlar 'aşağı' oynar.
    Eski sicil (p_up ≥ 50 = isabet) bunları isabet sayardı; denetim fark yönünde zarar yazar."""
    async def run():
        await _fresh()
        await _bars("ETH", "1h", lambda i: 100.0 * (1.002 ** i), 400)
        for k in range(10):
            last = T0 + (10 + k * 20) * H
            await _sig("ETH", last + 60, 6, last, edge=-12, p_up=58.0, base=70.0)   # p_up ≥ 50 ama fark < 0
        out = await audit.clusters({"set": "alertable", "cluster_s": 2 * 86400}, ts=T0 + 399 * H)
        assert out["n_ev"] == 10 and all(x < 0 for x in out["xc"].tolist()), out["xc"]
    asyncio.run(run())
    print("✅ denetim) piyasa yönü ödüllendirilmez: p_up ≥ 50 ama fark < 0 → aşağı yönde, yükselen piyasada zarar")


def test_stage_a_once_after_wait():
    """Örüntü denetimi kuralı: Aşama A kayıttan 2 gün sonra BİR kez (UNIQUE), kayıttan önceki satırlarla;
    güçlü etki → kagit (ileri bakışlar başlar); sonuç /strat ayrıntısında 'seçim örneği' etiketiyle."""
    async def run():
        import numpy as np
        from app.config import Config
        from app.lab import looks, registry, specs, ui
        from app.telegram import format as fmt
        await _fresh()
        rng = np.random.default_rng(2)
        steps = 1.0 + rng.normal(0.001, 0.001, 1300)
        path = 100.0 * np.cumprod(steps)
        await _bars("BTC", "1h", lambda i: float(path[i]), 1300)
        for k in range(24):                                   # 2 günde bir sinyal → 24 ayrı küme
            last = T0 + (10 + k * 48) * H
            await _sig("BTC", last + 60, 12, last, edge=+15)
        reg_ts = T0 + 1250 * H
        cfg = Config()
        cfg.lab_k_budget, cfg.lab_alpha, cfg.lab_epoch = 2, 0.05, 1
        oru = [r for r in specs.RULES if r.get("custom") == "oru"]
        registry._SEEN.clear()
        await registry.sync(cfg, ts=reg_ts, rules=oru)
        assert {registry.ACTIVE[r["id"]]["status"] for r in oru} == {"aday"}
        assert await looks.run(reg_ts + 86400) == [], "2 gün dolmadan Aşama A yok"
        out = await looks.run(reg_ts + 2 * 86400 + 60)
        by = {r["rule_id"]: r for r in out}
        assert by["ORU-ALERT-F"]["look"] == 0 and by["ORU-ALERT-F"]["n_c"] == 24, by
        assert registry.ACTIVE["ORU-ALERT-F"]["status"] == "kagit", by["ORU-ALERT-F"]
        assert await looks.run(reg_ts + 2 * 86400 + 3600) == [], "Aşama A bir kez"
        async with dbm.db() as c:
            n = (await (await c.execute("SELECT COUNT(*) n FROM lab_tests WHERE kind='backtest'")).fetchone())["n"]
        assert n == 2
        ui._CACHE.update(ts=0, v=None)
        v = await ui.overview(reg_ts + 2 * 86400 + 3700)
        r = next(x for x in v["rules"] if x["rule_id"] == "ORU-ALERT-F")
        det = fmt.strat_detail(r, "ORU-ALERT-F")
        assert "Aşama A (geçmiş — seçim örneği): 24 olay / 24 küme" in det and "örüntü sinyalleri üzerinden" in det
    asyncio.run(run())
    print("✅ denetim kuralı) Aşama A 2 gün sonra bir kez; güçlü etki → kâğıt; /strat'ta seçim örneği etiketi")
