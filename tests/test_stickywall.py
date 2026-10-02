"""🧲 Yapışkan duvar — en iyi fiyata yapışan dev TEK emir.

Kullanıcı isteği (02.10, SAND ekranı): "büyük bir emir sürekli oralarda bekliyor…
aşağı da gidebiliyor yukarı da… 1M$'dan büyükse ve hacme oranla yüksekse bildirim
istiyorum." Kararlar: ad "🧲 Yapışkan duvar"; ≥$1M VE 24s hacmin ≥%2'si; ana dex
kripto; ilk alarm kripto kanalına, gerisi yalnız /takip_N'e basana.

Fikstürler 02.10 canlı ölçümünden:
  • SAND defteri: 0.062640'ta 23,249,781 SAND n=1 ($1,456,366), önünde beş ~$300'lük
    tek emir, alış tarafı toplam ~$6K; 24s hacim $13.93M → %10.5.
  • sahip 0xc179… (vault "drkmttr"): post-only (Alo), reduce-only DEĞİL, SHORT 38.1M SAND.
  • WS akışı: 110 sn'de 0xc179 $200.5K maker, sonraki en büyük $27.8K.
  • ETH: en büyük seviye n=17 $1.09M, hacmin %0.09'u → sinyal DEĞİL.

Pinlenenler:
  • n==1 şart (MM yığını değil), en iyi fiyata 10 seviye / %1 içinde
  • $ VE hacim oranı birlikte; hacim bilinmiyorsa alarm yok
  • tek görüş alarm üretmez; ≥60 sn arayla ikinci görüşte teyit
  • sahip akıştan, açık emirle doğrulanır (✓) — doğrulanamazsa ≈ ve bunu YAZAR
  • kanal tek alarm; yarılanma / yeni dilim / bitiş / yeniden geliş YALNIZ takipçiye
  • bitiş: son kalanın ≥%70'i sahibin dolumu → yenildi; ≤%20 → çekildi; sahip yok → kayboldu
  • gönderilemeyen alarm/not işaretlenmez, sonraki adım dener
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-sticky.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.radar import stickywall as sw  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

OWNER = "0xc179e03922afe8fa9533d3f896338b9fb87ce0c8"
MM = "0x4e60e3a4a32d63d245aa4e4ce304b4254b088f95"
TAKER = "0x" + "1" * 40
SAND_VOL, SAND_MARK, SAND_OI = 13_930_000.0, 0.062545, 92_900_000.0
WALL_PX, WALL_SZ = 0.062640, 23_249_781.0


def sand_book(px=WALL_PX, sz=WALL_SZ, ahead=5, n=1):
    """Ekrandaki şekil: önde `ahead` küçük tek emir, sonra dev tek emir."""
    tick = 0.000001
    asks = [{"px": f"{px - (ahead - i) * tick:.6f}", "sz": "4800", "n": 1} for i in range(ahead)]
    if sz:
        asks.append({"px": f"{px:.6f}", "sz": str(sz), "n": n})
    asks += [{"px": f"{px + (i + 1) * 0.00002:.6f}", "sz": "3000", "n": 1} for i in range(8)]
    best_ask = float(asks[0]["px"])
    bids = [{"px": f"{best_ask - (i + 1) * tick:.6f}", "sz": "16000", "n": 1} for i in range(6)]
    return {"levels": [bids, asks]}


def order(px=WALL_PX, sz=WALL_SZ, tif="Alo", ro=False):
    return {"coin": "SAND", "side": "A", "limitPx": str(px), "sz": str(sz), "origSz": str(sz),
            "tif": tif, "orderType": "Limit", "reduceOnly": ro, "isTrigger": False,
            "cloid": "0xc10d", "timestamp": 1_790_000_000_000, "oid": 563569844697}


def short_state(szi=-38_094_162.0, ntl=2_381_075.0):
    return {"assetPositions": [{"position": {"coin": "SAND", "szi": str(szi), "positionValue": str(ntl),
                                             "entryPx": "0.064675", "liquidationPx": "0.3215"}}]}


class Client:
    def __init__(self):
        self.book = sand_book()
        self.orders = {OWNER: [order()]}
        self.state = {OWNER: short_state()}
        self.calls = {"l2": 0, "oo": 0, "ch": 0, "vd": 0, "uf": 0}
        self.fills = []            # sahibin dolum geçmişi (userFillsByTime)
        self.fills_err = False
        self.oo_seq = []           # sıradaki açık emir yanıtları (yeniden bakış; istisna = hata)
        self.book_seq = {}         # coin → sıradaki defter yanıtları

    async def l2_book(self, coin, n_sig_figs=None):
        self.calls["l2"] += 1
        q = self.book_seq.get(coin)
        if q:
            return q.pop(0)
        return self.book

    async def frontend_open_orders(self, user, dex=""):
        self.calls["oo"] += 1
        if self.oo_seq:
            r = self.oo_seq.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return self.orders.get(user, [])

    async def clearinghouse(self, user, dex="", priority=None, stats=None):
        self.calls["ch"] += 1
        return self.state.get(user, {"assetPositions": []})

    async def user_fills_by_time(self, user, start_ms, end_ms=None):
        self.calls["uf"] += 1
        if self.fills_err:
            raise RuntimeError("429")
        return [f for f in self.fills if f["time"] >= start_ms and (end_ms is None or f["time"] <= end_ms)]

    async def vault_details(self, a):
        self.calls["vd"] += 1
        return {"name": "drkmttr"} if a == OWNER else None


class Notifier:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def send(self, kind, text, **kw):
        self.sent.append((kind, kw.get("chat_id"), text))
        return self.ok


def hist_fill(usd, ts, px=WALL_PX, side="A", crossed=False):
    """userFillsByTime satırı (sahibin kendi dolumu)."""
    return {"coin": "SAND", "side": side, "px": str(px), "sz": str(usd / px), "time": ts * 1000,
            "crossed": crossed, "dir": "Open Short"}


def fills(maker, usd, ts, px=WALL_PX, n=20, aggr="B"):
    """`usd` tutarında `n` taker işlemi; maker = duvar sahibi (aggr B → satıcı maker)."""
    for i in range(n):
        sz = usd / n / px
        buyer, seller = (TAKER, maker) if aggr == "B" else (maker, TAKER)
        sw.observe("SAND", aggr, buyer, seller, px, sz, ts + (i % 5))


async def _fresh(chat="-200"):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "sticky.db"))
    sw.REG = sw.Registry()
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"SAND": {"m": SAND_MARK, "oi": SAND_OI, "v": SAND_VOL},
                                             "ETH": {"m": 2708.0, "oi": 400_000, "v": 1.25e9}},
                                       "ts": dbm.now()})
    sw.RECHECK_GAP = 0                      # yeniden bakış beklemesi testte yok
    cfg = Config()
    cfg.crypto_chat_id = chat
    return cfg


def test_find_sticky_fixtures():
    bids, asks = sw.parse_levels(sand_book())
    w = sw.find_sticky(bids, asks, SAND_VOL, 1_000_000, 2.0)
    assert len(w) == 1 and w[0]["side"] == "ask", w
    w = w[0]
    assert abs(w["ntl"] - WALL_PX * WALL_SZ) < 1 and w["level"] == 5 and w["n"] == 1
    assert 10.0 < w["vol_pct"] < 11.0, w["vol_pct"]
    assert w["opp_ntl"] < 7_000 and w["share"] > 0.95
    # MM yığını (n=17) $1.09M — tek emir DEĞİL
    eth = {"levels": [[{"px": "2707.9", "sz": "194", "n": 21}],
                      [{"px": "2708.0", "sz": "10", "n": 3}, {"px": "2708.9", "sz": "402.4", "n": 17}]]}
    assert not sw.find_sticky(*sw.parse_levels(eth), 1.25e9, 1_000_000, 0), "n=17 yığın sayılmaz"
    # n=1 ama BTC/ETH hacminde: %0.09 → oran kapısı keser
    eth1 = {"levels": [[{"px": "2707.9", "sz": "194", "n": 21}],
                       [{"px": "2708.0", "sz": "10", "n": 3}, {"px": "2708.9", "sz": "402.4", "n": 1}]]}
    assert not sw.find_sticky(*sw.parse_levels(eth1), 1.25e9, 1_000_000, 2.0)
    assert sw.find_sticky(*sw.parse_levels(eth1), 1.25e9, 1_000_000, 0), "oran kuralı kapalıyken $ yeter"
    # $1M altı, hacim bilinmiyor, %1 dışı
    assert not sw.find_sticky(*sw.parse_levels(sand_book(sz=12_000_000)), SAND_VOL, 1_000_000, 2.0)
    assert not sw.find_sticky(bids, asks, None, 1_000_000, 2.0), "hacim bilinmiyorsa alarm yok"
    far = {"levels": [[{"px": "0.062500", "sz": "16000", "n": 1}],
                      [{"px": "0.062540", "sz": "4800", "n": 1},
                       {"px": f"{0.062540 * 1.02:.6f}", "sz": str(WALL_SZ), "n": 1}]]}
    assert not sw.find_sticky(*sw.parse_levels(far), SAND_VOL, 1_000_000, 2.0), "%1 dışı sayılmaz"
    near = {"levels": [far["levels"][0], [far["levels"][1][0],
                                          {"px": f"{0.062540 * 1.009:.6f}", "sz": str(WALL_SZ), "n": 1}]]}
    assert sw.find_sticky(*sw.parse_levels(near), SAND_VOL, 1_000_000, 2.0), "%0.9 içeride"
    # izleme: aynı fiyata küçük bir MM emri denk gelince (n=2) kaybolmasın
    two = sw.parse_levels(sand_book(n=2))
    assert not sw.find_sticky(*two, SAND_VOL, 1_000_000, 2.0)
    assert sw.find_sticky(*two, SAND_VOL, 50_000, 0, max_n=2)
    print("✅ saf) SAND ekranı yakalanır (%10.5, 6. seviye); ETH n=17 yığını ve %0.09 tek emir"
          " elenir; hacim yoksa alarm yok; izlemede n=2 tolere")


def test_flow_dominant_and_trigger():
    sw.REG = sw.Registry()
    t = dbm.now()
    fills(OWNER, 200_500, t - 100, n=40)
    fills(MM, 27_840, t - 100, n=10)
    fills("0x" + "e" * 40, 6_700, t - 90, n=5)
    fills(MM, 50_000, t - 60, aggr="A")              # taker satış → bid tarafı makerı
    d = sw.dominant_maker(sw.REG.flow["SAND"], "ask", t - 120)
    assert d["address"] == OWNER and 0.85 < d["share"] < 0.9 and abs(d["ntl"] - 200_500) < 1
    assert sw.dominant_maker(sw.REG.flow["SAND"], "bid", t - 120)["address"] == MM
    assert sw._triggers(t) == ["SAND"] and sw.REG.focus["SAND"] > t
    # pencere: eski işlemler budanır
    sw.observe("SAND", "B", TAKER, MM, WALL_PX, 1, t + sw.FLOW_SEC + 200)
    assert len(sw.REG.flow["SAND"]) == 1
    print("✅ akış) 0xc179 ask akışının %87'si → odak; taker satış bid makerına yazılır; pencere budanır")


def test_lifecycle_alert_half_tranche_eaten_reopen():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        fills(OWNER, 200_000, t0 - 50, n=40)
        out = await sw.tick(cfg, cl, nt, ts=t0)
        assert out["triggers"] == 1 and out["cands"] == 1 and not nt.sent, "tek görüş alarm değil"
        out = await sw.tick(cfg, cl, nt, ts=t0 + 30)
        assert out["confirmed"] == 0 and not nt.sent
        cl.book = sand_book(px=WALL_PX + 0.00001)    # fiyatla yer değiştirdi
        out = await sw.tick(cfg, cl, nt, ts=t0 + 60)
        assert out["confirmed"] == 1 and out["alerted"] == 1, out
        kind, chat, text = nt.sent[-1]
        assert kind == "sticky" and chat == "-200", (kind, chat)
        for s_ in ("🧲 <b>YAPIŞKAN DUVAR</b>", "SAND", "SATIŞ", "post-only (Alo)", "reduce-only DEĞİL",
                   "drkmttr", "SHORT açıyor / büyütüyor", "/takip_", "24s hacme oranı <b>%10.",
                   "en az <b>1 kez</b>", "en iyi satışın 5 seviye gerisinde", "Likidasyon değil",
                   "fiyatla birlikte kendini yeniden koyan", "Duvardan şimdiye yenen"):
            assert s_ in text, (s_, text)
        assert "en iyi satışta" not in text, "6. seviyedeki emir 'en iyi satışta' denmez"
        rows = await sw._actives()
        w = rows[("SAND", "ask")]
        assert w["owner"] == OWNER and w["owner_src"] == "order" and w["effect"] == "open"
        assert w["pos_side"] == "short" and w["order_tif"] == "Alo" and w["reduce_only"] == 0
        assert w["eaten_usd"] >= 199_000 and w["offer_id"]
        async with dbm.db() as c:
            cur = await c.execute("SELECT * FROM track_offers WHERE kind='sticky'")
            offer = dict(await cur.fetchone())
        assert offer["ref_ts"] == w["id"] and f"/takip_{offer['id']}" in text
        await sw.follow_start(cfg, w["id"], chat_id="-300")
        await sw.tick(cfg, cl, nt, ts=t0 + 90)
        assert len(nt.sent) == 1, "kanal TEK alarm; aynı duvar yeniden alarm üretmez"

        # yarılandı → yalnız takipçiye (sahibin AÇIK EMRİ küçüldü)
        cl.book = sand_book(sz=WALL_SZ * 0.45)
        cl.orders[OWNER] = [order(sz=WALL_SZ * 0.45)]
        fills(OWNER, 700_000, t0 + 100)
        await sw.tick(cfg, cl, nt, ts=t0 + 120)
        assert nt.sent[-1][0] == "track" and nt.sent[-1][1] == "-300"
        assert "yarılandı" in nt.sent[-1][2] and "takip ettiğin duvar" in nt.sent[-1][2]

        # yeni dilim: sahibin kendi emri büyüdü
        big = 23_800_000.0
        cl.book = sand_book(px=0.062100, sz=big)
        cl.orders[OWNER] = [order(px=0.062100, sz=big)]
        await sw.tick(cfg, cl, nt, ts=t0 + 150)
        assert "yeni dilim" in nt.sent[-1][2] and nt.sent[-1][1] == "-300", nt.sent[-1]
        w = (await sw._actives())[("SAND", "ask")]
        assert w["tranches"] == 1 and w["peak_ntl"] > 1_470_000 and w["seg_peak"] > 1_470_000
        n_notes = len(nt.sent)
        await sw.tick(cfg, cl, nt, ts=t0 + 165)
        assert len(nt.sent) == n_notes, "dilimden hemen sonra sahte 'yarılandı' yok (dilim tepesiyle kıyas)"

        # emir gitti; sahibin GERÇEK dolum geçmişi son kalanı karşılıyor → yenildi
        cl.book = sand_book(sz=0)
        cl.orders[OWNER] = []
        cl.fills = [hist_fill(700_000, t0 + 155, px=0.0621), hist_fill(700_000, t0 + 170, px=0.0621)]
        cl.state[OWNER] = short_state(szi=-61_900_000.0, ntl=3_840_000.0)
        out = await sw.tick(cfg, cl, nt, ts=t0 + 195)
        assert out["ended"] == 0, "tek kaçırma bitirmez"
        out = await sw.tick(cfg, cl, nt, ts=t0 + 225)
        assert out["ended"] == 0, "iki kaçırma ama 60 sn dolmadı"
        out = await sw.tick(cfg, cl, nt, ts=t0 + 260)
        assert out["ended"] == 1
        e = nt.sent[-1][2]
        assert "YENİLDİ" in e and nt.sent[-1][1] == "-300" and "SHORT 61.90M SAND" in e, e
        w = await sw.wall(w["id"])
        assert w["status"] == "yenildi" and w["active"] == 0 and w["end_pos_side"] == "short"
        chan = [x for x in nt.sent if x[0] == "sticky"]
        assert len(chan) == 1, "kanal bitişi DUYMAZ — yalnız takipçi"

        # aynı sahip 15 dk içinde döner (açık emriyle doğrulanır) → aynı satır, kanal tekrar
        # yok, takipçiye "yeniden geldi" — ve eski tepenin yarısı altında olduğu için
        # ardından sahte "yarılandı" GELMEZ
        cl.book = sand_book(px=0.061900, sz=10_000_000)
        cl.orders[OWNER] = [order(px=0.061900, sz=10_000_000)]
        await sw.tick(cfg, cl, nt, ts=t0 + 310)
        await sw.tick(cfg, cl, nt, ts=t0 + 340)
        out = await sw.tick(cfg, cl, nt, ts=t0 + 375)
        assert out["reopened"] == 1 and out["confirmed"] == 0, out
        w2 = (await sw._actives())[("SAND", "ask")]
        assert w2["id"] == w["id"] and w2["tranches"] == 2 and w2["status"] == "aktif"
        assert "yeniden geldi" in nt.sent[-1][2] and nt.sent[-1][1] == "-300"
        n_notes = len(nt.sent)
        await sw.tick(cfg, cl, nt, ts=t0 + 410)
        assert len(nt.sent) == n_notes, "dönüşün ardından sahte 'yarılandı' yok"
        assert len([x for x in nt.sent if x[0] == "sticky"]) == 1
        print("✅ yaşam) 60 sn teyit → tek kanal alarmı (Alo, short açıyor, seviye dürüst);"
              " takipçiye yarılandı / yeni dilim / YENİLDİ (dolum geçmişinden) / yeniden geldi;"
              " tek kaçırma bitirmez; sahte 'yarılandı' yok")
    asyncio.run(run())


async def _confirmed(cfg, cl, nt, t0):
    fills(OWNER, 100_000, t0 - 30)
    for dt in (0, 30, 60):
        out = await sw.tick(cfg, cl, nt, ts=t0 + dt)
    return out


def test_pulled_unknown_owner_failed_send_no_chat():
    async def run():
        # çekildi: emir gitti, sahibin dolum geçmişinde son kalandan dolum yok
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        await _confirmed(cfg, cl, nt, t0)
        cl.book, cl.orders[OWNER] = sand_book(sz=0), []
        for dt in (90, 120, 155):
            await sw.tick(cfg, cl, nt, ts=t0 + dt)
        ended = (await sw.page_rows())["ended"]
        assert ended and ended[0]["status"] == "çekildi", ended

        # dolum geçmişi okunamadı + akış o aralığı görmedi (yeniden başlatma) → kayboldu
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        await _confirmed(cfg, cl, nt, t0)
        sw.REG = sw.Registry()                          # yeniden başlatma: akış/sayaçlar sıfır
        sw.REG.started = t0 + 80
        cl.book, cl.orders[OWNER], cl.fills_err = sand_book(sz=0), [], True
        out = await sw.tick(cfg, cl, nt, ts=t0 + 100)
        assert out["ended"] == 0, "yeniden başlatma sonrası TEK bakış bitirmez"
        for dt in (130, 165):
            await sw.tick(cfg, cl, nt, ts=t0 + dt)
        assert (await sw.page_rows())["ended"][0]["status"] == "kayboldu", "veri yokken 'çekildi' denmez"

        # sahip bulunamadı: akış yok, açık emir yok → alarm bunu SÖYLER, bitiş "kayboldu"
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.orders = {}
        sw.REG.focus["SAND"] = dbm.now() + 999
        for dt in (0, 30, 60):
            await sw.tick(cfg, cl, nt, ts=t0 + dt)
        assert "Sahibi bulunamadı" in nt.sent[-1][2] and "/takip_" in nt.sent[-1][2]
        cl.book = sand_book(sz=0)
        for dt in (90, 120, 155):
            await sw.tick(cfg, cl, nt, ts=t0 + dt)
        assert (await sw.page_rows())["ended"][0]["status"] == "kayboldu", "dolum/çekilme uydurulmaz"

        # gönderim başarısız → işaret yok, sonraki adım yeniden dener; sayaç ve teklif BİR kez
        cfg = await _fresh()
        cl, nt = Client(), Notifier(ok=False)
        out = await _confirmed(cfg, cl, nt, t0)
        assert out["failed"] == 1 and not (await sw._actives())[("SAND", "ask")]["alerted_ts"]
        out = await sw.tick(cfg, cl, nt, ts=t0 + 90)
        assert out["failed"] == 0, "aynı duvarın her denemesi ayrı 'gönderilemedi' sayılmaz"
        nt.ok = True
        out = await sw.tick(cfg, cl, nt, ts=t0 + 120)
        assert out["alerted"] == 1 and (await sw._actives())[("SAND", "ask")]["alerted_ts"]
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) n FROM track_offers WHERE kind='sticky'")
            assert (await cur.fetchone())["n"] == 1, "teklif duvar başına bir kez"
        assert nt.sent[-1][0] == "sticky"

        # tür kapalı → hiç denenmez, teklif yazılmaz
        cfg = await _fresh()
        cfg.notify_sticky = False
        cl, nt = Client(), Notifier()
        await _confirmed(cfg, cl, nt, t0)
        await sw.tick(cfg, cl, nt, ts=t0 + 90)
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) n FROM track_offers")
            assert (await cur.fetchone())["n"] == 0 and not nt.sent

        # CRYPTO_CHAT_ID boş → gönderilmez, BİR kez sayılır
        cfg = await _fresh(chat="")
        cl, nt = Client(), Notifier()
        out = await _confirmed(cfg, cl, nt, t0)
        assert out["no_chat"] == 1 and not nt.sent
        out = await sw.tick(cfg, cl, nt, ts=t0 + 90)
        assert out["no_chat"] == 0
        print("✅ kenar) çekildi (dolum geçmişi boş) / kayboldu (veri yok, sahip yok) ayrımı;"
              " restart sonrası tek bakış bitirmez; başarısız gönderim yeniden denenir ama"
              " sayaç ve teklif BİR kez; tür kapalıyken denenmez")
    asyncio.run(run())


def test_flapping_wall_recheck_and_cloid():
    """Canlıdaki hata (02.10, drkmttr SAND): duvar her 1–25 sn'de iptal edilip yeniden
    konuyor (yeni oid, AYNI cloid); açık emir okumalarının %38'inde YOK. Okumada yok →
    hızlı yeniden bakış; okunamayan bakış kaçırma değil; kimlik cloid."""
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        await _confirmed(cfg, cl, nt, t0)
        w = (await sw._actives())[("SAND", "ask")]
        assert w["owner_src"] == "order" and w["cloid"] == "0xc10d"
        await sw.follow_start(cfg, w["id"], chat_id="-300")
        n = len(nt.sent)
        # 12 bakış (6 dk): iki okuma boş, üçüncüde yeni oid + aynı cloid; defterde de boşluk
        for i in range(12):
            o = {**order(px=WALL_PX + (i % 3) * 0.00001), "oid": 600 + i}
            cl.oo_seq = [[], [], [o]]
            cl.book = sand_book(sz=0)
            out = await sw.tick(cfg, cl, nt, ts=t0 + 90 + i * 30)
            assert out["ended"] == 0 and not cl.oo_seq, i
        assert len(nt.sent) == n, "takipçiye de sahte 'bitti' yok"
        # kimlik cloid: yanına sahibin daha BÜYÜK başka emri gelse de izlenen aynı cloid
        other = {**order(px=WALL_PX + 0.00002, sz=WALL_SZ * 2), "cloid": "0xbeef", "oid": 999}
        cl.orders[OWNER] = [other, {**order(), "oid": 700}]
        cl.book = sand_book()
        await sw.tick(cfg, cl, nt, ts=t0 + 460)
        w = (await sw._actives())[("SAND", "ask")]
        assert abs(w["ntl_last"] - WALL_PX * WALL_SZ) < 1 and w["tranches"] == 0, \
            "aynı cloid izlenir; büyük komşu emir 'yeni dilim' sayılmaz"
        # okumada yok + yeniden bakış okunamadı (429) → kaçırma sayılmaz (4 dk)
        for i in range(8):
            cl.oo_seq = [[], RuntimeError("429")]
            out = await sw.tick(cfg, cl, nt, ts=t0 + 490 + i * 30)
            assert out["ended"] == 0, i
        assert len(nt.sent) == n
        # gerçekten kalktı: her bakışta tüm yeniden bakışlar boş → MISS_MIN + süre → bir kez
        cl.oo_seq, cl.orders[OWNER], cl.book = [], [], sand_book(sz=0)
        oo = cl.calls["oo"]
        ended = (await sw.tick(cfg, cl, nt, ts=t0 + 760))["ended"]
        assert cl.calls["oo"] - oo == 1 + sw.RECHECK_N, "kaçırma ancak yeniden bakışlardan sonra"
        for i in range(1, 4):
            ended += (await sw.tick(cfg, cl, nt, ts=t0 + 760 + i * 30))["ended"]
        assert ended == 1
        assert "ÇEKİLDİ" in nt.sent[-1][2] and nt.sent[-1][1] == "-300", nt.sent[-1]

        # sahibi bilinmeyen duvar defterden izlenir: defterdeki boşlukta defter yeniden okunur
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.orders = {}
        sw.REG.focus["SAND"] = dbm.now() + 9999
        for dt in (0, 30, 60):
            await sw.tick(cfg, cl, nt, ts=t0 + dt)
        assert "Sahibi bulunamadı" in nt.sent[-1][2]
        for i in range(10):
            cl.book_seq = {"SAND": [sand_book(sz=0), sand_book(px=WALL_PX + (i % 2) * 0.00001)]}
            out = await sw.tick(cfg, cl, nt, ts=t0 + 90 + i * 30)
            assert out["ended"] == 0 and not cl.book_seq["SAND"], i
        cl.book = sand_book(sz=0)
        ended = 0
        for i in range(4):
            ended += (await sw.tick(cfg, cl, nt, ts=t0 + 400 + i * 30))["ended"]
        assert ended == 1 and (await sw.page_rows())["ended"][0]["status"] == "kayboldu"
        print("✅ boşluk) okumada yok → yeniden bakış (açık emir / defter); yeni oid + aynı cloid"
              " aynı duvar, büyük komşu emir kimliği kaydırmaz; okunamayan bakış kaçırma değil;"
              " gerçek kalkış bir kez")
    asyncio.run(run())


def test_identity_foreign_orders_and_attribution():
    async def run():
        # doğrulanmış duvarın yanına BAŞKASININ daha büyük tek emri gelir → kimlik kaymaz
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        await _confirmed(cfg, cl, nt, t0)
        await sw.follow_start(cfg, (await sw._actives())[("SAND", "ask")]["id"], chat_id="-300")
        bk = sand_book()
        bk["levels"][1].insert(7, {"px": f"{WALL_PX + 0.000004:.6f}", "sz": "40000000", "n": 1})
        cl.book = bk
        before = len(nt.sent)
        for dt in (90, 120, 150, 180):
            out = await sw.tick(cfg, cl, nt, ts=t0 + dt)
            assert out["tranches"] == 0 and out["ended"] == 0
        w = (await sw._actives())[("SAND", "ask")]
        assert abs(w["ntl_last"] - WALL_PX * WALL_SZ) < 1, "duvar sahibin emri olarak kaldı"
        assert len(nt.sent) == before, "başkasının emri dilim/yarılanma notu üretmez"
        # sahip akış lideri değil ama yedek adaylardan doğrulanırsa: dolum O ADRESİN dolumu
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        async with dbm.db() as c:
            await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,notional,ts)"
                            " VALUES('SAND',?, '', 'short', -1, 2000000, ?)", (OWNER, dbm.now()))
        fills(MM, 300_000, t0 - 30)                      # akışı başka bir MM domine ediyor
        fills(OWNER, 6_000, t0 - 30, n=3)
        for dt in (0, 30, 60):
            await sw.tick(cfg, cl, nt, ts=t0 + dt)
        w = (await sw._actives())[("SAND", "ask")]
        assert w["owner"] == OWNER and w["owner_src"] == "order"
        assert w["eaten_usd"] < 10_000, "başka maker'ın $300K'ı sahibe yazılmaz"
        # pozisyondan büyük reduce-only olmayan emir → 'flip'
        assert sw.effect_of("ask", {"side": "long", "szi": 800_000.0}, False, 23_000_000) == "flip"
        assert sw.effect_of("ask", {"side": "long", "szi": 80_000_000.0}, False, 23_000_000) == "close"
        assert sw.effect_of("bid", {"side": "long", "szi": 1.0}, False, 9.0) == "open"
        print("✅ kimlik) yanına gelen büyük yabancı emir dilim/not üretmez; yedekten doğrulanan"
              " sahibin dolumu kendi dolumu; pozisyondan büyük emir 'flip'")
    asyncio.run(run())


def test_budget_sweep_low_priority():
    async def run():
        cfg = await _fresh()
        from app.hl import collector as colmod
        colmod.LIVE = None                            # önceki testlerin kollektörü evreni değiştirmesin
        cl, nt = Client(), Notifier()
        cl.book = sand_book(sz=0)                     # duvar yok
        for i in range(120):
            sw.observe(f"C{i:03d}", "B", TAKER, f"0x{i:040x}", 1.0, 1.0, dbm.now())
        seen = []
        from app.hl.client import PRIORITY

        async def l2(coin, n_sig_figs=None):
            seen.append(PRIORITY.get())
            return cl.book
        cl.l2_book = l2
        out = await sw.tick(cfg, cl, nt, ts=dbm.now())
        assert out["swept"] == 6 and set(seen) == {"low"}, (out, seen)
        cfg.sticky_poll_sec = 0
        seen.clear()
        out = await sw.tick(cfg, cl, nt, ts=dbm.now())
        assert out["swept"] == 0 and not seen, "0 = tarama kapalı, yalnız akış tetiği"
        cfg.sticky_poll_sec = 300
        cl.low_paused = lambda: 30.0                 # 429 sonrası düşük şerit susuyor
        seen.clear()
        out = await sw.tick(cfg, cl, nt, ts=dbm.now() + 20)
        assert out["swept"] == 0 and out["sweep_skipped"] == 1 and not seen, \
            "şerit susarken tarama atlanır — izlenen duvarlar ve notlar onu beklemez"
        print("✅ bütçe) 120 coin / 300 sn → adım başına 6 defter, DÜŞÜK öncelik; 0 = kapalı")
    asyncio.run(run())


def test_bot_follow_commands():
    async def run():
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        fills(OWNER, 100_000, t0 - 30)
        for dt in (0, 30, 60):
            await sw.tick(cfg, cl, nt, ts=t0 + dt)
        async with dbm.db() as c:
            cur = await c.execute("SELECT id FROM track_offers WHERE kind='sticky'")
            oid = (await cur.fetchone())["id"]
        from app.telegram.bot import TelegramBot
        bot = TelegramBot.__new__(TelegramBot)
        bot.cfg, bot.client = cfg, None
        sent = []

        async def _send(t, chat=""):
            sent.append((chat, t))
            return True
        bot.send = _send
        bot._track_chat = lambda c: "" if str(c) == str(cfg.telegram_chat_id) else str(c)
        await bot._cmd_track_start(f"takip_{oid}", "-300")
        fl = await sw.follows_active()
        assert len(fl) == 1 and fl[0]["chat_id"] == "-300" and fl[0]["coin"] == "SAND"
        assert "yapışkan duvarı takipte" in sent[-1][1] and "/birak_duvar_" in sent[-1][1]
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) n FROM trackers")
            assert (await cur.fetchone())["n"] == 0, "pozisyon takibi açılmaz"
        await bot._cmd_sticky_follow_list("-300")
        assert "İzlenen yapışkan duvarlar" in sent[-1][1] and "SAND" in sent[-1][1]
        await bot._cmd_track_stop(f"birak_duvar_{fl[0]['id']}", "-300")
        assert "bırakıldı" in sent[-1][1] and not await sw.follows_active()
        # dönüş penceresi de geçmiş bitmiş duvar → takip açılmaz
        async with dbm.db() as c:
            await c.execute("UPDATE sticky_walls SET active=0, status='çekildi', end_ts=?",
                            (dbm.now() - sw.REJOIN_SEC - 5,))
        await bot._cmd_track_start(f"takip_{oid}", "-301")
        assert "ZATEN BİTTİ" in sent[-1][1] and not await sw.follows_active()
        print("✅ bot) /takip_N (sticky) duvar takibi açar; /duvartakipler listeler;"
              " /birak_duvar_N bırakır; bitmiş duvara takip açılmaz")
    asyncio.run(run())


def test_page_and_wiring():
    async def run():
        from test_routes_smoke import _fresh as fresh_app, get
        app = await fresh_app()
        t = dbm.now()
        await sw._insert({"coin": "SAND", "side": "ask", "first_ts": t - 600, "confirm_ts": t - 540,
                          "last_ts": t - 10, "px_last": 0.06264, "px_min": 0.06210, "px_max": 0.06339,
                          "ntl_last": 1_241_982.0, "peak_ntl": 1_456_366.0, "day_vol": SAND_VOL,
                          "n_moves": 41, "owner": OWNER, "owner_src": "order", "owner_name": "drkmttr",
                          "order_tif": "Alo", "reduce_only": 0, "pos_side": "short", "pos_ntl": 2_381_075.0,
                          "effect": "open", "eaten_usd": 200_523.0, "status": "aktif", "active": 1})
        await sw._insert({"coin": "XMR", "side": "bid", "first_ts": t - 4000, "last_ts": t - 900,
                          "end_ts": t - 800, "peak_ntl": 1_100_000.0, "status": "çekildi", "active": 0,
                          "owner": "", "px_min": 550.0, "px_max": 552.0})
        st, body = await get(app, "/yapiskan")
        assert st == 200, body[-500:]
        for s in ("Yapışkan duvarlar", "SAND", "drkmttr", "SHORT açıyor / büyütüyor", "0.06264",
                  "0.0621", "çekildi", "Likidasyon değildir", "n=1"):
            assert s in body, s
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "/yapiskan" in rd("app", "web", "templates", "base.html"), "nav sekmesi"
        assert "/yapiskan" in rd("README.md") and "STICKY_MIN_USD=" in rd(".env.example")
        assert "stickywall.observe(" in rd("app", "hl", "collector.py"), "kollektör kancası"
        assert "stickywall.loop(" in rd("app", "main.py")
        assert 'await beat("twapfollow")' in rd("app", "radar", "twapfollow.py"), "nabız await'siz kalmasın"
        from app.health import limits
        from app.notify import KINDS, PUBLIC_KINDS
        assert "sticky" in KINDS and "sticky" in PUBLIC_KINDS and "stickywall" in limits(Config())
        for f in ("sticky_enabled", "notify_sticky", "sticky_min_usd", "sticky_min_vol_pct", "sticky_poll_sec"):
            assert f in EDITABLE_FIELDS and hasattr(Config(), f), f
            assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
        c = Config()
        assert c.sticky_min_usd == 1_000_000.0 and c.sticky_min_vol_pct == 2.0, "kullanıcı kuralı"
        assert "sticky" in c.public_kinds
        assert fmt.px5(0.062640) == "0.06264" and fmt.px5(85595.0) == "85,595"
        assert "e+" not in fmt.px5(99_999.7), "bilimsel gösterim yok"
        print("✅ sayfa+kablo) /yapiskan 200 (aktif + biten, 5 haneli fiyat); nav, README, .env,"
              " kollektör kancası, spawn, sağlık, tür, 5 ayar künyeli; twapfollow nabzı await'li")
    asyncio.run(run())
