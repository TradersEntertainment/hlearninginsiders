"""📨 Bot yönlendirme + HTML dayanıklılığı — "/tani yazdım bir şey olmadı", "metin kesildi".

Pinlenenler:
  • sahiplik: sahip DM'i YA DA sahibin kullanıcı id'si (TELEGRAM_CHAT_ID pozitifse o;
    TELEGRAM_OWNER_ID env-only) — botun kendi kanalında sahip /tani yazınca cevap gelir,
    sahip olmayan aynı kanalda sessiz kalır (başka botların komutları), coin komutu herkese
  • send_pre: uzun rapor parça parça, HER parça kendi <pre>…</pre>'si ile, ≤ MAX_LEN, kaçışlı
  • _split: tek satır sınırı aşarsa sert kesilir (sonsuz büyük parça yok)
  • etiket sıyırıcı yalnız gerçek etiketleri sıyırır: "(< $1K)" metni yutulmaz (_send_plain,
    _caption_fit görünür uzunluk, sendPhoto düz altyazı); düz metinde varlıklar çözülür
  • format: anlık görüntü metninde etiket dışı ham '<' yok (toz notu kaçışlı)
  • config: telegram_owner_id env-only, EDITABLE'da değil
"""
import asyncio
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-bot-routing.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.telegram import bot as botmod  # noqa: E402
from app.telegram import format as fmt  # noqa: E402
from app.telegram.bot import MAX_LEN, TelegramBot, _caption_fit, strip_tags  # noqa: E402


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
        return json.dumps(self.body)


class Sess:
    """sendMessage/sendPhoto taklidi: gövdeler kaydedilir; parse_mode=HTML ve metinde etiket
    dışı '<' varsa Telegram gibi 400 döner."""

    def __init__(self):
        self.posts = []

    def post(self, url, json=None, data=None, timeout=None):
        payload = json if json is not None else {"_form": data}
        self.posts.append((url.rsplit("/", 1)[-1], payload))
        if json and json.get("parse_mode") == "HTML":
            visible = strip_tags(json.get("text") or "")
            if "<" in visible:
                return Resp(400, {"ok": False, "description": "Bad Request: can't parse entities"})
        return Resp(200, {"ok": True, "result": {"message_id": 1}})


def upd(chat, uid, text, typ="supergroup"):
    return {"message": {"chat": {"id": chat, "type": typ},
                        "from": {"id": uid, "first_name": "Ömer"}, "text": text}}


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id = "111"          # sahip DM'i (pozitif → kullanıcı id'si)
    cfg.crypto_chat_id = "-100222"        # botun kendi kanalı
    cfg.telegram_bot_token = "t"
    cfg.public_bot_enabled = False
    return cfg


def _bot(cfg):
    bot = TelegramBot(cfg, Sess(), None, {})
    sent = []

    async def fake_send(text, chat_id=None, reply_markup=None):
        sent.append((chat_id, text))
        return True
    bot.send = fake_send
    return bot, sent


def test_owner_in_own_channel():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "routing.db"))
        cfg = _cfg()
        bot, sent = _bot(cfg)
        assert bot._owner_user_id() == "111"
        # sahip kendi kanalında /tani → rapor gelir (parça başına <pre>)
        await bot._handle_update(upd(-100222, 111, "/tani"))
        assert sent and sent[-1][0] == "-100222" and sent[-1][1].startswith("<pre>") and "[" in sent[-1][1], sent[-1][1][:80]
        assert all(t.startswith("<pre>") and t.endswith("</pre>") for _, t in sent), "her parça kendi <pre>'si ile"
        # sahip olmayan aynı kanalda /tani → sessiz (başka botların komutları); /id cevap verir
        n = len(sent)
        await bot._handle_update(upd(-100222, 999, "/tani"))
        assert len(sent) == n, "sahip olmayan kanalda /tani sessiz kalmalı"
        await bot._handle_update(upd(-100222, 999, "/id"))
        assert len(sent) == n + 1 and "chat id" in sent[-1][1]
        # sahip DM'i eskisi gibi; TELEGRAM_OWNER_ID grup sahipli kurulum için
        await bot._handle_update(upd(111, 111, "/id", typ="private"))
        assert sent[-1][0] == "111" and "Chat id" in sent[-1][1]
        cfg.telegram_chat_id, cfg.telegram_owner_id = "-100333", "4242"
        assert bot._owner_user_id() == "4242"
        await bot._handle_update(upd(-100222, 4242, "/id"))
        assert sent[-1][1].startswith("Chat id"), "sahip kullanıcı id'siyle kanalda tam zincir"
        await bot._handle_update(upd(-100222, 111, "/id"))
        assert sent[-1][1].startswith("Bu sohbetin"), "eski id artık sahip değil"
        # boş chat id → kimse sahip değil
        cfg.telegram_chat_id, cfg.telegram_owner_id = "", ""
        assert bot._owner_user_id() == "" and not bot._is_owner("111", {"from": {"id": 111}})
        print("✅ yönlendirme) sahip kanalda /tani cevap alır; sahip olmayan sessiz; DM aynı; TELEGRAM_OWNER_ID; boş id → sahip yok")
    asyncio.run(run())


