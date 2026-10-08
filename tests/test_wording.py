"""Ölçüm dili — kullanıcıya giden metinde ölçülmemiş tahmin / olasılık / işlem önerisi yok.

Kullanıcı (08.10): strateji laboratuvarından ÖNCE mevcut tahmin cümleleri ölçülü dile çevrilsin
("Evet, önce düzelt"). Bulunanlar (bir kısmı ücretli kullanıcılara da gidiyordu): örüntü
"%68 ihtimalle yukarı", liq saldırısı "Pazartesi fiyat Cuma kapanışına döner", kaskad "fiyat
kaçınılmaz ~X'a gidebilir", bilanço "DOĞRU BİLDİ (insider olabilir)", anomali "birileri biliyor
olabilir", 17 bildirimde "PROPR'da listeli — işlem açabilirsin", şablonlarda "bir şey biliyor".

Pinlenenler:
  • kaynak taraması (AST): mesaj üreten modüllerdeki dizgeler — docstring, log ve karşılaştırma
    hariç — yasak kalıp içermez; veri kalitesi uyarıları ("sabah açıklanmış olabilir") ve durum
    etiketleri ("SON UYARI bekleniyor") açık izin listesinde, her biri gerekçeli
  • şablon taraması: web sayfaları (script / yorum / Jinja etiketi ayıklanır); "işlem açabilirsin" hiç yok
  • oluşturulmuş mesajlar: örüntü, liq saldırısı, kaskad, kapalı seans, sicilli balina, yön sicili,
    bilanço notu, duvar, takip liq kaydı, sıcak saat — tahmin yok, beklenen ölçüm cümlesi var
  • eski bilanço notları DB'de bir kez düzeltilir (kv damgası)
"""
import ast
import asyncio
import glob
import html
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-wording.db")
from app import db as dbm  # noqa: E402
from app import propr  # noqa: E402
from app.earnings import evaluator  # noqa: E402
from app.radar import cascade as cz  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

FORBIDDEN = [
    (r"ihtimal", "olasılık dili"), (r"muhtemel", "olasılık dili"), (r"\bolası\b", "olasılık dili"),
    (r"olasılık", "olasılık dili"), (r"olabilir", "kanıtsız ihtimal"),
    (r"gidebilir|yükselebilir|düşebilir|dönebilir|süpürebilir|hareket edebilir", "fiyat kipi"),
    (r"yükselecek|düşecek|gidecek|dönecek", "gelecek zaman tahmini"),
    (r"kaçınılmaz|öngörülebilir|\bdöner\b|döneceğ", "kesinlik iddiası"),
    (r"spoof", "niyet atfı (çekilme/dolum ayırt edilmez)"),
    (r"doğru\s*/\s*yanlış|yanıldı|insider\s+şüphe", "tahmin / içeriden bilgi çerçevesi"),
    (r"garanti(?!\s*değil|si yok)", "kesinlik iddiası"),
    (r"işlem açabilirsin|girebilirsin|tavsiye ed|fırsat", "işlem önerisi"),
    (r"insider (olabilir|paterni|şüphesi)|erken kuş|(?<![\wçğıöşü])biliyor|doğru bil(di|en|iyor)|bilici"
     r"|haklı çık|conviction|göze almış|emin görünüyor", "içeriden bilgi atfı"),
    (r"\bbekleniyor\b|\bbeklenen(ler)?\b", "beklenti tahmini"),
    (r"tahmin(i|ler)?\b(?!\s+(değil|yok))", "tahmin dili"),
]
# Açık izinler — kaldırıldıktan sonra yasak kalıplara bakılır. Her biri ölçüm/durum/veri kalitesi.
ALLOW = [
    r"son uyarı.{0,160}?bekleniyor",                     # sim durumu: tetik henüz gelmedi
    r"(balina liq'i|iğne|geri çekilme|ilk tarama) bekleniyor",
    r"şansla (~[\d.{}]+ )?beklen",                       # istatistik: şansla beklenen sayı
    r"spam değil, beklenen|beklenen bir durum",          # boş liste açıklaması
    r"sabah (da |açıklanmış )?olabilir", r"'de açıklanmış olabilir", r"tsi de olabilir",   # bilanço saati belirsiz
    r"havuz henüz dar olabilir", r"eksik olabilir", r"ulaşılamıyor olabilir", r"normal olabilir",
    r"tahmin değil|tahmin yok",
]
# config.py / diag.py taranmaz: ayar ve tanı açıklamalarında "döner" (istek/döngü döner), "olabilir"
# (teknik açıklama) mesaj değil, sahibe teknik bilgi.
SOURCES = ["app/telegram/format.py", "app/telegram/public.py", "app/telegram/fanout.py", "app/telegram/bot.py",
           "app/radar/liqchart.py", "app/radar/patterns.py",
           "app/radar/cascade.py", "app/radar/scorer.py", "app/radar/anomaly.py", "app/radar/forensics.py",
           "app/radar/liqattack.py", "app/radar/hourstats.py", "app/earnings/evaluator.py", "app/propr.py"]
