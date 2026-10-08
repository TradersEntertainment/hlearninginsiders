"""🧪 Laboratuvar arayüzü (Faz 6) — /lab sayfası, /strat komutu, tanım denetimi.

Pinlenenler:
  • overview yalnız betimler: kayıttan sonraki (ileri) veri ile geçmiş (seçim örneği) AYRI; birincil
    ufuk işaretli; küme < 20 uyarısı; durum değiştirmez (lab_rules dokunulmaz)
  • /lab sayfası ham ASGI'da 200; kural, ileri sütun, kayıt izi, "strateji asla aramaz"; ?rule= son olaylar
  • /strat kartı ve ayrıntısı: ölçülü dil, Telegram'a ?key= sızmaz, bilinmeyen kural nazik yanıt
  • /strat sahip zincirinde ve botun kendi kanallarında (coin aramasına düşmez)
  • tanım denetimi: hatalı kural yuva harcamaz, 'emekli (tanım geçersiz)' kaydolur
"""
import asyncio
import importlib.util
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-labui.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.lab import data, registry, resolver, specs, ui  # noqa: E402

_spec = importlib.util.spec_from_file_location("lab_core_t", os.path.join(HERE, "test_lab_core.py"))
core = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(core)
_spec2 = importlib.util.spec_from_file_location("routes_smoke_t", os.path.join(HERE, "test_routes_smoke.py"))
smoke = importlib.util.module_from_spec(_spec2)
_spec2.loader.exec_module(smoke)
T = core.T


async def _with_data(name):
    cfg = await core._fresh(name)
    ui._CACHE.update(ts=0, v=None)
    await registry.sync(cfg, ts=T - 86400, rules=[core._rule("A"), core._rule("BAD", n_max=10)])
    registry.emit("A", "xyz:TEST", T, side=1, px_ref=100.0, trig_key="t1")
    await registry.flush(ts=T + 5)
    cl = core.Client({("xyz:TEST", "1m"): core.series(core.px, T - 3600, T + 4 * 3600),
                      ("xyz:XYZ100", "1m"): core.series(core.bx, T - 3600, T + 4 * 3600)})
    await resolver.resolve_due(cl, data.Budget(per_min=100_000), T + 2 * 3600)
    return cfg


def test_validate_and_overview():
    async def run():
        await _with_data("labui1.db")
        async with dbm.db() as c:
            rows = {r["rule_id"]: dict(r) for r in await (await c.execute("SELECT * FROM lab_rules")).fetchall()}
        assert rows["BAD"]["status"] == "emekli" and rows["BAD"]["slot"] is None
        assert "tanım geçersiz" in rows["BAD"]["status_note"] and "ilk bakış" in rows["BAD"]["status_note"]
        assert rows["A"]["slot"] == 1, "geçersiz kural yuva harcamadı"
        assert specs.validate(core._rule("X", cluster_s=600)), "küme < ufuk reddedilir"
        assert specs.validate(core._rule("X", metric="barrier")), "barrier TP+SL+h0 ister"
        assert specs.validate(core._rule("X", looks=[1.0, 0.5])), "bakışlar artan"
        assert any("barrier" in e for e in specs.validate(core._rule("X", metric="barrier", primary_h=0))), \
            "barrier metriği küme düzeyine geçene dek kapalı"
        before = dict(rows["A"])
        v = await ui.overview(T + 3 * 3600)
        a = next(r for r in v["rules"] if r["rule_id"] == "A")
        assert a["counts"]["n"] == 1 and a["counts"].get("done") == 1
        labels = [h["label"] for h in a["horizons"]]
        assert labels == ["15 dk", "1 sa", "çıkış kuralı"], labels
        prim = next(h for h in a["horizons"] if h["primary"])
        assert prim["label"] == "1 sa" and prim["fwd"]["n_ev"] == 1 and prim["fwd"]["n_c"] == 1
        assert prim["bt"]["n_ev"] == 0, "geçmiş sütunu ayrı (origin=backfill)"
        assert v["n_active"] == 1 and v["n_live"] == 0 and v["note"] == ui.NOTE
        async with dbm.db() as c:
            after = dict(await (await c.execute("SELECT * FROM lab_rules WHERE rule_id='A'")).fetchone())
        assert after == before, "betimleme durum değiştirmez"
        evs = await ui.recent_events("A")
        assert evs and evs[0]["outs"] and evs[0]["entry_src"] == "1m"
    asyncio.run(run())
    print("✅ görünüm) hatalı tanım yuva harcamaz; ileri/geçmiş ayrı; birincil ufuk; durum değişmez")


