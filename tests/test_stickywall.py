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
        self.calls = {"l2": 0, "oo": 0, "ch": 0, "vd": 0}

    async def l2_book(self, coin, n_sig_figs=None):
        self.calls["l2"] += 1
        return self.book

    async def frontend_open_orders(self, user, dex=""):
        self.calls["oo"] += 1
        return self.orders.get(user, [])

    async def clearinghouse(self, user, dex="", priority=None, stats=None):
        self.calls["ch"] += 1
        return self.state.get(user, {"assetPositions": []})

    async def vault_details(self, a):
        self.calls["vd"] += 1
        return {"name": "drkmttr"} if a == OWNER else None


class Notifier:
    def __init__(self, ok=True):
        self.sent, self.ok = [], ok

    async def send(self, kind, text, **kw):
        self.sent.append((kind, kw.get("chat_id"), text))
        return self.ok


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
        for s in ("🧲 <b>YAPIŞKAN DUVAR</b>", "SAND", "SATIŞ", "post-only (Alo)", "reduce-only DEĞİL",
                  "drkmttr", "SHORT açıyor / büyütüyor", "/takip_", "24s hacme oranı <b>%10.", "1 kez",
                  "Likidasyon değil", "Duvardan şimdiye yenen"):
            assert s in text, (s, text)
        rows = await sw._actives()
        w = rows[("SAND", "ask")]
        assert w["owner"] == OWNER and w["owner_src"] == "order" and w["effect"] == "open"
        assert w["pos_side"] == "short" and w["order_tif"] == "Alo" and w["reduce_only"] == 0
        assert w["eaten_usd"] >= 199_000
        async with dbm.db() as c:
            cur = await c.execute("SELECT * FROM track_offers WHERE kind='sticky'")
            offer = dict(await cur.fetchone())
        assert offer["ref_ts"] == w["id"] and f"/takip_{offer['id']}" in text
        fid, _ = await sw.follow_start(cfg, w["id"], chat_id="-300")
        await sw.tick(cfg, cl, nt, ts=t0 + 90)
        assert len(nt.sent) == 1, "kanal TEK alarm; aynı duvar yeniden alarm üretmez"

        # yarılandı → yalnız takipçiye
        cl.book = sand_book(sz=WALL_SZ * 0.45)
        fills(OWNER, 700_000, t0 + 100)
        await sw.tick(cfg, cl, nt, ts=t0 + 120)
        assert nt.sent[-1][0] == "track" and nt.sent[-1][1] == "-300"
        assert "yarılandı" in nt.sent[-1][2] and "takip ettiğin duvar" in nt.sent[-1][2]

        # yeni dilim: boyut sıçradı + sahibin açık emri doğruluyor
        big = 23_800_000.0
        cl.book = sand_book(px=0.062100, sz=big)
        cl.orders[OWNER] = [order(px=0.062100, sz=big)]
        await sw.tick(cfg, cl, nt, ts=t0 + 150)
        assert "yeni dilim" in nt.sent[-1][2] and nt.sent[-1][1] == "-300", nt.sent[-1]
        w = (await sw._actives())[("SAND", "ask")]
        assert w["tranches"] == 1 and w["peak_ntl"] > 1_470_000

        # duvar gitti, son kalanın tamamı sahibin dolumu → yenildi
        cl.book = sand_book(sz=0)
        fills(OWNER, 1_400_000, t0 + 160, px=0.062100)
        cl.state[OWNER] = short_state(szi=-61_900_000.0, ntl=3_840_000.0)
        out = await sw.tick(cfg, cl, nt, ts=t0 + 200)
        assert out["ended"] == 0, "90 sn dolmadan bitti denmez (iptal-yeniden-koy boşluğu)"
        out = await sw.tick(cfg, cl, nt, ts=t0 + 245)
        assert out["ended"] == 1
        e = nt.sent[-1][2]
        assert "YENİLDİ" in e and nt.sent[-1][1] == "-300" and "SHORT 61.90M SAND" in e, e
        w = await sw.wall(w["id"])
        assert w["status"] == "yenildi" and w["active"] == 0 and w["end_pos_side"] == "short"
        chan = [x for x in nt.sent if x[0] == "sticky"]
        assert len(chan) == 1, "kanal bitişi DUYMAZ — yalnız takipçi"

        # aynı sahip 15 dk içinde döner → aynı satır, kanal tekrar yok, takipçiye "yeniden geldi"
        cl.book = sand_book(px=0.061900, sz=10_000_000)
        cl.orders[OWNER] = [order(px=0.061900, sz=10_000_000)]
        fills(OWNER, 60_000, t0 + 300, px=0.061900)
        await sw.tick(cfg, cl, nt, ts=t0 + 310)
        await sw.tick(cfg, cl, nt, ts=t0 + 340)
        out = await sw.tick(cfg, cl, nt, ts=t0 + 375)
        assert out["reopened"] == 1 and out["confirmed"] == 0, out
        w2 = (await sw._actives())[("SAND", "ask")]
        assert w2["id"] == w["id"] and w2["tranches"] == 2 and w2["status"] == "aktif"
        assert "yeniden geldi" in nt.sent[-1][2] and nt.sent[-1][1] == "-300"
        assert len([x for x in nt.sent if x[0] == "sticky"]) == 1
        print("✅ yaşam) 60 sn teyit → tek kanal alarmı (Alo, short açıyor, %10.5);"
              " takipçiye yarılandı / yeni dilim / YENİLDİ / yeniden geldi; kanal tekrar yok")
    asyncio.run(run())


