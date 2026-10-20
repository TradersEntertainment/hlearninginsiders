"""👣 Boşaltma takibi (app/radar/unwindtrack.py) — 🧯 mesajlarındaki takip tuşu ve UNWIND_TRACK_CHAT_ID kanalı.

Kullanıcı (20.10, "büyükler kapatıyor" ekranı): "bu botun attığı pozlar için takip tuşu istiyorum poz kapanana kadar
bildirim atacak her %10 kapanışta adamın diğer top 10 pozunu da listelesin sorsun hepsini takip için şu buton sadece
bildirim gelen X için şu buton, bunlar için yeni chatid ayarlıcam railwayden bu kanala değil, o kanala şunu takibe
başladık cüzdan şu falan diye".

Kararlar: 🧯 mesajında [👣 Takip et: X] · [👣 Hepsini takip et (X + n)]; takip mevcut 👣 pozisyon takibi (src='unwind',
%10 adım, kapanış, yön) ve haberleri yalnız UNWIND_TRACK_CHAT_ID'ye; her takip mesajında cüzdanın öteki ilk 10
pozisyonu (≥ $100K) + [👣 Hepsini takip et (+k)] · [🎯 Yalnız X]; liq kayması notu yok; kanal boşsa tuş yok.

Pinlenenler:
  • 🧯 ilk mesajda tuşlar yalnız UNWIND_TRACK_CHAT_ID tanımlıyken; teklif satırı kind='unwind'; "hepsi" sayısı ≤ 10
  • tuş: X → tek takip (kanal = takip kanalı, src='unwind'); hepsi → X + öteki ilk 10 (≥ $100K, küçükler yok); ikinci
    basış yeni takip açmaz; kapanmış pozisyon açılmaz; başlangıç mesajının ilk satırları (ne · PROPR · kim · ne zaman)
  • takip turu: %10 adımda mesaj takip kanalına + öteki pozisyon listesi (👣 = takipte) + soru tuşları + 🛑; liq kayması
    notu yok; kapanışta yalnız "hepsini takip et"; sıradan takip değişmedi (liste yok, liq notu var)
  • callback: uwa (🧯 kanalında) → toast + takip kanalına mesaj; uto → öteki 🧯 takipleri bırakır, soru satırı kalkar;
    uta → kalanlar; ⏰ basılınca soru satırı korunur (🛑'de yalnız ↩️); yetkisiz ret; kanal tanımsızsa ret
  • kablolama: env-only, _own_chats, README + .env, /tani satırı, /takip_N ile aynı yol, ölçülü dil
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import tracker as tr  # noqa: E402
from app.radar import unwind as u  # noqa: E402
from app.radar import unwindtrack as ut  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = "0x30af0000000000000000000000000000000393e"
UW, TRK = "-100555", "-100777"


def _p(coin, szi, px, liq=None):
    return {"position": {"coin": coin, "szi": str(szi), "positionValue": str(abs(szi) * px), "entryPx": str(px),
                         "liquidationPx": str(liq) if liq else None, "leverage": {"value": "5"},
                         "unrealizedPnl": "0"}}


def _book(googl=17_000):
    """GOOGL long $42.8M (xyz) + 11 öteki ≥ $100K + bir küçük ($50K)."""
    xyz = [_p("xyz:GOOGL", googl, 2517.6, liq=1500.0), _p("xyz:AMZN", 5000, 240.0)]
    main = [_p(c, 1000, 100.0 * (12 - i), liq=50.0) for i, c in enumerate(["BTC", "ETH", "SOL", "HYPE", "TAO",
                                                                           "ZEC", "NEAR", "PUMP", "ENA", "WLD"])]
    main.append(_p("DOGE", 500, 100.0))                     # $50K: tabanın altında
    return {"main": {"assetPositions": main}, "xyz": {"assetPositions": xyz}}


class _Cli:
    def __init__(self, states):
        self.states, self.calls = states, 0

    async def clearinghouse_all(self, user, dexes):
        self.calls += 1
        return self.states

    async def clearinghouse(self, user, dex=""):
        return self.states.get(dex or "main") or {"assetPositions": []}

    async def info(self, p, **kw):
        return getattr(self, "fills", [])


class _N:
    def __init__(self):
        self.sent = []

    async def send(self, kind, text, **kw):
        self.sent.append((kind, text, kw))
        return True


def _cfg(track=TRK):
    cfg = Config()
    cfg.telegram_chat_id, cfg.telegram_owner_id = "111", "7"
    cfg.unwind_chat_id, cfg.unwind_track_chat_id = UW, track
    cfg.notify_track = True
    cfg.track_step_pct, cfg.track_liq_step_pct, cfg.track_expire_days = 10.0, 1.0, 14
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.public_bot_enabled = False
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "ut.db"))
    ut._CACHE.clear()


async def _offer(coin="xyz:GOOGL"):
    async with dbm.db() as c:
        cur = await c.execute("INSERT INTO track_offers(address,coin,symbol,side,notional,created_ts,kind)"
                              " VALUES(?,?,?,?,?,?,'unwind')", (A, coin, coin.split(":")[-1], "long", 4.28e7, dbm.now()))
        return int(cur.lastrowid)


async def _trackers():
    async with dbm.db() as c:
        cur = await c.execute("SELECT * FROM trackers ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


def _cbs(kb):
    return [b["callback_data"] for row in (kb or {}).get("inline_keyboard") or [] for b in row]


def test_alert_buttons():
    async def run():
        await _fresh()
        from tests.test_unwind import _fill, _reset
        _reset()
        from app.radar import twaplive
        twaplive.VOL_CACHE.update(ts=dbm.now(), v={"xyz:GOOGL": (2.9e7, 0), "xyz:AMZN": (2e7, 0)})
        await u._refresh_vol()
        T0 = 1_792_430_000
        for track, want in ((TRK, True), ("", False)):
            cfg = _cfg(track)
            n = _N()
            cli = _Cli(_book())
            cli.fills = [_fill("xyz:GOOGL", 2517.6, 170, 17_170, T0)]
            u.STATE["episodes"] = {}
            u.COOL.clear()
            cand = {"addr": A, "coin": "xyz:GOOGL", "side": "S", "usd": 425_000, "first_ts": T0, "last_ts": T0 + 30,
                    "px0": 2518.0, "px1": 2517.2}
            txt = await u.probe(cfg, cli, n, cand)
            assert txt and n.sent[-1][2]["chat_id"] == UW
            kb = n.sent[-1][2].get("reply_markup")
            if not want:
                assert kb is None
                continue
            cbs = _cbs(kb)
            assert cbs[0].startswith("trk:uw1:") and cbs[1].startswith("trk:uwa:"), cbs
            assert kb["inline_keyboard"][0][0]["text"] == "👣 Takip et: GOOGL"
            assert kb["inline_keyboard"][1][0]["text"] == "👣 Hepsini takip et (GOOGL + 10)"   # 11 öteki → ilk 10
            oid = int(cbs[0].split(":")[2])
            async with dbm.db() as c:
                row = dict(await (await c.execute("SELECT * FROM track_offers WHERE id=?", (oid,))).fetchone())
            assert row["kind"] == "unwind" and row["address"] == A and row["coin"] == "xyz:GOOGL"
            assert all(len(x.encode()) <= 64 for x in cbs)
        _reset()
    asyncio.run(run())
    print("✅ 🧯 tuşlar) takip kanalı tanımlıyken [Takip et: X] · [Hepsini (X + 10)]; boşken tuş yok")


def test_from_offer_single_all_dup():
    async def run():
        await _fresh()
        cfg = _cfg()
        cli = _Cli(_book())
        oid = await _offer()
        toast, text, kb = await ut.from_offer(cfg, cli, oid, all_=False)
        ts = await _trackers()
        assert toast == "👣 GOOGL takibe alındı → takip kanalı" and len(ts) == 1
        t = ts[0]
        assert t["src"] == "unwind" and t["chat_id"] == TRK and t["coin"] == "xyz:GOOGL" and t["side"] == "long"
        lines = text.split("\n")
        assert lines[0] == f"👣 <b>TAKİBE BAŞLADIK</b> — <b>GOOGL</b> 🟢LONG <b>$42.8M</b> (#{t['id']})", lines[0]
        assert any(x.startswith("👤 <a href=") and "🧯 boşaltma mesajındaki tuştan" in x for x in lines[:4])
        assert any("her %10 kapanışında" in x for x in lines[:5])
        oth = next(x for x in lines if x.startswith("Öteki açık pozisyonları"))
        assert oth.startswith("Öteki açık pozisyonları (11, ≥ $100K): ") and "+1 daha" in oth and "DOGE" not in oth
        assert "<b>AMZN</b> L $1.2M" in oth and "👣" not in oth
        assert f"/birak_{t['id']}" in text
        assert _cbs(kb) == [f"trk:uta:{t['id']}", f"trk:uto:{t['id']}", f"trk:stop:pos:{t['id']}",
                            f"trk:wake:pos:{t['id']}"], _cbs(kb)
        assert kb["inline_keyboard"][0][0]["text"] == "👣 Hepsini takip et (+10)"
        # ikinci basış: yeni takip yok
        toast2, text2, _ = await ut.from_offer(cfg, cli, oid, all_=False)
        assert text2 is None and toast2 == f"👣 Yeni takip yok: GOOGL zaten takipte (#{t['id']})"
        # hepsi: X zaten takipte → öteki ilk 10 (küçük DOGE ve 11. sıradaki yok)
        toast3, text3, kb3 = await ut.from_offer(cfg, cli, oid, all_=True)
        ts = await _trackers()
        assert len(ts) == 11 and toast3 == "👣 10 pozisyon takibe alındı → takip kanalı", toast3
        coins = {x["coin"] for x in ts}
        assert "DOGE" not in coins and "xyz:AMZN" in coins and "BTC" in coins and "WLD" not in coins
        assert text3.split("\n")[0].startswith("👣 <b>TAKİBE BAŞLADIK</b> — 10 pozisyon: <b>BTC</b> L $1.2M (#")
        assert f"Not: GOOGL zaten takipte (#{t['id']})" in text3
        assert "👣 <b>BTC</b>" in text3 and "+1 daha" in text3              # liste: takipteler 👣 ile
        assert _cbs(kb3) == [f"trk:uto:{t['id']}"], _cbs(kb3)   # ilk 10 takipte; Yalnız = GOOGL; çoklu: 🛑 yok
        # kapanmış pozisyon: açılmaz
        cli.states = {"main": {"assetPositions": []}, "xyz": {"assetPositions": []}}
        ut._CACHE.clear()
        oid2 = await _offer("xyz:AMZN")
        async with dbm.db() as c:
            await c.execute("UPDATE trackers SET active=0 WHERE coin='xyz:AMZN'")
        toast4, text4, _ = await ut.from_offer(cfg, cli, oid2, all_=False)
        assert text4 is None and toast4 == "👣 Yeni takip yok: AMZN kapanmış"
        # kanal tanımsız
        cfg.unwind_track_chat_id = ""
        assert (await ut.from_offer(cfg, cli, oid, all_=False))[0].startswith("UNWIND_TRACK_CHAT_ID tanımsız")
        from tests.test_wording import hits
        for m in (text, text3):
            assert not hits(m), hits(m)
    asyncio.run(run())
    print("✅ tuş) X → tek takip (takip kanalı, src=unwind); hepsi → öteki ilk 10 (≥$100K); tekrar yok; kapanmış yok")


def test_tracker_step_list_and_no_liq():
    async def run():
        await _fresh()
        cfg = _cfg()
        cli = _Cli(_book())
        _, _, _ = await ut.from_offer(cfg, cli, await _offer(), all_=False)
        t = (await _trackers())[0]
        live = await tr.live_position(cli, A, "BTC")
        plain = await tr.start_tracker(cfg, A, "BTC", "BTC", live, chat_id="")          # sıradan takip
        n = _N()
        # GOOGL %20 kapandı + liq kaydı; BTC'de yalnız liq kaydı
        cli.states = _book(googl=13_600)
        cli.states["xyz"]["assetPositions"][0]["position"]["liquidationPx"] = "1000"
        cli.states["main"]["assetPositions"][0]["position"]["liquidationPx"] = "80"
        ut._CACHE.clear()
        rows = await _trackers()
        for row in rows:
            await tr._check_one(cfg, cli, n, row, dbm.now())
        uw = [s for s in n.sent if s[2].get("chat_id") == TRK]
        assert len(uw) == 1, [s[1][:40] for s in n.sent]
        txt, kw = uw[0][1], uw[0][2]
        assert txt.startswith("✂️ <b>BALİNA KAPATIYOR</b> — GOOGL") and "toplamın <b>%20</b>" in txt
        assert "Öteki açık pozisyonları (11, ≥ $100K" in txt and "👣 <b>BTC</b>" in txt, txt
        assert _cbs(kw["reply_markup"])[:3] == [f"trk:uta:{t['id']}", f"trk:uto:{t['id']}", f"trk:stop:pos:{t['id']}"]
        # sıradan BTC takibi: liq notu var, liste yok
        other = [s for s in n.sent if s[2].get("chat_id") != TRK]
        assert len(other) == 1 and other[0][1].startswith("🛡 <b>LIQ FİYATI KAYDI</b> — BTC")
        assert "Öteki açık" not in other[0][1] and _cbs(other[0][2]["reply_markup"])[0] == f"trk:stop:pos:{plain}"
        # 🧯 takibinde liq kaydı tek başına mesaj üretmez
        n.sent.clear()
        cli.states["xyz"]["assetPositions"][0]["position"]["liquidationPx"] = "900"
        for row in await _trackers():
            if row["id"] == t["id"]:
                await tr._check_one(cfg, cli, n, row, dbm.now())
        assert not n.sent
        # tam kapanış: kritik, yalnız "hepsini takip et" (takipte olmayan öteki varsa)
        cli.states["xyz"]["assetPositions"] = cli.states["xyz"]["assetPositions"][1:]
        ut._CACHE.clear()
        row = next(r for r in await _trackers() if r["id"] == t["id"])
        await tr._check_one(cfg, cli, n, row, dbm.now())
        assert len(n.sent) == 1 and n.sent[0][1].startswith("🚨🚪 <b>BALİNA TAMAMEN KAPATTI</b> — GOOGL")
        assert n.sent[0][2]["chat_id"] == TRK and n.sent[0][2]["priority"] == "critical"
        assert _cbs(n.sent[0][2]["reply_markup"]) == [f"trk:uta:{t['id']}"] and "Öteki açık" in n.sent[0][1]
        # sorgu düşerse: mesaj yine gider, liste yok
        assert (await ut.extras(cfg, None, {**t, "address": "0xdead"}))[0] == ""
    asyncio.run(run())
    print("✅ takip turu) %10 adım takip kanalına + öteki liste + soru tuşları; liq notu yok; kapanışta yalnız 'hepsi'")


def _bot(cfg, cli, status=None):
    from app.telegram.bot import TelegramBot
    bot = TelegramBot(cfg, None, cli, {})
    calls, status = [], status or {}

    async def fake_call(method, payload, timeout=30):
        calls.append((method, payload))
        if method == "getChatMember":
            return 200, {"ok": True, "result": {"status": status.get(payload["user_id"], "member")}}
        return 200, {"ok": True, "result": {"message_id": 9}}

    async def fake_send(text, chat_id=None, reply_markup=None):
        calls.append(("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}))
        return True
    bot.call = fake_call
    bot.send = fake_send
    return bot, calls


def _cq(data, chat_id, chat_type, uid, kb=None, mid=55):
    msg = {"message_id": mid, "chat": {"id": chat_id, "type": chat_type}}
    if kb:
        msg["reply_markup"] = kb
    return {"update_id": 1, "callback_query": {"id": "cb", "from": {"id": uid, "is_bot": False},
                                               "data": data, "message": msg}}


def test_callbacks():
    async def run():
        await _fresh()
        cfg = _cfg()
        cli = _Cli(_book())
        bot, calls = _bot(cfg, cli, status={7: "creator", 8: "member"})
        oid = await _offer()
        toasts = lambda: [p.get("text", "") for m, p in calls if m == "answerCallbackQuery"]       # noqa: E731
        sends = lambda: [p for m, p in calls if m == "sendMessage"]                                # noqa: E731
        edits = lambda: [p for m, p in calls if m == "editMessageReplyMarkup"]                     # noqa: E731
        # yetkisiz (kanalda yönetici değil): ret
        await bot._handle_update(_cq(f"trk:uwa:{oid}", int(UW), "channel", 8))
        assert toasts() == ["Bu tuşu yalnız sahibi ya da kanal yöneticisi kullanabilir"] and not await _trackers()
        # sahip, 🧯 kanalında "hepsi": toast + takip kanalına mesaj
        calls.clear()
        await bot._handle_update(_cq(f"trk:uwa:{oid}", int(UW), "channel", 7))
        ts = await _trackers()
        assert toasts() == ["👣 takip başlatılıyor → takip kanalı"] and len(ts) == 11
        s = sends()
        assert len(s) == 1 and str(s[0]["chat_id"]) == TRK and "TAKİBE BAŞLADIK</b> — 11 pozisyon" in s[0]["text"]
        g = ts[0]
        kb = fmt.unwind_track_kb(g["id"], "GOOGL", 1)
        # takip kanalında ⏰ (takip açık kalır): soru satırı korunur; 🛑 bırakınca yalnız ↩️ geri al
        calls.clear()
        await bot._handle_update(_cq(f"trk:wake:pos:{g['id']}", int(TRK), "supergroup", 7, kb=kb))
        e = edits()
        assert e and _cbs(e[0]["reply_markup"]) == [f"trk:uta:{g['id']}", f"trk:uto:{g['id']}",
                                                    f"trk:stop:pos:{g['id']}", f"trk:nowake:pos:{g['id']}"], e
        calls.clear()
        await bot._handle_update(_cq(f"trk:stop:pos:{g['id']}", int(TRK), "supergroup", 7, kb=kb))
        assert _cbs(edits()[0]["reply_markup"]) == [f"trk:undo:pos:{g['id']}"]
        await bot._handle_update(_cq(f"trk:undo:pos:{g['id']}", int(TRK), "supergroup", 7, kb=kb))
        # 🎯 yalnız GOOGL: öteki 10 🧯 takibi bırakılır, soru satırı kalkar, 🛑 kalır
        calls.clear()
        await bot._handle_update(_cq(f"trk:uto:{g['id']}", int(TRK), "supergroup", 7, kb=kb))
        act = [x for x in await _trackers() if x["active"]]
        assert [x["id"] for x in act] == [g["id"]], act
        assert toasts() == ["🎯 Yalnız GOOGL takipte · 10 takip bırakıldı (/takipler)"]
        assert _cbs(edits()[0]["reply_markup"]) == [f"trk:stop:pos:{g['id']}", f"trk:wake:pos:{g['id']}"]
        # 👣 hepsini (takip mesajından): kalanlar yeniden açılır, "hepsi" tuşu kalkar
        calls.clear()
        await bot._handle_update(_cq(f"trk:uta:{g['id']}", int(TRK), "supergroup", 7, kb=kb))
        assert len([x for x in await _trackers() if x["active"]]) == 11
        assert "10 pozisyon" in sends()[0]["text"] and _cbs(edits()[0]["reply_markup"])[0] == f"trk:uto:{g['id']}"
        calls.clear()
        await bot._handle_update(_cq(f"trk:uta:{g['id']}", int(TRK), "supergroup", 7, kb=kb))
        assert sends()[0]["text"].startswith("👣 Yeni takip yok")
        # takip kanalı tanımsız: ret
        cfg.unwind_track_chat_id = ""
        calls.clear()
        await bot._handle_update(_cq(f"trk:uw1:{oid}", int(UW), "channel", 7))
        assert toasts() == ["UNWIND_TRACK_CHAT_ID tanımsız — takip kanalı yok"] and not sends()
    asyncio.run(run())
    print("✅ callback) hepsi → takip kanalı; 🛑'de soru satırı korunur; yalnız X öteki 🧯 takipleri bırakır; yetki")


def test_wiring():
    async def run():
        src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
        cfg = Config()
        assert cfg.unwind_track_chat_id == "" and "unwind_track_chat_id" not in EDITABLE_FIELDS
        readme, env = src("README.md"), src(".env.example")
        assert "### 👣 Takip tuşu (`UNWIND_TRACK_CHAT_ID`)" in readme and "UNWIND_TRACK_CHAT_ID=" in env
        assert "**Kör nokta, açıkça:** takip 120 sn'de bir bakar" in readme
        bot = src("app/telegram/bot.py")
        assert 'getattr(self.cfg, "unwind_track_chat_id", "")' in bot and 'offer.get("kind") == "unwind"' in bot
        assert "app/radar/unwindtrack.py" in src("tests/test_wording.py")
        await _fresh()
        cfg.unwind_chat_id = UW
        await dbm.kv_set(u.STATS_KV, {"coins_vol": 3, "candidates": 1})
        assert (await u.diag_line(cfg)).endswith("👣 takip tuşu yok (UNWIND_TRACK_CHAT_ID tanımsız)")
        cfg.unwind_track_chat_id = TRK
        await ut.from_offer(_cfg(), _Cli(_book()), await _offer(), all_=False)
        assert (await u.diag_line(cfg)).endswith("👣 açık takip 1")
        # /takip_N elle yazılırsa: tuşla aynı yol (haber takip kanalına)
        bot_, calls = _bot(_cfg(), _Cli(_book()))
        await bot_._cmd_track_start(f"takip_{await _offer('xyz:AMZN')}", "111")
        sent = [p for m, p in calls if m == "sendMessage"]
        assert str(sent[0]["chat_id"]) == TRK and "AMZN" in sent[0]["text"] and str(sent[1]["chat_id"]) == "111"
    asyncio.run(run())
    print("✅ kablolama) env-only, _own_chats, README + .env, /tani satırı, /takip_N aynı yol")


if __name__ == "__main__":
    test_alert_buttons()
    test_from_offer_single_all_dup()
    test_tracker_step_list_and_no_liq()
    test_callbacks()
    test_wiring()
