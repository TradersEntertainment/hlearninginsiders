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
    tl._LIVE_Q.clear()
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
        inj_hist = [order("INJ", B, side="A", minutes=300, start_ago=1200, ntl=5_000_000)]
        col = Col({A: [order("PUMP", A, ntl=7_000_000, start_ago=400, minutes=600)],
                   B: inj_hist, C: [order("WIF", C, ntl=300_000, start_ago=100, minutes=60)]})
        rows, info = await tl.live_twaps_full(col)
        assert [(r["coin"], r["src"]) for r in rows] == [("PUMP", "ask"), ("INJ", "ask"), ("WIF", "ask")], rows
        assert rows[1]["q_ts"] >= dbm.now() - 5, "kayıt satırı taze geçmişle doğrulandı (doğrulanmış kopya kalır)"
        assert sorted(col.calls) == sorted([A, B, C]) and info == {"asked": 3, "failed": 0, "skipped": 0}, info
        inj = rows[1]
        assert inj["alerted"] and abs(inj["planned_usd"] - 5e6) < 1 and abs(inj["filled_pct"] - 25) < 1e-6
        assert abs(rows[0]["vol_pct"] - 7) < 1e-6, "hacim kv'den (VOL_CACHE boşken de)"
        # 2 dk içinde yeniden /twap: yeniden sorulmaz
        rows2, info2 = await tl.live_twaps_full(col)
        assert len(col.calls) == 3 and info2["asked"] == 0 and [r["coin"] for r in rows2] == ["PUMP", "INJ", "WIF"]
        # kayıtta 'activated' kalmış ama taze geçmişte iptal (terminated) → listede YOK
        tl._LIVE_Q.clear()
        col_x = Col({A: [order("PUMP", A, ntl=7_000_000, start_ago=400, minutes=600)],
                     B: [order("INJ", B, side="A", status="terminated", minutes=300, start_ago=1200, ntl=5_000_000)],
                     C: [order("WIF", C, ntl=300_000, start_ago=100, minutes=60)]})
        rows_x, _ = await tl.live_twaps_full(col_x)
        assert [r["coin"] for r in rows_x] == ["PUMP", "WIF"], rows_x
        # sorgu tavanı: en büyük görülen önce sorulur, kalanlar "sorulamadı" sayılır
        tl._LIVE_Q.clear()
        col3 = Col({A: [order("PUMP", A, ntl=7_000_000, start_ago=400, minutes=600)]})
        rows3, info3 = await tl.live_twaps_full(col3, max_lookups=1)
        assert col3.calls == [A] and info3 == {"asked": 1, "failed": 0, "skipped": 2}, info3
        t = fmt.live_twaps(rows3, info=info3)
        assert "⚠ HL TWAP dilimi görülen 2 adresin emri bu sefer sorulamadı" in t, t
        # sorgu düştü / collector yok
        tl._LIVE_Q.clear()

        class Dead(Col):
            async def fetch_twap_history(self, addr, timeout=6.0):
                self.calls.append(addr)
                return None
        rows4, info4 = await tl.live_twaps_full(Dead({}))
        assert info4["failed"] == 3 and [r["coin"] for r in rows4] == ["INJ"], "sorgu düşerse kayıt kalır"
        rows5, info5 = await tl.live_twaps_full(None)
        assert info5["skipped"] == 3 and [r["coin"] for r in rows5] == ["INJ"]
        # kayıtlı emir ama adres son 5 dk'da HL alt emri dilimi atmıyor → listede yok (iptal olmuş olabilir)
        tl.REG.clear()
        feed("INJ", B, "sell", n=3, t_end=dbm.now() - 900)
        rows6, _ = await tl.live_twaps_full(None)
        assert rows6 == []
        print("✅ restart) kayıtlı emir + canlı HL dilimi → listede; emri bilinmeyen en büyükler anlık sorulur"
              " (önbellek, tavan, düşen sorgu dürüst); dilim yoksa kayıt yetmez")
    asyncio.run(run())


