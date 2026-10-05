"""🔂 Dilimli alım-satım — TWAP emri olmadan dilim dilim alan hesabın dizileri.

Kullanıcı isteği (05.10): "hisse tarafında adam TWAP emri vermeden TWAP atıyor;
almaya başladığında ve bitirdiğinde bildirim istiyorum — sadece CBRS" (0x30af…393e).
Kararlar: hesap takip grubu (ACCOUNT_CHAT_ID), alış + satış, mesajda aynı dakikalardaki
diğer hisse işlemleri tek satır.

Fikstürler canlı ölçümden (03–05.10): her dilim tek Limit IOC (5 dolum / 3 seviye, aynı
ms, %100 taker, twapId yok, limit dolum ortalamasının %0.279 üstü), dilimler arası
21–50 sn, dilim ~$8K; aynı dakikalarda DRAM/INTC/MU Close Long satışları; 04.10 15–16
UTC CBRS hacminin %16.9'u.

Pinlenenler:
  • ilk tur "izleme başladı" özeti; 2 emir sessiz, 3. emirde TEK "BAŞLADI"; ara <300 sn
    bitirmez; 300 sn sessizlikte TEK "BİTTİ" (toplam, hacim payı, pozisyon, diğer coinler)
  • yalnız listedeki coin mesaj üretir; diğerleri "aynı dakikalarda" satırında
  • tek ters emir alımı bitirmez; yerleşen satış dizisi "ardından SATIŞ" + "SATIŞ BAŞLADI"
  • okuma hatası / boş (gecikmeli düğüm) / eksik okumada bitiş yok (veri yok ≠ sessizlik)
  • sayfa sınırında bölünen IOC bir kez; süreklilik sondası (gap → yeniden başla)
  • ≤2 sa kesinti yeniden oynatılır (geç notu, başla-bit tek mesaj); >2 sa → yeniden başladı
  • kanal yok / tür kapalı → 0 istek; gönderilemeyen mesaj → durum yazılmaz, geri çekilme
"""
import asyncio
import collections
import datetime as dt
import itertools
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-slice.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import slicewatch as sw  # noqa: E402

A = "0x30afce2f6842bf183c7e3fe7162e279ff0b6393e"
UTC = dt.timezone.utc
T0 = int(dt.datetime(2026, 10, 5, 0, 8, tzinfo=UTC).timestamp() * 1000)   # 03:08 TSİ
GAPS = [22, 32, 43, 28, 50, 35, 31, 25, 40, 33]                         # ölçülen 21.4–50.3 sn
OTHERS = (("xyz:DRAM", 3400.0, 63.0), ("xyz:INTC", 3400.0, 116.6), ("xyz:MU", 2700.0, 1030.0))
BANNED = ("muhtemel", "tahmin", "olabilir", "bekleniyor")
_TID = itertools.count(10_000_000)
_OID = itertools.count(565_000_000_000)


def order(coin, side, t_ms, usd, px, pos0, n=5, levels=3, d=None, twap=None):
    """Tek IOC emri: n dolum AYNI ms'de, `levels` fiyat seviyesi, startPosition sıralı."""
    oid = next(_OID)
    each = usd / px / n
    rows, pos = [], pos0
    for i in range(n):
        p = px * (1 + 0.0002 * (i % levels)) if side == "B" else px * (1 - 0.0002 * (i % levels))
        rows.append({"coin": coin, "side": side, "px": f"{p:.4f}", "sz": f"{each:.6f}", "time": t_ms,
                     "oid": oid, "tid": next(_TID), "crossed": True, "twapId": twap,
                     "dir": d or ("Open Long" if side == "B" else "Close Long"),
                     "startPosition": f"{pos:.6f}", "hash": "0xabc", "fee": "0", "closedPnl": "0"})
        pos = pos + each if side == "B" else pos - each
    return rows


