"""⏳ /twap — en büyük canlı (süren) HL TWAP'lar, her grupta.

Kullanıcı (13.10): "grupların herhangi birine fark etmeksizin /twap yazdığımda en büyük canlı TWAP'ları sıralasın".

Pinlenenler:
  • kaynak: canlı sayaç (REG — emri sorulmuş diziler) + ilk dilim yolunun bildirdiği emirler (twapfast.F.open);
    aynı emir iki kaynakta tek satır (en taze sorgu, 📣 birleşir)
  • süren = HL 'activated' + planlanan bitiş gelmemiş + emir diziyle örtüşüyor + son 5 dk'da dilim görüldü;
    bitmiş / iptal / bitişi geçmiş / susmuş / dizi öncesi eski emir listede YOK
  • sıra: planlanan büyüklük (büyükten küçüğe), en çok 15 satır ("en büyük 15 / 20")
  • mesaj ölçülü: plan, 24s hacme oranı, doldu %, kalan süre, ⚡ (≤ 1 sa), 📣 (kanala bildirildi), adres;
    sorgu 5 dk'dan eskiyse yaşı yazılır; tahmin dili yok
  • yönlendirme: sahip her sohbette /twap → liste (/twap durum → eski özet; /twap COIN, /twap 0x… teşhis
    aynen); botun kendi kanal/grubunda sahip olmayan / kanal gönderisi /twap → liste (coin-liq araması değil);
    yabancı grup → sessiz
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twaplist.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.radar import twapfast as tf  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A, B, C, D = ("0x" + ch * 40 for ch in "abcd")
PX = 10.0


class Col:
    def __init__(self, by_addr):
        self.by_addr, self.calls, self.twap_sample = by_addr, [], None

    async def fetch_twap_history(self, addr, timeout=6.0):
        self.calls.append(addr)
        return self.by_addr.get(addr, [])


def order(coin, user, side="B", status="activated", start_ago=1200, minutes=120, ntl=2_000_000, tid=1):
    t0 = dbm.now() - start_ago
    return {"time": t0, "twapId": tid, "status": {"status": status},
            "state": {"coin": coin, "user": user, "side": side, "sz": str(ntl / PX), "executedSz": str(ntl / PX / 4),
                      "executedNtl": str(ntl / 4), "minutes": minutes, "timestamp": t0 * 1000,
                      "reduceOnly": False, "randomize": False}}


def feed(coin, addr, side, n=20, ntl=15_000, gap=30, t_end=None, native=True):
    t_end = t_end or dbm.now() - 5
    for i in range(n):
        tl.observe(coin, addr, side, PX, ntl / PX, ntl, int(t_end - (n - 1 - i) * gap), 1, native)


def _cfg():
    cfg = Config()
    cfg.telegram_chat_id, cfg.crypto_chat_id = "111", "-100"
    cfg.twap_mirror_chat_id = cfg.hidden_twap_chat_id = ""
    cfg.twap_live_enabled = True
    cfg.twap_alert_min_slices, cfg.twap_lookup_min_usd, cfg.twap_lookup_cooldown = 10, 50_000, 600
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    cfg.twap_follow_enabled = False
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "tl.db"))
    tl.REG.clear()
    tl._lookup_cache.clear()
    tl.VOL_CACHE.update(ts=0, v={})
    tf.F.clear()
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {c: {"m": PX, "oi": 1, "f": 0, "v": v, "p": None}
                                             for c, v in (("PUMP", 100e6), ("INJ", 50e6), ("SOL", 900e6),
                                                          ("WIF", 30e6))},
                                       "ts": dbm.now()})


class Bot:
    def __init__(self):
        self.sent = []

    async def send(self, text, chat_id=None, **k):
        self.sent.append((chat_id, text))
        return True


def test_live_list_sources_and_filters():
    async def run():
        from app.notify import Notifier
        await _fresh()
        cfg = _cfg()
        feed("PUMP", A, "buy")                                     # $2M aktif (hacmin %2'si: kanala gitmez) → listede
        feed("INJ", B, "sell")                                     # $5M aktif (⚡ 30 dk) → en üstte
        feed("SOL", C, "buy")                                      # bitmiş emir → yok
        feed("WIF", D, "buy", t_end=dbm.now() - 900)               # emir aktif ama dizi 15 dk susmuş → yok
        col = Col({A: [order("PUMP", A, ntl=2_000_000)],
                   B: [order("INJ", B, side="A", minutes=30, start_ago=600, ntl=5_000_000)],
                   C: [order("SOL", C, status="finished")],
                   D: [order("WIF", D, start_ago=3000, minutes=600)]})
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None, col)
        rows = tl.live_twaps()
        assert [(r["coin"], r["addr"]) for r in rows] == [("INJ", B), ("PUMP", A)], rows
        inj = rows[0]
        assert inj["side"] == "sell" and abs(inj["planned_usd"] - 5_000_000) < 1 and abs(inj["filled_pct"] - 25) < 1e-6
        assert 0 < inj["left_sec"] <= 1200 and abs(inj["vol_pct"] - 10) < 1e-6 and inj["minutes"] == 30
        assert inj["alerted"] and not rows[1]["alerted"] and abs(rows[1]["vol_pct"] - 2) < 1e-6, "📣 = kanala gitti"
        # bitişi geçmiş aktif emir (HL henüz kapatmamış) → yok; dizi öncesi bitmiş eski emir → yok
        r = tl.REG.runs[("PUMP", A, "buy")]
        keep = r.order
        r.order = dict(keep, started_ts=dbm.now() - 9000, minutes=60)
        assert [x["coin"] for x in tl.live_twaps()] == ["INJ"]
        r.order = keep
        # hızlı yolun bildirdiği emir: REG'de olmasa da listede; aynı emir iki kaynakta → tek satır, 📣
        o = tl.parse_twap_orders([order("WIF", D, start_ago=120, minutes=60, ntl=9_000_000, tid=7)], "WIF", "buy",
                                 PX, dbm.now())[0]
        tf.F.open[("WIF", D, "buy")] = {"coin": "WIF", "addr": D, "side": "buy", "started": o["started_ts"],
                                        "end_at": o["started_ts"] + 3600, "order": o, "alert_ts": dbm.now() - 60,
                                        "first_ts": dbm.now() - 120, "last_ts": dbm.now() - 10,
                                        "last_slice": dbm.now() - 10, "n": 4, "total": 300_000.0, "sz": 0.0,
                                        "px_first": PX, "px_last": PX, "lookup_ts": dbm.now() - 60, "ended": 0}
        o2 = tl.parse_twap_orders([order("PUMP", A, ntl=2_000_000)], "PUMP", "buy", PX, dbm.now())[0]
        tf.F.open[("PUMP", A, "buy")] = {"coin": "PUMP", "addr": A, "side": "buy", "started": o2["started_ts"],
                                         "end_at": o2["started_ts"] + 7200, "order": o2, "alert_ts": dbm.now() - 900,
                                         "first_ts": dbm.now() - 1200, "last_ts": dbm.now() - 5,
                                         "last_slice": dbm.now() - 5, "n": 30, "total": 400_000.0, "sz": 0.0,
                                         "px_first": PX, "px_last": PX, "lookup_ts": dbm.now() - 2000, "ended": 0}
        rows = tl.live_twaps()
        assert [(x["coin"], x["alerted"]) for x in rows] == [("WIF", True), ("INJ", True), ("PUMP", True)], rows
        assert rows[2]["q_ts"] > dbm.now() - 100, "aynı emir: en taze sorgu (REG) kalır, 📣 hızlı yoldan birleşir"
        tf.F.open[("WIF", D, "buy")]["ended"] = dbm.now()
        assert [x["coin"] for x in tl.live_twaps()] == ["INJ", "PUMP"]
        print("✅ kaynak) REG + hızlı yol, tekil; bitmiş / bitişi geçmiş / susmuş / eski emir yok; büyükten küçüğe")
    asyncio.run(run())


def test_after_restart_db_and_ask():
    """13.10 20:39: deploy'dan hemen sonra /twap boş geldi (bellek sıfırlanmıştı). Artık kayıt + anlık sorgu."""
    async def run():
        from app.notify import Notifier
        await _fresh()
        cfg = _cfg()
        feed("INJ", B, "sell")
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None,
                          Col({B: [order("INJ", B, side="A", minutes=300, start_ago=1200, ntl=5_000_000)]}))
        async with dbm.db() as c:
            n = (await (await c.execute("SELECT COUNT(*) n FROM twap_runs WHERE order_status='activated'"))
                 .fetchone())["n"]
        assert n == 1, "emir kayıtlı"
        # --- restart: bellek boş; yeni HL alt emri dolumları geliyor (henüz 5 dk / 10 dilim yok) ---
        tl.REG.clear()
        tl._lookup_cache.clear()
        tl.VOL_CACHE.update(ts=0, v={})
        tf.F.clear()
        tl._LIVE_Q.clear()
        feed("INJ", B, "sell", n=3, t_end=dbm.now() - 2)          # kayıtlı emrin adresi dilim atıyor
        feed("PUMP", A, "buy", n=2, ntl=40_000, t_end=dbm.now() - 3)   # emri bilinmeyen, görülen $80K
        feed("WIF", C, "buy", n=2, ntl=1_000, t_end=dbm.now() - 3)     # emri bilinmeyen, küçük
        feed("SOL", D, "buy", n=2, ntl=9_000, native=False)             # HL alt emri değil → sorulmaz
        col = Col({A: [order("PUMP", A, ntl=7_000_000, start_ago=400, minutes=600)],
                   C: [order("WIF", C, ntl=300_000, start_ago=100, minutes=60)]})
        rows, info = await tl.live_twaps_full(col)
        assert [(r["coin"], r["src"]) for r in rows] == [("PUMP", "ask"), ("INJ", "db"), ("WIF", "ask")], rows
        assert sorted(col.calls) == sorted([A, C]) and info == {"asked": 2, "failed": 0, "skipped": 0}, info
        inj = rows[1]
        assert inj["alerted"] and abs(inj["planned_usd"] - 5e6) < 1 and abs(inj["filled_pct"] - 25) < 1e-6
        assert abs(rows[0]["vol_pct"] - 7) < 1e-6, "hacim kv'den (VOL_CACHE boşken de)"
        # 2 dk içinde yeniden /twap: yeniden sorulmaz
        rows2, info2 = await tl.live_twaps_full(col)
        assert len(col.calls) == 2 and info2["asked"] == 0 and [r["coin"] for r in rows2] == ["PUMP", "INJ", "WIF"]
        # sorgu tavanı: en büyük görülen önce sorulur, kalanlar "sorulamadı" sayılır
        tl._LIVE_Q.clear()
        col3 = Col({A: [order("PUMP", A, ntl=7_000_000, start_ago=400, minutes=600)]})
        rows3, info3 = await tl.live_twaps_full(col3, max_lookups=1)
        assert col3.calls == [A] and info3 == {"asked": 1, "failed": 0, "skipped": 1}, info3
        t = fmt.live_twaps(rows3, info=info3)
        assert "⚠ HL TWAP dilimi görülen 1 adresin emri bu sefer sorulamadı" in t, t
        # sorgu düştü / collector yok
        tl._LIVE_Q.clear()

        class Dead(Col):
            async def fetch_twap_history(self, addr, timeout=6.0):
                self.calls.append(addr)
                return None
        rows4, info4 = await tl.live_twaps_full(Dead({}))
        assert info4["failed"] == 2 and [r["coin"] for r in rows4] == ["INJ"]
        rows5, info5 = await tl.live_twaps_full(None)
        assert info5["skipped"] == 2 and [r["coin"] for r in rows5] == ["INJ"]
        # kayıtlı emir ama adres son 5 dk'da HL alt emri dilimi atmıyor → listede yok (iptal olmuş olabilir)
        tl.REG.clear()
        feed("INJ", B, "sell", n=3, t_end=dbm.now() - 900)
        rows6, _ = await tl.live_twaps_full(None)
        assert rows6 == []
        print("✅ restart) kayıtlı emir + canlı HL dilimi → listede; emri bilinmeyen en büyükler anlık sorulur"
              " (önbellek, tavan, düşen sorgu dürüst); dilim yoksa kayıt yetmez")
    asyncio.run(run())


