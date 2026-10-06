"""🛑 Takibi bırak tuşu — tek dokunuş bırakır, ↩️ geri alır.

Kullanıcı (07.10, "🛡 LIQ FİYATI KAYDI — HYPE (#9)" ekran görüntüsüyle): "şu bildirimlerin içine
takibi bırak tuşu ekle". Kararlar: tek dokunuş + Geri al (yanlışlıkla dokunma; geri alınan takip
kaldığı yerden sürer); tüm takip bildirimleri (👣 pozisyon, 🧲 duvar, 👁 TWAP emri); takibin
bittiği mesajlarda tuş yok.

Pinlenenler:
  • klavye: trk:stop|undo:pos|wall|twap:N, ≤ 64 bayt, herkese açık akışın önekleriyle çakışmaz
  • trackctl: üç tür için bırak / geri al / durum; geri alma yalnız ELLE bırakılmışta; aynı
    balinada yeni takip, dönüş penceresi geçmiş / budanmış duvar → ret
  • Notifier: tuş yalnız bot.send'e (fan-out'a değil); verilmezse eski çağrı biçimi
  • pozisyon: adım / 🛡 liq / 🔁 yön / ⏳ süre notunda tuş, kapanışta yok; tur ortasında
    bırakılan takip bayat mesaj üretmez
  • 🧲 duvar notu ve 👁 TWAP yarılandı notunda tuş, TWAP bitişinde yok; bırakılmışa not gitmez
  • callback: sahip → bırak (toast + geri al tuşu) / geri al (takip tuşu); eski tuş durumdan
    çizilir; bitmiş takipte tuş kalkar; değişmeyen klavye düzenlenmez; grup üyesi izinli; kanalda
    yalnız sahip id'si / yönetici (getChatMember önbellekli); yabancı sohbet ret; bozuk veri;
    herkese açık akış açıkken trk: oraya gitmez, k: gider
  • komutlar: /birak_N, /birak_duvar_N, /birak_twap_N onayında geri al tuşu; başlangıç
    mesajlarında 🛑 tuşu; yardımda anlatılır
"""
import asyncio
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-trackbtn.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.notify import Notifier  # noqa: E402
from app.radar import stickywall as sw  # noqa: E402
from app.radar import tracker as tr  # noqa: E402
from app.radar import trackctl  # noqa: E402
from app.radar import twapfollow as tf  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A = "0x" + "a" * 40
B = "0x" + "b" * 40
MARK = 40.0


class Client:
    """Sahte HL: pos[addr] = (szi, liq_px) ya da None (kapanmış)."""

    def __init__(self, pos):
        self.pos, self.fills = dict(pos), {}

    async def clearinghouse(self, addr, dex=""):
        p = self.pos.get(addr)
        if not p:
            return {"assetPositions": []}
        szi, liq = p
        return {"assetPositions": [{"position": {
            "coin": "HYPE", "szi": str(szi), "positionValue": str(abs(szi) * MARK),
            "liquidationPx": str(liq), "entryPx": str(MARK), "leverage": {"value": "10"},
            "unrealizedPnl": "0"}}]}

    async def user_fills_by_time(self, addr, start_ms, end_ms=None):
        return self.fills.get(addr, [])


class Bot:
    def __init__(self):
        self.sent = []

    async def send(self, text, chat_id=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return True


class OldBot:
    """reply_markup bilmeyen eski sahte — Notifier tuşsuz çağrıda onu bozmamalı."""

    def __init__(self):
        self.sent = []

    async def send(self, text, chat_id=None):
        self.sent.append((chat_id, text))
        return True


class Nt:
    def __init__(self):
        self.sent = []

    async def send(self, kind, text, **kw):
        self.sent.append((kind, kw.get("chat_id"), text, kw.get("reply_markup")))
        return True


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id, cfg.telegram_owner_id = "111", "7"
    cfg.crypto_chat_id, cfg.account_chat_id = "-100", "-500"
    cfg.notify_track = True
    cfg.track_step_pct, cfg.track_liq_step_pct, cfg.track_expire_days = 10.0, 1.0, 14
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.public_bot_enabled = False
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "tb.db"))
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HYPE": {"m": MARK, "oi": 1, "f": 0, "v": 1, "p": None}},
                                       "ts": dbm.now()})
    return _cfg()