def campaign(coin, side, t0_ms, n, usd=8000.0, px=179.0, pos0=119_400.0, others=True, gaps=GAPS):
    """n dilimlik dizi; her dilimin ardından aynı saniyede DRAM/INTC/MU satış dilimleri."""
    rows, t, pos = [], t0_ms, pos0
    for i in range(n):
        rows += order(coin, side, t, usd, px, pos)
        pos += usd / px if side == "B" else -usd / px
        if others:
            for j, (c, u, p) in enumerate(OTHERS):
                rows += order(c, "A", t + 300 + j * 50, u, p, 100_000.0, n=2, levels=1, d="Close Long")
        t += gaps[i % len(gaps)] * 1000
    return rows


def last_t(rows, coin="xyz:CBRS"):
    return max(f["time"] for f in rows if f["coin"] == coin)


class Client:
    def __init__(self):
        self.fills = []
        self.now_ms = None              # bu andan sonraki dolumlar henüz "olmadı"
        self.fail = None                # istisna → user_fills_by_time fırlatır
        self.empty = False              # gecikmeli düğüm: boş yanıt
        self.calls = collections.Counter()
        self.dexes = []
        self.szi = 146_329.36
        self.share = 0.169              # mum hacmi: kendi adedi / bu pay

    def _vis(self):
        return [f for f in self.fills if self.now_ms is None or f["time"] <= self.now_ms]

    async def user_fills_by_time(self, user, start_ms, end_ms=None):
        self.calls["fills"] += 1
        if self.fail:
            raise self.fail
        if self.empty:
            return []
        rows = [f for f in self._vis() if f["time"] >= start_ms and (end_ms is None or f["time"] <= end_ms)]
        rows.sort(key=lambda f: (f["time"], f["tid"]))
        return rows[:2000]

    async def clearinghouse(self, user, dex="", priority=None, stats=None):
        self.calls["ch"] += 1
        self.dexes.append(dex)
        return {"assetPositions": [{"position": {
            "coin": "xyz:CBRS", "szi": str(self.szi), "positionValue": str(self.szi * 179.03),
            "entryPx": "180.6741", "unrealizedPnl": "-240582.48", "liquidationPx": "142.7345796675",
            "leverage": {"type": "isolated", "value": 4}}}],
            "marginSummary": {"accountValue": "10737866.78"}}

    async def order_status(self, user, oid):
        self.calls["os"] += 1
        fs = [f for f in self.fills if f["oid"] == oid]
        sz = sum(float(f["sz"]) for f in fs)
        vw = sum(float(f["sz"]) * float(f["px"]) for f in fs) / sz
        lim = vw * (1.00279 if fs[0]["side"] == "B" else 1 - 0.00279)
        return {"status": "order", "order": {"order": {
            "coin": fs[0]["coin"], "side": fs[0]["side"], "limitPx": f"{lim:.4f}", "sz": "0.0",
            "oid": oid, "orderType": "Limit", "origSz": f"{sz:.6f}", "tif": "Ioc", "cloid": None},
            "status": "filled", "statusTimestamp": fs[0]["time"]}}

    async def candles(self, coin, interval, start_ms, end_ms):
        self.calls["candles"] += 1
        his = sum(float(f["sz"]) for f in self._vis()
                  if f["coin"] == coin and start_ms <= f["time"] <= end_ms)
        mins = max(1, (end_ms - start_ms) // 60_000 + 1)
        v = his / self.share / mins
        return [{"t": start_ms + i * 60_000, "v": f"{v:.6f}", "o": "179", "c": "179"} for i in range(mins)]


class Notifier:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def send(self, kind, text, **kw):
        self.sent.append((kind, kw.get("chat_id"), text, kw.get("public")))
        return self.ok


async def _fresh(chat="-500", ticker=True):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "slice.db"))
    if ticker:
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", ("xyz:CBRS", "xyz", "CBRS"))
            await c.commit()
    sw._BACKOFF.clear()
    cfg = Config()
    cfg.account_chat_id = chat
    cfg.slice_watch_list = f"{A}:CBRS"
    return cfg