def test_message():
    now = dbm.now()
    rows = [{"coin": "INJ", "addr": B, "side": "sell", "planned_usd": 5e6, "executed_usd": 1.25e6, "filled_pct": 25.0,
             "minutes": 30, "started_ts": now - 600, "left_sec": 1200, "q_ts": now - 30, "last_ts": now - 5,
             "alerted": True, "n": 20, "seen_usd": 3e5, "vol_pct": 10.0, "spot": False},
            {"coin": "@107", "addr": A, "side": "buy", "planned_usd": 2e6, "executed_usd": None, "filled_pct": None,
             "minutes": 600, "started_ts": now - 3000, "left_sec": 33000, "q_ts": now - 900, "last_ts": now - 5,
             "alerted": False, "n": 20, "seen_usd": 3e5, "vol_pct": None, "spot": True}]
    t = fmt.live_twaps(rows)
    assert t.startswith("⏳ <b>Canlı TWAP'lar</b> · en büyük 2 · planlanan büyüklüğe göre"), t
    l1, l2 = t.split("\n")[1:3]
    assert l1.startswith("1. <b>INJ</b> 🔴 SATIŞ · plan <b>$5.0M</b> (24s hacmin %10.0'i) · doldu %25 · 20 dk kaldı"
                         " · ⚡ · 📣 · <a href=\"https://hypurrscan.io/address/"), l1
    assert "sorgu" not in l1
    assert l2.startswith("2. 🪙 <b>") and "doldu" not in l2 and "⚡" not in l2 and "📣" not in l2, l2
    assert "9 s 10 dk kaldı" in l2 and "(sorgu 15 dk önce)" in l2, l2
    assert "/twap durum" in t and "/twap durum" not in fmt.live_twaps(rows, owner=False)
    for bad in ("tahmin", "bitecek", "olacak", "bu hızla", "?key="):
        assert bad not in t, bad
    many = [dict(rows[0], coin=f"C{i}", planned_usd=1e6 * (30 - i)) for i in range(20)]
    tm = fmt.live_twaps(many)
    assert "en büyük 15 / 20" in tm and tm.count("\n1") >= 1 and "16. " not in tm and len(tm) < 4096
    e = fmt.live_twaps([])
    assert "Şu an radarın gördüğü süren HL TWAP emri yok." in e
    assert "/twap — ⏳ en büyük canlı" in fmt.help_text()
    print("✅ mesaj) plan, hacme oran, doldu, kalan, ⚡, 📣, eski sorgu yaşı; 15 satır tavanı; boş liste; yardım")