async def _row(table, rid):
    async with dbm.db() as c:
        cur = await c.execute(f"SELECT * FROM {table} WHERE id=?", (rid,))
        r = await cur.fetchone()
    return dict(r) if r else None


async def _tracker(cfg, cli, addr=A, chat=""):
    live = await tr.live_position(cli, addr, "HYPE")
    return await tr.start_tracker(cfg, addr, "HYPE", "HYPE", live, chat_id=chat)


async def _wall(active=1, end_ago=0):
    t = dbm.now()
    async with dbm.db() as c:
        cur = await c.execute(
            "INSERT INTO sticky_walls(coin,side,first_ts,last_ts,px_last,ntl_last,peak_ntl,seg_peak,"
            "n_moves,tranches,active,status,end_ts) VALUES('SAND','ask',?,?,0.062,400000,1000000,1000000,"
            "3,0,?,?,?)", (t - 600, t, active, "aktif" if active else "çekildi", (t - end_ago) if not active else None))
        return int(cur.lastrowid)


def _btn(kb):
    return kb["inline_keyboard"][0][0]


# ---------------- klavye ve trackctl ----------------

def test_keyboards():
    b = _btn(fmt.stop_kb("pos", 9))
    assert b == {"text": "🛑 Takibi bırak (#9)", "callback_data": "trk:stop:pos:9"}, b
    u = _btn(fmt.undo_kb("wall", 3))
    assert u == {"text": "↩️ Geri al — takip #3 bırakıldı", "callback_data": "trk:undo:wall:3"}, u
    for kind in trackctl.KINDS:
        for f in (fmt.stop_kb, fmt.undo_kb):
            data = _btn(f(kind, 2 ** 53))["callback_data"]
            assert len(data.encode()) <= 64 and not data.startswith(("k:", "q:", "m:", "pay:")), data
    assert fmt.TRACK_CB == "trk"
    print("✅ klavye) trk:stop|undo:tür:N, ≤64 bayt, herkese açık öneklerle çakışmaz")


