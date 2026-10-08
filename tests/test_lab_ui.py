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
        assert not specs.validate(core._rule("X", metric="barrier", primary_h=0))
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