def test_review_ghost_bell_query_age():
    """İnceleme (13.10): iptal edilmiş emir 'süren' görünmesin; 📣 yalnız GERÇEKTEN gönderilen emirde; sorgu anı."""
    async def run():
        from app.notify import Notifier
        await _fresh()
        cfg = _cfg()
        # 1) hızlı yolun bildirdiği X iptal; aynı adres + coin + yönde Y dilim atıyor → X F.open'da 'activated' kalmış
        feed("PUMP", A, "buy")
        x = tl.parse_twap_orders([order("PUMP", A, ntl=5_000_000, start_ago=3600, minutes=360, tid=1)], "PUMP", "buy",
                                 PX, dbm.now())[0]
        tf._adopt(("PUMP", A, "buy"), {"t0": dbm.now() - 3600, "ts": dbm.now() - 5, "px": PX}, x, dbm.now() - 3500,
                  dbm.now() - 3500)
        x["q_ts"] = dbm.now() - 3500
        y_hist = [order("PUMP", A, status="terminated", ntl=5_000_000, start_ago=3600, minutes=360, tid=1),
                  order("PUMP", A, ntl=3_000_000, start_ago=600, minutes=120, tid=2)]
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None, Col({A: y_hist}))   # REG sorgusu önbelleği tazeler
        rows, _ = await tl.live_twaps_full(None)
        assert [(r["coin"], round(r["planned_usd"])) for r in rows] == [("PUMP", 3_000_000)], rows
        # lifecycle da uzun emri dilim gelse 5 dk'da bir sorar → iptali görür (⛔ / ended)
        tl._lookup_cache.clear()
        rec = tf.F.open[("PUMP", A, "buy")]
        rec["lookup_ts"] = dbm.now() - tf.OPEN_POLL - 1
        rec["last_ts"] = dbm.now() - 5
        await tf.lifecycle(cfg, Notifier(cfg, Bot()), Col({A: y_hist}), dbm.now())
        assert rec["ended"], "iptal görüldü"
        # 2) 📣: bekleme benimsemesi / kanalsız tur 📣 değil; gerçekten gönderilen ve hızlı yol işareti 📣
        await _fresh()
        cfg2 = _cfg()
        cfg2.crypto_chat_id = ""                                # kanal yok, ayna yok → gönderilemez
        feed("INJ", B, "sell")
        await tl.evaluate(cfg2, Notifier(cfg2, Bot()), None,
                          Col({B: [order("INJ", B, side="A", minutes=300, ntl=9_000_000)]}))
        assert tl.REG.runs[("INJ", B, "sell")].alerted_ts, "kanalsız turda da alerted_ts yazılıyor (eski davranış)"
        rows, _ = await tl.live_twaps_full(None)
        assert [(r["coin"], r["alerted"]) for r in rows] == [("INJ", False)], rows
        await dbm.alert_log("twap", f"INJ:{B}:sell", "⏳ TWAP …")      # gerçekten gönderilmiş olsaydı
        rows, _ = await tl.live_twaps_full(None)
        assert rows[0]["alerted"] is True
        async with dbm.db() as c:                                     # aynı anahtarın ESKİ emri 1 sa önce bildirildi
            await c.execute("INSERT INTO alerts_log(kind,key,ts,payload) VALUES('twap',?,?,'⏳ eski emir')",
                            (f"PUMP:{C}:buy", dbm.now() - 3600))
            await c.commit()
        feed("PUMP", C, "buy")                                        # yeni emir: bekleme benimser (gönderim yok)
        colc = Col({C: [order("PUMP", C, ntl=1_000_000, start_ago=60)]})
        await tl.evaluate(cfg2, Notifier(cfg2, Bot()), None, colc)
        rows, _ = await tl.live_twaps_full(colc)
        assert {r["coin"]: r["alerted"] for r in rows} == {"INJ": True, "PUMP": False}, rows
        # 3) sorgu anı: önbellekten gelen emir önbelleğin anını taşır; bilinmiyorsa dürüst not
        r = tl.REG.runs[("INJ", B, "sell")]
        assert int(r.order["q_ts"]) <= dbm.now() and int(r.order["q_ts"]) == tl._lookup_cache[B][0]
        e = dict(rows[0], q_ts=0)
        assert "(sorgu zamanı bilinmiyor)" in fmt.live_twaps([e])
        assert "⚡" in fmt.live_twaps([dict(e, minutes=90)], quick_max=120)
        assert "⚡" not in fmt.live_twaps([dict(e, minutes=90)], quick_max=60)
        # STRAT_CHAT_ID da botun kendi sohbeti
        from app.telegram.bot import TelegramBot
        c3 = _cfg()
        c3.strat_chat_id = "-888"
        assert "-888" in TelegramBot._own_chats(type("B", (), {"cfg": c3})())
        print("✅ inceleme) iptal edilen emir taze önbellekle düşer + lifecycle 5 dk'da sorar; 📣 yalnız gönderilen"
              " emirde; sorgu anı önbellekten; bilinmiyorsa yazılır; ⚡ ayarlı süre; STRAT sohbeti")
    asyncio.run(run())