def test_trackctl_stop_resume_state():
    async def run():
        cfg = await _fresh()
        cli = Client({A: (-50_000.0, 41.0)})
        tid = await _tracker(cfg, cli)
        st = await trackctl.state("pos", tid)
        assert st["active"] and st["label"] == f"👣 Takip #{tid} HYPE", st
        assert await trackctl.stop("pos", tid) and not await trackctl.stop("pos", tid)
        assert (await _row("trackers", tid))["end_note"] == trackctl.MANUAL
        assert await trackctl.resume("pos", tid) == (True, "")
        t = await _row("trackers", tid)
        assert t["active"] == 1 and t["end_note"] is None and t["base_szi"] == 50_000.0, "kaldığı yerden"
        assert await trackctl.resume("pos", tid) == (False, "takip zaten açık")
        # aynı balinada yeni takip açıldıysa eskisi geri alınmaz
        await trackctl.stop("pos", tid)
        tid2 = await _tracker(cfg, cli)
        assert await trackctl.resume("pos", tid) == (False, f"bu balina için yeni takip açık (#{tid2})")
        # kapanmış takip geri alınmaz
        async with dbm.db() as c:
            await c.execute("UPDATE trackers SET active=0, end_note='kapandı' WHERE id=?", (tid2,))
        assert await trackctl.resume("pos", tid2) == (False, "takip bitmiş (kapandı) — geri alınamaz")
        # 🧲 duvar takibi
        wid = await _wall()
        fid, _ = await sw.follow_start(cfg, wid, chat_id="-300")
        st = await trackctl.state("wall", fid)
        assert st["active"] and st["label"] == f"🧲 Duvar takibi #{fid} SAND", st
        assert await trackctl.stop("wall", fid) and await trackctl.resume("wall", fid) == (True, "")
        wid2 = await _wall(active=0, end_ago=60)                     # 15 dk içinde: geri alınır
        fid2, _ = await sw.follow_start(cfg, wid2, chat_id="-300")
        await trackctl.stop("wall", fid2)
        assert await trackctl.resume("wall", fid2) == (True, "")
        async with dbm.db() as c:
            await c.execute("UPDATE sticky_walls SET end_ts=? WHERE id=?", (dbm.now() - 2000, wid2))
        await trackctl.stop("wall", fid2)
        assert await trackctl.resume("wall", fid2) == (False, "duvar bitti — geri alınacak bir şey kalmadı")
        async with dbm.db() as c:
            await c.execute("DELETE FROM sticky_walls WHERE id=?", (wid2,))
        assert await trackctl.resume("wall", fid2) == (False, "duvar kaydı yok — geri alınacak bir şey kalmadı")
        # 👁 TWAP emri takibi
        tw = await tf.start(cfg, "xyz:CBRS", A, "B", 1_700_000_000_000)
        assert (await trackctl.state("twap", tw))["label"] == f"👁 TWAP takibi #{tw} CBRS"
        assert await trackctl.stop("twap", tw) and await trackctl.resume("twap", tw) == (True, "")
        assert await trackctl.still_active("twap", tw) and not await trackctl.still_active("twap", 999)
        assert await trackctl.state("xx", 1) is None and not await trackctl.stop("xx", 1)
        assert await trackctl.resume("pos", 999) == (False, "böyle bir takip yok")
        print("✅ trackctl) üç tür bırak/geri al/durum; geri al yalnız elle bırakılmışta, kaldığı yerden;"
              " yeni takip / dönüş penceresi geçmiş / budanmış duvar → ret")
    asyncio.run(run())


# ---------------- Notifier ve bildirimler ----------------

def test_notifier_markup_only_to_bot():
    async def run():
        cfg = await _fresh()
        import app.telegram.fanout as fanout
        pub, real = [], fanout.publish
        fanout.publish = lambda *a, **k: pub.append((a, k))
        try:
            bot = Bot()
            kb = fmt.stop_kb("pos", 4)
            assert await Notifier(cfg, bot).send("track", "metin", chat_id="-100", reply_markup=kb)
            assert bot.sent == [("-100", "metin", kb)]
            assert pub and all("reply_markup" not in k and kb not in a for a, k in pub), pub
            old = OldBot()
            assert await Notifier(cfg, old).send("track", "eski", chat_id="-100") and old.sent == [("-100", "eski")]
        finally:
            fanout.publish = real
        print("✅ Notifier) tuş yalnız bot.send'e; fan-out tuşsuz; tuşsuz çağrı eski sahteyi bozmaz")
    asyncio.run(run())


