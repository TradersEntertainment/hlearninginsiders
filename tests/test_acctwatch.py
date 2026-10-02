"""👤 İzlenen hesaplar — drkmttr gibi adı konmuş hesapların pozisyon ve emirleri.

Kullanıcı isteği (02.10): "bu adama özel bildirim girelim, pozları için mesaj gelsin,
yine benzer şeyi yapıyorsa"; kararlar: dört olayın hepsi, 'adres:isim' listesi
(başlangıç drkmttr), AYRI GRUP (ACCOUNT_CHAT_ID) — kripto kanalı spam olmasın.

Fikstürler 02.10 canlı ölçümünden (drkmttr, 0xc179…): ETH/HYPE/XMR/LIT/VVV long,
SAND short; SAND'de 0.044–0.0605 arası 34 Gtc reduce-only alış, LIT'te 4.35–5.4 arası
29 reduce-only satış; SAND'de fiyatın dibinde post-only satış duvarı (cloid SABİT, oid
her 1–25 sn'de değişir; açık emir okumalarının %38'inde YOK — yeniden koyma boşluğu);
fiyatın %0.2–0.4 altında ~$14K'lık 6 post-only reduce-only alış (piyasa yapıcının geri
alım kotasyonu — kullanıcı "kâr al" etiketine haklı olarak itiraz etti).

Pinlenenler:
  • ilk tur "takip başladı" özeti (+ son 1 saatin GERÇEKLEŞEN dolumları); mevcut
    duvar/emir grupları sonra "yeni" diye bildirilmez
  • aç / kapa / ters çevir; ≥%25 VE ≥$500K adım, son BİLDİRİLENE göre (fiyat değil adet)
  • duvar: iki yoklama teyit; kalkış için 2 DOĞRULANMIŞ kaçırma (her biri hızlı yeniden
    bakışlarla) VE son görüşten 150 sn; aynı cloid aynı duvar
  • emir adları niyet varsaymaz: "kapatma emirleri" / "post-only kapatma kotasyonu",
    girişe göre kârda/zararda ölçülerek yazılır; kotasyon olay üretmez
  • bir yoklamanın olayları TEK mesaj; gönderilemezse durum yazılmaz (tekrar bulunur)
  • ACCOUNT_CHAT_ID boşsa ne mesaj ne yoklama
"""
import asyncio
import itertools
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
WALL_CLOID = "0xc55cc94685c36abfef0200001691bf32"


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


def ladder(coin, side, lo, hi, n, sz_each, oid0, tif="Gtc"):
    step = (hi - lo) / (n - 1)
    return [{"coin": coin, "side": side, "limitPx": f"{lo + i * step:.6g}", "sz": str(sz_each),
             "oid": oid0 + i, "isTrigger": False, "triggerPx": "0.0", "reduceOnly": True,
             "orderType": "Limit", "tif": tif} for i in range(n)]


def wall(oid, px=0.06181, sz=22_445_518.0, side="A", ro=False, cloid=None):
    return {"coin": "SAND", "side": side, "limitPx": str(px), "sz": str(sz), "oid": oid,
            "isTrigger": False, "triggerPx": "0.0", "reduceOnly": ro, "orderType": "Limit",
            "tif": "Alo", "cloid": cloid}


def quotes(oid0, top=0.06170, n=6, step=0.00002):
    """Piyasa yapıcının geri alım kotasyonu: post-only reduce-only küçük alışlar."""
    return [{"coin": "SAND", "side": "B", "limitPx": f"{top - i * step:.6g}", "sz": "237141",
             "oid": oid0 + i, "isTrigger": False, "triggerPx": "0.0", "reduceOnly": True,
             "orderType": "Limit", "tif": "Alo", "cloid": f"0x8f2439e8{i:04d}"} for i in range(n)]


_TID = itertools.count(1)


def fill(coin, d, usd, ts, px=0.0632, crossed=False):
    return {"coin": coin, "dir": d, "px": str(px), "sz": str(usd / px), "time": ts * 1000,
            "crossed": crossed, "side": "A" if d in ("Open Short", "Close Long") else "B",
            "tid": next(_TID)}


SAND_TP = ladder("SAND", "B", 0.044, 0.0605, 34, 790_000.0, 1000)
LIT_TP = ladder("LIT", "A", 4.35, 5.4, 29, 41_850.0, 2000)