WEB_SKIP = {"olabilir", "olasılık", "kesin"}            # web sayfalarında bayatlık/kapsam uyarıları çok


def tr_lower(s: str) -> str:
    return html.unescape(s).replace("İ", "i").replace("I", "ı").lower()


def hits(text: str, rules=FORBIDDEN) -> list[str]:
    t = tr_lower(text)
    for rx in ALLOW:
        t = re.sub(rx, " ", t)
    return [f"{why}: {m.group(0)!r}" for rx, why in rules for m in [re.search(rx, t)] if m]


def _strings(path: str):
    """Modüldeki dizgeler: docstring, log çağrısı, karşılaştırma ve SQL hariç."""
    tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    skip = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            skip.add(id(n.body[0].value))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) \
                and n.func.value.id in ("log", "logger", "logging"):
            skip.update(id(a) for a in ast.walk(n))
        if isinstance(n, ast.Compare):
            skip.update(id(a) for a in ast.walk(n))
        # eski→yeni düzeltme tabloları (ör. evaluator.REASON_FIXES) eski metni bilerek taşır
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id.endswith("_FIXES") for t in n.targets):
            skip.update(id(a) for a in ast.walk(n))
    for n in ast.walk(tree):
        if id(n) in skip:
            continue
        if isinstance(n, ast.JoinedStr):
            skip.update(id(v) for v in n.values)
            txt = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in n.values)
            if "UPDATE " not in txt:                       # SQL düzeltmesi eski metni arar
                yield n.lineno, txt
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and "UPDATE " not in n.value:
            yield n.lineno, n.value


def test_source_strings_measured():
    bad = [f"{p}:{ln}: {h} — {s.strip()[:90]!r}" for p in SOURCES for ln, s in _strings(p) for h in hits(s)]
    assert not bad, "\n".join(bad)
    assert propr.PROPR_NOTE == "✅ <b>PROPR'da listeli</b>"
    print(f"✅ kaynak) {len(SOURCES)} modülün dizgelerinde tahmin / öneri / içeriden bilgi atfı yok")


def test_templates_measured():
    rules = [(rx, why) for rx, why in FORBIDDEN if rx not in WEB_SKIP]
    rules.append((r"(insider|bilgi sahibi|spoof|ucuz) olabilir", "kanıtsız ihtimal"))
    bad = []
    for p in sorted(glob.glob(os.path.join(ROOT, "app/web/templates/*.html"))):
        src = open(p, encoding="utf-8").read()
        assert "işlem açabilirsin" not in src, p
        src = re.sub(r"<script.*?</script>|<!--.*?-->|\{#.*?#\}|\{%.*?%\}|/\*.*?\*/", " ", src, flags=re.S)
        for i, line in enumerate(src.split("\n"), 1):
            bad += [f"{os.path.basename(p)}:{i}: {h} — {line.strip()[:90]!r}" for h in hits(line, rules)]
    assert not bad, "\n".join(bad)
    print("✅ şablonlar) web sayfalarında tahmin / öneri / içeriden bilgi atfı yok; PROPR notu nötr")


def _cascade():
    lv = lambda *pp: [{"px": p, "sz": s} for p, s in pp]   # noqa: E731
    trig = {"side": "short", "liq_px": 89.93, "notional": 13_300_000, "address": "0xtrig"}
    pool = [{"side": "short", "liq_px": 90.30, "notional": 2_000_000, "address": "0xa"}]
    return (cz.simulate([], lv((89.95, 50_000), (90.20, 40_000), (90.50, 60_000)), trig, pool, 89.13),
            cz.simulate([], lv((89.95, 50_000), (90.20, 40_000)), trig, pool, 89.13))