def test_review2_marks_gate_persist_ceiling():
    """İkinci inceleme: 📣 emre özgü; yalnız HL alt emri dolumu 'süren' sayılır; yenileme kaydı günceller; soket tavanı."""
    async def run():
        from app.notify import Notifier
        await _fresh()
        cfg = _cfg()
        # eski yol gerçekten gönderdi → 'twap_o' emir işareti; aynı anahtarın eşzamanlı başka emri 📣 almaz
        feed("INJ", B, "sell")
        hist = [order("INJ", B, side="A", minutes=300, start_ago=1200, ntl=5_000_000, tid=1),
                order("INJ", B, side="A", minutes=600, start_ago=1800, ntl=1_000_000, tid=2)]
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None, Col({B: hist}))
        sent_o = tl.REG.runs[("INJ", B, "sell")].order
        async with dbm.db() as c:
            n = (await (await c.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind='twap_o' AND key=?",
                                        (f"INJ:{B}:sell:{sent_o['started_ts']}",))).fetchone())["n"]
        assert n == 1
        tl._LIVE_Q.clear()
        rows, _ = await tl.live_twaps_full(Col({B: hist}))
        assert {round(r["planned_usd"]): r["alerted"] for r in rows} == {5_000_000: True, 1_000_000: False}, rows
        # hızlı yol: Notifier'ın başarılı gönderim kaydı (sent:twap, 'twap_fast:' + emir anahtarı) 📣 sayılır
        okey = f"INJ:{B}:sell:{int(rows[1]['started_ts'])}"
        await dbm.alert_log("sent:twap", f"twap_fast:{okey}", "x")
        rows, _ = await tl.live_twaps_full(Col({B: hist}))
        assert all(r["alerted"] for r in rows), rows
        # yalnız HL alt emri dolumu 'süren' sayar: elle (imzalı) işlem anahtarı canlı tutmaz
        await _fresh()
        feed("PUMP", A, "buy", n=3, t_end=dbm.now() - 600)                 # HL dilimleri 10 dk önce bitti
        feed("PUMP", A, "buy", n=3, t_end=dbm.now() - 2, native=False)     # şimdi elle işlem
        assert not tl._native_active(dbm.now())
        # alarm verilmiş turun 10 dk yenilemesi emri değiştirirse kayıt güncellenir (iptal edilen 'activated' kalmaz)
        await _fresh()
        feed("INJ", B, "sell")
        x = [order("INJ", B, side="A", minutes=300, start_ago=1200, ntl=5_000_000, tid=1)]
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None, Col({B: x}))
        run_ = tl.REG.runs[("INJ", B, "sell")]
        assert run_.alerted_ts
        run_.lookup_ts = dbm.now() - tl.REFRESH_SEC - 1
        y = [order("INJ", B, side="A", status="terminated", minutes=300, start_ago=1200, ntl=5_000_000, tid=1),
             order("INJ", B, side="A", minutes=120, start_ago=60, ntl=3_000_000, tid=2)]
        tl._lookup_cache.clear()
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None, Col({B: y}))
        async with dbm.db() as c:
            st = [dict(r) for r in await (await c.execute(
                "SELECT order_status, planned_usd FROM twap_runs WHERE address=?", (B,))).fetchall()]
        assert st and all(not (r["order_status"] == "activated" and round(r["planned_usd"]) == 5_000_000)
                          for r in st), st
        # soket tavanı doluysa anlık sorgu yapılmaz (radarın sorgularına yer kalsın), 'sorulamadı' sayılır
        await _fresh()
        feed("PUMP", A, "buy", n=3)

        class Busy(Col):
            def __init__(self, h):
                super().__init__(h)
                self._twap_waiters = {f"w{i}": None for i in range(tf.WS_CEILING)}
        busy = Busy({A: [order("PUMP", A)]})
        rows, info = await tl.live_twaps_full(busy)
        assert not busy.calls and info["skipped"] == 1 and not rows, (info, rows)
        print("✅ inceleme 2) 📣 emre özgü (eski yol 'twap_o', hızlı yol gönderim kaydı); elle işlem 'süren' tutmaz;"
              " yenileme kaydı günceller; soket tavanında anlık sorgu yok")
    asyncio.run(run())