class Client:
    def __init__(self):
        self.st = state(**BASE)
        self.orders = SAND_TP + LIT_TP
        self.seq = []              # sıradaki frontendOpenOrders yanıtları (yeniden bakış testleri)
        self.full = None           # openOrders (sınırsız) — None: frontend ile aynı
        self.calls = 0
        self.mids = None
        self.fills = []

    async def all_mids(self, dex=""):
        return {c: str(m) for c, m in (self.mids or MARKS).items()}

    async def open_orders(self, user, dex=""):
        self.calls += 1
        return list(self.full if self.full is not None else self.orders)

    async def clearinghouse(self, user, dex="", priority=None, stats=None):
        self.calls += 1
        return self.st

    async def frontend_open_orders(self, user, dex=""):
        self.calls += 1
        if self.seq:
            r = self.seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return list(r)
        return list(self.orders)

    async def user_fills_by_time(self, user, start_ms, end_ms=None):
        self.calls += 1
        rows = sorted((f for f in self.fills if f["time"] >= start_ms
                       and (end_ms is None or f["time"] <= end_ms)), key=lambda f: f["time"])
        return rows[:2000]


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
    aw.RECHECK_GAP = 0                      # yeniden bakış beklemesi testte yok
    aw._FLOW_CACHE.clear()
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
        cl.fills = ([fill("SAND", "Open Short", 290_000, t0 - 600 + i) for i in range(10)]
                    + [fill("SAND", "Close Short", 260_000, t0 - 300 + i, px=0.0630) for i in range(10)])
        out = await aw.run_once(cfg, cl, nt, ts=t0)
        assert out["first"] == 1 and len(nt.sent) == 1
        kind, chat, text, public = nt.sent[-1]
        assert kind == "acct" and chat == "-500" and public is False, "ayrı grup, satılabilir bota gitmez"
        for s in ("drkmttr", "takip başladı", "ETH", "SHORT 56.88M SAND",
                  "Bekleyen kapatma / stop emirleri", "henüz dolmamış", "arayüzde görünmüyor",
                  "<b>SAND</b> kapatma emirleri (alış, reduce-only): 34 emir 0.044–0.0605",
                  "fiyatın %2–29 altında · girişin altında → kârda kapatır",
                  "<b>LIT</b> kapatma emirleri (satış, reduce-only): 29 emir 4.35–5.4",
                  "girişin üstünde → kârda kapatır",
                  "🔁 <b>Son 1 saat — gerçekleşen dolumlar</b>",
                  "<b>SAND</b>: açılış (Open Short) $2.9M · kapatma (Close Short) $2.6M · %100 maker"):
            assert s in text, (s, text)
        assert "kâr al" not in text, "niyet varsayan ad yok"
        # değişiklik yok → mesaj yok
        await aw.run_once(cfg, cl, nt, ts=t0 + 60)
        assert len(nt.sent) == 1, "ilk turdaki emir grupları 'yeni' diye bildirilmez"

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
        cl.orders = SAND_TP[:-1] + LIT_TP + [wall(9001, cloid=WALL_CLOID)]
        await aw.run_once(cfg, cl, nt, ts=t0 + 240)
        assert len(nt.sent) == n, "tek görüş duvar sayılmaz"
        cl.orders = SAND_TP[:-1] + LIT_TP + [wall(9002, px=0.06180, cloid=WALL_CLOID)]
        await aw.run_once(cfg, cl, nt, ts=t0 + 300)
        text = nt.sent[-1][2]
        assert "🧲 Yeni duvar: <b>SAND</b> SATIŞ duvarı" in text and "post-only" in text, text
        assert "SHORT açıyor / büyütüyor" in text and "2 farklı emir numarası" in text
        # duvar gerçekten kalktı: kalkış için 2 doğrulanmış kaçırma VE 150 sn
        cl.orders = SAND_TP[:-1] + LIT_TP
        cl.st = state(**{**BASE, "SAND": -100_000_000.0})
        await aw.run_once(cfg, cl, nt, ts=t0 + 360)
        text = nt.sent[-1][2]
        assert "📈 <b>SAND</b> SHORT büyüttü" in text and "duvarı kalktı" not in text
        n = len(nt.sent)
        await aw.run_once(cfg, cl, nt, ts=t0 + 420)
        assert len(nt.sent) == n, "iki kaçırma ama 150 sn dolmadı"
        await aw.run_once(cfg, cl, nt, ts=t0 + 480)
        text = nt.sent[-1][2]
        assert "🧲❌ <b>SAND</b> satış duvarı kalktı" in text, text
        assert "pozisyon SHORT 79.00M SAND → SHORT 100.00M SAND" in text, text

        # merdiven değişti: yeni emirler, iki yoklama sabit kalınca
        extra = ladder("SAND", "B", 0.0606, 0.0612, 3, 500_000.0, 3000)
        cl.orders = SAND_TP[:-1] + extra + LIT_TP
        n = len(nt.sent)
        await aw.run_once(cfg, cl, nt, ts=t0 + 540)
        assert len(nt.sent) == n, "yeni emir ilk yoklamada beklenir (kurulum ortası olabilir)"
        await aw.run_once(cfg, cl, nt, ts=t0 + 600)
        text = nt.sent[-1][2]
        assert "🎯 Değiştirdi: <b>SAND</b> kapatma emirleri (alış, reduce-only): 36 emir 0.044–0.0612" in text, text
        assert "önceki 33 emir 0.044–" in text, "önceki = değişiklikten hemen önceki CANLI hal"

        # aynı yoklamada üç olay → TEK mesaj: yeni coin, kapanış, yön değişimi; toz atlanır
        st = {**BASE, "SAND": -100_000_000.0, "ETH": -500.0}
        st.pop("LIT")
        cl.st = state(**st)
        cl.st["assetPositions"].append(pos("VVV", 1.0))   # zaten var, toz değişimi
        MARKS["DUST"] = 1.0
        cl.st["assetPositions"].append({"position": {"coin": "DUST", "szi": "5000", "positionValue": "5000",
                                                     "entryPx": "1"}})
        cl.orders = SAND_TP[:-1] + extra
        await aw.run_once(cfg, cl, nt, ts=t0 + 660)
        text = nt.sent[-1][2]
        assert "🚪 <b>LIT</b> LONG kapattı" in text and "🔄 <b>ETH</b> yön değiştirdi: LONG →" in text, text
        assert "DUST" not in text, "taban altı yeni pozisyon 'açtı' sayılmaz"
        await aw.run_once(cfg, cl, nt, ts=t0 + 720)
        text = nt.sent[-1][2]
        assert "🎯❌ <b>LIT</b> kapatma emirleri kalktı (son görülen" in text and "LIT pozisyonu yok" in text, text
        print("✅ olaylar) takip başladı özeti (+ gerçekleşen dolumlar); büyüttü (adım son bildirilene"
              " göre); duvar iki yoklama + kalktı (2 kaçırma + 150 sn); merdiven değişti/kalktı,"
              " basamak dolumu sessiz; çoklu olay tek mesaj")
    asyncio.run(run())


