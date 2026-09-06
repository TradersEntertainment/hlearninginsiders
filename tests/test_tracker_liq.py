"""/takip_N pozisyon takibi — kripto liq mesajlarından takip, liq fiyatı kayması,
boyut adımı, likidasyon teyidi, kanal içinden komut.

Pinlenenler:
  • /hype cevabı ve kademe mesajında her satırın yanında /takip_N (teklif satırla hizalı)
  • kanaldan basılan /takip_N → takip o kanala bağlanır; haberler oraya gider
  • liq fiyatı son bildirilene göre ≥ track_liq_step_pct (%1) kayınca 🛡 mesaj,
    liq_px YALNIZ gönderim başarılıysa ilerler; küçük kayma sessiz
  • boyut toplamın %10'u kadar değişince ✂️/➕ adım mesajı, liq satırıyla
  • poz yok olunca fill'de likidasyon varsa 💀 LİKİDE OLDU, end_note 'likide oldu';
    likidasyon yoksa 🚨 TAMAMEN KAPATTI, end_note 'kapandı'
  • track_liq_step_pct=0 → liq kayması bildirilmez
  • /takip 0xADRES HYPE (kripto) elle çalışır — hisse ÖNCE, sonra ana dex kripto
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db as dbm
from app.config import Config
from app.hl import universe as uni
from app.notify import Notifier
from app.radar import tracker as tr
from app.telegram import format as fmt

A = "0x" + "a" * 40
B = "0x" + "b" * 40
MARK = 40.0


class Client:
    """Sahte HL: `pos[addr]` = (szi, liq_px) ya da None (kapanmış);
    `fills[addr]` = userFillsByTime yanıtı."""

    def __init__(self, pos, fills=None):
        self.pos, self.fills = dict(pos), fills or {}
        self.calls, self.fill_calls = [], []

    async def clearinghouse(self, addr, dex=""):
        self.calls.append((addr, dex))
        p = self.pos.get(addr)
        if not p:
            return {"assetPositions": []}
        szi, liq = p
        return {"assetPositions": [{"position": {
            "coin": "HYPE", "szi": str(szi), "positionValue": str(abs(szi) * MARK),
            "liquidationPx": str(liq), "entryPx": str(MARK), "leverage": {"value": "10"},
            "unrealizedPnl": "0"}}]}

    async def user_fills_by_time(self, addr, start_ms, end_ms=None):
        self.fill_calls.append(addr)
        return self.fills.get(addr, [])


class Bot:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, text, chat_id=None):
        if self.ok:
            self.sent.append((chat_id, text))
        return self.ok


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id = "111"
    cfg.crypto_chat_id = "-100"
    cfg.notify_track = True
    cfg.track_step_pct = 10.0
    cfg.track_liq_step_pct = 1.0
    cfg.track_expire_days = 14
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0      # sessiz saat kapalı
    return cfg


async def _fresh_db():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "tr.db"))
    async with dbm.db() as c:
        await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:SNDK','SNDK')")
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HYPE": {"m": MARK, "oi": 1, "f": 0, "v": 1, "p": None}},
                                       "ts": dbm.now()})


async def _tracker(tid):
    async with dbm.db() as c:
        cur = await c.execute("SELECT * FROM trackers WHERE id=?", (tid,))
        return dict(await cur.fetchone())


# ------------------------------------------------ 1) mesajlarda /takip_N
def test_offers_in_messages():
    async def run():
        await _fresh_db()
        rows = [{"address": A, "side": "short", "notional": 2_000_000.0, "liq_px": 41.0,
                 "dist": 2.5, "mark": MARK, "leverage": 10.0, "ts": dbm.now()},
                {"address": B, "side": "long", "notional": 800_000.0, "liq_px": 39.2,
                 "dist": 2.0, "mark": MARK, "leverage": 5.0, "ts": dbm.now()}]
        offers = await tr.offer_positions("HYPE", "HYPE", rows)
        assert len(offers) == 2 and offers[0] < offers[1], offers
        async with dbm.db() as c:
            cur = await c.execute("SELECT * FROM track_offers ORDER BY id")
            got = [dict(r) for r in await cur.fetchall()]
        assert [g["address"] for g in got] == [A, B] and got[0]["coin"] == "HYPE"
        assert got[0]["side"] == "short" and got[1]["notional"] == 800_000.0

        s = {"coin": "HYPE", "mark": MARK, "rows": rows, "n_all": 2, "n_big": 2,
             "min_usd": 500_000, "age": dbm.now()}
        snap = fmt.crypto_liq_snapshot(s, offers=offers)
        lines = snap.split("\n")
        assert f"/takip_{offers[0]}" in lines[1] and A[:6] in lines[1], lines[1]
        assert f"/takip_{offers[1]}" in lines[2] and B[:6] in lines[2], lines[2]
        assert "/takip_" not in fmt.crypto_liq_snapshot(s), "teklif yoksa komut yok"

        alert = fmt.crypto_liq_alert("HYPE", MARK, rows, [], 2.5, 1, offers=offers)
        al = alert.split("\n")
        assert f"/takip_{offers[0]}" in al[1] and f"/takip_{offers[1]}" in al[2], al[:3]
        assert "/takip_" not in fmt.crypto_liq_alert("HYPE", MARK, rows, [], 2.5, 1)
        assert "/takip_N" in fmt.help_text(), "yardımda komut anlatılır"
        print("✅ teklif) /hype ve kademe mesajında satır başına /takip_N, satırla hizalı")
    asyncio.run(run())


# ------------------------------------------------ 2) liq kayması / adım / likidasyon
def test_liq_move_step_and_liquidation():
    async def run():
        await _fresh_db()
        cfg = _cfg()
        cli = Client({A: (-50_000.0, 41.0)})
        live = await tr.live_position(cli, A, "HYPE")
        assert live["side"] == "short" and live["liq_px"] == 41.0 and live["leverage"] == 10.0
        tid = await tr.start_tracker(cfg, A, "HYPE", "HYPE", live, chat_id="-100")
        t = await _tracker(tid)
        assert t["liq_px"] == 41.0 and t["leverage"] == 10.0 and t["chat_id"] == "-100"
        started = fmt.track_started(tid, "HYPE", A, live, cfg)
        assert f"liq <b>{fmt.px(41.0)}</b>" in started and "10x" in started and "%1" in started, started
        assert "likidasyon teyidiyle" in started

        # a) küçük kayma (%0,5) sessiz; liq_px değişmez
        bot = Bot()
        notifier = Notifier(cfg, bot)
        cli.pos[A] = (-50_000.0, 41.2)
        await tr.check_trackers(cfg, cli, notifier)
        assert bot.sent == [], bot.sent
        assert (await _tracker(tid))["liq_px"] == 41.0

        # b) %1,2 uzaklaştı (short: liq yukarı) → 🛡 mesaj KANALA; liq_px ilerler
        cli.pos[A] = (-50_000.0, 41.5)
        await tr.check_trackers(cfg, cli, notifier)
        assert len(bot.sent) == 1 and bot.sent[0][0] == "-100", bot.sent
        txt = bot.sent[0][1]
        assert "LIQ FİYATI KAYDI" in txt and f"{fmt.px(41.0)} → <b>{fmt.px(41.5)}</b>" in txt and "+1.22%" in txt, txt
        assert "uzaklaştı" in txt and f"#{tid}" in txt
        assert (await _tracker(tid))["liq_px"] == 41.5
        await tr.check_trackers(cfg, cli, notifier)
        assert len(bot.sent) == 1, "aynı liq tekrar bildirilmez"

        # c) gönderim düşerse liq_px İLERLEMEZ (sonraki turda yeniden denenir)
        bad = Notifier(cfg, Bot(ok=False))
        cli.pos[A] = (-50_000.0, 40.8)                      # yaklaştı, -%1,7
        await tr.check_trackers(cfg, cli, bad)
        assert (await _tracker(tid))["liq_px"] == 41.5
        await tr.check_trackers(cfg, cli, notifier)
        assert len(bot.sent) == 2 and "yaklaştı" in bot.sent[1][1] and "-1.69%" in bot.sent[1][1]
        assert (await _tracker(tid))["liq_px"] == 40.8

        # d) boyut %20 küçüldü + liq kaydı → ADIM mesajı (liq satırıyla), tek mesaj
        cli.pos[A] = (-40_000.0, 42.0)
        await tr.check_trackers(cfg, cli, notifier)
        assert len(bot.sent) == 3 and bot.sent[2][0] == "-100", bot.sent[2:]
        step = bot.sent[2][1]
        assert "BALİNA KAPATIYOR" in step and "%20" in step and f"liq {fmt.px(40.8)} → <b>{fmt.px(42.0)}</b>" in step, step
        t = await _tracker(tid)
        assert t["last_szi"] == 40_000.0 and t["liq_px"] == 42.0

        # e) poz yok + fill'de likidasyon → 💀 LİKİDE OLDU, takip biter
        cli.pos[A] = None
        cli.fills[A] = [{"coin": "HYPE", "dir": "Liquidated Isolated Short", "px": "42.01",
                         "liquidation": {"liquidatedUser": A}, "time": dbm.now() * 1000}]
        await tr.check_trackers(cfg, cli, notifier)
        assert len(bot.sent) == 4 and bot.sent[3][0] == "-100"
        closed = bot.sent[3][1]
        assert "BALİNA LİKİDE OLDU" in closed and fmt.px(42.01) in closed and f"#{tid}" in closed, closed
        t = await _tracker(tid)
        assert t["active"] == 0 and t["end_note"] == "likide oldu" and cli.fill_calls == [A]
        print("✅ takip) liq %1 kayması kanala, gönderim düşerse ilerlemez; adım mesajı liq'li; 💀 teyit")
    asyncio.run(run())


# ------------------------------------------------ 3) kapanış (likidasyon yok) + kapalı ayar
def test_close_and_disabled_liq_step():
    async def run():
        await _fresh_db()
        cfg = _cfg()
        cfg.track_liq_step_pct = 0                           # kapalı
        cli = Client({B: (10_000.0, 39.0)})
        live = await tr.live_position(cli, B, "HYPE")
        tid = await tr.start_tracker(cfg, B, "HYPE", "HYPE", live)   # ana sohbet
        assert (await _tracker(tid))["chat_id"] == ""
        assert "🛡" not in fmt.track_started(tid, "HYPE", B, live, cfg), "kapalıysa satır yok"
        bot = Bot()
        notifier = Notifier(cfg, bot)
        cli.pos[B] = (10_000.0, 36.0)                        # -%7,7 kaydı ama ayar kapalı
        await tr.check_trackers(cfg, cli, notifier)
        assert bot.sent == [], bot.sent
        # kapandı: fill var, likidasyon yok → 🚨 TAMAMEN KAPATTI, ana sohbete (chat_id None)
        cli.pos[B] = None
        cli.fills[B] = [{"coin": "HYPE", "dir": "Close Long", "px": "40.5", "time": dbm.now() * 1000}]
        await tr.check_trackers(cfg, cli, notifier)
        assert len(bot.sent) == 1 and bot.sent[0][0] is None, bot.sent
        assert "TAMAMEN KAPATTI" in bot.sent[0][1] and "doğrulanamadı" not in bot.sent[0][1]
        t = await _tracker(tid)
        assert t["active"] == 0 and t["end_note"] == "kapandı"
        print("✅ kapanış) ayar 0 → liq kayması sessiz; fill var likidasyon yok → 🚨 kapattı")
    asyncio.run(run())


# ------------------------------------------------ 4) kanaldan /takip_N ve /takip 0x… HYPE
def test_bot_channel_commands():
    async def run():
        await _fresh_db()
        from app.telegram.bot import TelegramBot
        cfg = _cfg()
        cli = Client({A: (-50_000.0, 41.0)})
        bot = TelegramBot(cfg, None, cli, {})
        sent = []

        async def fake_send(text, chat_id=None):
            sent.append((chat_id, text))
            return True
        bot.send = fake_send
        offers = await tr.offer_positions("HYPE", "HYPE", [
            {"address": A, "side": "short", "notional": 2_000_000.0}])
        await bot._handle_update({"channel_post": {"chat": {"id": -100}, "text": f"/takip_{offers[0]}"}})
        assert len(sent) == 1 and sent[0][0] == "-100" and "TAKİP BAŞLADI" in sent[0][1], sent
        async with dbm.db() as c:
            cur = await c.execute("SELECT * FROM trackers WHERE active=1")
            rows = [dict(r) for r in await cur.fetchall()]
        assert len(rows) == 1 and rows[0]["chat_id"] == "-100" and rows[0]["liq_px"] == 41.0
        async with dbm.db() as c:
            cur = await c.execute("SELECT used FROM track_offers WHERE id=?", (offers[0],))
            assert (await cur.fetchone())["used"] == 1
        # aynı balina ikinci kez: zaten takipte
        await bot._handle_update({"channel_post": {"chat": {"id": -100}, "text": f"/takip_{offers[0]}"}})
        assert "zaten takipte" in sent[-1][1]
        # yabancı sohbetten komut → sessiz (yalnız /id, /start)
        await bot._handle_update({"message": {"chat": {"id": 999}, "text": "/takipler"}})
        assert len(sent) == 2, sent[-1]
        # ana sohbetten /takip 0x… HYPE → kripto çözülür, chat_id boş (ana sohbet)
        await bot._handle_update({"message": {"chat": {"id": 111}, "text": f"/takip {B} hype"}})
        assert "pozisyonu yok" in sent[-1][1], sent[-1]
        cli.pos[B] = (10_000.0, 39.0)
        await bot._handle_update({"message": {"chat": {"id": 111}, "text": f"/takip {B} HYPE"}})
        assert "TAKİP BAŞLADI" in sent[-1][1] and sent[-1][0] == "111", sent[-1]
        async with dbm.db() as c:
            cur = await c.execute("SELECT * FROM trackers WHERE address=?", (B,))
            t = dict(await cur.fetchone())
        assert t["chat_id"] == "" and t["coin"] == "HYPE" and t["symbol"] == "HYPE"
        await bot._handle_update({"message": {"chat": {"id": 111}, "text": f"/takip {B} NOPE"}})
        assert "HL evreninde yok" in sent[-1][1]
        # kanaldan /takipler ve /birak_N
        await bot._handle_update({"channel_post": {"chat": {"id": -100}, "text": "/takipler"}})
        assert "HYPE" in sent[-1][1] and sent[-1][0] == "-100"
        await bot._handle_update({"channel_post": {"chat": {"id": -100}, "text": f"/birak_{rows[0]['id']}"}})
        async with dbm.db() as c:
            cur = await c.execute("SELECT active FROM trackers WHERE id=?", (rows[0]["id"],))
            assert (await cur.fetchone())["active"] == 0
        print("✅ bot) kanaldan /takip_N → takip kanala bağlı; /takip 0x… HYPE kripto çözer; /birak_N")
    asyncio.run(run())