def test_tracker_messages_carry_button():
    async def run():
        cfg = await _fresh()
        cli = Client({A: (-50_000.0, 41.0)})
        tid = await _tracker(cfg, cli, chat="-100")
        bot = Bot()
        nt = Notifier(cfg, bot)
        want = fmt.stop_kb("pos", tid)
        cli.pos[A] = (-50_000.0, 41.5)                                  # 🛡 liq kaydı
        await tr.check_trackers(cfg, cli, nt)
        cli.pos[A] = (-40_000.0, 41.5)                                  # ✂️ adım
        await tr.check_trackers(cfg, cli, nt)
        cli.pos[A] = (40_000.0, 30.0)                                   # 🔁 yön
        await tr.check_trackers(cfg, cli, nt)
        async with dbm.db() as c:                                       # ⏳ süre notu
            await c.execute("UPDATE trackers SET expires_ts=? WHERE id=?", (dbm.now() - 10, tid))
        await tr.check_trackers(cfg, cli, nt)
        heads = [s[1].split("\n")[0] for s in bot.sent]
        assert len(bot.sent) == 4 and all(s[2] == want for s in bot.sent), heads
        assert "LIQ FİYATI KAYDI" in heads[0] and "KAPATIYOR" in heads[1] and "YÖN DEĞİŞTİRDİ" in heads[2]
        assert "Takip sürüyor" in heads[3]
        # tur başında okunan takip bu arada bırakıldıysa bayat mesaj yok
        stale = await _row("trackers", tid)
        await trackctl.stop("pos", tid)
        cli.pos[A] = (20_000.0, 30.0)
        await tr._check_one(cfg, cli, nt, stale, dbm.now())
        assert len(bot.sent) == 4, bot.sent[4:]
        # kapanış: takip biter → tuş yok
        await trackctl.resume("pos", tid)
        cli.pos[A] = None
        await tr.check_trackers(cfg, cli, nt)
        assert "TAMAMEN KAPATTI" in bot.sent[-1][1] and bot.sent[-1][2] is None
        print("✅ pozisyon) 🛡 liq / ✂️ adım / 🔁 yön / ⏳ süre notunda 🛑 tuşu; tur ortasında bırakılan"
              " takip mesaj üretmez; 🚪 kapanışta tuş yok")
    asyncio.run(run())


def test_follow_notes_carry_button():
    async def run():
        cfg = await _fresh()
        wid = await _wall()                                             # kalan %40 → yarılandı notu
        fid, _ = await sw.follow_start(cfg, wid, chat_id="-300")
        nt, out = Nt(), {"follow_sent": 0, "follow_failed": 0}
        await sw.follow_tick(cfg, nt, dbm.now(), out)
        assert len(nt.sent) == 1 and "yarılandı" in nt.sent[0][2] and nt.sent[0][3] == fmt.stop_kb("wall", fid), nt.sent
        # okunduktan sonra bırakıldıysa not gitmez (gönderim öncesi aktiflik kontrolü)
        async with dbm.db() as c:
            await c.execute("UPDATE sticky_follows SET half_ts=NULL WHERE id=?", (fid,))
        real = trackctl.still_active

        async def stopped(kind, f):
            return False
        trackctl.still_active = stopped
        try:
            await sw.follow_tick(cfg, nt, dbm.now(), out)
        finally:
            trackctl.still_active = real
        assert len(nt.sent) == 1
        # 👁 TWAP: yarılandı notunda tuş, bitişte yok, bırakılmışa hiçbir şey
        tw = await tf.start(cfg, "xyz:CBRS", A, "B", 1_700_000_000_000)
        f = await _row("twap_follows", tw)
        order = {"status": "activated", "filled_pct": 60.0, "started_ts": f["first_ts"]}
        live = types.SimpleNamespace(parse_twap_orders=lambda *a: [dict(order)], klass_of=lambda c: "hisse")
        ff = types.SimpleNamespace(twap_progress=lambda m, ctx: "YARI", twap_end=lambda m, ctx: "BİTTİ",
                                   stop_kb=fmt.stop_kb)
        cfg.twap_follow_progress = True
        nt2, out2 = Nt(), {k: 0 for k in ("progress", "failed", "ended", "cancelled", "no_order")}
        await tf._one(cfg, f, [], dbm.now(), nt2, ff, live, out2)
        assert nt2.sent[-1][2].endswith("YARI") and nt2.sent[-1][3] == fmt.stop_kb("twap", tw), nt2.sent
        order["status"] = "finished"
        await trackctl.stop("twap", tw)
        await tf._one(cfg, f, [], dbm.now(), nt2, ff, live, out2)
        assert len(nt2.sent) == 1, "bırakılmış takibe bitiş haberi de gitmez"
        await trackctl.resume("twap", tw)
        await tf._one(cfg, f, [], dbm.now(), nt2, ff, live, out2)
        assert nt2.sent[-1][2].endswith("BİTTİ") and nt2.sent[-1][3] is None and out2["ended"] == 1
        assert (await _row("twap_follows", tw))["active"] == 0
        print("✅ takip notları) 🧲 duvar ve 👁 TWAP yarılandı notunda tuş; TWAP bitişinde yok;"
              " bırakılmış takibe not gitmez, geri alınınca bitiş haberi gelir")
    asyncio.run(run())