def test_flapping_wall_never_falsely_ends():
    """Canlıdaki hata: duvar her 1–25 sn'de iptal edilip yeniden konuyor, okumaların %38'inde
    yok. Bot iki yoklama üst üste 'yok' görüp "kalktı" dedi — oysa duvar defterdeydi."""
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        await aw.run_once(cfg, cl, nt, ts=t0)
        for i, dt in enumerate((60, 120)):
            cl.orders = SAND_TP + LIT_TP + [wall(9000 + i, cloid=WALL_CLOID)]
            await aw.run_once(cfg, cl, nt, ts=t0 + dt)
        assert "🧲 Yeni duvar" in nt.sent[-1][2]
        n = len(nt.sent)
        base = SAND_TP + LIT_TP
        # 30 yoklama: her birinde ilk okuma YOK (yeniden koyma boşluğu), yeniden bakışta VAR
        # (yeni oid, aynı cloid, fiyat oynuyor)
        for i in range(30):
            cl.seq = [base, base, base + [wall(9100 + i, px=0.06181 + (i % 5) * 0.00002, cloid=WALL_CLOID)]]
            cl.orders = base
            await aw.run_once(cfg, cl, nt, ts=t0 + 180 + i * 60)
        assert len(nt.sent) == n, ("yeniden koyma boşluğu 'kalktı' değil", nt.sent[-1][2])
        # aynı cloid, eriyip tabanın altına indi (yenen kuyruk) → hâlâ aynı duvar
        cl.seq = []
        cl.orders = base + [wall(9300, sz=600_000.0, cloid=WALL_CLOID)]
        for dt in (2000, 2060, 2120, 2180):
            await aw.run_once(cfg, cl, nt, ts=t0 + dt)
        assert len(nt.sent) == n, "aynı cloid'li eriyen kuyruk kalkış değil"
        # okumada yok + yeniden bakış OKUNAMADI (429) → belirsiz, kaçırma değil (5 dk)
        for i in range(5):
            cl.seq = [base, RuntimeError("429")]
            await aw.run_once(cfg, cl, nt, ts=t0 + 2240 + i * 60)
        assert len(nt.sent) == n, "okunamayan yeniden bakış 'kalktı' saydırmaz"
        st = await dbm.kv_get(aw.STATE_KV + A)
        assert st["walls"]["SAND|ask"]["miss"] == 0
        # gerçekten kalktı: tüm yeniden bakışlarda yok → 2 doğrulanmış kaçırma (+150 sn) → bir kez
        cl.seq = []
        cl.orders = base
        calls = cl.calls
        await aw.run_once(cfg, cl, nt, ts=t0 + 2540)
        assert cl.calls - calls == 2 + aw.RECHECK_N, "kaçırma ancak RECHECK_N yeniden bakıştan sonra"
        for dt in (2600, 2660):
            await aw.run_once(cfg, cl, nt, ts=t0 + dt)
        ends = [x for x in nt.sent[n:] if "duvarı kalktı" in x[2]]
        assert len(ends) == 1, [x[2] for x in nt.sent[n:]]
        calls = cl.calls
        await aw.run_once(cfg, cl, nt, ts=t0 + 2720)
        assert cl.calls - calls == 2, "biten duvar için yeniden bakış yok"
        print("✅ duvar) okumada yok + yeniden bakışta var → kaçırma değil (30 yoklama, sıfır sahte"
              " 'kalktı'); aynı cloid eriyen kuyruk; okunamayan yeniden bakış belirsiz; gerçek"
              " kalkış tek mesaj")
    asyncio.run(run())