async def _tick(cfg, cl, nt, ts_ms):
    """Saat ts_ms'deyken bir tur (o ana kadarki dolumlar görünür)."""
    cl.now_ms = ts_ms
    return await sw.run_once(cfg, cl, nt, ts=ts_ms // 1000)


def _texts(nt, n0=0):
    return [x[2] for x in nt.sent[n0:]]


def test_parse_list_default():
    got = sw.parse_slice_list(f"{A}:cbrs, 0xBAD:X, {A}:MU:balina, 0x{'b' * 40}:CBRS, {A}")
    assert got == {A: {"name": "balina", "syms": ["CBRS", "MU"]},
                   "0x" + "b" * 40: {"name": "", "syms": ["CBRS"]}}, got
    assert sw.parse_slice_list(Config().slice_watch_list) == {A: {"name": "", "syms": ["CBRS"]}}
    print("✅ liste) adres:SEMBOL[:isim], aynı adres gruplanır, geçersiz atlanır; varsayılan 0x30af… CBRS")


def test_orders_from_fills():
    rows = (order("xyz:CBRS", "B", T0, 8000, 179.0, 119_400.0)
            + order("xyz:CBRS", "A", T0 + 1000, 4000, 179.0, 119_444.69, d="Close Long")
            + order("xyz:CBRS", "B", T0 + 2000, 1000, 179.0, 0.0, twap=987))
    os_ = sw.orders_from_fills(list(reversed(rows)))
    assert [o["side"] for o in os_] == ["B", "A", "B"], "zamana göre sıralı"
    b, a, tw = os_
    assert b["n_fills"] == 5 and b["levels"] == 3 and b["taker"] and b["twap"] is None
    assert abs(b["usd"] - 8000) < 5 and abs(b["sz"] - 8000 / 179) < 1e-3
    assert abs(b["pos_before"] - 119_400) < 1e-3 and abs(b["pos_after"] - (119_400 + 8000 / 179)) < 1e-3
    assert abs(a["pos_before"] - 119_444.69) < 1e-3 and abs(a["pos_after"] - (119_444.69 - 4000 / 179)) < 1e-3
    assert tw["twap"] == 987
    print("✅ emir) dolumlar oid ile tek emir: 5 dolum / 3 seviye, $, startPosition'dan önce/sonra; twapId")


def test_buy_campaign_start_end():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 20)
        end_t = last_t(cl.fills)
        # ilk tur: dizi henüz yok → "izleme başladı", son 6 saatte yok, pozisyon
        await _tick(cfg, cl, nt, T0 - 60_000)
        assert len(nt.sent) == 1
        kind, chat, text, public = nt.sent[0]
        assert kind == "slice" and chat == "-500" and public is False, "hesap grubu, satılmaz"
        for s in ("Dilimli alım-satım — izleme başladı", "son 6 saatte dilimli alım/satım yok",
                  "📊 Pozisyon: <b>LONG 146.3K CBRS</b>", "4x izole", "liq 142.73",
                  "aynı yönde ≥3 emir ve ≥$10K", "Diğer coinler yalnız bağlam satırında"):
            assert s in text, (s, text)
        # 2 emir → sessiz
        await _tick(cfg, cl, nt, T0 + 30_000)
        assert len(nt.sent) == 1, "2 emir dizi sayılmaz"
        # 3. emir (T0+54 sn) → TEK "BAŞLADI"
        await _tick(cfg, cl, nt, T0 + 60_000)
        assert len(nt.sent) == 2, _texts(nt)
        text = nt.sent[-1][2]
        for s in ("🔂🟢 <b>CBRS · DİLİMLİ ALIM BAŞLADI</b>", "İlk dilim <b>03:08 TSİ</b>",
                  "şimdiye 3 emir", "HL TWAP emri değil (dolumlarda twapId yok)",
                  "her dilim tek <b>Limit IOC</b> emri", "%0.28 üstünde", "5 dolum / 3 fiyat seviyesi",
                  "tam doldu", "📊 Pozisyon LONG 119.4K CBRS →", "<b>DRAM</b> kapatma (Close Long)",
                  "<b>INTC</b>", "<b>MU</b>", "Bitti mesajı: aynı yönde 5 dk"):
            assert s in text, (s, text)
        assert not any(b in text.lower() for b in BANNED)
        # dizi sürer: her 30 sn yeni dilim → sessiz; ara <300 sn bitirmez
        t = T0 + 90_000
        while t <= end_t + 299_000:
            await _tick(cfg, cl, nt, t)
            t += 30_000
        assert len(nt.sent) == 2, ("sürerken ve 300 sn dolmadan mesaj yok", _texts(nt, 2))
        # 300 sn sessizlik → TEK "BİTTİ"
        await _tick(cfg, cl, nt, end_t + 301_000)
        assert len(nt.sent) == 3, _texts(nt, 2)
        text = nt.sent[-1][2]
        dram_n = 19                       # son dilimden SONRAKİ diğer coin işlemi diziye yazılmaz
        for s in ("🔂🏁 <b>CBRS · DİLİMLİ ALIM BİTTİ</b>", "<b>03:08 →", "Toplam <b>20 emir",
                  "son dilimden sonra 5 dk aynı yönde dilim gelmedi", "%100 taker",
                  "📈 CBRS hacmindeki payı: <b>%16.9</b>", "📊 Pozisyon LONG 119.4K CBRS → <b>LONG 120.3K CBRS</b>",
                  f"<b>DRAM</b> kapatma (Close Long) $65K ({dram_n} emir)", "dilimler arası medyan"):
            assert s in text, (s, text)
        assert "geç fark edildi" not in text and "Başlangıç ölçülmedi" not in text
        assert not any(b in text.lower() for b in BANNED)
        assert set(cl.dexes) == {"xyz"}, "pozisyon xyz dex'inden okunur"
        # bitti: tekrar mesaj yok; DRAM/INTC/MU dizileri asla mesaj üretmez
        await _tick(cfg, cl, nt, end_t + 400_000)
        assert len(nt.sent) == 3
        assert all("DRAM · DİLİMLİ" not in x and "MU · DİLİMLİ" not in x for x in _texts(nt))
        print("✅ alış) özet → 2 emir sessiz → 3.'de TEK BAŞLADI (03:08 TSİ, Limit IOC %0.28, DRAM/INTC/MU"
              " satırı) → sürerken sessiz → 300 sn'de TEK BİTTİ (20 emir, %16.9 hacim, pozisyon önce→sonra)")
    asyncio.run(run())