# ---------------- callback ----------------

def _cq(data, chat_id, chat_type, uid, kb=None, mid=55):
    msg = {"message_id": mid, "chat": {"id": chat_id, "type": chat_type}}
    if kb:
        msg["reply_markup"] = kb
    return {"update_id": 1, "callback_query": {"id": "cb", "from": {"id": uid, "is_bot": False},
                                               "data": data, "message": msg}}


def _bot(cfg, cli, status=None):
    from app.telegram.bot import TelegramBot
    bot = TelegramBot(cfg, None, cli, {})
    calls, status = [], status or {}

    async def fake_call(method, payload, timeout=30):
        calls.append((method, payload))
        if method == "getChatMember":
            return 200, {"ok": True, "result": {"status": status.get(payload["user_id"], "member")}}
        return 200, {"ok": True, "result": True}
    bot.call = fake_call
    return bot, calls


def _toasts(calls):
    return [p.get("text", "") for m, p in calls if m == "answerCallbackQuery"]


def _edits(calls):
    return [p for m, p in calls if m == "editMessageReplyMarkup"]


def test_callback_stop_undo_and_render():
    async def run():
        cfg = await _fresh()
        cli = Client({A: (-50_000.0, 41.0), B: (10_000.0, 39.0)})
        tid = await _tracker(cfg, cli)
        bot, calls = _bot(cfg, cli)
        lbl = f"👣 Takip #{tid} HYPE"
        # sahip, özel sohbet: bırak → toast + geri al tuşu
        await bot._handle_update(_cq(f"trk:stop:pos:{tid}", 111, "private", 111, kb=fmt.stop_kb("pos", tid)))
        t = await _row("trackers", tid)
        assert t["active"] == 0 and t["end_note"] == trackctl.MANUAL
        assert _toasts(calls) == [f"🛑 {lbl} bırakıldı — yanlışlıkla mı? ↩️ Geri al"], calls
        assert _edits(calls) == [{"chat_id": "111", "message_id": 55, "reply_markup": fmt.undo_kb("pos", tid)}]
        # geri al → kaldığı yerden, tuş yeniden "bırak"
        calls.clear()
        await bot._handle_update(_cq(f"trk:undo:pos:{tid}", 111, "private", 111, kb=fmt.undo_kb("pos", tid)))
        assert (await _row("trackers", tid))["active"] == 1
        assert _toasts(calls) == [f"↩️ {lbl} yeniden açık — kaldığı yerden sürüyor"]
        assert _edits(calls)[0]["reply_markup"] == fmt.stop_kb("pos", tid)
        # eski mesajdaki tuş: komutla bırakılmış takip → "zaten", tuş durumdan (geri al)
        await trackctl.stop("pos", tid)
        calls.clear()
        await bot._handle_update(_cq(f"trk:stop:pos:{tid}", 111, "private", 111, kb=fmt.stop_kb("pos", tid), mid=40))
        assert _toasts(calls) == [f"{lbl} zaten bırakılmış"] and _edits(calls)[0]["reply_markup"] == fmt.undo_kb("pos", tid)
        # klavye zaten doğruysa düzenleme çağrısı yok (Telegram "not modified")
        calls.clear()
        await bot._handle_update(_cq(f"trk:stop:pos:{tid}", 111, "private", 111, kb=fmt.undo_kb("pos", tid)))
        assert _toasts(calls) == [f"{lbl} zaten bırakılmış"] and not _edits(calls)
        # bitmiş takip → tuş kalkar
        tid2 = await _tracker(cfg, cli, addr=B)
        async with dbm.db() as c:
            await c.execute("UPDATE trackers SET active=0, end_note='kapandı' WHERE id=?", (tid2,))
        calls.clear()
        await bot._handle_update(_cq(f"trk:stop:pos:{tid2}", 111, "private", 111, kb=fmt.stop_kb("pos", tid2)))
        assert _toasts(calls) == [f"👣 Takip #{tid2} HYPE bitmiş: kapandı"]
        assert _edits(calls)[0]["reply_markup"] == {"inline_keyboard": []}
        # geri alma reddi: aynı balinada yeni takip → neden yazılır, tuş durumdan (geri al)
        tid3 = await _tracker(cfg, cli)
        calls.clear()
        await bot._handle_update(_cq(f"trk:undo:pos:{tid}", 111, "private", 111, kb=fmt.undo_kb("pos", tid)))
        assert _toasts(calls) == [f"{lbl}: bu balina için yeni takip açık (#{tid3})"] and not _edits(calls)
        assert (await _row("trackers", tid))["active"] == 0
        # bozuk veri → yalnız boş cevap
        calls.clear()
        await bot._handle_update(_cq("trk:stop:pos:abc", 111, "private", 111))
        assert calls == [("answerCallbackQuery", {"callback_query_id": "cb"})], calls
        print("✅ callback) bırak → toast + ↩️; geri al → kaldığı yerden; eski tuş 'zaten' (durumdan çizim);"
              " değişmeyen klavye düzenlenmez; bitmiş takipte tuş kalkar; yeni takip varsa geri alma ret")
    asyncio.run(run())