def test_pulled_unknown_owner_failed_send_no_chat():
    async def run():
        # çekildi: sahip biliniyor, son kalandan dolum yok
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        t0 = dbm.now()
        fills(OWNER, 100_000, t0 - 30)
        for dt in (0, 30, 60):
            await sw.tick(cfg, cl, nt, ts=t0 + dt)
        cl.book = sand_book(sz=0)
        await sw.tick(cfg, cl, nt, ts=t0 + 155)
        ended = (await sw.page_rows())["ended"]
        assert ended and ended[0]["status"] == "çekildi", ended

        # sahip bulunamadı: akış yok, açık emir yok → alarm bunu SÖYLER, bitiş "kayboldu"
        cfg = await _fresh()
        cl, nt = Client(), Notifier()
        cl.orders = {}
        sw.REG.focus["SAND"] = dbm.now() + 999
        for dt in (0, 30, 60):
            await sw.tick(cfg, cl, nt, ts=t0 + dt)
        assert "Sahibi bulunamadı" in nt.sent[-1][2] and "/takip_" in nt.sent[-1][2]
        cl.book = sand_book(sz=0)
        await sw.tick(cfg, cl, nt, ts=t0 + 155)
        assert (await sw.page_rows())["ended"][0]["status"] == "kayboldu", "dolum/çekilme uydurulmaz"

        # gönderim başarısız → işaret yok, sonraki adım yeniden dener
        cfg = await _fresh()
        cl, nt = Client(), Notifier(ok=False)
        fills(OWNER, 100_000, t0 - 30)
        for dt in (0, 30, 60):
            out = await sw.tick(cfg, cl, nt, ts=t0 + dt)
        assert out["failed"] == 1 and not (await sw._actives())[("SAND", "ask")]["alerted_ts"]
        nt.ok = True
        out = await sw.tick(cfg, cl, nt, ts=t0 + 90)
        assert out["alerted"] == 1 and (await sw._actives())[("SAND", "ask")]["alerted_ts"]

        # CRYPTO_CHAT_ID boş → gönderilmez, sayılır
        cfg = await _fresh(chat="")
        cl, nt = Client(), Notifier()
        fills(OWNER, 100_000, t0 - 30)
        for dt in (0, 30, 60):
            out = await sw.tick(cfg, cl, nt, ts=t0 + dt)
        assert out["no_chat"] == 1 and not nt.sent
        print("✅ kenar) çekildi / kayboldu ayrımı; sahipsiz alarm dürüst; başarısız gönderim"
              " yeniden denenir; kanal tanımsızsa sayılır, gönderilmez")
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
        assert fmt.px5(0.062640) == "0.06264" and fmt.px5(85595.0) == "85595"
        print("✅ sayfa+kablo) /yapiskan 200 (aktif + biten, 5 haneli fiyat); nav, README, .env,"
              " kollektör kancası, spawn, sağlık, tür, 5 ayar künyeli; twapfollow nabzı await'li")
    asyncio.run(run())