def test_send_pre_split_and_plain():
    async def run():
        cfg = _cfg()
        bot = TelegramBot(cfg, Sess(), None, {})
        # send_pre: 3 parça, her biri ≤ MAX_LEN ve kendi <pre>'si ile, '<' kaçışlı
        raw = "\n".join(f"satır {i} a<b & c" for i in range(700))
        assert len(raw) > 2 * MAX_LEN
        assert await bot.send_pre(raw, "111")
        msgs = [p for m, p in bot.session.posts if m == "sendMessage"]
        assert len(msgs) >= 3 and all(p["parse_mode"] == "HTML" for p in msgs), len(msgs)
        assert all(p["text"].startswith("<pre>") and p["text"].endswith("</pre>") and len(p["text"]) <= MAX_LEN for p in msgs)
        assert all("&lt;" in p["text"] and "<b" not in p["text"].replace("<pre>", "") for p in msgs)
        assert "".join(fmt.esc(raw).split("\n")) == "".join(p["text"][5:-6] for p in msgs).replace("\n", ""), "içerik kaybı yok"
        # _split: tek uzun satır sert kesilir
        parts = TelegramBot._split("x" * (MAX_LEN * 2 + 5) + "\nkısa", MAX_LEN)
        assert [len(p) for p in parts] == [MAX_LEN, MAX_LEN, 5 + 1 + 4] or [len(p) for p in parts][:2] == [MAX_LEN, MAX_LEN], parts and [len(p) for p in parts]
        assert all(len(p) <= MAX_LEN for p in parts) and "kısa" in parts[-1]
        # etiket sıyırıcı: ham '<' yutulmaz; düz metinde varlıklar çözülür
        assert strip_tags("<b>a</b> (< $1K) <i>b</i>") == "a (< $1K) b"
        assert strip_tags("x &lt; y <a href='u'>l</a>") == "x &lt; y l"
        cap, is_html = _caption_fit("<b>a</b> (< $1K)" + " z" * 10)
        assert is_html and cap.startswith("<b>a</b>")
        # HTML reddedilince düz metin: "(< $1K)" ve devamı korunur, &lt; → <
        s = Sess()
        bot2 = TelegramBot(cfg, s, None, {})
        ok = await bot2.send("<i>havuzda 5 açık pozisyon · 2 toz pozisyon (< $1K) bantlarda var, tek listesinde yok</i>\n"
                             "<i>ℹ️ Gözlem aracıdır &amp; tavsiye değildir.</i>", "111")
        assert ok, s.posts
        html_try, plain = s.posts[0][1], s.posts[1][1]
        assert html_try["parse_mode"] == "HTML" and "parse_mode" not in plain
        assert plain["text"] == ("havuzda 5 açık pozisyon · 2 toz pozisyon (< $1K) bantlarda var, tek listesinde yok\n"
                                 "ℹ️ Gözlem aracıdır & tavsiye değildir."), plain["text"]
        print("✅ gönderim) send_pre parça başına <pre>, kaçışlı, ≤ MAX_LEN; uzun satır sert kesim; sıyırıcı '(< $1K)' yutmuyor; düz metinde varlıklar çözük")
    asyncio.run(run())


def test_format_no_raw_lt_and_config():
    s = {"coin": "PUMP", "mark": 0.0043, "age": 5,
         "rows": [{"address": "0x" + "a" * 40, "side": "long", "notional": 986_000.0, "liq_px": 0.0036, "dist": 16.37,
                   "leverage": 6, "ts": dbm.now() - 60}],
         "n_all": 635, "n_big": 12, "min_usd": 500_000, "png": None, "cascade": None, "coverage": None,
         "all_far": False, "n_far": 328, "far_pct": 50.0, "n_dust": 288, "dust": 1950.0,
         "clusters": [{"side": "long", "px_lo": 0.0022, "px_hi": 0.0030, "px": 0.0030, "dist_lo": 30.3, "dist_hi": 49.6,
                       "total": 19_000_000.0, "n": 80}], "main_band": None}
    s["main_band"] = s["clusters"][0]
    t = fmt.crypto_liq_snapshot(s, offers=[74])
    visible = strip_tags(t)
    assert "<" not in visible and "288 toz pozisyon (&lt; $2K)" in t, t
    assert "yatırım tavsiyesi değildir" in t and "/takip_74" in t
    # aynı kontrol: html.escape edilmiş görünür metin Telegram'ın kabul edeceği şekilde
    assert not re.search(r"<(?![/A-Za-z])", t), "etiket dışı '<' kalmamalı"
    c = Config()
    assert hasattr(c, "telegram_owner_id") and "telegram_owner_id" not in EDITABLE_FIELDS and "telegram_chat_id" not in EDITABLE_FIELDS
    if not os.getenv("TELEGRAM_OWNER_ID"):
        assert c.telegram_owner_id == ""
    env = open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    assert "TELEGRAM_OWNER_ID=" in env
    print("✅ format/config) toz notu kaçışlı, görünür metinde ham '<' yok; telegram_owner_id env-only; .env")