def test_sell_flip_and_stray():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        buys = campaign("xyz:CBRS", "B", T0, 30, others=False)
        b_end = last_t(buys)
        stray_t = T0 + 5 * 32_000 + 5_000
        stray = order("xyz:CBRS", "A", stray_t, 4000, 179.0, 120_000.0, d="Close Long")
        s0 = b_end + 40_000
        sells = campaign("xyz:CBRS", "A", s0, 6, pos0=120_500.0, others=False)
        cl.fills = buys + stray + sells
        await _tick(cfg, cl, nt, T0 - 60_000)                 # özet
        t = T0 + 30_000
        while t < s0 + 90_000:
            await _tick(cfg, cl, nt, t)
            t += 30_000
        texts = _texts(nt, 1)
        starts = [x for x in texts if "ALIM BAŞLADI" in x]
        assert len(starts) == 1, texts
        assert not any("ALIM BİTTİ" in x for x in texts[:-1]), "tek ters emir alımı bitirmez"
        flip = texts[-1]
        assert "CBRS · DİLİMLİ ALIM BİTTİ" in flip and "ardından SATIŞ dizisi başladı" in flip, flip
        assert "CBRS · DİLİMLİ SATIŞ BAŞLADI" in flip
        assert flip.index("ALIM BİTTİ") < flip.index("SATIŞ BAŞLADI"), "önce biten, sonra başlayan"
        assert "<b>CBRS</b> kapatma (Close Long) $4K (1 emir)" in flip, "ters emir bağlam satırında"
        assert "Toplam <b>30 emir" in flip
        # satış dizisi biter
        await _tick(cfg, cl, nt, last_t(sells) + 301_000)
        assert "CBRS · DİLİMLİ SATIŞ BİTTİ" in nt.sent[-1][2]
        print("✅ yön) tek ters emir alımı bitirmez (bağlam satırında); yerleşen satış dizisi: ALIM BİTTİ"
              " 'ardından SATIŞ' + SATIŞ BAŞLADI tek mesaj; satış da biter")
    asyncio.run(run())


