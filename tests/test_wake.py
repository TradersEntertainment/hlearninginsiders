"""🚨 Uyandırma alarmı — gece bir şey olursa seni Telegram'dan ARAR (CallMeBot).

Kullanıcı (07.10): "uyumadan önce pozum açık oluyor, bir şey olursa uyanmak istiyorum ama telefonum
rahatsız etmede — bildirim gelmez, yalnız aramalara uyanırım, o da 2 kez peş peşe". Kararlar:
kanal yalnız Telegram araması (ücretsiz); tetikler fiyat seviyesi, yüzde, HL adresi, takip.

Pinlenenler:
  • /alarm argümanları: seviye (virgüllü ondalık), iki seviye, %X / X%, adres [%X]; hatalar
  • kurma: yön şimdiki fiyattan; seviye = fiyat / aralık dışı / yok coin → ret; HIP-3 allMids
    anahtarı 'xyz:' önekiyle eşlenir
  • tetik: kesişmede TEK olay, alarm kapanır; yüzde; süresi dolan sessiz kalkar; veri 5 dk
    okunamazsa tek "KÖR" uyarısı, arama yok; adres: liq'e ≤ %2, pozisyon kayboldu (likide/kapandı)
  • teslimat: ✅ tuşlu mesaj (id saklanır) + arama 1 → ~70 sn → arama 2 → 4 dk → … en çok 6;
    onayda arama durur; izin yoksa tek uyarı ve arama kesilir; kullanıcı yoksa yalnız mesaj;
    deneme tek tur; sesli metin ≤ 256, Türkçe virgüllü sayı
  • takip: ⏰ tuşu yalnız sahibe; wake=1 kapanış / yön → olay; wake=0 → yok
  • komutlar yalnız sahip: /alarm yardım, kurma + 🗑, /alarmlar, /alarm_test, /uyandim;
    grup üyesi sessiz; ✅ / 🗑 tuşları yalnız sahip
  • CallMeBot isteği: parametreler, "not authorized", HTTP hatası
  • kablolama: spawn, tablolar, migration, ayarlar, sağlık, tanı (kişisel bilgi maskeli), README, .env
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-wake.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.notify import Notifier  # noqa: E402
from app.radar import tracker as tr  # noqa: E402
from app.radar import trackctl  # noqa: E402
from app.radar import wake  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A = "0x" + "a" * 40
B = "0x" + "b" * 40
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class HL:
    """Sahte HL: mids[dex] = {coin: px} (None → okuma hatası); pos[(adres, dex)] = konumlar."""

    def __init__(self):
        self.mids = {"": {"HYPE": "36.5", "BTC": "61000"}, "xyz": {"SNDK": "495.2"}}
        self.pos, self.fills, self.calls = {}, {}, []

    async def all_mids(self, dex=""):
        self.calls.append(("mids", dex))
        m = self.mids.get(dex)
        if m is None:
            raise RuntimeError("allMids düştü")
        return dict(m)

    async def clearinghouse(self, addr, dex=""):
        self.calls.append(("ch", addr, dex))
        out = []
        for coin, (szi, px, liq) in (self.pos.get((addr, dex)) or {}).items():
            out.append({"position": {"coin": coin, "szi": str(szi), "positionValue": str(abs(szi) * px),
                                     "liquidationPx": str(liq) if liq else None, "entryPx": str(px),
                                     "leverage": {"value": "10"}, "unrealizedPnl": "0"}})
        return {"assetPositions": out}

    async def user_fills_by_time(self, addr, start_ms, end_ms=None):
        return self.fills.get(addr, [])


class TBot:
    """Sahte Telegram: call() (sendMessage / edit / answer) + send()."""

    def __init__(self, ok=True):
        self.ok, self.calls, self.sent, self._mid = ok, [], [], 900

    async def call(self, method, payload, timeout=30):
        self.calls.append((method, payload))
        if not self.ok:
            return 502, {"ok": False}
        if method == "sendMessage":
            self._mid += 1
            return 200, {"ok": True, "result": {"message_id": self._mid}}
        return 200, {"ok": True, "result": True}

    async def send(self, text, chat_id=None, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))
        return self.ok


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id, cfg.telegram_owner_id = "111", "7"
    cfg.account_chat_id, cfg.crypto_chat_id = "-500", "-100"
    cfg.wake_telegram_user = "@omer"
    cfg.wake_enabled = True
    cfg.equity_dexes = ["xyz"]
    cfg.notify_track = True
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.public_bot_enabled = False
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "wake.db"))
    async with dbm.db() as c:
        await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('xyz:SNDK','xyz','SNDK')")
        await c.commit()
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HYPE": {"m": 36.5, "oi": 1, "v": 1.0},
                                             "BTC": {"m": 61000.0, "oi": 1, "v": 1.0}}, "ts": dbm.now()})
    wake._data.update(fail_since=None, warned=False)
    return _cfg()


class Calls:
    """Sahte CallMeBot."""

    def __init__(self, ok=True, note="ok"):
        self.ok, self.note, self.made = ok, note, []

    async def __call__(self, session, user, text, lang, rpt=2):
        self.made.append((user, text, lang))
        return self.ok, self.note


def _patch_calls(c):
    real = wake.callmebot_call
    wake.callmebot_call = c
    return real


async def _events():
    async with dbm.db() as c:
        cur = await c.execute("SELECT * FROM wake_events ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def _notify_sink(sink):
    async def f(text):
        sink.append(text)
    return f


# CallMeBot'un canlı yanıtı (07.10, izin vermemiş kullanıcı): analytics <script>'li HTML sayfası
GTAG = ('<head><!-- Global site tag (gtag.js) - Google Analytics -->\n<script async '
        'src="https://www.googletagmanager.com/gtag/js?id=UA-149177385-1"></script>\n<script>\n'
        '  window.dataLayer = window.dataLayer || [];\n  function gtag(){dataLayer.push(arguments);}\n'
        '  gtag("js", new Date());\n\n  gtag("config", "UA-149177385-1");\n</script>\n</head>')
NOT_AUTH = (GTAG + "<p>Checking Authorization for @omer...<br>Using bot: @CallMeBot_API<p>Authorization for user"
            " @omer is not received.<p><font color=\"red\">Warning! User not authorized. Click <a"
            " href=\"https://api2.callmebot.com/txt/auth.php\">here</a> to authorize CallMeBot to contact you.</font>")


# ---------------- argümanlar ve kurma ----------------

def test_parse_args():
    p = wake.parse_args
    assert p(["SNDK", "480"]) == {"kind": "price", "sym": "SNDK", "levels": [480.0]}
    assert p(["sndk", "480,5"])["levels"] == [480.5] and p(["$SNDK", "520", "480"])["levels"] == [480.0, 520.0]
    assert p(["SNDK", "%3"]) == {"kind": "pct", "sym": "SNDK", "pct": 3.0} and p(["SNDK", "2.5%"])["pct"] == 2.5
    assert p([A]) == {"kind": "addr", "address": A, "liq_pct": None}
    assert p([A.upper().replace("0X", "0x"), "%2,5"])["liq_pct"] == 2.5
    for bad in ([], ["SNDK"], ["SNDK", "abc"], ["SNDK", "%0"], ["SNDK", "1", "2", "3"], [A, "x"], ["SNDK", "-5"]):
        assert "error" in p(bad), bad
    assert wake.fshort(480.0) == "480" and wake.fshort(480.5) == "480.5" and wake.say_num(479.9) == "479,9"
    assert wake.say_num(61000.4) == "61000" and wake.fpx(36.5) == "36.50"
    print("✅ argümanlar) seviye (virgüllü), iki seviye, %X / X%, adres [%X]; bozuklar ret; Türkçe sayı")


def test_arm_and_level_trigger():
    async def run():
        cfg = await _fresh()
        hl = HL()
        r = await wake.arm(cfg, hl, wake.parse_args(["SNDK", "480"]))
        a = r["alarm"]
        assert r["ok"] and a["coin"] == "xyz:SNDK" and a["lo"] == 480 and a["hi"] is None and a["ref_px"] == 495.2
        assert wake.describe(a) == "SNDK ≤ 480" and a["expires_ts"] - a["created_ts"] == 18 * 3600
        up = (await wake.arm(cfg, hl, wake.parse_args(["SNDK", "520"])))["alarm"]
        assert up["hi"] == 520 and up["lo"] is None and wake.describe(up) == "SNDK ≥ 520"
        for args, why in ((["SNDK", "495.2"], "aynı"), (["SNDK", "500", "520"], "ARASINDA"),
                          (["ZZZZ", "5"], "evreninde yok")):
            res = await wake.arm(cfg, hl, wake.parse_args(args))
            assert not res["ok"] and why in res["reason"], (args, res)
        band = (await wake.arm(cfg, hl, wake.parse_args(["SNDK", "480", "520"])))["alarm"]
        assert (band["lo"], band["hi"]) == (480, 520) and wake.describe(band) == "SNDK ≤ 480 ya da ≥ 520"
        await wake.cancel(up["id"])
        await wake.cancel(band["id"])
        # kesişme yok → olay yok; kesişme → TEK olay, alarm kapanır
        sink = []
        notify = await _notify_sink(sink)
        hl.mids["xyz"]["SNDK"] = "490"
        out = await wake.check_alarms(cfg, hl, dbm.now(), notify)
        assert out["fired"] == 0 and not await _events()
        hl.mids["xyz"]["SNDK"] = "479.9"
        out = await wake.check_alarms(cfg, hl, dbm.now(), notify)
        await wake.check_alarms(cfg, hl, dbm.now(), notify)
        evs = await _events()
        assert out["fired"] == 1 and len(evs) == 1, evs
        ev = evs[0]
        assert ev["key"] == f"alarm:{a['id']}" and ev["title"] == "SNDK 480 altına indi — şimdi 479.90", ev["title"]
        assert ev["speech"].startswith("Dikkat. SNDK 480 altına indi. Şu an 479,9.") and len(ev["speech"]) <= 256
        a2 = await wake.get(a["id"])
        assert a2["active"] == 0 and a2["end_note"] == "tetiklendi" and a2["fired_ts"]
        # yüzde: ±%3
        hl.mids["xyz"]["SNDK"] = "495.2"
        pa = (await wake.arm(cfg, hl, wake.parse_args(["SNDK", "%3"])))["alarm"]
        assert abs(pa["lo"] - 495.2 * 0.97) < 1e-9 and abs(pa["hi"] - 495.2 * 1.03) < 1e-9
        hl.mids["xyz"]["SNDK"] = "511"
        await wake.check_alarms(cfg, hl, dbm.now(), notify)
        assert (await _events())[-1]["title"] == "SNDK %3.2 yükseldi — şimdi 511.00"
        assert "SNDK yüzde 3,2 yükseldi. Şu an 511." in (await _events())[-1]["speech"]
        # süresi dolan sessiz kalkar
        ex = (await wake.arm(cfg, hl, wake.parse_args(["BTC", "50000"])))["alarm"]
        async with dbm.db() as c:
            await c.execute("UPDATE wake_alarms SET expires_ts=? WHERE id=?", (dbm.now() - 1, ex["id"]))
        out = await wake.check_alarms(cfg, hl, dbm.now(), notify)
        assert out["expired"] == 1 and (await wake.get(ex["id"]))["end_note"] == "süre doldu" and len(await _events()) == 2
        # veri 5 dk okunamazsa TEK uyarı (arama yok); düzelince sıfırlanır
        await wake.arm(cfg, hl, wake.parse_args(["HYPE", "30"]))
        hl.mids[""] = None
        t0 = dbm.now()
        await wake.check_alarms(cfg, hl, t0, notify)
        await wake.check_alarms(cfg, hl, t0 + 200, notify)
        assert not sink
        await wake.check_alarms(cfg, hl, t0 + 301, notify)
        await wake.check_alarms(cfg, hl, t0 + 400, notify)
        assert len(sink) == 1 and "KÖR" in sink[0], sink
        hl.mids[""] = {"HYPE": "36.5"}
        await wake.check_alarms(cfg, hl, t0 + 420, notify)
        assert wake._data["fail_since"] is None and len(await _events()) == 2
        print("✅ alarm) yön şimdiki fiyattan; ret: aynı seviye / aralık dışı / yok coin; kesişmede tek olay,"
              " alarm kapanır; ±%3; süresi dolan sessiz; veri 5 dk yoksa tek 'KÖR' uyarısı")
    asyncio.run(run())


def test_addr_alarm():
    async def run():
        cfg = await _fresh()
        hl = HL()
        hl.pos[(A, "")] = {"HYPE": (1000.0, 36.65, 30.0)}
        hl.pos[(A, "xyz")] = {"xyz:SNDK": (-10.0, 495.2, 600.0)}
        r = await wake.arm(cfg, hl, wake.parse_args([A]))
        a = r["alarm"]
        assert r["ok"] and a["liq_pct"] == 2.0 and set(r["positions"]) == {"HYPE", "xyz:SNDK"}
        text = fmt.wake_armed(r, "@omer", wake.plan(cfg), 18)
        assert "izlenen 2 pozisyon" in text and "HYPE LONG" in text and "liq'e %18.1" in text, text
        sink = []
        notify = await _notify_sink(sink)
        assert (await wake.check_alarms(cfg, hl, dbm.now(), notify))["fired"] == 0
        hl.pos[(A, "")] = {"HYPE": (1000.0, 36.65, 36.0)}                 # liq'e %1.8
        await wake.check_alarms(cfg, hl, dbm.now(), notify)
        ev = (await _events())[-1]
        assert ev["title"] == "HYPE LONG: likidasyona %1.8 kaldı" and "yüzde 1,8" in ev["speech"], ev
        # pozisyon kayboldu: fill'de likidasyon → LİKİDE OLDU; yoksa kapandı
        b = (await wake.arm(cfg, hl, wake.parse_args([A, "%1"])))["alarm"]
        assert b["liq_pct"] == 1.0
        hl.pos[(A, "xyz")] = {}
        hl.fills[A] = [{"coin": "xyz:SNDK", "dir": "Liquidated Isolated Short", "px": "601",
                        "liquidation": {"liquidatedUser": A}, "time": dbm.now() * 1000}]
        await wake.check_alarms(cfg, hl, dbm.now(), notify)
        assert (await _events())[-1]["title"] == "SNDK SHORT pozisyonu LİKİDE OLDU"
        hl.pos[(B, "")] = {"HYPE": (500.0, 36.65, 20.0)}
        c = (await wake.arm(cfg, hl, wake.parse_args([B])))["alarm"]
        hl.pos[(B, "")] = {}
        hl.fills[B] = [{"coin": "HYPE", "dir": "Close Long", "px": "36.7", "time": dbm.now() * 1000}]
        await wake.check_alarms(cfg, hl, dbm.now(), notify)
        assert (await _events())[-1]["title"] == "HYPE LONG pozisyonu kapandı"
        assert not (await wake.get(c["id"]))["active"]
        print("✅ adres) ana dex + xyz pozisyonları; liq'e ≤ %2 → olay; kaybolan pozisyon likide / kapandı")
    asyncio.run(run())


# ---------------- teslimat ----------------

def test_drive_calls_and_ack():
    async def run():
        cfg = await _fresh()
        calls = Calls()
        real = _patch_calls(calls)
        try:
            bot = TBot()
            t0 = dbm.now()
            eid = await wake.fire(cfg, "SNDK 480 altına indi", "⏰ Alarm #1: <b>SNDK</b>", "Dikkat. SNDK.", "alarm:1")
            assert await wake.fire(cfg, "x", "x", "x", "alarm:1") is None, "aynı olay iki kez çalmaz"
            await wake.drive(cfg, bot, None, t0)
            sends = [p for m, p in bot.calls if m == "sendMessage"]
            assert len(sends) == 1 and sends[0]["chat_id"] == "111" and sends[0]["reply_markup"] == fmt.wake_ack_kb(eid)
            assert "🚨 <b>UYANDIRMA</b>" in sends[0]["text"] and "@omer" in sends[0]["text"]
            ev = await wake.event(eid)
            assert ev["msg_id"] == 901 and ev["call_n"] == 1 and calls.made[0][0] == "@omer"
            assert calls.made[0][2] == "tr-TR-Standard-A"
            gap = ev["next_call_ts"] - ev["last_call_ts"]
            assert gap == 70, gap
            # zaman ilerler: 2. arama ~70 sn sonra, sonra tur arası 240 sn
            await wake.drive(cfg, bot, None, ev["next_call_ts"] - 1)
            assert len(calls.made) == 1
            await wake.drive(cfg, bot, None, ev["next_call_ts"])
            ev = await wake.event(eid)
            assert ev["call_n"] == 2 and ev["next_call_ts"] - ev["last_call_ts"] == 240
            # onay → arama durur, olay kapanır
            acked = await wake.ack(eid, "tuş")
            assert acked and acked[0]["ack_by"] == "tuş"
            await wake.drive(cfg, bot, None, ev["next_call_ts"] + 10)
            assert len(calls.made) == 2 and (await wake.event(eid))["done_ts"]
            # onay gelmezse en çok 3 tur = 6 arama, sonra kapanır
            e2 = await wake.fire(cfg, "t", "b", "s", "alarm:2")
            t = dbm.now()
            for _ in range(12):
                await wake.drive(cfg, bot, None, t)
                t += 300
            assert (await wake.event(e2))["call_n"] == 6 and (await wake.event(e2))["done_ts"]
            # deneme: tek tur (2 arama)
            e3 = await wake.fire(cfg, "🧪", "b", "s", "test:1", source="test")
            t = dbm.now()
            for _ in range(6):
                await wake.drive(cfg, bot, None, t)
                t += 300
            assert (await wake.event(e3))["call_n"] == 2
            # izin yok → tek uyarı, arama kesilir
            calls.ok, calls.note = False, "izin yok — " + wake.AUTH_HINT
            n0 = len(calls.made)
            e4 = await wake.fire(cfg, "t", "b", "s", "alarm:4")
            t = dbm.now()
            for _ in range(4):
                await wake.drive(cfg, bot, None, t)
                t += 300
            assert len(calls.made) == n0 + 1, "izin yoksa yeniden denemek boşuna"
            warn = [x for x in bot.calls if x[0] == "sendMessage" and "yapılamadı" in x[1]["text"]]
            assert len(warn) == 1 and "CallMeBot_txtbot" in warn[0][1]["text"]
            assert (await wake.event(e4))["warned"] == 1
            # kullanıcı yok → yalnız mesaj, arama yok
            calls.ok = True
            cfg.wake_telegram_user = ""
            n0 = len(calls.made)
            e5 = await wake.fire(cfg, "t", "b", "s", "alarm:5")
            await wake.drive(cfg, bot, None, dbm.now())
            last = [p for m, p in bot.calls if m == "sendMessage"][-1]
            assert "Arama kapalı" in last["text"] and len(calls.made) == n0 and (await wake.event(e5))["done_ts"]
            # mesaj gönderilemezse sonraki adımda yeniden denenir
            cfg.wake_telegram_user = "@omer"
            bad = TBot(ok=False)
            e6 = await wake.fire(cfg, "t", "b", "s", "alarm:6")
            await wake.drive(cfg, bad, None, dbm.now())
            assert not (await wake.event(e6))["msg_ts"]
            await wake.drive(cfg, bot, None, dbm.now())
            assert (await wake.event(e6))["msg_ts"]
        finally:
            wake.callmebot_call = real
        print("✅ teslimat) ✅ tuşlu mesaj (id saklı) + arama 1 → 70 sn → arama 2 → 240 sn; onayda durur;"
              " en çok 6; deneme 2; izin yoksa tek uyarı; kullanıcı yoksa yalnız mesaj; düşen mesaj yeniden")
    asyncio.run(run())


def test_callmebot_request():
    async def run():
        class Resp:
            def __init__(self, status, body):
                self.status, self.body = status, body

            async def text(self):
                return self.body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Sess:
            def __init__(self, status=200, body="<p>Call queued for @omer</p>"):
                self.status, self.body, self.reqs = status, body, []

            def get(self, url, params=None, timeout=None):
                self.reqs.append((url, params))
                return Resp(self.status, self.body)
        s = Sess()
        ok, note = await wake.callmebot_call(s, "@omer", "x" * 400, "tr-TR-Standard-A")
        url, params = s.reqs[0]
        assert ok and note == "Call queued for @omer" and url == wake.CALLMEBOT_URL
        assert params["user"] == "@omer" and len(params["text"]) == 256 and params["cc"] == "no"
        assert params["lang"] == "tr-TR-Standard-A" and params["rpt"] == "2"
        ok, note = await wake.callmebot_call(Sess(body=NOT_AUTH), "@omer", "x", "tr")
        assert not ok and note.startswith("izin yok") and "CallMeBot_txtbot" in note
        ok, note = await wake.callmebot_call(Sess(body=GTAG + "<p>Call to @omer queued</p>"), "@omer", "x", "tr")
        assert ok and note == "Call to @omer queued" and "dataLayer" not in note, note
        ok, note = await wake.callmebot_call(Sess(status=500, body="boom"), "@omer", "x", "tr")
        assert not ok and note.startswith("HTTP 500")
        cfg = Config()
        cfg.wake_telegram_user = "omer"
        assert wake.tg_user(cfg) == "@omer"
        cfg.wake_telegram_user = "+905551112233"
        assert wake.tg_user(cfg) == "+905551112233"
        print("✅ CallMeBot) user/text≤256/lang/rpt/cc=no; 'not authorized' → izin yolu; HTTP hatası")
    asyncio.run(run())


# ---------------- takip ve komutlar ----------------

def _cq(data, chat_id, chat_type, uid, kb=None, mid=55):
    msg = {"message_id": mid, "chat": {"id": chat_id, "type": chat_type}}
    if kb:
        msg["reply_markup"] = kb
    return {"update_id": 1, "callback_query": {"id": "cb", "from": {"id": uid, "is_bot": False},
                                               "data": data, "message": msg}}


def _bot(cfg, cli):
    from app.telegram.bot import TelegramBot
    bot = TelegramBot(cfg, None, cli, {})
    calls, sent = [], []

    async def fake_call(method, payload, timeout=30):
        calls.append((method, payload))
        return 200, {"ok": True, "result": True}

    async def fake_send(text, chat_id=None, reply_markup=None):
        sent.append((chat_id, text, reply_markup))
        return True
    bot.call, bot.send = fake_call, fake_send
    return bot, calls, sent


class PosClient(HL):
    """Takip motoru için: tek coinde (HYPE) pozisyon."""

    def __init__(self, szi):
        super().__init__()
        self.szi = szi

    async def clearinghouse(self, addr, dex=""):
        if not self.szi:
            return {"assetPositions": []}
        return {"assetPositions": [{"position": {
            "coin": "HYPE", "szi": str(self.szi), "positionValue": str(abs(self.szi) * 40),
            "liquidationPx": "30", "entryPx": "40", "leverage": {"value": "10"}, "unrealizedPnl": "0"}}]}


def test_tracker_wake_toggle_and_fire():
    async def run():
        cfg = await _fresh()
        cli = PosClient(1000.0)
        live = await tr.live_position(cli, A, "HYPE")
        tid = await tr.start_tracker(cfg, A, "HYPE", "HYPE", live)
        bot, calls, _ = _bot(cfg, cli)
        toasts = lambda: [p.get("text", "") for m, p in calls if m == "answerCallbackQuery"]   # noqa: E731
        # grup üyesi açamaz (sahibin telefonunu çaldırır)
        await bot._handle_update(_cq(f"trk:wake:pos:{tid}", -500, "supergroup", 42, kb=fmt.stop_kb("pos", tid)))
        assert toasts()[-1] == "⏰ Uyandırmayı yalnız sahibi açıp kapatabilir"
        assert not (await trackctl.state("pos", tid))["wake"]
        # sahip açar → tuş "AÇIK — kapat"
        await bot._handle_update(_cq(f"trk:wake:pos:{tid}", 111, "private", 111, kb=fmt.stop_kb("pos", tid)))
        assert (await trackctl.state("pos", tid))["wake"] == 1
        assert toasts()[-1].startswith("⏰ 👣 Takip #") and "arayacağım" in toasts()[-1]
        edit = [p for m, p in calls if m == "editMessageReplyMarkup"][-1]
        assert edit["reply_markup"] == fmt.stop_kb("pos", tid, wake=True)
        assert edit["reply_markup"]["inline_keyboard"][1][0]["text"] == "⏰ Uyandırma AÇIK — kapat"
        # bildirimlerdeki tuş durumu taşır; kapanış → uyandırma olayı
        sent = []

        class B:
            async def send(self, text, chat_id=None, reply_markup=None):
                sent.append((chat_id, text, reply_markup))
                return True
        nt = Notifier(cfg, B())
        cli.szi = 800.0                                                # ✂️ adım (%20)
        await tr.check_trackers(cfg, cli, nt)
        assert sent[-1][2] == fmt.stop_kb("pos", tid, wake=True)
        cli.szi = 0
        await tr.check_trackers(cfg, cli, nt)
        evs = await _events()
        assert len(evs) == 1 and evs[0]["key"] == f"trk:{tid}:closed" and evs[0]["source"] == "takip", evs
        assert "tamamen kapattı" in evs[0]["title"] and evs[0]["speech"].startswith("Dikkat. takip ettiğin HYPE")
        # wake=0 → olay yok; yön değişimi (wake=1) → olay
        cli2 = PosClient(500.0)
        tid2 = await tr.start_tracker(cfg, B_ADDR, "HYPE", "HYPE", await tr.live_position(cli2, B_ADDR, "HYPE"))
        cli2.szi = 0
        await tr.check_trackers(cfg, cli2, nt)
        assert len(await _events()) == 1
        cli3 = PosClient(500.0)
        tid3 = await tr.start_tracker(cfg, C_ADDR, "HYPE", "HYPE", await tr.live_position(cli3, C_ADDR, "HYPE"))
        await trackctl.set_wake(tid3, True)
        cli3.szi = -500.0
        await tr.check_trackers(cfg, cli3, nt)
        assert (await _events())[-1]["key"] == f"trk:{tid3}:flip:short"
        assert tid2 != tid3
        # sahip kapatır
        await bot._handle_update(_cq(f"trk:nowake:pos:{tid3}", 111, "private", 111))
        assert not (await trackctl.state("pos", tid3))["wake"] and toasts()[-1].startswith("🔕")
        print("✅ takip) ⏰ tuşu yalnız sahibe; tuş durumu bildirimlerde; wake=1 kapanış / yön → olay; wake=0 yok")
    asyncio.run(run())


B_ADDR = "0x" + "c" * 40
C_ADDR = "0x" + "d" * 40


def test_commands_and_wake_buttons():
    async def run():
        cfg = await _fresh()
        hl = HL()
        bot, calls, sent = _bot(cfg, hl)

        async def cmd(text, chat=111, uid=111, ctype="private"):
            await bot._handle_update({"message": {"chat": {"id": chat, "type": ctype},
                                                  "from": {"id": uid}, "text": text}})
        await cmd("/alarm")
        assert "🚨 <b>Uyandırma alarmı</b>" in sent[-1][1] and "/alarm SNDK 480" in sent[-1][1]
        await cmd("/alarm SNDK 480")
        a = (await wake.active_alarms())[0]
        assert f"⏰ <b>Alarm #{a['id']} kuruldu</b> — SNDK ≤ 480" in sent[-1][1] and "/alarm_test" in sent[-1][1]
        assert sent[-1][2] == fmt.wake_del_kb([a])
        await cmd("/alarm SNDK abc")
        assert sent[-1][1].startswith("⏰✗ Alarm kurulamadı")
        await cmd("/alarmlar")
        assert "⏰ <b>Aktif alarmlar</b> (1)" in sent[-1][1] and "şimdi 495.20 (%3.1 uzakta)" in sent[-1][1], sent[-1][1]
        # grup üyesi: /alarm sessiz, alarm kurulmaz
        n = len(sent)
        await cmd("/alarm SNDK 470", chat=-500, uid=42, ctype="supergroup")
        assert len(sent) == n and len(await wake.active_alarms()) == 1
        # 🗑 tuşu: yalnız sahip
        await bot._handle_update(_cq(f"wak:del:{a['id']}", -500, "supergroup", 42, kb=fmt.wake_del_kb([a])))
        assert len(await wake.active_alarms()) == 1
        await bot._handle_update(_cq(f"wak:del:{a['id']}", 111, "private", 111, kb=fmt.wake_del_kb([a])))
        assert not await wake.active_alarms()
        edit = [p for m, p in calls if m == "editMessageReplyMarkup"][-1]
        assert edit["reply_markup"] == {"inline_keyboard": []}
        # /alarm_test → deneme olayı; ✅ tuşu ve /uyandim
        await cmd("/alarm_test")
        ev = (await _events())[-1]
        assert ev["source"] == "test" and sent[-1][1].startswith("🧪 Deneme başladı")
        await bot._handle_update(_cq(f"wak:ack:{ev['id']}", -500, "supergroup", 42))
        assert not (await wake.event(ev["id"]))["ack_ts"], "grupta başkası susturamaz"
        await bot._handle_update(_cq(f"wak:ack:{ev['id']}", 111, "private", 111, kb=fmt.wake_ack_kb(ev["id"])))
        ev = await wake.event(ev["id"])
        assert ev["ack_ts"] and ev["ack_by"] == "tuş"
        toasts = [p.get("text", "") for m, p in calls if m == "answerCallbackQuery"]
        assert toasts[-1] == "✅ Günaydın — aramalar durdu"
        edit = [p for m, p in calls if m == "editMessageReplyMarkup"][-1]
        assert edit["reply_markup"]["inline_keyboard"][0][0]["text"].startswith("✅ Uyandın")
        await bot._handle_update(_cq(f"wak:ack:{ev['id']}", 111, "private", 111))
        assert [p.get("text", "") for m, p in calls if m == "answerCallbackQuery"][-1].startswith("✅ Zaten onaylandı")
        e2 = await wake.fire(cfg, "t", "b", "s", "alarm:99")
        await wake._set(e2, msg_id=77, msg_chat="111")
        await cmd("/uyandim")
        assert sent[-1][1] == "✅ Aramalar durdu (1 uyandırma)." and (await wake.event(e2))["ack_by"] == "komut"
        assert [p for m, p in calls if m == "editMessageReplyMarkup"][-1]["message_id"] == 77
        await cmd("/uyandım")
        assert sent[-1][1] == "Açık uyandırma yok."
        assert "/alarm" in fmt.help_text()
        print("✅ komutlar) yardım, kurma + 🗑, hata, liste (şimdiki fiyat, uzaklık); grup üyesi sessiz ve"
              " tuşları işlemez; /alarm_test; ✅ tuşu ve /uyandim aramaları durdurur")
    asyncio.run(run())


# ---------------- kablolama ----------------

def test_wiring():
    async def run():
        from app import diag, health
        src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
        assert '_spawn("wake", lambda: wake.loop(cfg, client, bot, session), notifier)' in src("app/main.py")
        assert "CREATE TABLE IF NOT EXISTS wake_alarms" in dbm.SCHEMA and "key TEXT UNIQUE" in dbm.SCHEMA
        assert "ALTER TABLE trackers ADD COLUMN wake INTEGER DEFAULT 0" in dbm.MIGRATIONS
        cfg = Config()
        assert cfg.wake_enabled and cfg.wake_call_rounds == 3 and cfg.wake_call_gap_sec == 70
        assert cfg.wake_round_gap_sec == 240 and cfg.wake_alarm_hours == 18 and cfg.wake_liq_pct == 2.0
        assert "wake_telegram_user" not in EDITABLE_FIELDS, "kişisel: yalnız env"
        for f in ("wake_enabled", "wake_call_rounds", "wake_call_gap_sec", "wake_round_gap_sec", "wake_tts_lang",
                  "wake_liq_pct", "wake_alarm_hours", "wake_poll_sec"):
            meta = EDITABLE_FIELDS[f]
            assert meta["group"] == "🚨 Uyandırma alarmı" and meta["label"] and len(meta["desc"]) > 30, f
        assert health.limits(cfg)["wake"] == 600 and health.periods(cfg)["wake"] == 5
        assert fmt.TASK_TR["wake"] == "uyandırma alarmı"
        readme, env = src("README.md"), src(".env.example")
        assert "## 🚨 Uyandırma Alarmı: `/alarm`" in readme and "@CallMeBot_txtbot" in readme
        assert "WAKE_TELEGRAM_USER=" in env
        cfg2 = await _fresh()
        cfg2.wake_telegram_user = "+905551112233"
        txt = "\n".join(await diag._subsystems(cfg2))
        assert "uyandırma: arama +90…33 ✓ · aktif alarm 0 · açık uyandırma 0" in txt, \
            [x for x in txt.split("\n") if "uyandırma" in x]
        assert "5551112233" not in txt, "numara /tani'ye düşmez"
        print("✅ kablolama) spawn, tablolar, migration, 8 ayar künyeli (kullanıcı yalnız env), sağlık,"
              " TASK_TR, README, .env; /tani satırı numarayı maskeler")
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