def test_callback_permissions():
    async def run():
        cfg = await _fresh()
        cli = Client({A: (-50_000.0, 41.0)})
        tid = await _tracker(cfg, cli, chat="-100")
        bot, calls = _bot(cfg, cli, status={43: "administrator"})
        no = "Bu tuşu yalnız sahibi ya da kanal yöneticisi kullanabilir"
        # kanalda sıradan abone → ret, takip açık kalır; yönetici bilgisi önbellekte
        await bot._handle_update(_cq(f"trk:stop:pos:{tid}", -100, "channel", 42, kb=fmt.stop_kb("pos", tid)))
        await bot._handle_update(_cq(f"trk:stop:pos:{tid}", -100, "channel", 42, kb=fmt.stop_kb("pos", tid)))
        assert _toasts(calls) == [no, no] and not _edits(calls)
        assert [m for m, _ in calls].count("getChatMember") == 1, "10 dk önbellek"
        assert (await _row("trackers", tid))["active"] == 1
        # kanal yöneticisi → izinli
        await bot._handle_update(_cq(f"trk:stop:pos:{tid}", -100, "channel", 43, kb=fmt.stop_kb("pos", tid)))
        assert (await _row("trackers", tid))["active"] == 0
        # sahip kullanıcı id'si → kanalda da izinli, yönetici sorgusu yok
        calls.clear()
        await bot._handle_update(_cq(f"trk:undo:pos:{tid}", -100, "channel", 7, kb=fmt.undo_kb("pos", tid)))
        assert (await _row("trackers", tid))["active"] == 1 and "getChatMember" not in [m for m, _ in calls]
        # hesap grubu üyesi → izinli (grupta /birak_N'i zaten yazabiliyor)
        await bot._handle_update(_cq(f"trk:stop:pos:{tid}", -500, "supergroup", 42, kb=fmt.stop_kb("pos", tid)))
        assert (await _row("trackers", tid))["active"] == 0
        await trackctl.resume("pos", tid)
        # yabancı grup → ret, durum değişmez
        calls.clear()
        await bot._handle_update(_cq(f"trk:stop:pos:{tid}", -999, "group", 42, kb=fmt.stop_kb("pos", tid)))
        assert _toasts(calls) == [no] and (await _row("trackers", tid))["active"] == 1
        # herkese açık akış açıkken: trk: oraya gitmez, kendi önekleri gider
        import app.telegram.public as public
        seen, real = [], public.handle

        async def fake_handle(b, upd):
            seen.append(upd["callback_query"]["data"])
            return True
        public.handle = fake_handle
        cfg.public_bot_enabled = True
        try:
            await bot._handle_update(_cq(f"trk:stop:pos:{tid}", 111, "private", 111))
            await bot._handle_update(_cq("k:cryptoliq", 555, "private", 555))
        finally:
            public.handle = real
            cfg.public_bot_enabled = False
        assert seen == ["k:cryptoliq"] and (await _row("trackers", tid))["active"] == 0
        print("✅ yetki) kanalda abone ret (takip açık kalır, yönetici sorgusu önbellekli), yönetici ve sahip"
              " id'si izinli; grup üyesi izinli; yabancı sohbet ret; trk: herkese açık akışa gitmez")
    asyncio.run(run())


