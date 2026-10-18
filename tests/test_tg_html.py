"""Telegram HTML: kaçışsız "<" mesajı düz metne düşürür (link ve kalın kaybolur).

Kullanıcı (18.10, 🕐 büyük TWAP kanalı ekran görüntüsü): "bu bildirimleri neden link gelmemiş ?". Kök neden: hafta
sonu notu "süre < 24 sa" kaçışsızdı. Telegram (tdlib parse_html) desteklenmeyen her "<"de 400 "can't parse entities"
döner; bot mesaj kaybolmasın diye etiketleri sıyırıp düz metin yollar (bot._send_plain) → adres linki, kalın yazı ve
/takip dışındaki her biçim gider. Aynı hata 🆕🐋 YENİ BÜYÜK POZİSYON'daki skor nedeninde de vardı ("taze cüzdan (<7g)").

Kararlar: notlar "&lt;" yazar; skor nedenleri esc() ile. /tani (send_pre kaçışlar) ve site (Jinja kaçışlar) "<" yazabilir.

Pinlenenler:
  • tg_html_errors: tdlib gibi — her "<" desteklenen bir etiket açar/kapar, etiketler dengeli
  • 🕐 hafta sonu / normal kural notu, side_note ile birleşik TWAP mesajı, 🆕🐋 taze cüzdan nedeni, /defter mesajı
  • kaynak taraması: app/ altındaki metin sabitlerinde kaçışsız "<" yalnız izinli (Telegram'a HTML gitmeyen) yerlerde
"""
import ast
import os
import pathlib
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-tg-html.db")
from app.telegram import format as fmt  # noqa: E402

# tdlib (MessageEntity.cpp parse_html) kabul ettiği etiketler
TG_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "a", "code", "pre", "tg-spoiler", "span",
           "blockquote", "tg-emoji"}


def tg_html_errors(text: str) -> list[str]:
    """Telegram'ın HTML ayrıştırıcısı gibi: desteklenmeyen / kapanmayan etiket ve kaçışsız "<" → hata listesi."""
    errs, stack, i = [], [], 0
    while True:
        i = text.find("<", i)
        if i < 0:
            break
        j = text.find(">", i)
        if j < 0:
            errs.append(f"kapanmayan '<' @ {i}: {text[i:i + 30]!r}")
            break
        body = text[i + 1:j]
        if body.startswith("/"):
            name = body[1:].strip().lower()
            if not stack or stack[-1] != name:
                errs.append(f"eşleşmeyen kapanış </{name}> @ {i}")
            else:
                stack.pop()
        else:
            m = re.match(r"([a-z][a-z-]*)(\s|$)", body.lower())
            if not m or m.group(1) not in TG_TAGS:
                errs.append(f"desteklenmeyen etiket / kaçışsız '<' @ {i}: {text[i:i + 30]!r}")
            else:
                stack.append(m.group(1))
        i = j + 1
    if stack:
        errs.append(f"kapanmayan etiket(ler): {stack}")
    return errs


def test_validator_itself():
    assert not tg_html_errors('<b>x</b> <a href="https://a">y</a> &lt; 5 · a > b')
    assert tg_html_errors("süre < 24 sa") and tg_html_errors("taze cüzdan (<7g)") and tg_html_errors("<b>x")
    assert tg_html_errors("<foo>x</foo>")
    print("✅ doğrulayıcı) tdlib gibi: kaçışsız '<', desteklenmeyen ve kapanmayan etiket yakalanır")


def test_big_channel_note_and_message():
    from app.config import Config
    from app.radar import twaploose as lo
    cfg = Config()
    for rule, coin in (("weekend", "xyz:SP500"), ("ok", "BTC"), ("ok", "SOL")):
        head = lo.big_head(cfg, coin, rule)
        assert not tg_html_errors(head), (rule, head, tg_html_errors(head))
    wk = lo.big_head(cfg, "xyz:SP500", "weekend")
    assert "süre &lt; 24 sa" in wk and fmt.strip_tags(wk) == wk
    # ekran görüntüsündeki mesaj biçimi: TWAP metni + kanal notu (side_note) — linkli adres korunur
    msg = ("⏳ <b>TWAP başladı</b> · ⚡ HIZLI · 20 dk · SP500 · 🟢 ALIŞ · plan <b>$484K</b>\n"
           f"👤 {fmt.alink('0x' + 'c' * 40)} · 📍 pozisyon bilinmiyor\n"
           "🔔 iptal edilirse ⛔ buraya otomatik yazılır\n\nℹ️ Gözlem aracıdır, yatırım tavsiyesi değildir.")
    out = fmt.side_note(msg, wk)
    assert not tg_html_errors(out), tg_html_errors(out)
    assert 'href="https://hypurrscan.io/address/0x' in out
    print("✅ 🕐 kanal notu) hafta sonu 'süre &lt; 24 sa'; TWAP mesajıyla birleşik metin Telegram HTML'ine uygun")


def test_new_big_fresh_wallet_reason():
    import json
    p = {"coin": "xyz:TSLA", "side": "long", "notional": 9e6, "entry_px": 400.0, "leverage": 5, "address": "0x" + "a" * 40,
         "score": 80, "opened_ts": None, "score_reasons": json.dumps(["taze cüzdan (<7g)", "yeni fonlama"])}
    txt = fmt.new_big_position_alert("xyz:TSLA", p, None)
    assert "taze cüzdan (&lt;7g)" in txt and not tg_html_errors(txt), tg_html_errors(txt)
    blk = fmt._reasons_block([p])
    assert "(&lt;7g)" in blk and not tg_html_errors(blk)
    print("✅ 🆕🐋) skor nedeni 'taze cüzdan (<7g)' kaçışlı — mesaj düz metne düşmez")


