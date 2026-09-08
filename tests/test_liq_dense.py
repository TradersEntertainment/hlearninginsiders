"""🗺 /sembol ≥$200K TÜM pozisyonları listeler, hepsi grafikte çizgi olur.

Kullanıcı isteği (08.09): "200k+ tüm pozları görelim, liq olacak grafikte". Eskiden
dört ayrı sınır vardı: liste `[:3]`, taban $500K (alarm tabanı) ve grafikte üst üste
üç `[:4]`; üstüne bot compact altyazılı TEK fotoyu tercih ediyordu ve o altyazı
tekleri 2'yle sınırladığı için liste 30 satır olsa bile kullanıcı 3 satır görüyordu.

Pinlenenler:
  • İKİ EŞİK: `crypto_liq_show_usd` (SAYFA, $200K) listeyi ve grafiği besler;
    `crypto_liq_min_usd` (BİLDİRİM, $500K) alarmı VE ⭐/zincir seçimini besler —
    "sayfa daha çok gösterir, alarm daha sıkı". SAYFA eşiği bildirim eşiğinin
    üstüne çıkamaz (kırpılır); toz tabanı daha yüksekse mesajda o yazılır
  • liste yakından uzağa, SHOW_MAX'te kesilir ve kesilen sayı SÖYLENİR
  • tam metin her pozisyonu tek satır yazar; `_split` parçalar, hiçbir adres kaybolmaz
  • bot: tam metin altyazıya sığmıyorsa metin (parçalı) + foto ⭐ altyazısıyla
  • /takip teklifi en çok OFFER_MAX satıra yazılır (DB yazımı sınırlı), sonrası teklifsiz
  • plan_levels(fit_all=True): far_pct içindeki her seviye `draw`'a girer (pencere açılır);
    fit_all=False bugünkü dar pencere — alarm grafiği ve INJ koruması değişmez
  • label_groups: her seviyenin çizgisi var ama etiketler y'de birleşir, ≤ MAX_LABELS
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-dense.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.radar import cryptoliq as cl  # noqa: E402
from app.radar import liqchart  # noqa: E402
from app.radar import tracker  # noqa: E402
from app.telegram import format as fmt  # noqa: E402
from app.telegram.bot import MAX_LEN, TelegramBot  # noqa: E402

M = 0.00424
N_BIG = 34                                   # ≥$200K pozisyon sayısı


def _cfg():
    cfg = Config()
    cfg.crypto_liq_min_usd = 500_000
    cfg.crypto_liq_show_usd = 200_000
    cfg.max_liq_distance_pct = 50.0
    cfg.crypto_liq_cascade = False           # bu dosyanın konusu defter değil
    return cfg


def _rows():
    """34 × ≥$200K (0,4%…28%, birkaçı aynı kovada) + 6 küçük + 2 toz."""
    out = []

    def add(side, dist, ntl):
        liq = M * (1 - dist / 100) if side == "long" else M * (1 + dist / 100)
        out.append({"coin": "PUMP", "address": "0x%040x" % (len(out) + 1), "side": side,
                    "notional": float(ntl), "liq_px": liq, "leverage": 10.0,
                    "entry_px": M, "ts": dbm.now()})
    add("long", 0.4, 900_000)                        # ⭐ adayı: alarm tabanını da geçer
    for k in range(11):
        add("long", 1.2 + k * 0.35, 260_000 + k * 1_000)
    for k in range(8):                               # aynı kovaya düşenler (%10-15)
        add("long", 10.4 + k * 0.5, 240_000)
    for k in range(9):
        add("long", 20.5 + k * 0.8, 310_000)
    for k in range(5):
        add("short", 6.0 + k * 1.5, 220_000)
    for k in range(6):                               # SAYFA eşiği altı → listede yok
        add("long", 2.0 + k * 0.4, 150_000)
    for k in range(2):                               # toz (OI'nin binde biri altı)
        add("short", 3.0 + k, 900)
    return out


async def _seed(rows, oi_ntl=60e6):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "dense.db"))
    async with dbm.db() as c:
        for p in rows:
            await c.execute(
                "INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,"
                "upnl,notional,ts,closed_ts) VALUES(?,?,'',?,1,?,?,?,0,?,?,NULL)",
                (p["coin"], p["address"], p["side"], p["entry_px"], p["leverage"],
                 p["liq_px"], p["notional"], p["ts"]))
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": M, "oi": oi_ntl / M}}, "ts": dbm.now()})


def _candles(n=192):
    t0 = dbm.now() - n * 900
    out, px = [], M * 0.99
    for i in range(n):
        o = px
        px = px * (1 + (0.0006 if i % 3 else -0.0011))
        out.append({"t": (t0 + i * 900) * 1000, "o": str(o), "h": str(max(o, px) * 1.001),
                    "l": str(min(o, px) * 0.999), "c": str(px), "v": "1"})
    return out


class Client:
    async def meta_and_ctxs(self, dex=""):
        return [{"universe": [{"name": "PUMP"}]},
                [{"markPx": str(M), "openInterest": str(60e6 / M), "funding": "0", "dayNtlVlm": "1e8"}]]

    async def l2_book(self, coin, n_sig_figs=None):
        return {"levels": [[{"px": str(M * 0.99), "sz": "1e8"}], [{"px": str(M * 1.01), "sz": "1e8"}]]}

    async def candles(self, coin, interval, start_ms, end_ms):
        return _candles()


def test_lists_every_position():
    async def run():
        rows = _rows()
        await _seed(rows)
        s = await cl.snapshot(_cfg(), Client(), "PUMP")
        assert len(s["rows"]) == N_BIG and s["n_big"] == N_BIG, (len(s["rows"]), s["n_big"])
        assert all(r["notional"] >= 200_000 for r in s["rows"]), "SAYFA eşiği altı listeye girmez"
        d = [r["dist"] for r in s["rows"]]
        assert d == sorted(d), "yakından uzağa"
        assert s["min_usd"] == 200_000 and s["alert_usd"] == 500_000 and s["n_more"] == 0
        assert s["n_all"] == len(rows) and s["n_dust"] == 2
        # ⭐ ALARM tabanına bakar: $900K band başlıkta, $260K'lık yakın komşusu değil
        assert s["main_band"] and s["main_band"]["total"] >= 500_000, s["main_band"]
        # metin: her adres TAM BİR KEZ (band satırıyla yazılan tek de tekrar etmez)
        txt = fmt.crypto_liq_snapshot(s)
        for r in s["rows"]:
            assert txt.count(fmt.short(r["address"])) == 1, r["address"]
        assert "havuzda 42 açık pozisyon, 34'ü ≥ $200K" in txt, txt
        assert "kanala düşme eşiği $500K" in txt, "iki eşik farkı dürüstçe yazılı"
        print("✅ liste) ≥$200K 34 pozisyonun hepsi tek tek, yakından uzağa; ⭐ hâlâ alarm tabanında")
    asyncio.run(run())


def test_show_floor_clamped_by_alert_floor():
    async def run():
        await _seed(_rows())
        cfg = _cfg()
        cfg.crypto_liq_show_usd = 1_000_000          # yanlış ayar: bildirim eşiğinin üstü
        s = await cl.snapshot(cfg, Client(), "PUMP")
        assert s["min_usd"] == 500_000, "SAYFA eşiği BİLDİRİM eşiğine kenetlenir"
        assert all(r["notional"] >= 500_000 for r in s["rows"]) and s["rows"], s["rows"]
        cfg.crypto_liq_show_usd = 0                  # boş → bildirim eşiğine düşer
        assert (await cl.snapshot(cfg, Client(), "PUMP"))["min_usd"] == 500_000
        # toz tabanı (OI'nin binde biri) SAYFA eşiğinden yüksekse mesajdaki "≥ $X"
        # ONU söyler — yoksa "≥ $200K" derken $200K'lık pozisyon listede olmazdı
        await _seed(_rows(), oi_ntl=900e6)           # OI $900M → toz $900K
        cfg.crypto_liq_show_usd = 200_000
        s3 = await cl.snapshot(cfg, Client(), "PUMP")
        assert s3["dust"] == 900_000 and s3["min_usd"] == 900_000, (s3["min_usd"], s3["dust"])
        assert all(r["notional"] >= 900_000 for r in s3["rows"]) or not s3["rows"]
        print("✅ eşik) SAYFA eşiği alarm eşiğini AŞAMAZ; 0 ise ona düşer; toz tabanı yüksekse o yazılır")
    asyncio.run(run())


def test_message_splits_and_offer_cap():
    async def run():
        await _seed(_rows())
        s = await cl.snapshot(_cfg(), Client(), "PUMP")
        offers = list(range(1, tracker.OFFER_MAX + 1))
        txt = fmt.crypto_liq_snapshot(s, offers=offers)
        parts = TelegramBot._split(txt, MAX_LEN)
        assert len(parts) >= 2 and all(len(p) <= MAX_LEN for p in parts), [len(p) for p in parts]
        joined = "".join(parts)
        for r in s["rows"]:                          # parçalanınca da hiçbir adres kaybolmaz
            assert joined.count(fmt.short(r["address"])) == 1, r["address"]
        assert txt.count("/takip_") == tracker.OFFER_MAX, txt.count("/takip_")
        assert tracker.OFFER_MAX == 12 and "OFFER_MAX" in open(
            os.path.join(ROOT, "app", "telegram", "bot.py"), encoding="utf-8").read()
        print(f"✅ mesaj) tam liste {len(parts)} parçaya bölünür, adresler eksiksiz;"
              f" /takip yalnız ilk {tracker.OFFER_MAX} satırda (DB yazımı sınırlı)")
    asyncio.run(run())


def test_bot_sends_photo_plus_full_list():
    async def run():
        from app.hl import universe
        from app.radar import cryptoliq
        await _seed(_rows())
        s = await cl.snapshot(_cfg(), Client(), "PUMP")
        cfg = _cfg()
        cfg.telegram_chat_id = "111"
        bot = TelegramBot(cfg, None, None, {})
        sent, photos = [], []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.extend(TelegramBot._split(text, MAX_LEN))
            return True

        async def fake_photo(png, caption="", chat_id=None, reply_markup=None):
            photos.append(caption)
            return True
        bot.send, bot.send_photo = fake_send, fake_photo

        async def fake_snapshot(cfg_, client, coin, kind="crypto", **kw):
            return s

        async def fake_resolve(cmd):
            return {"coin": "PUMP", "symbol": "PUMP", "kind": "crypto"} if cmd == "pump" else None
        orig = cryptoliq.snapshot, universe.resolve_coin
        cryptoliq.snapshot, universe.resolve_coin = fake_snapshot, fake_resolve
        try:
            assert await bot._cmd_coin_liq("pump", "111") is True
        finally:
            cryptoliq.snapshot, universe.resolve_coin = orig
        assert len(sent) >= 2, "uzun liste parçalı gider"
        assert len(photos) == 1 and photos[0].startswith("📈 <b>PUMP</b>"), photos
        for r in s["rows"]:
            assert "".join(sent).count(fmt.short(r["address"])) == 1, r["address"]
        print("✅ bot) uzun listede foto + parçalı TAM liste; compact altyazı yolu devrede değil")
    asyncio.run(run())


def _lv(px, side, ntl, main=False, **kw):
    return {"px": px, "side": side, "notional": ntl, "main": main, **kw}


def test_plan_fit_all_and_label_groups():
    cs = [{"t": i, "o": M, "h": M * 1.005, "l": M * 0.995, "c": M} for i in range(10)]
    lv = [_lv(M * 0.996, "long", 900_000, main=True)]
    lv += [_lv(M * (1 - (0.05 + i * 0.02)), "long", 300_000) for i in range(12)]   # %5…%27
    p_off = liqchart.plan_levels(cs, M, lv, None, far_pct=50.0)
    p_on = liqchart.plan_levels(cs, M, lv, None, far_pct=50.0, fit_all=True)
    assert p_off["pinned"]["bottom"], "dar pencere: uzaklar kenarda (bugünkü davranış)"
    assert not p_on["pinned"]["bottom"] and not p_on["pinned"]["top"], "fit_all: hepsi çizgi"
    assert len(p_on["draw"]) == len(lv) and p_on["win"] >= 26, p_on["win"]
    assert p_on["lo"] < p_off["lo"], "pencere aşağı açıldı"
    # far_pct hâlâ son söz: %60'taki seviye fit_all ile de görünmez
    far = liqchart.plan_levels(cs, M, lv + [_lv(M * 0.4, "long", 1e6)], None, 50.0, fit_all=True)
    assert [round(x["_d"]) for x in far["omitted"]] == [60], far["omitted"]

    # etiketler: her seviyenin çizgisi var ama etiket sayısı sınırlı, yakınlar birleşir
    def y_of(px):                                   # 525px'lik plot kutusunu taklit et
        return 92 + (M * 1.01 - px) / (M * 0.62) * 525
    groups, rest = liqchart.label_groups(p_on["draw"], y_of)
    assert 0 < len(groups) <= liqchart.MAX_LABELS, len(groups)
    assert sum(len(g) for g in groups) + len(rest) == len(p_on["draw"]), "hiçbir seviye kaybolmaz"
    assert any(len(g) > 1 for g in groups), "üst üste binenler tek etikette"
    multi = next(g for g in groups if len(g) > 1)
    assert liqchart._group_text(multi).startswith(f"{len(multi)} long "), liqchart._group_text(multi)
    # band varsa söz onun: tekleri bir daha TOPLAMAZ ($ şişmesin)
    band = _lv(M * 0.9, "long", 5_000_000, cluster=True, n=40, px_lo=M * 0.88, px_hi=M * 0.9)
    assert liqchart._group_text([band, _lv(M * 0.9, "long", 300_000)]).startswith("LONG $5.0M")
    print("✅ pencere/etiket) fit_all tüm seviyeleri çizgi yapar (far_pct tavan); etiketler"
          f" birleşir, ≤ {liqchart.MAX_LABELS} grup, band toplamı tekrar toplanmaz")


def test_dense_render():
    async def run():
        await _seed(_rows())
        cfg = _cfg()
        s = await cl.snapshot(cfg, Client(), "PUMP")
        if s["png"] is None:
            print("⚠️ Pillow yok, PNG atlandı")
            return
        assert s["png"][:8] == b"\x89PNG\r\n\x1a\n" and len(s["png"]) > 5000
        cfg.crypto_liq_chart_fit_all = False
        s2 = await cl.snapshot(cfg, Client(), "PUMP")
        assert s2["png"] and s2["png"] != s["png"], "ayar grafiği gerçekten değiştiriyor"
        # tek pozisyonlar sağa yaslı çubuk (derinlik merdiveni), tam genişlik çizgi DEĞİL —
        # 34 tam genişlik çizgi grafiği okunmaz bir duvara çeviriyordu
        src = open(os.path.join(ROOT, "app", "radar", "liqchart.py"), encoding="utf-8").read()
        assert "STUB_MIN, STUB_MAX" in src and "plot_r - ln" in src, "derinlik merdiveni"
        assert liqchart.STUB_MAX > liqchart.STUB_MIN > 0
        sp = os.environ.get("HLR_PNG_OUT")
        if sp:
            open(os.path.join(sp, "dense_34.png"), "wb").write(s["png"])
            open(os.path.join(sp, "dense_off.png"), "wb").write(s2["png"])
        print("✅ grafik) 34 pozisyonlu PNG üretiliyor; crypto_liq_chart_fit_all ayarı uçtan uca bağlı")
    asyncio.run(run())


def test_alert_floor_untouched():
    """Ev kuralının yürütülebilir hâli: $300K sayfada var, kanalda yok."""
    async def run():
        from app.notify import Notifier
        await _seed([{"coin": "PUMP", "address": "0x" + "7" * 40, "side": "long",
                      "notional": 300_000.0, "liq_px": M * 0.99, "leverage": 10.0,
                      "entry_px": M, "ts": dbm.now()}])
        cfg = _cfg()
        cfg.crypto_chat_id = "-100"

        class Bot:
            sent = []

            async def send(self, text, chat_id=None):
                Bot.sent.append(text)
                return True
        out = await cl.scan(cfg, Client(), Notifier(cfg, Bot()))
        assert out.get("events", 0) == 0 and Bot.sent == [], (out, Bot.sent)
        s = await cl.snapshot(cfg, Client(), "PUMP")
        assert len(s["rows"]) == 1 and s["rows"][0]["notional"] == 300_000.0
        rd = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        assert "Sayfa daha çok gösterir, alarm daha sıkı" in rd
        print("✅ kural) $300K sayfada listelenir, kanala DÜŞMEZ — iki eşik gerçekten ayrı")
    asyncio.run(run())
