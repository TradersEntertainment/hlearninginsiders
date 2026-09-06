"""Hacim patlaması: $500K kripto bildirim eşiği, geniş 5 dk grafik, tek mesaj.

Pinlenenler:
  • parse_vol_candles h/l tutar (yoksa gövdeden), rekor mantığı değişmez
  • volchart.render PNG üretir; rekor kovası son kapalı mum; mum azsa None
  • cryptovol.scan: $1.2M rekor → resim + altyazı TEK mesaj; $400K → sayfaya
    yazılır, kanala düşmez (below_alert); resim reddedilirse metin gider
  • Notifier.send_rich yolları: combined / split / text / gitmedi
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db as dbm
from app.config import Config
from app.hl import universe as uni
from app.radar import cryptovol as cv
from app.radar import volchart

PX = 0.0032


def raw_candles(n=300, record_usd=1_200_000, base_usd=120_000, end_offset=600):
    """HL candleSnapshot biçimi (ms damgalı, string): son KAPALI kova rekor."""
    t_end = (dbm.now() - end_offset) // 300 * 300
    out, px = [], PX * 0.97
    for i in range(n):
        t = t_end - (n - 1 - i) * 300
        o = px
        px = px * (1 + (0.0006 if i % 4 else -0.0011))
        usd = record_usd if i == n - 1 else base_usd * (0.4 + (i % 7) / 7)
        out.append({"t": t * 1000, "o": str(o), "h": str(max(o, px) * 1.002),
                    "l": str(min(o, px) * 0.998), "c": str(px), "v": str(usd / px)})
    return out


class Client:
    def __init__(self, candles):
        self.raw = candles

    async def meta_and_ctxs(self, dex=""):
        return [{"universe": [{"name": "PUMP"}]}, [{"markPx": str(PX), "dayNtlVlm": "3.3e8"}]]

    async def candles(self, coin, interval, start_ms, end_ms):
        assert interval == "5m"
        return self.raw


class Bot:
    def __init__(self, photo_ok=True):
        self.photo_ok, self.sent, self.photos = photo_ok, [], []

    async def send(self, text, chat_id=None):
        self.sent.append((chat_id, text)); return True

    async def send_photo(self, png, caption="", chat_id=None):
        if self.photo_ok:
            self.photos.append((chat_id, caption, len(png)))
        return self.photo_ok


def _cfg():
    cfg = Config()
    cfg.crypto_chat_id = "-100"
    cfg.alert_forensics = False
    return cfg


# ------------------------------------------------ 1) mum ayrıştırma
def test_parse():
    cs = cv.parse_vol_candles(raw_candles(20))
    assert len(cs) == 20 and all(c["h"] >= max(c["o"], c["c"]) >= min(c["o"], c["c"]) >= c["l"] for c in cs)
    bare = cv.parse_vol_candles([{"t": 1_800_000_000_000, "o": "2", "c": "1", "v": "5"}])
    assert bare[0]["h"] == 2.0 and bare[0]["l"] == 1.0, "h/l yoksa gövdeden"
    rec = cv.find_record(cs)
    assert rec and abs(rec["notional"] - 1_200_000) < 1 and rec["ratio"] > 5
    print("✅ ayrıştırma) h/l tutuluyor, yoksa gövdeden; rekor mantığı aynı")


# ------------------------------------------------ 2) grafik
def test_render():
    cs = cv.parse_vol_candles(raw_candles(300))
    rec = cv.find_record(cs)
    png = volchart.render("PUMP", cs, rec, day_vol=3.3e8)
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 8000, "PNG üretilmedi"
    from PIL import Image
    from io import BytesIO
    im = Image.open(BytesIO(png))
    assert im.size == (volchart.W, volchart.H) and im.size[0] > 1.5 * im.size[1], "geniş olmalı"
    # rekor mumu örtülmemeli: kovanın sütununda mum rengi (UP/DOWN) piksel var
    n = len([c for c in cs if c["t"] <= rec["bucket_ts"]])
    gap = max(8, n // 10)
    step = (volchart.W - volchart.PAD_R - volchart.PAD_L) / (n + gap)
    xr = int(volchart.PAD_L + step * (n - 0.5))
    total_h = volchart.H - volchart.PAD_T - volchart.PAD_B
    col = [im.getpixel((xr, y)) for y in range(volchart.PAD_T, volchart.PAD_T + int(total_h * 0.58))]
    assert any(p in (volchart.UP, volchart.DOWN) for p in col), "rekor mumu vurgunun altında kaldı"
    assert any(p == volchart.AMBER for p in col), "▼/▲ işaret yok"
    assert volchart.render("PUMP", cs[:5], rec) is None, "mum azsa None"
    assert volchart.render("PUMP", [], rec) is None
    print("✅ grafik) geniş PNG; yetersiz mumda None")


# ------------------------------------------------ 3) tarama: eşik + tek mesaj + yedek
def test_scan_threshold_and_chart():
    async def run():
        from app.notify import Notifier
        cfg = _cfg()
        assert cfg.crypto_vol_alert_min_usd == 500_000, "kullanıcı kuralı: varsayılan $500K"
        # (a) $1.2M rekor → tek birleşik mesaj (resim + tam metin)
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cv.db"))
        bot = Bot()
        out = await cv.scan(cfg, Client(raw_candles(300)), Notifier(cfg, bot))
        assert out["coins"] == 1 and out["events"] == 1 and out["alerted"] == 1, out
        assert out["combined"] == 1 and out["photos"] == 1 and bot.sent == [], out
        chat, cap, nbytes = bot.photos[0]
        assert chat == "-100" and nbytes > 8000 and "24 saatin en yüksek 5 dakikalık hacmi" in cap
        assert "PUMP" in cap and "$1.2M" in cap, cap
        async with dbm.db() as c:
            cur = await c.execute("SELECT alerted FROM vol_events WHERE coin='PUMP'")
            assert (await cur.fetchone())["alerted"] == 1
        # (b) $400K rekor → sayfaya yazılır, kanala düşmez
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cv2.db"))
        bot2 = Bot()
        out2 = await cv.scan(cfg, Client(raw_candles(300, record_usd=400_000)), Notifier(cfg, bot2))
        assert out2["events"] == 1 and out2["alerted"] == 0 and out2["below_alert"] == 1, out2
        assert bot2.sent == [] and bot2.photos == []
        # (c) resim reddedilir → metin gider, alerted sayılır
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cv3.db"))
        bot3 = Bot(photo_ok=False)
        out3 = await cv.scan(cfg, Client(raw_candles(300)), Notifier(cfg, bot3))
        assert out3["alerted"] == 1 and out3["photos"] == 0 and len(bot3.sent) == 1, out3
        # (d) grafik kapalı → düz metin
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cv4.db"))
        cfg.crypto_vol_chart = False
        bot4 = Bot()
        out4 = await cv.scan(cfg, Client(raw_candles(300)), Notifier(cfg, bot4))
        assert out4["alerted"] == 1 and out4["photos"] == 0 and len(bot4.sent) == 1 and bot4.photos == []
        print("✅ tarama) $1.2M → tek mesaj (resim + metin); $400K → sessiz; resim düşünce metin; ayar kapalıysa metin")
    asyncio.run(run())


# ------------------------------------------------ 4) send_rich yolları
def test_send_rich():
    async def run():
        from app.notify import Notifier
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "sr.db"))
        cfg = _cfg()
        png = b"\x89PNG" + b"0" * 2000
        bot = Bot()
        ok, mode = await Notifier(cfg, bot).send_rich("cryptovol", "<b>kısa</b>", png, key="k", chat_id="-1")
        assert (ok, mode) == (True, "combined") and bot.sent == [] and bot.photos[0][1] == "<b>kısa</b>"
        ok, mode = await Notifier(cfg, bot).send_rich("cryptovol", "u" * 1200, png, key="k2", chat_id="-1",
                                                      short_caption="kısa altyazı")
        assert (ok, mode) == (True, "split") and len(bot.sent) == 1 and bot.photos[-1][1] == "kısa altyazı"
        ok, mode = await Notifier(cfg, bot).send_rich("cryptovol", "metin", None, key="k3", chat_id="-1")
        assert (ok, mode) == (True, "text") and len(bot.sent) == 2
        bad = Bot(photo_ok=False)
        ok, mode = await Notifier(cfg, bad).send_rich("cryptovol", "metin", png, key="k4", chat_id="-1",
                                                      short_caption="x")
        assert (ok, mode) == (True, "text") and len(bad.sent) == 1 and bad.photos == []
        cfg.notify_cryptovol = False
        ok, mode = await Notifier(cfg, Bot()).send_rich("cryptovol", "metin", png, key="k5", chat_id="-1")
        assert (ok, mode) == (False, ""), "tip kapalıysa hiçbir yol gitmez"
        async with dbm.db() as c:
            cur = await c.execute("SELECT key FROM alerts_log WHERE kind='sent:cryptovol' ORDER BY id")
            keys = [r["key"] for r in await cur.fetchall()]
        assert keys == ["k", "k2", "k2:img", "k3", "k4"], keys
        print("✅ send_rich) combined / split / text / kapalı; her başarılı parça sent: kaydı")
    asyncio.run(run())


# ------------------------------------------------ 5) bağlantı
def test_wiring():
    from app.config import EDITABLE_FIELDS
    c = Config()
    for f in ("crypto_vol_chart", "equity_vol_chart"):
        assert f in EDITABLE_FIELDS and hasattr(c, f) and getattr(c, f) is True, f
        assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
    assert "500K" in EDITABLE_FIELDS["crypto_vol_alert_min_usd"]["desc"]
    assert c.equity_vol_alert_min_usd == 100_000, "hisse eşiği değişmedi"
    print("✅ bağlantı) grafik ayarları künyeli, hisse eşiği aynı")


test_parse()
test_render()
test_scan_threshold_and_chart()
test_send_rich()
test_wiring()
print("\n✅ HACİM GRAFİĞİ TESTLERİ GEÇTİ")