def test_strat_text():
    async def run():
        from app.telegram import format as fmt
        await _with_data("labui2.db")
        v = await ui.overview(T + 3 * 3600)
        card = fmt.strat_card(v, base_url="https://x.test")
        assert "🧪 <b>Strateji laboratuvarı</b> · 1 kural etkin · canlı 0" in card
        assert "<b>A</b> v1" in card and "ileri, 1 sa: 1 olay / 1 küme" in card and "en az 20 gerekir" in card
        assert "🔗 https://x.test/lab" in card and "key=" not in card
        assert "1 emekli kural" in card and "strateji asla aramaz" in card
        a = next(r for r in v["rules"] if r["rule_id"] == "A")
        det = fmt.strat_detail(a, "A", base_url="https://x.test")
        assert "Yuva 1 · α 0.01250 (kayıtta dondu)" in det and "<b>1 sa</b> (birincil)" in det and "/lab?rule=A" in det
        assert "key=" not in det
        assert "adlı kural yok" in fmt.strat_detail(None, "ZZZ")
        bad = re.compile(r"ihtimal|tavsiye ed|fırsat|kesin|yükselecek|düşecek")
        assert not bad.search(card) and not bad.search(det)
        assert len(card) <= 4000
    asyncio.run(run())
    print("✅ /strat) kart + ayrıntı ölçülü; ?key= yok; bilinmeyen kural nazik")


def test_lab_page_route():
    async def run():
        app = await smoke._fresh()
        ui._CACHE.update(ts=0, v=None)
        cfg = app.state.cfg
        await registry.sync(cfg, ts=T - 86400, rules=[core._rule("A")])
        registry.emit("A", "xyz:TEST", T, side=1, trig_key="p1")
        await registry.flush(ts=T + 5)
        st, body = await smoke.get(app, "/lab")
        assert st == 200, (st, body[:300])
        assert "Strateji laboratuvarı" in body and "deneme A" in body and "Kayıttan sonra (ileri veri)" in body
        assert "strateji asla aramaz" in body and "Kayıt izi" in body
        st, body = await smoke.get(app, "/lab", b"rule=A")
        assert st == 200 and "A · son olaylar" in body and "TEST" in body
        base = open(os.path.join(ROOT, "app/web/templates/base.html"), encoding="utf-8").read()
        assert "nl('/lab'" in base
    asyncio.run(run())
    print("✅ /lab) sayfa 200, ileri sütun, kayıt izi; ?rule= son olaylar; nav")


def test_strat_routing():
    src = open(os.path.join(ROOT, "app/telegram/bot.py"), encoding="utf-8").read()
    assert src.count('elif cmd in ("strat", "strateji", "stratejiler"):') == 2, "sahip zinciri + kendi kanallar"
    i_track = src.index("async def _dispatch_track")
    i_strat = src.index('elif cmd in ("strat"', i_track)
    assert i_strat > i_track, "_dispatch_track içinde (coin aramasından önce)"
    fsrc = open(os.path.join(ROOT, "app/telegram/format.py"), encoding="utf-8").read()
    assert '"/strat — 🧪 strateji laboratuvarı' in fsrc, "yardım metni"
    print("✅ /strat yönlendirme) sahip zinciri + kendi kanallar (coin aramasına düşmez)")


# ---------------- sinyale ne kaldı · açık pozisyonlar · kâğıt hesap · işlemler · veri sağlığı ----------------

D = 86400