def test_review3_replacement_and_manual():
    """Üçüncü inceleme: iptal edilip yerine konan TWAP sorulur; elle işlem iptal edilmiş emri canlı tutmaz."""
    async def run():
        from app.notify import Notifier
        await _fresh()
        cfg = _cfg()
        # X alarm verildi; sonra iptal (bellekteki kopya terminated) ve aynı anahtarda Y başladı
        feed("INJ", B, "sell")
        x = [order("INJ", B, side="A", minutes=300, start_ago=1200, ntl=5_000_000, tid=1)]
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None, Col({B: x}))
        r = tl.REG.runs[("INJ", B, "sell")]
        x_term = [order("INJ", B, side="A", status="terminated", minutes=300, start_ago=1200, ntl=5_000_000, tid=1)]
        tl._lookup_cache[B] = (dbm.now() - 150, x_term)              # 10 dk yenilemesi iptali gördü (Y henüz yok)
        r.order = dict(r.order, status="terminated", q_ts=dbm.now() - 150)
        y = [order("INJ", B, side="A", status="terminated", minutes=300, start_ago=1200, ntl=5_000_000, tid=1),
             order("INJ", B, side="A", minutes=120, start_ago=90, ntl=9_000_000, tid=2)]
        tl._LIVE_Q.clear()
        col = Col({B: y})
        rows, info = await tl.live_twaps_full(col)
        assert col.calls == [B] and [round(e["planned_usd"]) for e in rows] == [9_000_000], (rows, info)
        # elle işlem: HL alt emri dilimleri 6 dk önce durdu, adres şimdi elle satıyor → listede yok
        await _fresh()
        feed("INJ", B, "sell", t_end=dbm.now() - 400)
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None, Col({B: x}))
        feed("INJ", B, "sell", n=3, t_end=dbm.now() - 2, native=False)
        assert tl.live_twaps() == []
        rows, _ = await tl.live_twaps_full(None)
        assert rows == [], rows
        # yeni HL alt emri bölümü: önbellek bölümün başından eskiyse yeniden sorulur (elle işlemden sonra yeni TWAP)
        await _fresh()
        feed("PUMP", A, "buy", n=5, t_end=dbm.now() - 1000, native=False)  # önce elle işlem
        tl.live_q_put(A, dbm.now() - 60, [])                             # 1 dk önce sorulmuştu: emir yoktu
        feed("PUMP", A, "buy", n=3, gap=20, t_end=dbm.now() - 2)         # 42 sn önce başlayan HL alt emri dolumları
        assert tl.REG.runs[("PUMP", A, "buy")].nat_first_ts >= dbm.now() - 45
        col2 = Col({A: [order("PUMP", A, ntl=4_000_000, start_ago=60)]})
        rows, _ = await tl.live_twaps_full(col2)
        assert col2.calls == [A] and [r["coin"] for r in rows] == ["PUMP"], rows
        print("✅ inceleme 3) iptal edilip yerine konan TWAP sorulur ve listelenir; elle işlem iptal edilmiş emri canlı"
              " tutmaz; yeni HL alt emri bölümünde eski önbellek yetmez")
    asyncio.run(run())