def test_rendered_messages():
    A = "0x" + "a" * 40
    pat = fmt.pattern_alert({"coin": "xyz:SNDK", "p_up": 68, "tf": "1h", "horizon": 24, "base_up": 52,
                             "edge": 16, "z": 2.4, "n": 41, "med": 1.2, "q25": -0.5, "q75": 2.8,
                             "record": {"rate": 71, "n": 300}})
    assert "n=<b>41</b> benzer geçmiş örnekte <b>24 bar (24s)</b> sonra fiyatı yukarıda olan pay <b>%68</b>" in pat, pat
    assert "taban oranı (şekle bakmadan) %52 → fark <b>+16 puan</b>" in pat, pat
    assert "sicili" not in pat and "PROPR" not in pat, "yanlı sicil satırı ve PROPR notu örüntüde yok"
    c, c_ex = _cascade()
    casc = "\n".join(cz.describe(c) + cz.describe(c_ex))
    assert "zincir sonu" in casc and "ötesi ölçülmedi" in casc
    atk = fmt.liq_attack_alert({"symbol": "SNDK", "direction": "down", "dist_pct": 1.2, "cost_usd": 2e5,
                                "liq_usd": 3e6, "score": 15, "book_thin": True, "target_px": 470,
                                "mark": 476, "dev_close": -1.1, "targets": []}, (0, dbm.now() + 3600))
    assert "Pazartesi açılış fiyatı hakkında ölçüm içermez" in atk
    off = fmt.offhours_move({"symbol": "SNDK", "kind": "band", "pct": 2.1, "base_px": 470, "px": 480,
                             "anchor_ts": dbm.now() - 7200, "oi_chg": -3.0})
    assert "OI) kapanıştan beri %-3.0" in off and "kuruluyor" not in off
    whale = fmt.whale_fill_alert("xyz:SNDK", A, "buy", 480, 2e6, True, (2, 1))
    assert "Sicil: 2 kez yönü tuttu / 1 kez tutmadı" in whale
    win = fmt.winners_list([{"address": A, "hits": 2, "misses": 0, "watchlist": 1}])
    assert "Bilanço yön sicili" in win and "şansla" in win
    top = {"side": "long", "address": A, "notional": 5e6}
    note = evaluator.result_note(top, 6.2, [{"hit": True}, {"hit": False}], 2.0)
    assert note.endswith("· ✅ yön tuttu · 1/2 adresin yönü tuttu"), note
    assert "yön tutmadı" in evaluator.result_note({**top, "side": "short"}, 6.2, [], 2.0)
    wall = fmt.wall_gone({"symbol": "SNDK", "side": "ask", "first_ts": dbm.now() - 600, "peak_notional": 5e6})
    liqm = fmt.track_liq_move({"symbol": "SNDK", "id": 3, "address": A, "side": "short", "base_notional": 1e6},
                              {"notional": 8e5}, 500, 506)
    assert "fiyattan <b>uzaklaştı</b>" in liqm and "boyut" not in liqm, "farklı aralıklar yan yana yazılmaz"
    hot = fmt.hot_hours_channel([{"symbol": "SNDK", "avg": 0.4, "win": 61, "n": 55}], 16)
    assert "GEÇMİŞ KARNESİ" in hot and "şansla" in hot
    bad = [f"{name}: {h}" for name, t in (("örüntü", pat), ("kaskad", casc), ("liq saldırısı", atk),
                                          ("kapalı seans", off), ("balina", whale), ("sicil", win),
                                          ("bilanço notu", note), ("duvar", wall), ("takip liq", liqm),
                                          ("sıcak saat", hot)) for h in hits(t)]
    assert not bad, "\n".join(bad)
    print("✅ mesajlar) örüntü, kaskad, liq saldırısı, kapalı seans, balina, yön sicili, bilanço notu,"
          " duvar, takip liq, sıcak saat — ölçüm dili")


def test_old_notes_fixed_once():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "wording.db"))
        async with dbm.db() as c:
            await c.execute("INSERT INTO earnings_events(symbol, date_et, evaluated, result_note) VALUES"
                            " ('SNDK','2026-08-01',1,'En büyük poz LONG $5M (0xaaaa..aaaa) → fiyat %+6.2 · ✅ DOĞRU"
                            " BİLDİ (insider olabilir) · 3/4 adres doğru'), ('MU','2026-08-02',1,"
                            "'En büyük poz SHORT $2M (0xbbbb..bbbb) → fiyat %+4.0 · ❌ yanılmış · 0/2 adres doğru')")
            await c.commit()
        assert await evaluator.fix_old_notes() == 2
        assert await evaluator.fix_old_notes() == 0, "kv damgası: bir kez"
        async with dbm.db() as c:
            cur = await c.execute("SELECT result_note FROM earnings_events ORDER BY symbol")
            notes = [r["result_note"] for r in await cur.fetchall()]
        assert notes[0].endswith("❌ yön tutmadı · 0/2 adresin yönü tuttu"), notes
        assert notes[1].endswith("✅ yön tuttu · 3/4 adresin yönü tuttu"), notes
        assert not any(hits(n) for n in notes)
    asyncio.run(run())
    print("✅ eski notlar) DB'deki 'DOĞRU BİLDİ (insider olabilir)' notları bir kez ölçüm diline çevrildi")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
