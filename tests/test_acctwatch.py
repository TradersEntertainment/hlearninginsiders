"""👤 İzlenen hesaplar — drkmttr gibi adı konmuş hesapların pozisyon ve emirleri.

Kullanıcı isteği (02.10): "bu adama özel bildirim girelim, pozları için mesaj gelsin,
yine benzer şeyi yapıyorsa"; kararlar: dört olayın hepsi, 'adres:isim' listesi
(başlangıç drkmttr), AYRI GRUP (ACCOUNT_CHAT_ID) — kripto kanalı spam olmasın.

Fikstürler 02.10 canlı ölçümünden (drkmttr, 0xc179…): ETH/HYPE/XMR/LIT/VVV long,
SAND short; SAND'de 0.044–0.0605 arası 34 reduce-only alış (kâr al), LIT'te 4.35–5.4
arası 29 reduce-only satış; zaman zaman SAND'de fiyatın dibinde post-only satış duvarı.

Pinlenenler:
  • ilk tur "takip başladı" özeti; mevcut duvar/merdiven sonra "yeni" diye bildirilmez
  • aç / kapa / ters çevir; ≥%25 VE ≥$500K adım, son BİLDİRİLENE göre (fiyat değil adet)
  • duvar: iki yoklama teyit; kalkınca süre + pozisyon değişimi
  • merdiven: basamak dolumu mesaj üretmez; yeni emir iki yoklama sabitse "değiştirdi"
  • bir yoklamanın olayları TEK mesaj; gönderilemezse durum yazılmaz (tekrar bulunur)
  • ACCOUNT_CHAT_ID boşsa ne mesaj ne yoklama
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-acct.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.radar import acctwatch as aw  # noqa: E402

A = "0xc179e03922afe8fa9533d3f896338b9fb87ce0c8"
MARKS = {"SAND": 0.0618, "LIT": 3.7638, "ETH": 2700.0, "HYPE": 89.8, "XMR": 544.0, "VVV": 29.3}


def pos(coin, szi, entry=None):
    m = MARKS[coin]
    return {"position": {"coin": coin, "szi": str(szi), "positionValue": str(abs(szi) * m),
                         "entryPx": str(entry or m), "liquidationPx": None,
                         "leverage": {"type": "cross", "value": 4}}}


def state(**coins):
    ap = [pos(c, s) for c, s in coins.items()]
    tot = sum(abs(s) * MARKS[c] for c, s in coins.items())
    return {"assetPositions": ap, "marginSummary": {"accountValue": "13109817", "totalNtlPos": str(tot)}}


BASE = dict(ETH=4108.379, SAND=-56_884_213.0, HYPE=70_447.69, VVV=35_821.97, LIT=1_213_691.0, XMR=12_908.673)


def ladder(coin, side, lo, hi, n, sz_each, oid0):
    step = (hi - lo) / (n - 1)
    return [{"coin": coin, "side": side, "limitPx": f"{lo + i * step:.6g}", "sz": str(sz_each),
             "oid": oid0 + i, "isTrigger": False, "triggerPx": "0.0", "reduceOnly": True,
             "orderType": "Limit", "tif": "Gtc"} for i in range(n)]


def wall(oid, px=0.06181, sz=22_445_518.0):
    return {"coin": "SAND", "side": "A", "limitPx": str(px), "sz": str(sz), "oid": oid,
            "isTrigger": False, "triggerPx": "0.0", "reduceOnly": False, "orderType": "Limit", "tif": "Alo"}


SAND_TP = ladder("SAND", "B", 0.044, 0.0605, 34, 790_000.0, 1000)
LIT_TP = ladder("LIT", "A", 4.35, 5.4, 29, 41_850.0, 2000)


class Client:
    def __init__(self):
        self.st = state(**BASE)
        self.orders = SAND_TP + LIT_TP
        self.calls = 0

    async def clearinghouse(self, user, dex="", priority=None, stats=None):
        self.calls += 1
        return self.st

    async def frontend_open_orders(self, user, dex=""):
        self.calls += 1
        return list(self.orders)


class Notifier:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def send(self, kind, text, **kw):
        self.sent.append((kind, kw.get("chat_id"), text, kw.get("public")))
        return self.ok


async def _fresh(chat="-500"):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "acct.db"))
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {c: {"m": m, "oi": 1, "v": 1e8} for c, m in MARKS.items()},
                                       "ts": dbm.now()})
    cfg = Config()
    cfg.account_chat_id = chat
    return cfg


def test_parse_watch_list_and_default():
    got = aw.parse_watch_list(f"{A}:drkmttr, 0xBAD:x, {A.upper().replace('0X', '0x')}:dup ,"
                              f" 0x{'b' * 40}")
    assert got == [(A, "drkmttr"), ("0x" + "b" * 40, "0xbbbbbb")], got
    assert aw.parse_watch_list(Config().acct_watch_list) == [(A, "drkmttr")], "başlangıç: drkmttr"
    print("✅ liste) adres:isim, geçersiz/tekrar atlanır; varsayılan drkmttr")


def test_started_then_events_in_one_message():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        out = await aw.run_once(cfg, cl, nt, ts=t0)
        assert out["first"] == 1 and len(nt.sent) == 1
        kind, chat, text, public = nt.sent[-1]
        assert kind == "acct" and chat == "-500" and public is False, "ayrı grup, satılabilir bota gitmez"
        for s in ("drkmttr", "takip başladı", "ETH", "SHORT 56.88M SAND", "Kâr al / stop emirleri",
                  "arayüzde görünmüyor", "<b>SAND</b> kâr al (alış, reduce-only): 34 emir 0.044–0.0605",
                  "<b>LIT</b> kâr al (satış, reduce-only): 29 emir 4.35–5.4", "fiyatın %2–29 altında"):
            assert s in text, (s, text)
        # değişiklik yok → mesaj yok
        await aw.run_once(cfg, cl, nt, ts=t0 + 60)
        assert len(nt.sent) == 1, "ilk turdaki merdivenler 'yeni' diye bildirilmez"

        # SHORT büyüdü (+33%, ~$1.16M) + basamak doldu (oid kayboldu) → TEK satır (merdiven sessiz)
        cl.st = state(**{**BASE, "SAND": -75_689_743.0})
        cl.orders = SAND_TP[:-1] + LIT_TP
        await aw.run_once(cfg, cl, nt, ts=t0 + 120)
        text = nt.sent[-1][2]
        assert "📈 <b>SAND</b> SHORT büyüttü: 56.88M → <b>75.69M</b> SAND (+33%" in text, text
        assert "🎯" not in text, "basamak dolumu mesaj üretmez"
        # küçük değişim (adım altı) → mesaj yok; adım son BİLDİRİLENE göre
        cl.st = state(**{**BASE, "SAND": -79_000_000.0})
        n = len(nt.sent)
        await aw.run_once(cfg, cl, nt, ts=t0 + 180)
        assert len(nt.sent) == n

        # yeni post-only duvar: iki yoklama teyit, yeniden koyma sayılır
        cl.orders = SAND_TP[:-1] + LIT_TP + [wall(9001)]
        await aw.run_once(cfg, cl, nt, ts=t0 + 240)
        assert len(nt.sent) == n, "tek görüş duvar sayılmaz"
        cl.orders = SAND_TP[:-1] + LIT_TP + [wall(9002, px=0.06180)]
        await aw.run_once(cfg, cl, nt, ts=t0 + 300)
        text = nt.sent[-1][2]
        assert "🧲 Yeni duvar: <b>SAND</b> SATIŞ duvarı" in text and "post-only" in text, text
        assert "SHORT açıyor / büyütüyor" in text and "2 farklı emir numarası" in text
        # duvar kalktı (iki yoklama yok) → süre + pozisyon değişimi
        cl.orders = SAND_TP[:-1] + LIT_TP
        cl.st = state(**{**BASE, "SAND": -100_000_000.0})
        await aw.run_once(cfg, cl, nt, ts=t0 + 360)
        text = nt.sent[-1][2]
        assert "📈 <b>SAND</b> SHORT büyüttü" in text and "duvarı kalktı" not in text
        await aw.run_once(cfg, cl, nt, ts=t0 + 420)
        text = nt.sent[-1][2]
        assert "🧲❌ <b>SAND</b> satış duvarı kalktı" in text and "pozisyon 79.00M → 100.00M SAND" in text, text

        # merdiven değişti: yeni emirler, iki yoklama sabit kalınca
        extra = ladder("SAND", "B", 0.0606, 0.0612, 3, 500_000.0, 3000)
        cl.orders = SAND_TP[:-1] + extra + LIT_TP
        n = len(nt.sent)
        await aw.run_once(cfg, cl, nt, ts=t0 + 480)
        assert len(nt.sent) == n, "yeni emir ilk yoklamada beklenir (kurulum ortası olabilir)"
        await aw.run_once(cfg, cl, nt, ts=t0 + 540)
        text = nt.sent[-1][2]
        assert "🎯 Değiştirdi: <b>SAND</b> kâr al (alış, reduce-only): 36 emir 0.044–0.0612" in text, text
        assert "önceki 34 emir 0.044–0.0605" in text

        # aynı yoklamada üç olay → TEK mesaj: yeni coin, kapanış, yön değişimi; toz atlanır
        st = {**BASE, "SAND": -100_000_000.0, "ETH": -500.0}
        st.pop("LIT")
        cl.st = state(**st)
        cl.st["assetPositions"].append(pos("VVV", 1.0))   # zaten var, toz değişimi
        MARKS["DUST"] = 1.0
        cl.st["assetPositions"].append({"position": {"coin": "DUST", "szi": "5000", "positionValue": "5000",
                                                     "entryPx": "1"}})
        cl.orders = SAND_TP[:-1] + extra
        await aw.run_once(cfg, cl, nt, ts=t0 + 600)
        text = nt.sent[-1][2]
        assert "🚪 <b>LIT</b> LONG kapattı" in text and "🔄 <b>ETH</b> yön değiştirdi: LONG →" in text, text
        assert "DUST" not in text, "taban altı yeni pozisyon 'açtı' sayılmaz"
        await aw.run_once(cfg, cl, nt, ts=t0 + 660)
        text = nt.sent[-1][2]
        assert "🎯❌ <b>LIT</b> kâr al emirleri kalktı" in text and "LIT pozisyonu yok" in text, text
        print("✅ olaylar) takip başladı özeti; büyüttü (adım son bildirilene göre); duvar iki yoklama"
              " + kalktı (pozisyon değişimi); merdiven değişti/kalktı, basamak dolumu sessiz;"
              " çoklu olay tek mesaj")
    asyncio.run(run())


def test_no_chat_no_polls_and_failed_send_retries():
    async def run():
        cfg = await _fresh(chat="")
        cl, nt = Client(), Notifier()
        out = await aw.run_once(cfg, cl, nt, ts=dbm.now())
        assert out["no_chat"] == 1 and cl.calls == 0 and not nt.sent, "kanal yoksa yoklama da yok"
        # ilk tur gönderilemezse bir sonraki tur yine ilk tur
        cfg = await _fresh()
        cl, nt = Client(), Notifier(ok=False)
        t0 = dbm.now()
        out = await aw.run_once(cfg, cl, nt, ts=t0)
        assert out["failed"] == 1 and out["first"] == 0
        nt.ok = True
        out = await aw.run_once(cfg, cl, nt, ts=t0 + 60)
        assert out["first"] == 1 and "takip başladı" in nt.sent[-1][2]
        # olay mesajı gönderilemezse durum yazılmaz → aynı olay tekrar bulunur
        cl.st = state(**{**BASE, "SAND": -75_689_743.0})
        nt.ok = False
        await aw.run_once(cfg, cl, nt, ts=t0 + 120)
        nt.ok = True
        await aw.run_once(cfg, cl, nt, ts=t0 + 180)
        assert "SHORT büyüttü" in nt.sent[-1][2], "gönderilemeyen olay kaybolmaz"
        print("✅ kenar) ACCOUNT_CHAT_ID yoksa istek yok; başarısız gönderimde durum yazılmaz")
    asyncio.run(run())


def test_wiring_and_command():
    async def run():
        cfg = await _fresh()
        await aw.run_once(cfg, Client(), Notifier(), ts=dbm.now())
        from app.telegram.bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.cfg, bot.client = cfg, None
        sent = []

        async def _send(t, chat=""):
            sent.append((chat, t))
            return True
        bot.send = _send
        await bot._cmd_accounts("-500")
        assert "anlık durum" in sent[-1][1] and "SHORT 56.88M SAND" in sent[-1][1]
        assert "Bundan sonra" not in sent[-1][1]
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "acctwatch.loop(" in rd("app", "main.py")
        assert "ACCOUNT_CHAT_ID=" in rd(".env.example") and "ACCOUNT_CHAT_ID" in rd("README.md")
        assert "/hesaplar" in rd("README.md")
        from app.health import limits
        from app.notify import KINDS, PUBLIC_KINDS
        assert "acct" in KINDS and "acct" not in PUBLIC_KINDS, "kişisel liste satılmaz"
        assert "acctwatch" in limits(Config())
        assert "account_chat_id" not in EDITABLE_FIELDS, "sohbet kimliği yalnız env"
        for f in ("acct_watch_enabled", "acct_watch_list", "notify_acct", "acct_poll_sec",
                  "acct_min_pos_usd", "acct_step_pct", "acct_step_min_usd", "acct_wall_min_usd"):
            assert f in EDITABLE_FIELDS and hasattr(Config(), f), f
            assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
        print("✅ kablo) /hesaplar son kaydı gösterir; spawn, sağlık, tür (satılmaz), env-only kanal,"
              " 8 ayar künyeli")
    asyncio.run(run())