def test_coin_twap_command_and_liq_hint():
    """Kullanıcı 13.10: "/xpl yazdığımda XPL'deki en büyük TWAP'lar — liq + TWAP ya da /xpltwap (X yerine herhangi
    bir ticker)". /TICKERtwap ayrı liste; /xpl liq mesajının altında tek satır."""
    async def run():
        from app.hl import universe as U
        from app.notify import Notifier
        from app.radar import cryptoliq
        from app.telegram.bot import TelegramBot
        await _fresh()
        cfg = _cfg()
        feed("PUMP", A, "buy")
        feed("PUMP", B, "sell")
        feed("INJ", C, "buy")
        col = Col({A: [order("PUMP", A, ntl=2_000_000)], B: [order("PUMP", B, side="A", ntl=5_000_000, minutes=30,
                                                                     start_ago=600)],
                   C: [order("INJ", C, ntl=9_000_000)]})
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None, col)
        # coin süzgeci: yalnız PUMP; anlık sorgular da yalnız o coinin adreslerine
        tl.REG.clear()
        tl._lookup_cache.clear()
        tl._LIVE_Q.clear()
        feed("PUMP", A, "buy", n=3)
        feed("PUMP", B, "sell", n=3)
        feed("INJ", C, "buy", n=3, ntl=90_000)
        col2 = Col(col.by_addr)
        rows, info = await tl.live_twaps_full(col2, coin="PUMP", max_lookups=2)
        assert sorted(col2.calls) == sorted([A, B]) and [r["side"] for r in rows] == ["sell", "buy"], (rows, col2.calls)
        t = fmt.coin_twaps("PUMP", rows, info, quick_max=60, listened=True)
        assert t.startswith("⏳ <b>PUMP</b> · süren HL TWAP'ları · 2 emir · 🟢 alış 1 (plan $2.0M) · 🔴 satış 1"
                            " (plan $5.0M)"), t
        assert "\n1. 🔴 SATIŞ · plan <b>$5.0M</b>" in t and "<b>PUMP</b> 🔴" not in t and "⚡" in t, t
        assert "dinlenmiyor" not in t
        assert "dinlenmiyor" in fmt.coin_twaps("PUMP", [], {}, listened=False)
        assert fmt.coin_twaps("PUMP", [], {}).startswith("⏳ <b>PUMP</b> · şu an radarın gördüğü süren HL TWAP emri yok.")
        # komut: /pumptwap, /pump_twap — sahip DM ve grubun üyesi; bilinmeyen ticker dürüst cevap
        real_res, real_snap, real_fmt = U.resolve_coin, cryptoliq.snapshot, fmt.crypto_liq_snapshot

        async def fake_resolve(x):
            x = (x or "").lower()
            return {"coin": x.upper(), "symbol": x.upper(), "kind": "crypto"} if x in ("pump", "inj") else None

        async def fake_snap(cfg_, client, coin, kind):
            return {"rows": [], "png": None}
        U.resolve_coin, cryptoliq.snapshot = fake_resolve, fake_snap
        fmt.crypto_liq_snapshot = lambda s, offers=None, compact=False, extra="": "LIQ" + ("\n" + extra if extra else "")
        try:
            bot = TelegramBot(cfg, None, None, {})
            bot.collector = Col(col.by_addr)
            sent = []

            async def fake_send(text, chat_id=None, reply_markup=None):
                sent.append((str(chat_id), text))
                return True
            bot.send = fake_send

            async def msg(text, chat, uid, ctype="supergroup"):
                await bot._handle_update({"message": {"chat": {"id": chat, "type": ctype}, "from": {"id": uid},
                                                      "text": text}})
            for text, chat, uid in (("/pumptwap", 111, 111), ("/PUMP_twap", 111, 111), ("/pumptwap", -100, 42)):
                await msg(text, chat, uid, "private" if chat == 111 else "supergroup")
                assert sent[-1][0] == str(chat) and sent[-1][1].startswith("⏳ <b>PUMP</b> · süren HL TWAP'ları"), sent[-1]
            await bot._handle_update({"channel_post": {"chat": {"id": -100, "type": "channel"}, "text": "/injtwap"}})
            assert sent[-1][1].startswith("⏳ <b>INJ</b>"), sent[-1]
            await msg("/zzztwap", 111, 111, "private")
            assert sent[-1][1].startswith("❓ <b>ZZZ</b> diye bir coin bulamadım"), sent[-1]
            # /pump: liq mesajının altında TWAP satırı (anlık sorgu yok)
            n_calls = len(bot.collector.calls)
            await msg("/pump", 111, 111, "private")
            assert sent[-1][1].startswith("LIQ\n⏳ PUMP'de süren HL TWAP: 2 · en büyüğü 🔴 SATIŞ plan $5.0M → /pumptwap"), sent[-1]
            assert len(bot.collector.calls) == n_calls, "liq satırı için HL'ye sorulmaz"
            await msg("/inj", -100, 42)
            assert sent[-1][1].startswith("LIQ\n⏳ INJ'de süren HL TWAP: 1"), sent[-1]
            assert "/xpltwap" in fmt.help_text()
        finally:
            U.resolve_coin, cryptoliq.snapshot, fmt.crypto_liq_snapshot = real_res, real_snap, real_fmt
        print("✅ /TICKERtwap) coin süzgeci (sorgular o coine), alış/satış özeti; /pumptwap, /pump_twap, grup üyesi,"
              " kanal gönderisi; bilinmeyen ticker; /pump liq mesajının altında tek satır (sorgusuz)")
    asyncio.run(run())


