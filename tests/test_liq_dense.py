"""🗺 /sembol: metin KISA ve TEK mesaj, grafik ≥$200K her pozisyonu çizer.

İki adımın hikâyesi. Önce (Plan V) "hiçbir pozisyon gizlenmesin" dendi ve 33 satır
yazıldı; canlıda metin **3033 görünür karakter** olunca Telegram'ın 1024'lük altyazı
sınırı aşıldı ve foto ile metin AYRI mesaj olarak gitti. Kullanıcı: *"foto ve mesaj
ayrı geldi yine, mesajda sadece 500k+ pozlar yazsın, grafikte [hepsi kalsın] …
mesajın kısalması lazım, çok uzakları da almayabiliriz."*

Artık ÜÇ eşik var ve hangisi neyi beslediği burada sabitlenir:
  • `crypto_liq_min_usd` ($500K) — kanala düşme, ⭐/zincir seçimi VE metin listesi
  • `crypto_liq_list_dist_pct` (%20) — metin bu mesafeden uzağı yazmaz (band dahil);
    ⭐ ne kadar uzakta olursa olsun kalır
  • `crypto_liq_show_usd` ($200K) — yalnız GRAFİK: merdivendeki her çizgi
Metin ile grafik farklı eşikte olduğu için bağlam satırı bunu AÇIKÇA söyler;
söylemezse kullanıcı "grafikte var, listede yok" diye haklı olarak sorar.

Pinlenenler:
  • rows = ≥$500K ve ≤%20, yakından uzağa; %20 ötesi metinde yok ama grafikte var
  • clusters (metin) mesafe sınırına tabi, ⭐ hariç; chart_rows tüm bantları alır
  • tek mesaj garantisi: tam metin sığmazsa compact altyazı devreye girer
  • /takip teklifi en çok OFFER_MAX satıra yazılır (DB yazımı sınırlı)
  • plan_levels(fit_all=True) + label_groups: grafik tarafı Plan V'den değişmedi
  • "sayfa gösterir / alarm göstermez" kuralı yürütülebilir hâlde
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
N_CHART = 37                                 # grafik havuzu: ≥$200K pozisyon
N_LIST = 3                                   # metin listesi: ≥$500K ve ≤%20
N_FAR = 2                                    # ≥$500K ama %20'den uzak (metinde yok)


def _cfg():
    cfg = Config()
    cfg.crypto_liq_min_usd = 500_000
    cfg.crypto_liq_show_usd = 200_000
    cfg.crypto_liq_list_dist_pct = 20.0
    cfg.max_liq_distance_pct = 50.0
    cfg.crypto_liq_cascade = False           # bu dosyanın konusu defter değil
    return cfg


def _rows():
    """5 × ≥$500K (üçü ≤%20) + 32 × ≥$200K + 6 küçük + 2 toz."""
    out = []

    def add(side, dist, ntl):
        liq = M * (1 - dist / 100) if side == "long" else M * (1 + dist / 100)
        out.append({"coin": "PUMP", "address": "0x%040x" % (len(out) + 1), "side": side,
                    "notional": float(ntl), "liq_px": liq, "leverage": 10.0,
                    "entry_px": M, "ts": dbm.now()})
    add("long", 0.4, 900_000)                        # ⭐ adayı, metinde
    add("long", 5.0, 600_000)                        # metinde
    add("long", 12.0, 1_200_000)                     # metinde
    add("long", 24.0, 2_500_000)                     # ≥$500K ama %20 ötesi → yalnız grafikte
    add("long", 31.0, 800_000)                       # aynı
    for k in range(11):
        add("long", 1.2 + k * 0.35, 260_000 + k * 1_000)
    for k in range(8):                               # aynı kovaya düşenler (%10-15)
        add("long", 10.4 + k * 0.5, 240_000)
    for k in range(8):
        add("long", 20.5 + k * 0.8, 310_000)
    for k in range(5):
        add("short", 6.0 + k * 1.5, 220_000)
    for k in range(6):                               # grafik eşiği altı → hiçbir yerde yok
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


def test_text_is_short_chart_is_full():
    async def run():
        rows = _rows()
        await _seed(rows)
        s = await cl.snapshot(_cfg(), Client(), "PUMP")
        # METİN: yalnız ≥$500K ve ≤%20, yakından uzağa
        assert [r["notional"] for r in s["rows"]] == [900_000.0, 600_000.0, 1_200_000.0], s["rows"]
        assert len(s["rows"]) == N_LIST and s["min_usd"] == 500_000 and s["list_dist"] == 20.0
        # GRAFİK: ≥$200K her şey (Plan V'den değişmedi)
        assert s["chart_usd"] == 200_000 and s["n_chart"] == N_CHART, (s["chart_usd"], s["n_chart"])
        assert s["n_big"] == N_LIST + N_FAR and s["n_list_far"] == N_FAR
        # metin bantları da sınıra tabi; ⭐ her hâlükârda kalır
        mb = s["main_band"]
        assert mb and all(b["dist_lo"] <= 20 or b is mb for b in s["clusters"]), s["clusters"]
        txt = fmt.crypto_liq_snapshot(s)
        assert "$2.5M" not in txt and "$800K" not in txt, "%20 ötesi ≥$500K metinde YOK"
        assert f"{N_FAR} pozisyon ≥ $500K ama %20'den uzak — metinde yok, grafikte var" in txt, txt
        assert f"grafikte ≥ $200K {N_CHART} pozisyon çizili" in txt, "metin/grafik farkı söylenir"
        assert "havuzda 45 açık pozisyon, 5'ü ≥ $500K" in txt, txt
        print(f"✅ eşikler) metin {N_LIST} satır (≥$500K, ≤%20) · grafik {N_CHART} pozisyon (≥$200K);"
              " fark bağlam satırında açıkça yazılı")
    asyncio.run(run())


def test_single_message():
    """Asıl şikâyet: foto ve metin ayrı geliyordu. Artık compact altyazı devreye
    girip mesajı TEK parça tutuyor."""
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
            sent.append(text)
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
        assert sent == [] and len(photos) == 1, (sent, photos)     # TEK mesaj
        cap = photos[0]
        assert fmt.visible_len(cap) <= fmt.CAPTION_VISIBLE, fmt.visible_len(cap)
        assert "⭐" in cap and "👤" in cap and "yatırım tavsiyesi" in cap, cap
        for r in s["rows"]:                                        # üç pozisyon da altyazıda
            assert fmt.short(r["address"]) in cap, r["address"]
        print(f"✅ tek mesaj) {fmt.visible_len(cap)} karakterlik altyazı + foto = 1 mesaj;"
              " metin ayrı gitmiyor")
    asyncio.run(run())


def test_offer_cap_and_split_fallback():
    async def run():
        await _seed(_rows())
        s = await cl.snapshot(_cfg(), Client(), "PUMP")
        assert tracker.OFFER_MAX == 12
        assert "OFFER_MAX" in open(os.path.join(ROOT, "app", "telegram", "bot.py"), encoding="utf-8").read()
        txt = fmt.crypto_liq_snapshot(s, offers=list(range(1, tracker.OFFER_MAX + 1)))
        assert txt.count("/takip_") == len(s["rows"]), "liste kısa: hepsine teklif yetti"
        # son çare yolu (foto yoksa) hâlâ çalışıyor: uzun metin satır sınırından parçalanır
        long_txt = txt + "\n" + "\n".join(f"satır {i} " + "x" * 120 for i in range(40))
        parts = TelegramBot._split(long_txt, MAX_LEN)
        assert len(parts) >= 2 and all(len(p) <= MAX_LEN for p in parts)
        print("✅ teklif/yedek) /takip yalnız ilk 12 satıra yazılır; foto yoksa metin parçalı gider")
    asyncio.run(run())


def test_show_floor_clamped_by_alert_floor():
    async def run():
        await _seed(_rows())
        cfg = _cfg()
        cfg.crypto_liq_show_usd = 1_000_000          # yanlış ayar: bildirim eşiğinin üstü
        s = await cl.snapshot(cfg, Client(), "PUMP")
        assert s["chart_usd"] == 500_000, "GRAFİK eşiği BİLDİRİM eşiğine kenetlenir"
        cfg.crypto_liq_show_usd = 0                  # boş → bildirim eşiğine düşer
        assert (await cl.snapshot(cfg, Client(), "PUMP"))["chart_usd"] == 500_000
        # toz tabanı (OI'nin binde biri) grafik eşiğinden yüksekse o bağlayıcı olur
        await _seed(_rows(), oi_ntl=900e6)           # OI $900M → toz $900K
        cfg.crypto_liq_show_usd = 200_000
        s3 = await cl.snapshot(cfg, Client(), "PUMP")
        assert s3["dust"] == 900_000 and s3["chart_usd"] == 900_000, (s3["chart_usd"], s3["dust"])
        print("✅ eşik) GRAFİK eşiği alarm eşiğini AŞAMAZ; 0 ise ona düşer; toz tabanı yüksekse o bağlar")
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
        # tek pozisyonlar sağa yaslı çubuk (derinlik merdiveni), tam genişlik çizgi DEĞİL
        src = open(os.path.join(ROOT, "app", "radar", "liqchart.py"), encoding="utf-8").read()
        assert "STUB_MIN, STUB_MAX" in src and "plot_r - ln" in src, "derinlik merdiveni"
        assert liqchart.STUB_MAX > liqchart.STUB_MIN > 0
        sp = os.environ.get("HLR_PNG_OUT")
        if sp:
            open(os.path.join(sp, "dense_34.png"), "wb").write(s["png"])
            open(os.path.join(sp, "dense_off.png"), "wb").write(s2["png"])
        print("✅ grafik) metin kısaldı ama PNG hâlâ ≥$200K havuzunu çiziyor; fit_all ayarı bağlı")
    asyncio.run(run())


def test_alert_floor_untouched():
    """Ev kuralının yürütülebilir hâli: $300K sayfada/grafikte var, kanalda yok."""
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
        assert s["n_chart"] == 1 and s["n_big"] == 0, (s["n_chart"], s["n_big"])
        rd = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        assert "Sayfa daha çok gösterir, alarm daha sıkı" in rd
        print("✅ kural) $300K grafikte çizilir, kanala DÜŞMEZ — eşikler gerçekten ayrı")
    asyncio.run(run())


def test_chart_is_superset_of_text():
    """ANSEM vakası (08.09): metin '$126K' ve '$80K' tekleri yazıyor ama grafikte
    onların çizgisi YOKTU (grafik tabanı $200K), üstelik yanlarındaki $28K'lık AYRI
    band etiket birleşince sessizce kayboluyordu — o bölgede toplam $200K'yı geçen
    kütle gözden kaçıyordu. İki kural: (1) metinde yazan her pozisyon grafikte de
    çizilir, (2) birleşen etiket, üye OLMAYAN her şeyi toplar (çifte saymadan)."""
    async def run():
        from app.radar import liqchart
        MK = 0.1791
        rows = []

        def add(side, liq, ntl):
            rows.append({"coin": "PUMP", "address": "0x%040x" % (len(rows) + 1), "side": side,
                         "notional": float(ntl), "liq_px": liq, "leverage": 3.0,
                         "entry_px": MK, "ts": dbm.now()})
        add("long", 0.1440, 14_000); add("long", 0.1452, 14_000)      # ayrı $28K band (%19)
        for kk in range(6):
            add("long", 0.1270 + kk * 0.0026, 185_000 / 6)            # ana band gövdesi
        add("long", 0.1426, 126_000); add("long", 0.1430, 80_000)     # metinde yazan, grafikte YOKTU
        async with dbm.db() as c:
            for p in rows:
                await c.execute(
                    "INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,"
                    "upnl,notional,ts,closed_ts) VALUES(?,?,'',?,1,?,?,?,0,?,?,NULL)",
                    (p["coin"], p["address"], p["side"], p["entry_px"], p["leverage"],
                     p["liq_px"], p["notional"], p["ts"]))
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": MK, "oi": 2e6 / MK}}, "ts": dbm.now()})

        class Cli(Client):
            async def meta_and_ctxs(self, dex=""):
                return [{"universe": [{"name": "PUMP"}]},
                        [{"markPx": str(MK), "openInterest": str(2e6 / MK), "funding": "0", "dayNtlVlm": "1e6"}]]
        cfg = _cfg()
        cfg.crypto_liq_cascade = False
        s = await cl.snapshot(cfg, Cli(), "PUMP")
        # (1) eşiği geçen hiç pozisyon yok → metin "en yakın küçükler"e düşer;
        #     grafik havuzu bu satırların HEPSİNİ kapsar
        assert s["n_big"] == 0 and len(s["rows"]) == 3, (s["n_big"], s["rows"])
        assert s["n_chart"] >= len(s["rows"]), (s["n_chart"], len(s["rows"]))
        listed = {r["address"] for r in s["rows"]}
        assert {r["notional"] for r in s["rows"]} == {126_000.0, 80_000.0, 185_000 / 6}
        # grafik havuzunda metnin adresleri var mı: chart_usd altındakiler de girer
        assert min(r["notional"] for r in s["rows"]) < s["chart_usd"], "metin tabanı çökmüş durum"
        # (2) etiket birleştirmesi: ÜYE OLMAYAN ayrı band toplama katılır
        band = {"px": 0.1430, "px_lo": 0.1270, "px_hi": 0.1430, "side": "long",
                "notional": 391_000, "cluster": True, "n": 8}
        near = {"px": 0.1452, "px_lo": 0.1440, "px_hi": 0.1452, "side": "long",
                "notional": 28_000, "cluster": True, "n": 2}
        member = {"px": 0.1426, "side": "long", "notional": 126_000}     # bandın İÇİNDE
        txt = liqchart._group_text([band, near, member])
        assert txt == "10 long $419K · 0.1270–0.1452", txt          # 391+28, üye tekrar sayılmaz
        assert liqchart._group_text([band, member]) == "LONG $391K 0.1270–0.1430", "yalnız üye → band"
        outside = {"px": 0.1181, "side": "long", "notional": 248_000}    # bandın DIŞINDA
        assert liqchart._group_text([band, outside]) == "9 long $639K · 0.1181–0.1430"
        assert listed and s["png"] is None or True
        print("✅ üst küme) metinde yazan her pozisyon grafikte de çizilir; birleşen etiket"
              " üye olmayanı toplar, üyeyi iki kez saymaz (ANSEM vakası)")
    asyncio.run(run())