def test_quote_close_wall_and_entry_tags():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        # canlıdaki şekil: Gtc merdiven yok, 6 post-only RO alış kotasyonu fiyatın dibinde
        cl.orders = LIT_TP + quotes(5000)
        await aw.run_once(cfg, cl, nt, ts=t0)
        text = nt.sent[-1][2]
        assert "<b>SAND</b> post-only kapatma kotasyonu (alış, reduce-only): 6 emir" in text, text
        assert "fiyatın %0.2–0.3 altında" in text, "fiyatın dibindeki grup tek değerle yazılmaz"
        assert "kâr al" not in text
        # kotasyon yeniden konur, fiyat %1.5 kayar, sayı değişir → MESAJ YOK
        n = len(nt.sent)
        for i, dt in enumerate((60, 120, 180, 240, 300)):
            cl.orders = LIT_TP + quotes(6000 + i * 10, top=0.06170 * (1 - 0.015 * (i % 2)), n=4 + i % 3)
            await aw.run_once(cfg, cl, nt, ts=t0 + dt)
        assert len(nt.sent) == n, ("kotasyon olay üretmez", nt.sent[-1][2])
        # aynı yapışkan yolla KAPATMA: reduce-only post-only $1.4M alış duvarı fiyatın dibinde
        cl.orders = LIT_TP + quotes(7000) + [wall(9500, px=0.06178, side="B", ro=True, cloid="0xclose")]
        await aw.run_once(cfg, cl, nt, ts=t0 + 360)
        cl.orders = LIT_TP + quotes(7100) + [wall(9501, px=0.06177, side="B", ro=True, cloid="0xclose")]
        await aw.run_once(cfg, cl, nt, ts=t0 + 420)
        text = nt.sent[-1][2]
        assert "🧲 Yeni duvar: <b>SAND</b> ALIŞ duvarı" in text and "post-only, reduce-only" in text, text
        assert "SHORT'u kapatıyor" in text
        # girişin iki yanındaki Gtc kapatma emirleri → "kısmen zararda"
        from app.telegram import format as fmt
        L = {"coin": "SAND", "side": "bid", "kind": "close", "n": 3, "lo": 0.0610, "hi": 0.0625,
             "sz": 1.0, "ntl": 1.0, "ro": True}
        short = {"side": "short", "entry": 0.0618, "szi": -1.0}
        assert "kısmen zararda" in fmt._ladder_txt(L, 0.0618, short)
        assert "zararda kapatır" in fmt._ladder_txt({**L, "lo": 0.0620, "hi": 0.0630}, 0.0618, short)
        assert fmt._entry_tag(L, None) == "", "pozisyon yoksa kâr/zarar yazılmaz"
        # canlı (02.10 21:00): kotasyon 0.062187–0.062312, fiyat 0.06232 → "%0.0" değil
        assert fmt._dist_range(0.062187, 0.062312, 0.06232) == " · fiyatın %0.01–0.2 altında"
        print("✅ adlar) post-only RO kotasyon sessiz; RO post-only alış duvarı 'SHORT'u kapatıyor';"
              " Gtc kapatma emirleri girişe göre kârda/zararda/kısmen ölçülerek")
    asyncio.run(run())