def test_coin_twap_review_fixes():
    """İnceleme: TWAP satırı 1024 altyazı bütçesinin içinde (foto + metin tek mesaj); spot çiftleri; kayıt satırı
    sorgusuz yazılmaz; tıklanamayan komut kod olarak."""
    async def run():
        import importlib
        from app import assets
        from app.hl import universe as U
        from app.radar import cryptoliq
        from app.telegram.bot import TelegramBot
        snapmod = importlib.import_module("test_snapshot_compact")
        await _fresh()
        cfg = _cfg()
        # 1) gerçek PUMP anlık görüntüsü + foto + TWAP satırı → tek mesaj (foto), altyazı ≤ 1024 ve satırı içerir
        real_res, real_snap, real_live = U.resolve_coin, cryptoliq.snapshot, tl.live_twaps_full

        async def fake_resolve(x):
            x = (x or "").lower()
            return {"coin": "PUMP", "symbol": "PUMP", "kind": "crypto"} if x == "pump" else None

        async def fake_snap(cfg_, client, coin, kind):
            s_ = snapmod.pump_snapshot()
            s_["png"] = b"\x89PNG fake"
            return s_

        async def fake_live(*a, **k):
            return ([{"coin": "PUMP", "addr": A, "side": "buy", "planned_usd": 2.4e6, "src": "mem"}],
                    {"asked": 0, "failed": 0, "skipped": 0})
        U.resolve_coin, cryptoliq.snapshot, tl.live_twaps_full = fake_resolve, fake_snap, fake_live
        try:
            bot = TelegramBot(cfg, None, None, {})
            texts, photos = [], []

            async def fake_send(text, chat_id=None, reply_markup=None):
                texts.append(text)
                return True

            async def fake_photo(png, caption="", chat_id=None, **k):
                photos.append(caption)
                return True
            bot.send, bot.send_photo = fake_send, fake_photo
            await bot._cmd_coin_liq("pump", "111")
            assert not texts and len(photos) == 1, (texts, photos)
            assert fmt.visible_len(photos[0]) <= 1024 and "→ /pumptwap" in photos[0], photos[0][-200:]
        finally:
            U.resolve_coin, cryptoliq.snapshot, tl.live_twaps_full = real_res, real_snap, real_live
        # 2) spot çiftleri: /hypetwap perp + HYPE/USDC; yalnız spotta olan UBTC bulunur
        await _fresh()
        old_names = dict(assets.SPOT_NAMES)
        assets.SPOT_NAMES.clear()
        assets.SPOT_NAMES.update({"@107": "HYPE/USDC", "@142": "UBTC/USDC"})

        async def res2(x):
            return {"coin": "HYPE", "symbol": "HYPE", "kind": "crypto"} if (x or "").lower() == "hype" else None
        U.resolve_coin = res2
        try:
            feed("@107", A, "buy")
            feed("HYPE", B, "sell")
            feed("@142", C, "buy")
            col = Col({A: [order("@107", A, ntl=5_000_000)], B: [order("HYPE", B, side="A", ntl=1_000_000)],
                       C: [order("@142", C, ntl=3_000_000)]})
            bot = TelegramBot(cfg, None, None, {})
            bot.collector = col
            sent = []

            async def fs(text, chat_id=None, reply_markup=None):
                sent.append(text)
                return True
            bot.send = fs
            assert await bot._cmd_coin_twap("hypetwap", "111")
            t = sent[-1]
            assert t.startswith("⏳ <b>HYPE</b> · süren HL TWAP'ları · 2 emir · 🟢 alış 1 (plan $5.0M) · 🔴 satış 1"), t
            assert "1. 🪙 <b>HYPE/USDC</b> 🟢 ALIŞ · plan <b>$5.0M</b>" in t and "2. <b>HYPE</b> 🔴 SATIŞ" in t, t
            assert "UBTC" not in t
            assert await bot._cmd_coin_twap("ubtctwap", "111")
            assert sent[-1].startswith("⏳ <b>UBTC</b> · süren HL TWAP'ları · 1 emir"), sent[-1]
        finally:
            U.resolve_coin = real_res
            assets.SPOT_NAMES.clear()
            assets.SPOT_NAMES.update(old_names)
        # 3) ipucu: kayıt satırı sorgusuz yazılmaz; emri bilinmeyen dilim söylenir; ':' komutu kod olarak
        db_row = {"coin": "PUMP", "addr": A, "side": "buy", "planned_usd": 9e6, "src": "db"}
        mem_row = {"coin": "PUMP", "addr": B, "side": "sell", "planned_usd": 1e6, "src": "mem"}
        assert fmt.coin_twap_hint("PUMP", "pump", [db_row], {"skipped": 0}) == ""
        h = fmt.coin_twap_hint("PUMP", "pump", [db_row, mem_row], {"skipped": 2})
        assert h == "⏳ PUMP'de süren HL TWAP: 1 · en büyüğü 🔴 SATIŞ plan $1.0M (+2 adreste dilim görülüyor) → /pumptwap", h
        assert fmt.coin_twap_hint("PUMP", "pump", [], {"skipped": 1}) == \
            "⏳ PUMP'de 1 adreste HL TWAP dilimi görülüyor, emri henüz sorulmadı → /pumptwap"
        assert "<code>/para:stxtwap</code>" in fmt.coin_twap_hint("STX", "para:stx", [mem_row])
        print("✅ inceleme) TWAP satırı altyazı bütçesinde (foto + metin tek mesaj); spot çiftleri ve yalnız-spot token;"
              " kayıt satırı sorgusuz yazılmaz; ':' komutu kod")
    asyncio.run(run())