def test_defter_message_html():
    sys.path.insert(0, HERE)
    import test_tape_book as tb
    r = tb.test_book_owners_measured()
    txt = fmt.book_owners("PUMP", {**r, "age": 3}, {"n": 0, "window": 900, "span": 900}, listened=True)
    assert not tg_html_errors(txt), tg_html_errors(txt)
    print("✅ /defter) mesaj Telegram HTML'ine uygun")


# Kaçışsız "<" içerebilen, Telegram'a HTML olarak GİTMEYEN metinler: dosya → BAĞLAMLI parçalar (f-string'ler "{}"
# yer tutucusuyla tek metin). Kısa operatör dizgeleri ("<", "<=", "<12") yalnız birebir eşleşir. Yeni bir mesaj metni
# "<" yazacaksa "&lt;" yazmalı; buraya ekleme yalnız site / /tani (send_pre) / esc() ile giden / regex / SQL için.
ALLOWED = {
    "app/ai/schema.py": ("<=", '">=" veya "<="'),                       # AI istemi
    "app/diag.py": ("<12", "(elle: /pro_ver <"),                         # biçim belirteci; /tani send_pre kaçışlar
    "app/lab/gate.py": ("yetersiz küme ({} < {})", "sola çarpık örnek (g1 {} < {})"),     # site (Jinja kaçışlar)
    "app/lab/specs.py": ("<", "<=", "denetim kümesi < 2 gün", "küme genişliği < 2 ×"),     # işleç adları; site
    "app/notify.py": ("<[^>]+>",),
    "app/radar/hourstats.py": (" · az örnek (<{}) — güven düşük",),      # site ipucu
    "app/radar/scorer.py": ("taze cüzdan (<7g)",),                       # format.py esc() ile (test aşağıda)
    "app/radar/seans.py": ("yetersiz (n={} < {})",),                     # site
    "app/radar/sim.py": ("hedef çok yakın (%{} < %{})", '<svg class="simcurve"', "<line x1=", "<text x=", "<polyline ",
                         "<circle cx="),                                  # skip_reason ve SVG: site
    "app/radar/twaplive.py": (" — emir < {}", " — kalan {} < {}", " — spot çiftinin 24s hacmi {} < {}"),  # esc(kap)
    "app/radar/twaploose.py": ("📅 hafta sonu kripto olmayan: plan ≥ {} · süre < {} dk",),  # /tani send_pre
    "app/radar/wake.py": ("<!--.*?-->",),
    "app/telegram/bot.py": ("<[^>]+>",),
    "app/telegram/fanout.py": ("<[^>]+>",),
    "app/telegram/format.py": ("</?[A-Za-z][^<>]*>",),
    "app/web/routes.py": ('<svg xmlns="http://www.w3.org/2000/svg"',),  # favicon
}


def _texts(tree):
    """(satır, metin): docstring'ler hariç sabit dizgeler + f-string'ler bütün olarak ("{}" yer tutucusuyla —
    ifadenin içindeki kod "<"i metin sayılmaz)."""
    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and node.body:
            b = node.body[0]
            if isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant):
                skip.add(id(b.value))
        if isinstance(node, ast.JoinedStr):
            skip.update(id(v) for v in node.values)
    for node in ast.walk(tree):
        if id(node) in skip:
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            yield node.lineno, "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)


def _allowed(rel: str, s: str) -> bool:
    return any(s == a or (len(a) > 3 and a in s) for a in ALLOWED.get(rel, ()))


def test_no_new_raw_lt_in_strings():
    tags = "|".join(sorted(TG_TAGS, key=len, reverse=True))
    bad = re.compile(rf"<(?!/|!|(?:{tags})[\s>])")         # Telegram etiketi olmayan her "<" (sahte "<adres>" de)
    sql = re.compile(r"\b(SELECT|DELETE|UPDATE|WHERE|AND|OR)\b")
    found = []
    for p in sorted(pathlib.Path(ROOT, "app").rglob("*.py")):
        rel = p.relative_to(ROOT).as_posix()
        for ln, s in _texts(ast.parse(p.read_text(encoding="utf-8"))):
            if bad.search(s) and not sql.search(s) and not _allowed(rel, s):
                found.append(f"{rel}:{ln}: {s[:80]!r}")
    assert not found, "kaçışsız '<' (Telegram HTML'de mesajı düz metne düşürür; '&lt;' yaz):\n" + "\n".join(found)
    # tarama kendini de sınar: sahte etiket, aynı dosyada izinsiz yeni metin, f-string yakalanır
    probe = ast.parse('x = "Kullanım: /takip <adres>"\ny = f"⏳ <b>TWAP</b> · kalan < {a}"\nz = f"{a if b < c else d}"')
    hits = [s for _ln, s in _texts(probe) if bad.search(s) and not _allowed("app/radar/twaplive.py", s)]
    assert hits == ["Kullanım: /takip <adres>", "⏳ <b>TWAP</b> · kalan < {}"], hits
    print("✅ kaynak) metin sabitlerinde ve f-string'lerde yeni kaçışsız '<' yok (sahte etiket dahil; izinler bağlamlı)")


def test_status_text_escapes_hl_error_page():
    """3. inceleme (18.10): HL 502/504'te nginx / CloudFront HTML sayfası döner; hata metni /status'a kaçışsız giriyordu."""
    txt = fmt.status_text({"süpürücü": "RuntimeError: HL info HTTP 502: <html>\r\n<head><title>502 Bad Gateway</t",
                           "akış": None, "coin": 12})
    assert not tg_html_errors(txt) and "&lt;html&gt;" in txt and "akış: None" in txt, tg_html_errors(txt)
    print("✅ /status) HL'nin HTML hata sayfası kaçışlı — mesaj düz metne düşmez")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