def test_incomplete_or_unsure_never_ends():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 6)
        end_t = last_t(cl.fills)
        await _tick(cfg, cl, nt, T0 - 60_000)
        await _tick(cfg, cl, nt, end_t + 10_000)
        assert "ALIM BAŞLADI" in nt.sent[-1][2]
        n = len(nt.sent)
        st0 = await dbm.kv_get(sw.STATE_KV + A)
        # okuma hatası: durum değişmez, bitti yok
        cl.fail = RuntimeError("429")
        out = await _tick(cfg, cl, nt, end_t + 400_000)
        assert out["errors"] == 1 and len(nt.sent) == n
        assert (await dbm.kv_get(sw.STATE_KV + A))["cursor"] == st0["cursor"]
        cl.fail = None
        # gecikmeli düğüm: boş sayfa (imleç dolumları gelmedi) → belirsiz, bitti yok
        sw.UNSURE_MAX = 3
        try:
            cl.empty = True
            for i in range(3):
                out = await _tick(cfg, cl, nt, end_t + 430_000 + i * 30_000)
                assert out["unsure"] == 1 and len(nt.sent) == n, i
            # UNSURE_MAX tur sonra boşluk kabul edilir → bitti
            await _tick(cfg, cl, nt, end_t + 520_000)
            assert "ALIM BİTTİ" in nt.sent[-1][2] and len(nt.sent) == n + 1
        finally:
            sw.UNSURE_MAX = 10
            cl.empty = False

        # eksik okuma (yetişme): sayfa sınırına takılan turda bitti yok, tamamlanınca bir kez
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 6)
        end_t = last_t(cl.fills)
        await _tick(cfg, cl, nt, T0 - 60_000)
        await _tick(cfg, cl, nt, end_t + 10_000)
        n = len(nt.sent)
        cl.fills += [order("xyz:DRAM", "A", end_t + 20_000 + i * 50, 3400.0, 63.0, 100_000.0, n=1)[0]
                     for i in range(2500)]
        sw.CATCHUP_PAGES = 1
        try:
            await _tick(cfg, cl, nt, end_t + 400_000)
            assert len(nt.sent) == n, "eksik okumada bitti yok"
            assert (await dbm.kv_get(sw.STATE_KV + A))["cursor"] < end_t + 20_000 + 2499 * 50
            await _tick(cfg, cl, nt, end_t + 430_000)
        finally:
            sw.CATCHUP_PAGES = 4
        assert len(nt.sent) == n + 1 and "Toplam <b>6 emir" in nt.sent[-1][2], _texts(nt, n)
        print("✅ belirsiz) okuma hatası / boş sayfa (gecikmeli düğüm) / eksik okuma → bitti yok, durum"
              " korunur; tamamlanınca TEK bitti")
    asyncio.run(run())