def test_review4_stale_known_and_cap_order():
    """İnceleme 4: bellek kopyası 2 dk'dan eskiyse yine sorulur (iptal-yeniden-koy / eşzamanlı ikinci TWAP gizlenmesin);
    sorgu tavanı önbellek süzgecinden SONRA (taze önbellekli adres yer kaplamaz)."""
    async def run():
        from app.notify import Notifier
        await _fresh()
        cfg = _cfg()
        feed("INJ", B, "sell")
        x = [order("INJ", B, side="A", minutes=300, start_ago=1200, ntl=5_000_000, tid=1)]
        await tl.evaluate(cfg, Notifier(cfg, Bot()), None, Col({B: x}))
        r = tl.REG.runs[("INJ", B, "sell")]
        old = tl._lookup_cache[B][0] - 300                         # radarın kopyası 5 dk önce (henüz 20 dk değil)
        tl._lookup_cache[B] = (old, tl._lookup_cache[B][1])
        r.order["q_ts"] = old
        y = [order("INJ", B, side="A", status="terminated", minutes=300, start_ago=1200, ntl=5_000_000, tid=1),
             order("INJ", B, side="A", minutes=120, start_ago=100, ntl=9_000_000, tid=2)]
        col = Col({B: y})
        rows, info = await tl.live_twaps_full(col)
        assert col.calls == [B] and [round(e["planned_usd"]) for e in rows] == [9_000_000], (rows, info)
        # tavan önbellekten sonra: 4 adres, 3'ünün önbelleği taze → yalnız 1 sorgu, uyarı yok
        await _fresh()
        addrs = ["0x" + ch * 40 for ch in "1234"]
        for i, ad in enumerate(addrs):
            feed("PUMP", ad, "buy", n=3, ntl=10_000 * (i + 1))
            if i < 3:
                tl.live_q_put(ad, dbm.now(), [order("PUMP", ad, ntl=1_000_000 * (i + 1), tid=10 + i)])
        col2 = Col({addrs[3]: [order("PUMP", addrs[3], ntl=4_000_000, tid=99)]})
        rows, info = await tl.live_twaps_full(col2, max_lookups=1)
        assert col2.calls == [addrs[3]] and info == {"asked": 1, "failed": 0, "skipped": 0}, (col2.calls, info)
        assert len(rows) == 4, rows
        print("✅ inceleme 4) 2 dk'dan eski bellek kopyası yeniden sorulur (yerine konan emir görünür); tavan önbellek"
              " süzgecinden sonra")
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