def test_version_restart_and_fill_flow():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        # eski (v1) durum: 'tp' anahtarlı merdiven → sahte kalktı/kurdu yerine yeniden başla
        await dbm.kv_set(aw.STATE_KV + A, {"ts": t0 - 60, "name": "drkmttr", "chat": "-500",
                                           "pos": {}, "walls": {}, "ladders": {
                                               "SAND|bid|tp": {"coin": "SAND", "side": "bid", "kind": "tp",
                                                               "n": 34, "lo": 0.044, "hi": 0.0605, "sz": 1,
                                                               "ntl": 1, "oids": [1], "alerted": True}}})
        await aw.run_once(cfg, cl, nt, ts=t0)
        text = nt.sent[-1][2]
        assert "takip yeniden başladı (emir etiketleri düzeltildi)" in text, text
        assert "kalktı" not in text and "Kurdu" not in text
        assert (await dbm.kv_get(aw.STATE_KV + A))["v"] == aw.STATE_V
        # dolum akışı: sayfalama, eksik notu
        cl.fills = [fill("SAND", "Open Short", 100.0, t0 - 3000 + i // 2) for i in range(2500)]
        f = await aw.fill_flow(cl, A, t0 - 3600, t0, max_pages=1)
        assert f["complete"] is False and f["n"] == 2000
        f = await aw.fill_flow(cl, A, t0 - 3600, t0)
        assert f["complete"] is True and f["n"] == 2500
        assert abs(f["coins"]["SAND"]["Open Short"]["usd"] - 250_000) < 1
        from app.telegram import format as fmt
        # sayfa sınırında aynı milisaniyede 5 dolum (tek taker süpürmesi): HL'nin önerdiği gibi
        # sonraki sayfa son zamandan (dahil) başlar, tekrarlar tid ile atılır — kayıp yok
        cl.fills = [fill("SAND", "Close Short", 10.0, t0 - 3000 + i) for i in range(1998)]
        cl.fills += [fill("SAND", "Close Short", 10.0, t0 - 1000) for _ in range(5)]
        cl.fills += [fill("SAND", "Close Short", 10.0, t0 - 900 + i) for i in range(10)]
        f2 = await aw.fill_flow(cl, A, t0 - 3600, t0)
        assert f2["n"] == 2013 and f2["complete"], f2["n"]
        # tek milisaniyede 2000+ dolum: ilerlenemez → döngü yok, eksik denir
        cl.fills = [fill("SAND", "Close Short", 10.0, t0 - 500) for _ in range(2100)]
        f3 = await aw.fill_flow(cl, A, t0 - 3600, t0)
        assert f3["complete"] is False and f3["n"] == 2000
        lines = fmt._flow_lines({**f, "complete": False})
        assert any("fazlası var" in x for x in lines)
        assert fmt._flow_lines({"coins": {}, "since": t0 - 3600, "until": t0, "n": 0, "complete": True}) \
            == ["🔁 <b>Son 1 saat: gerçekleşen dolum yok</b>"]
        print("✅ sürüm+akış) v1 durumu → 'emir etiketleri düzeltildi' özeti, sahte olay yok;"
              " dolum akışı sayfalı (sınırdaki aynı-ms dolumlar kaybolmaz), eksikse söyler")
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
        cl = Client()
        t0 = dbm.now()
        cl.fills = [fill("SAND", "Close Short", 50_000, t0 - 100)]
        await aw.run_once(cfg, cl, Notifier(), ts=t0)
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
        assert "Bundan sonra" not in sent[-1][1] and "🔁" not in sent[-1][1], "istemci yoksa akış yok"
        bot.client = cl
        await bot._cmd_accounts("-500")
        assert "kapatma (Close Short) $50K" in sent[-1][1], sent[-1][1]
        calls = cl.calls
        await bot._cmd_accounts("-500")
        assert cl.calls == calls, "60 sn önbellek: tekrar komut HL'ye yüklenmez"
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
        print("✅ kablo) /hesaplar son kayıt + gerçekleşen dolumlar (60 sn önbellek); spawn, sağlık,"
              " tür (satılmaz), env-only kanal, 8 ayar künyeli")
    asyncio.run(run())


def test_review_regressions_wall_ladder_tpsl_dust():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        await aw.run_once(cfg, cl, nt, ts=t0)
        # duvar kur (iki yoklama)
        cl.orders = SAND_TP + LIT_TP + [wall(9001)]
        await aw.run_once(cfg, cl, nt, ts=t0 + 60)
        cl.orders = SAND_TP + LIT_TP + [wall(9002)]
        await aw.run_once(cfg, cl, nt, ts=t0 + 120)
        assert "🧲 Yeni duvar" in nt.sent[-1][2]
        n = len(nt.sent)
        # fiyat %1.5 kaydı: duvar aynı yerde durur → "kalktı" YOK (histerezis)
        cl.mids = {**MARKS, "SAND": 0.0627}
        cl.st = state(**BASE)
        cl.st["assetPositions"] = [p_ for p_ in cl.st["assetPositions"] if p_["position"]["coin"] != "SAND"]
        cl.st["assetPositions"].append({"position": {"coin": "SAND", "szi": str(BASE["SAND"]),
                                                     "positionValue": str(abs(BASE["SAND"]) * 0.0627),
                                                     "entryPx": "0.0632"}})
        for dt in (180, 240, 300):
            await aw.run_once(cfg, cl, nt, ts=t0 + dt)
        assert len(nt.sent) == n, "fiyat oynaması duvar kalkışı değil"
        # duvar çekildi, yanında $3K'lık küçük emir kaldı → duvarı canlı tutmaz
        cl.mids = None
        cl.st = state(**BASE)
        cl.orders = SAND_TP + LIT_TP + [wall(9100, sz=50_000.0)]
        for dt in (360, 420, 480):
            await aw.run_once(cfg, cl, nt, ts=t0 + dt)
        assert "duvarı kalktı" in nt.sent[-1][2], "küçük yabancı emir duvarı yaşatmaz"
        # 15 dk içinde dönen duvar → "geri geldi"
        cl.orders = SAND_TP + LIT_TP + [wall(9201)]
        await aw.run_once(cfg, cl, nt, ts=t0 + 540)
        cl.orders = SAND_TP + LIT_TP + [wall(9202)]
        await aw.run_once(cfg, cl, nt, ts=t0 + 600)
        assert "🧲↩️ Duvar geri geldi" in nt.sent[-1][2], nt.sent[-1][2]
        # merdivende aynı fiyata yeniden koyma (yeni oid) → mesaj YOK
        n = len(nt.sent)
        refill = SAND_TP[:-1] + [{**SAND_TP[-1], "oid": 7777}]
        cl.orders = refill + LIT_TP + [wall(9203)]
        for dt in (660, 720, 780):
            await aw.run_once(cfg, cl, nt, ts=t0 + dt)
        assert len(nt.sent) == n, "aynı yere yeniden koyma 'Değiştirdi' değil"
        # pozisyona bağlı TP/SL (sz 0) → ayrı gruplar, "tüm pozisyon", tetik fiyatıyla $
        tpsl = [{"coin": "ETH", "side": "A", "limitPx": "2790", "sz": "0.0", "oid": 8001, "isTrigger": True,
                 "triggerPx": "3100", "isPositionTpsl": True, "reduceOnly": True, "orderType": "Take Profit Market"},
                {"coin": "ETH", "side": "A", "limitPx": "2160", "sz": "0.0", "oid": 8002, "isTrigger": True,
                 "triggerPx": "2400", "isPositionTpsl": True, "reduceOnly": True, "orderType": "Stop Market"}]
        cl.orders = refill + LIT_TP + tpsl + [wall(9204)]
        await aw.run_once(cfg, cl, nt, ts=t0 + 840)
        await aw.run_once(cfg, cl, nt, ts=t0 + 900)
        text = "\n".join(x[2] for x in nt.sent[n:])
        assert "<b>ETH</b> kâr al (tetik) (satış, reduce-only): 1 emir 3100 · tüm pozisyon" in text, text
        assert "<b>ETH</b> stop (satış, reduce-only): 1 emir 2400 · tüm pozisyon" in text, text
        assert f"(${4108.379 * 3100 / 1e6:.1f}M)" in text, "TP $'ı tetik fiyatıyla"
        assert "girişin üstünde → kârda kapatır" in text and "girişin altında → zararda kapatır" in text
        print("✅ inceleme) histerezis; küçük emir duvarı yaşatmaz; geri geldi; yeniden koyma sessiz;"
              " pozisyon TP/SL ayrı, 'tüm pozisyon', girişe göre kâr/zarar")
    asyncio.run(run())


def test_review_regressions_lists_mute_restart_dust():
    async def run():
        # kesik liste: frontend 100 döner, openOrders daha fazla → görünmeyen "kalktı" sayılmaz
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        filler = [{"coin": "HYPE", "side": "B", "limitPx": str(50 + i * 0.01), "sz": "10", "oid": 50_000 + i,
                   "isTrigger": False, "reduceOnly": True, "orderType": "Limit"} for i in range(80)]
        allo = SAND_TP + LIT_TP + filler
        cl.orders = allo[:100]
        cl.full = allo
        await aw.run_once(cfg, cl, nt, ts=t0)
        hidden = [o for o in allo if o not in allo[:100]]
        cl.orders = [o for o in allo if o not in LIT_TP][:100]   # pencere kaydı: LIT gizlendi, yeni dolgu göründü
        for dt in (60, 120, 180):
            await aw.run_once(cfg, cl, nt, ts=t0 + dt)
        assert len(nt.sent) == 1, ("kesik listede pencere kayması olay üretmez", nt.sent[-1][2])
        assert "kesik" in nt.sent[0][2], "kesik olduğu açıkça yazılır"
        assert hidden, "fikstür gerçekten kesik"
        # bildirim kapalı → yoklama yok
        cfg = await _fresh()
        cfg.notify_acct = False
        cl, nt = Client(), Notifier()
        out = await aw.run_once(cfg, cl, nt, ts=t0)
        assert out["muted"] == 1 and cl.calls == 0 and not nt.sent
        # uzun ara → eski fark yazılmaz, "takip yeniden başladı"
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        await aw.run_once(cfg, cl, nt, ts=t0)
        cl.st = state(**{**BASE, "SAND": -10_000_000.0})
        await aw.run_once(cfg, cl, nt, ts=t0 + 7200)
        text = nt.sent[-1][2]
        assert "takip yeniden başladı" in text and "küçülttü" not in text, text
        # grup değişti → yeni gruba özet
        cfg.account_chat_id = "-777"
        await aw.run_once(cfg, cl, nt, ts=t0 + 7260)
        assert nt.sent[-1][1] == "-777" and "— <b>takip başladı</b>" in nt.sent[-1][2]
        # listeden çıkar → durum silinir; geri ekle → "takip başladı"
        cfg.acct_watch_list = ""
        await aw.run_once(cfg, cl, nt, ts=t0 + 7320)
        assert not await dbm.kv_get(aw.STATE_KV + A)
        cfg.acct_watch_list = f"{A}:drkmttr"
        await aw.run_once(cfg, cl, nt, ts=t0 + 7380)
        assert "— <b>takip başladı</b>" in nt.sent[-1][2]
        # toz pozisyon fiyatla tabanı geçerse "açtı" YOK
        MARKS["DUST2"] = 1.0
        st = state(**{**BASE, "SAND": -10_000_000.0})
        st["assetPositions"].append({"position": {"coin": "DUST2", "szi": "95000", "positionValue": "95000",
                                                  "entryPx": "1"}})
        cl.st = st
        n = len(nt.sent)
        await aw.run_once(cfg, cl, nt, ts=t0 + 7440)
        st["assetPositions"][-1]["position"]["positionValue"] = "101000"
        await aw.run_once(cfg, cl, nt, ts=t0 + 7500)
        assert len(nt.sent) == n, "yalnız fiyatla tabanı geçen toz 'açtı' değil"
        print("✅ inceleme) kesik liste güvenli; bildirim kapalıysa yoklama yok; uzun ara / grup"
              " değişimi / yeniden ekleme → özet; toz fiyatla 'açtı' olmaz")
    asyncio.run(run())