def test_page_boundary_same_ms_and_gap():
    async def run():
        await _fresh()
        cl = Client()
        T = T0 + 5_000_000
        cl.fills = [order("xyz:DRAM", "A", T0 + i * 100, 3400.0, 63.0, 0.0, n=1)[0] for i in range(1998)]
        ioc = order("xyz:CBRS", "B", T, 8000, 179.0, 119_400.0)        # 5 dolum aynı ms
        cl.fills += ioc
        r = await sw.fetch_fills(cl, A, T0, [], 1)
        assert r["complete"] is False and all(f["time"] < T for f in r["fills"]), "son ms grubu bırakılır"
        assert r["cursor"] == T0 + 1997 * 100 and r["edge"]
        r2 = await sw.fetch_fills(cl, A, r["cursor"], r["edge"], 1)
        os_ = sw.orders_from_fills(r2["fills"])
        assert r2["complete"] and len(os_) == 1 and os_[0]["n_fills"] == 5, "bölünen IOC bir kez"
        # süreklilik: imleç dolumları geçmişten silinmiş → gap
        cl.fills = [f for f in cl.fills if f["time"] > T0 + 1997 * 100]
        assert (await sw.fetch_fills(cl, A, r["cursor"], r["edge"], 1)).get("gap")
        cl.fills = []
        assert (await sw.fetch_fills(cl, A, r["cursor"], r["edge"], 1)).get("unsure")
        print("✅ okuma) sayfa sınırındaki ms grubu sonraki tura → IOC bölünmez; edge yok → gap; boş → belirsiz")
    asyncio.run(run())


def test_first_run_ongoing_pre_and_warm():
    async def run():
        # süren dizi: 2 sa önce başladı, son dilim 20 sn önce → "SÜRÜYOR …'den beri", ayrıca BAŞLADI yok
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 400)
        end_t = last_t(cl.fills)
        now_ms = T0 + 2 * 3600_000
        await _tick(cfg, cl, nt, now_ms)
        assert len(nt.sent) == 1
        text = nt.sent[0][2]
        assert "şu an <b>ALIM SÜRÜYOR</b> — 03:08 TSİ'den beri" in text, text
        assert "Limit IOC" in text and "<b>DRAM</b> kapatma (Close Long)" in text
        assert "pencerenin dışında" not in text
        t = now_ms + 30_000
        while t <= end_t + 290_000:
            await _tick(cfg, cl, nt, t)
            t += 30_000
        assert len(nt.sent) == 1, "sürerken mesaj yok"
        await _tick(cfg, cl, nt, end_t + 301_000)
        texts = _texts(nt, 1)
        assert not any("BAŞLADI" in x for x in texts), "süren dizi için ayrıca BAŞLADI yok"
        assert len(texts) == 1 and "ALIM BİTTİ" in texts[0] and "Toplam <b>400 emir" in texts[0]
        assert "Başlangıç ölçülmedi" not in texts[0]

        # pencereden önce başlamış dizi → "daha öncesi bakılan pencerenin dışında" + bitişte uyarı
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        start = T0 - 7 * 3600_000
        cl.fills = campaign("xyz:CBRS", "B", start, 900, others=False)
        end_t = last_t(cl.fills)
        now_ms = start + 7 * 3600_000 + 60_000
        assert end_t > now_ms - 60_000, "fikstür: dizi hâlâ sürüyor"
        await _tick(cfg, cl, nt, now_ms)
        assert "pencerenin dışında" in nt.sent[-1][2], nt.sent[-1][2]
        await _tick(cfg, cl, nt, end_t + 301_000)
        assert "Başlangıç ölçülmedi" in nt.sent[-1][2] and "ALIM BİTTİ" in nt.sent[-1][2]

        # ısınma birkaç tura yayılır: tamamlanana kadar mesaj yok
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 300)
        sw.LOOKBACK_PAGES = 1
        try:
            out = await _tick(cfg, cl, nt, T0 + 3 * 3600_000)
            assert out["warm"] == 1 and not nt.sent and (await dbm.kv_get(sw.STATE_KV + A))["warm"]
            for i in range(1, 6):
                await _tick(cfg, cl, nt, T0 + 3 * 3600_000 + i * 30_000)
                if nt.sent:
                    break
        finally:
            sw.LOOKBACK_PAGES = 8
        assert len(nt.sent) == 1 and "izleme başladı" in nt.sent[0][2]
        assert "son dizi: ALIM 03:08 →" in nt.sent[0][2], nt.sent[0][2]
        print("✅ ilk tur) süren dizi '…'den beri' (ayrıca BAŞLADI yok, bitişi gelir); pencere öncesi"
              " başlayan 'ölçülmedi'; ısınma birkaç tura yayılır, tamamlanınca TEK özet")
    asyncio.run(run())