async def _seed_done(rid, ver, reg_ts, days, net=0.004, coin="xyz:TEST", h=3600, start_day=1):
    """Kayıttan sonra her gün bir olay (küme = gün), birincil ufuk ölçülmüş — kapının saydığı biçimde."""
    async with dbm.db() as c:
        for d in range(start_day, start_day + days):
            t = (reg_ts // D + d) * D + 3600
            cur = await c.execute(
                "INSERT INTO strat_events(rule_id, rule_ver, origin, coin, klass, ts_decision, trig_key, side, latency_s,"
                " entry_ts, entry_px, cluster, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, ver, "live", coin, "hisse", t, f"s{d}", 1, 60, t + 60, 100.0, t // D, "done"))
            await c.execute(
                "INSERT INTO strat_outcomes(event_id, h, due_ts, status, exit_ts, exit_px, ret_raw, ret_bench, ret_adj,"
                " cost, net) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (cur.lastrowid, h, t + 60 + h, "done", t + 60 + h, 100.0 * (1 + net + 0.001), net + 0.001, 0.0,
                 net + 0.001, 0.001, net))


def test_progress_counts_not_stats():
    async def run():
        from app.lab import gate, looks
        cfg = await core._fresh("labui_prog.db")
        ui._CACHE.update(ts=0, v=None)
        reg = T - 30 * D
        await registry.sync(cfg, ts=reg, rules=[core._rule("A"), core._rule("L", evidence="log"),
                                                core._rule("FB", evidence="frozen_backtest")])
        await _seed_done("A", 1, reg, 5)
        ts = reg + 10 * D
        v = await ui.overview(ts)
        by = {r["rule_id"]: r for r in v["rules"]}
        a = by["A"]
        fc = await looks.clusters_for("A", 1, a["spec_d"], a["registered_ts"], ts)
        assert len(fc["xc"]) == 5, "kapının saydığı küme"
        pg = a["progress"]
        assert pg["line"].startswith("1. bakışa 15 küme kaldı (5/20)"), pg["line"]
        assert "bu hızla en erken" in pg["line"] and pg["rank"] and pg["rank"] > 0
        z1 = ui._z(gate.alpha_increments({**a["spec_d"], "alpha": a["alpha"]})[0])
        step = next(s for s in pg["steps"] if s["label"] == "1. bakış")
        assert step["state"] == "now" and step["note"] == f"5/20 küme · eşik z ≥ {z1:.2f}", step
        assert [s["label"] for s in pg["steps"]] == ["kayıt", "1. bakış", "2. bakış", "sahip onayı", "canlı (mesaj)"]
        assert set(pg) <= {"line", "steps", "rank", "guard", "pipe", "unres_share"}, "ara p / z / ortalama yok"
        assert "p " not in pg["line"] and "ortalama" not in pg["line"]
        assert by["L"]["progress"]["line"] == "yalnız kayıt — sinyal üretemez (yuvasız)"
        fb = by["FB"]["progress"]
        assert fb["line"].startswith("Aşama A (geçmiş veri testi)") and "Aşama A (geçmiş)" in [s["label"] for s in fb["steps"]]
        assert v["closest"] and v["closest"]["rule_id"] in ("A", "FB")
        empty = await ui.progress({**a, "registered_ts": ts - 3600}, a["spec_d"], ts)
        assert "henüz kapanmış küme yok" in empty["line"]
        from app.telegram import format as fmt
        card = fmt.strat_card(v)
        assert "⏳ 1. bakışa 15 küme kaldı (5/20)" in card and "Sinyale en yakın:" in card
        assert "⏳ yalnız kayıt" not in card, "log kuralı kartta ilerleme satırı almaz"
    asyncio.run(run())
    print("✅ ne kaldı) kapının saydığı küme; sıradaki eşik ve z; bu hızla en erken; ara p/z yok; log/Aşama A ayrı")


def test_open_positions_and_paper():
    async def run():
        from app.hl.universe import MAIN_CTX_KV
        from app.lab import costs
        cfg = await core._fresh("labui_pos.db")
        ui._CACHE.update(ts=0, v=None)
        reg = T - 30 * D
        await registry.sync(cfg, ts=reg, rules=[core._rule("A")])
        ts = T
        async with dbm.db() as c:
            for rid, coin, side, eid in (("A", "BTC", 1, 1), ("A", "xyz:TEST", -1, 2), ("A", "ZZZ", 1, 3),
                                         ("LOG-TWAP", "ETH", 1, 4)):
                await c.execute(
                    "INSERT INTO strat_events(id, rule_id, rule_ver, origin, coin, klass, ts_decision, trig_key, side,"
                    " entry_ts, entry_px, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, rid, 1 if rid == "A" else 2, "live", coin, "kripto" if coin in ("BTC", "ZZZ", "ETH") else "hisse",
                     ts - 1800, f"o{eid}", side, ts - 1700, 100.0, "open"))
                await c.execute("INSERT INTO strat_outcomes(event_id, h, due_ts, status) VALUES(?,?,?,?)",
                                (eid, 3600, ts - 1700 + 3600, "open"))
            await c.execute("INSERT INTO strat_events(rule_id, rule_ver, origin, coin, ts_decision, trig_key, status)"
                            " VALUES('A', 1, 'live', 'SOL', ?, 'p1', 'pending')", (ts,))
            await c.execute("INSERT INTO asset_metrics(coin, ts, mark_px) VALUES('xyz:TEST', ?, 98.0)", (ts - 120,))
        await dbm.kv_set(MAIN_CTX_KV, {"c": {"BTC": {"m": 102.0}}, "ts": ts - 30})
        pos = await ui.open_positions(ts)
        by = {r["coin"]: r for r in pos["rows"]}
        assert set(by) == {"BTC", "xyz:TEST", "ZZZ"} and pos["pending"] == 1, "LOG-* hariç; giriş bekleyen ayrı"
        c_btc = costs.cost("kripto", False, 1700, 1, None)["total"]
        assert abs(by["BTC"]["ret_now"] - 0.02) < 1e-12 and abs(by["BTC"]["net_now"] - (0.02 - c_btc)) < 1e-12
        assert by["BTC"]["px_age"] == 30 and by["BTC"]["left_s"] == 1900
        assert abs(by["xyz:TEST"]["ret_now"] - 0.02) < 1e-12, "short: fiyat düştü → +"
        assert by["xyz:TEST"]["px_now"] == 98.0 and by["xyz:TEST"]["px_age"] == 120
        assert by["ZZZ"]["px_now"] is None and by["ZZZ"]["net_now"] is None, "fiyat yoksa —"
        # kâğıt hesap: kayıttan sonraki birincil ufuk işlemleri (ham − maliyet)
        await _seed_done("A", 1, reg, 4, net=0.01)
        r = dict(registry.ACTIVE["A"])
        async with dbm.db() as c:
            r = dict(await (await c.execute("SELECT * FROM lab_rules WHERE rule_id='A'")).fetchone())
        pa = await ui.paper_for(r, core._rule("A"), cfg)
        assert pa["n"] == 4 and pa["balance"] > 10_000 and pa["svg"].startswith("<svg"), pa
        assert "points" not in pa
        assert await ui.paper_for({**r, "evidence": "log"}, core._rule("A"), cfg) is None
    asyncio.run(run())
    print("✅ pozlar) ağsız fiyat (kv mark / HIP-3 metrik); anlık ham ve maliyet sonrası; LOG-* hariç; kâğıt hesap")