def test_routing_everywhere():
    async def run():
        await _fresh()
        cfg = _cfg()
        cfg.hidden_twap_chat_id, cfg.twap_mirror_chat_id = "-555", "-777"
        from app.telegram.bot import TelegramBot
        bot = TelegramBot(cfg, None, None, {})
        sent = []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append((str(chat_id), text))
            return True
        bot.send = fake_send
        bot.collector = Col({})

        async def msg(text, chat, uid, ctype="supergroup"):
            await bot._handle_update({"message": {"chat": {"id": chat, "type": ctype}, "from": {"id": uid},
                                                  "text": text}})
        feed("PUMP", A, "buy")
        tl.REG.runs[("PUMP", A, "buy")].order = tl.parse_twap_orders([order("PUMP", A)], "PUMP", "buy", PX,
                                                                      dbm.now())[0]
        await msg("/twap", 111, 111, "private")                      # sahip DM
        assert sent[-1][0] == "111" and sent[-1][1].startswith("⏳ <b>Canlı TWAP'lar</b>") and "PUMP" in sent[-1][1]
        for chat in (-100, -555, -777):                              # kripto kanalı, gizli TWAP grubu, ayna
            await msg("/twap", chat, 111)                            # sahip grupta
            assert sent[-1][0] == str(chat) and sent[-1][1].startswith("⏳ <b>Canlı TWAP'lar</b>"), sent[-1]
            await msg("/twap", chat, 42)                             # grubun başka üyesi
            assert sent[-1][0] == str(chat) and sent[-1][1].startswith("⏳ <b>Canlı TWAP'lar</b>"), sent[-1]
            assert "/twap durum" not in sent[-1][1]
        await bot._handle_update({"channel_post": {"chat": {"id": -100, "type": "channel"}, "text": "/twap"}})
        assert sent[-1][0] == "-100" and sent[-1][1].startswith("⏳ <b>Canlı TWAP'lar</b>"), "kanal gönderisi"
        await msg("/twaplar@HLRadarBot", -555, 42)
        assert sent[-1][1].startswith("⏳ <b>Canlı TWAP'lar</b>")
        n = len(sent)
        await msg("/twap", -999, 42)                                 # yabancı grup: sessiz
        assert len(sent) == n
        await msg("/twap durum", 111, 111, "private")
        assert sent[-1][1].startswith("📡 Canlı TWAP") and "/twap → en büyük canlı TWAP'lar" not in sent[-1][1] \
            or "Kullanım: /twap → en büyük canlı TWAP'lar" in sent[-1][1], sent[-1]
        await msg("/twap PUMP", 111, 111, "private")
        assert sent[-1][1].startswith("📡 <b>PUMP</b>"), sent[-1]
        print("✅ yönlendirme) sahip DM + her kendi grubunda (sahip / üye / kanal gönderisi) liste; /twaplar;"
              " yabancı grup sessiz; /twap durum özet; /twap COIN teşhis")
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