def test_replay_late_end_collapse_and_restart():
    async def run():
        # ≤2 sa kesinti: dizi kesintide bitti → geç fark edilen TEK bitti
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 10)
        end_t = last_t(cl.fills)
        await _tick(cfg, cl, nt, T0 - 60_000)
        await _tick(cfg, cl, nt, T0 + 120_000)
        assert "ALIM BAŞLADI" in nt.sent[-1][2]
        await _tick(cfg, cl, nt, end_t + 20 * 60_000)                # 20 dk sonra uyandı
        text = nt.sent[-1][2]
        assert "ALIM BİTTİ" in text and "Toplam <b>10 emir" in text and "geç fark edildi" in text, text
        # kesintide başlayıp biten dizi → TEK "BAŞLADI VE BİTTİ"
        n = len(nt.sent)
        t1 = end_t + 25 * 60_000
        cl.fills += campaign("xyz:CBRS", "B", t1, 8, pos0=120_000.0)
        await _tick(cfg, cl, nt, t1 + 40 * 60_000)
        assert len(nt.sent) == n + 1 and "BAŞLADI VE BİTTİ" in nt.sent[-1][2], _texts(nt, n)

        # >2 sa kesinti → "yeniden başladı", aradakiler yazılmaz; pencere dışındaki eski dizi
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 10)
        await _tick(cfg, cl, nt, T0 - 60_000)
        await _tick(cfg, cl, nt, T0 + 120_000)
        n = len(nt.sent)
        await _tick(cfg, cl, nt, T0 + 7 * 3600_000)
        text = nt.sent[-1][2]
        assert len(nt.sent) == n + 1 and "izleme yeniden başladı (son yoklama 6 s 58 dk önce" in text, text
        assert "Kesintiden önce: <b>CBRS</b> ALIM sürüyordu" in text and "BİTTİ" not in text

        # gap: imleç dolumları HL geçmişinden düşmüş → yeniden başla
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 10)
        end_t = last_t(cl.fills)
        await _tick(cfg, cl, nt, T0 - 60_000)
        await _tick(cfg, cl, nt, end_t + 10_000)
        cl.fills = [f for f in cl.fills if f["time"] > T0 + 200_000] + campaign(
            "xyz:CBRS", "B", end_t + 60_000, 3, pos0=121_000.0)
        st = await dbm.kv_get(sw.STATE_KV + A)
        st["cursor"], st["edge"] = T0, [str(cl.fills[0]["tid"] - 999_999)]
        await dbm.kv_set(sw.STATE_KV + A, st)
        await _tick(cfg, cl, nt, end_t + 200_000)
        assert "HL geçmişinde artık yok" in nt.sent[-1][2], nt.sent[-1][2]
        print("✅ kesinti) ≤2 sa: geç bitiş notu, kesintide başla-bit tek mesaj; >2 sa: 'yeniden başladı'"
              " + kesintiden önceki dizi; gap → yeniden başla")
    asyncio.run(run())