def test_commands_and_start_messages():
    async def run():
        cfg = await _fresh()
        cli = Client({A: (-50_000.0, 41.0)})
        from app.telegram.bot import TelegramBot
        bot = TelegramBot(cfg, None, cli, {})
        sent = []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append((chat_id, text, reply_markup))
            return True
        bot.send = fake_send

        async def cmd(text, chat=111):
            await bot._handle_update({"message": {"chat": {"id": chat, "type": "private"},
                                                  "from": {"id": chat}, "text": text}})
            return sent[-1]
        _, text, kb = await cmd(f"/takip {A} HYPE")
        tid = (await trackctl.state("pos", 1))["id"]
        assert "TAKİP BAŞLADI" in text and kb == fmt.stop_kb("pos", tid), (text, kb)
        _, text, kb = await cmd(f"/birak_{tid}")
        assert text == f"👣 Takip #{tid} (<b>HYPE</b> {fmt.short(A)}) bırakıldı." and kb == fmt.undo_kb("pos", tid)
        _, text, kb = await cmd(f"/birak_{tid}")
        assert "aktif takip yok" in text and kb is None
        # 🧲 duvar ve 👁 TWAP: başlangıç mesajında 🛑, /birak onayında ↩️
        wid = await _wall()
        await bot._cmd_sticky_follow({"symbol": "SAND", "coin": "SAND", "ref_ts": wid}, 0, "111")
        fid = (await sw.follows_active())[0]["id"]
        assert "yapışkan duvarı takipte" in sent[-1][1] and sent[-1][2] == fmt.stop_kb("wall", fid)
        _, text, kb = await cmd(f"/birak_duvar_{fid}")
        assert text == f"🧲 Yapışkan duvar takibi #{fid} bırakıldı." and kb == fmt.undo_kb("wall", fid)
        await bot._cmd_twap_follow({"symbol": "CBRS", "coin": "xyz:CBRS", "address": A, "side": "B",
                                    "ref_ts": 1_700_000_000_000}, 0, "111")
        tw = (await tf.active())[0]["id"]
        assert "TWAP emri takipte" in sent[-1][1] and sent[-1][2] == fmt.stop_kb("twap", tw)
        _, text, kb = await cmd(f"/birak_twap_{tw}")
        assert text == f"👁 TWAP emri takibi #{tw} bırakıldı." and kb == fmt.undo_kb("twap", tw)
        _, text, _ = await cmd("/birak_twap_x")
        assert text.startswith("Kullanım: /twaptakipler")
        assert "🛑" in fmt.help_text() and "geri alınabilir" in fmt.help_text()
        print("✅ komutlar) /takip başlangıcında 🛑; /birak_N, /birak_duvar_N, /birak_twap_N onayında ↩️;"
              " olmayan takip tuşsuz; yardımda anlatılır")
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