async def _get(app, path, query=b""):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query, "root_path": "",
             "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    out = {"body": []}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"], out["headers"] = msg["status"], dict(msg.get("headers") or [])
        elif msg["type"] == "http.response.body":
            out["body"].append(msg.get("body", b""))
    await app(scope, receive, send)
    return out["status"], out["headers"], b"".join(out["body"]).decode("utf-8", "replace")


def test_lab_sections_trades_csv_health():
    async def run():
        app = await smoke._fresh()
        ui._CACHE.update(ts=0, v=None)
        ui._HEALTH.update(ts=0, v=None)
        cfg = app.state.cfg
        reg = dbm.now() - 30 * D
        await registry.sync(cfg, ts=reg, rules=[core._rule("A")])
        await _seed_done("A", 1, reg, 6)
        async with dbm.db() as c:
            await c.execute("INSERT INTO lab_candles(coin, tf, ts, c) VALUES('BTC', 3600, ?, 1.0)", (dbm.now() - 3600,))
        st, hd, body = await _get(app, "/lab")
        assert st == 200, body[:300]
        for s in ("Sinyale ne kaldı", "1. bakışa 14 küme kaldı (6/20)", "Açık kâğıt pozisyonlar", "Kâğıt hesap",
                  "Sonuçlanan işlemler", "Veri sağlığı", "saatlik metrik 2 saatten eski", "Mum 1 sa", "Kayıt izi"):
            assert s in body, s
        assert "Bölüm kurulamadı" not in body
        st, hd, body = await _get(app, "/lab/islemler", b"rule=A")
        assert st == 200 and body.count("<td class=\"l stick\"><b>TEST</b>") == 6, body[:500]
        st, hd, body = await _get(app, "/lab/islemler", b"coin=NOPE")
        assert st == 200 and "Bu süzgeçle sonuçlanan işlem yok" in body
        st, hd, body = await _get(app, "/lab/islemler", b"fmt=csv&rule=A&h=3600")
        assert st == 200 and hd[b"content-type"].startswith(b"text/csv")
        assert b"attachment; filename=lab_islemler.csv" in hd[b"content-disposition"]
        lines = body.lstrip("﻿").strip().splitlines()
        assert lines[0].startswith("kural,sürüm,coin,yön,karar_tsi") and len(lines) == 7, lines[:2]
        assert lines[1].split(",")[:4] == ["A", "1", "xyz:TEST", "long"]
        st, hd, body = await _get(app, "/lab/islemler", b"fmt=csv&h=900")
        assert len(body.lstrip("﻿").strip().splitlines()) == 1, "süzgeç CSV'ye de uygulanır"
        h = await ui.data_health(cfg)
        assert h["heavy"]["lc"][0]["tf"] == 3600 and h["ev"]["kural"]["done"] == 6
        h2 = await ui.data_health(cfg)
        assert h2["cached_at"] == h["cached_at"], "pahalı sayımlar önbellekli"
    asyncio.run(run())
    print("✅ /lab bölümleri) ne kaldı, pozlar, kâğıt hesap, işlemler + CSV (süzgeç, başlık, tip), veri sağlığı")