def test_no_chat_muted_backoff_and_removal():
    async def run():
        cfg = await _fresh(chat="")
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 5)
        out = await _tick(cfg, cl, nt, T0)
        assert out["no_chat"] == 1 and not cl.calls and not nt.sent, "kanal yoksa istek yok"
        cfg = await _fresh()
        cfg.notify_slice = False
        out = await _tick(cfg, cl, nt, T0)
        assert out["muted"] == 1 and not cl.calls and not nt.sent

        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 12)
        await _tick(cfg, cl, nt, T0 - 60_000)
        nt.ok = False
        await _tick(cfg, cl, nt, T0 + 60_000)                         # BAŞLADI gönderilemedi
        assert out is not None and len(nt.sent) == 2
        st = await dbm.kv_get(sw.STATE_KV + A)
        assert not (st.get("runs") or {}), "gönderilemeyen olayın durumu yazılmaz"
        calls = sum(cl.calls.values())
        out = await _tick(cfg, cl, nt, T0 + 90_000)
        assert out["backoff"] == 1 and sum(cl.calls.values()) == calls, "geri çekilmede istek yok"
        nt.ok = True
        await _tick(cfg, cl, nt, T0 + 150_000)
        assert "ALIM BAŞLADI" in nt.sent[-1][2] and len(nt.sent) == 3
        # listeden çıkarılınca durum silinir
        cfg.slice_watch_list = ""
        await _tick(cfg, cl, nt, T0 + 180_000)
        assert not await dbm.kv_get(sw.STATE_KV + A)
        print("✅ kenar) kanal yok / tür kapalı → 0 istek; gönderilemeyen olay → durum yazılmaz + geri"
              " çekilme; listeden çıkan adresin durumu silinir")
    asyncio.run(run())


def test_wiring_and_status_command():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.fills = campaign("xyz:CBRS", "B", T0, 12)
        await _tick(cfg, cl, nt, T0 - 60_000)
        await _tick(cfg, cl, nt, T0 + 120_000)
        from app.telegram.bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.cfg, bot.client = cfg, None
        sent = []

        async def _send(t, chat=""):
            sent.append(t)
            return True
        bot.send = _send
        import app.telegram.bot as botmod
        real_now = botmod.now
        botmod.now = lambda: (T0 + 130_000) // 1000
        try:
            await bot._cmd_accounts("-500")
        finally:
            botmod.now = real_now
        assert sent and "🔂 <b>Dilimli alım-satım</b>" in sent[0], sent
        assert "<b>CBRS</b>: <b>ALIM SÜRÜYOR</b> — 03:08 TSİ'den beri" in sent[0] and "son dilim" in sent[0]
        assert len(sent) >= 2 and "🔂" not in sent[-1], "hesap özeti en sonda (🔁 testi sent[-1]'e bakar)"
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "slicewatch.loop(" in rd("app", "main.py")
        assert "SLICE_WATCH_LIST=" in rd(".env.example") and "Dilimli alım-satım" in rd("README.md")
        assert "slicewatch_stats" in rd("app", "diag.py")
        from app.health import limits
        from app.hl.client import HLClient
        from app.notify import KINDS, PUBLIC_KINDS
        from app.telegram import format as fmt
        assert KINDS["slice"][2] == "high" and "slice" not in PUBLIC_KINDS, "kişisel liste satılmaz"
        assert "slicewatch" in limits(Config()) and fmt.TASK_TR.get("slicewatch")
        assert hasattr(HLClient, "order_status")
        assert "account_chat_id" not in EDITABLE_FIELDS, "sohbet kimliği yalnız env"
        for f in ("slice_watch_enabled", "notify_slice", "slice_watch_list", "slice_poll_sec",
                  "slice_quiet_sec", "slice_start_orders", "slice_start_usd"):
            assert f in EDITABLE_FIELDS and hasattr(Config(), f), f
            assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
        print("✅ kablo) /hesaplar en üstte 🔂 durum (kv'den), spawn, sağlık, tür satılmaz, env-only kanal,"
              " 7 ayar künyeli, order_status, README/.env, /tani")
    asyncio.run(run())
