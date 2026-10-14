"""Aynı adres + coin + yönde ikinci HL TWAP — gecikme düzeltmelerinin (14.10 NEAR / TRX) inceleme bulguları.

Pinlenenler:
  • iki emir örtüştükçe yeniden sorgu tekrarlanmaz: bulunan emir bilinenlere eklenir (tempo eşiği 3k+1); yeniden
    sorgu yeni emir bulamazsa aralık katlanır (dakikada bir boşa sorgu yok)
  • aynı anahtarın iki emri de bildirildiyse ikisinin de ⛔ notu yazılır (yenisi eskisinin kaydını silmez); yeniden
    başlatmada ikisi de geri yüklenir
  • dilimleri aynı saniyeye düşen ikinci emir (tempo değişmez) küme boyutundan sezilir
  • eski yolun adres önbelleği dizinin ilk HL alt emrinden eskiyse kullanılmaz (başka coin'deki erken sorgu yeni emri
    "emir yok" göstermez)
  • erken sorgu: ortalama dilim ilk dilim tabanının altındaysa Cumartesi hissede tabansız ön süzgeç geçirmez
  • /tani "erken sorgu" sayacı
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twap-relook.db")
from test_twapfast import A, PX, Bot, Col, _cfg, _drain, _fresh, twap_order  # noqa: E402

from app import db as dbm  # noqa: E402
from app.notify import Notifier  # noqa: E402
from app.radar import twapfast as tf  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402

KEY = ("PUMP", A, "buy")


def _slice_sz(plan, minutes):
    return plan / PX / (2 * minutes)                          # HL: 30 sn'de bir alt emir → olağan dilim adedi


async def _play(cfg, notifier, col, events):
    """(ts, adet) dilimleri sırayla; sıraya giren dilim hemen sorulur. Dönüş: ilk yeni bildirim anı (yoksa None)."""
    bot = notifier.bot
    n0, first = len(bot.sent), None
    for ts, sz in events:
        tf.observe("PUMP", A, "buy", PX, sz, sz * PX, ts, 1000)
        if tf.F.pending.get(KEY, {}).get("queued"):
            await _drain(cfg, notifier, col, ts)
            if first is None and len(bot.sent) > n0:
                first = ts
    return first


def test_overlap_relooks_once_and_both_cancel_notes():
    async def run():
        cfg = _cfg()
        cfg.twap_alert_cooldown = 0                          # adres beklemesi yok: aynı anahtarın 2. emri de bildirilir
        await _fresh(cfg)
        now = dbm.now()
        s0, s1 = now - 400, now - 400 + 47                    # ikinci emir 47 sn sonra (dilimleri 15 sn kayık)
        st = {s0: "activated", s1: "activated"}
        col = Col(lambda: [twap_order(s1, minutes=10, plan=900_000, status=st[s1]),
                           twap_order(s0, minutes=10, plan=600_000, status=st[s0])])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        x, y = _slice_sz(600_000, 10), _slice_sz(900_000, 10)
        assert await _play(cfg, notifier, col, [(s0 + 1, x)]) == s0 + 1 and len(bot.sent) == 2
        ev = sorted([(s0 + 1 + 30 * i, x) for i in range(1, 13)] + [(s1 + 1 + 30 * j, y) for j in range(0, 11)])
        first = await _play(cfg, notifier, col, ev)
        assert first is not None and first - s1 <= 70, (first, s1)
        assert len(bot.sent) == 4 and "plan <b>$900K</b>" in bot.sent[2][1], bot.sent
        # #0: iki emir ~5 dk örtüştü — tek yeniden sorgu (eskiden dakikada bir)
        assert tf.F.stats["relook"] == 1 and len(col.calls) == 2, (tf.F.stats["relook"], col.calls)
        assert tf.F.open[KEY]["started"] == s1 and set(tf.F.older[KEY]) == {s0}
        assert tf.F.snapshot(now)["open"] == 2
        assert {(o["coin"], o["planned_usd"]) for o in tl.live_twaps(now)} == {("PUMP", 600_000), ("PUMP", 900_000)}
        # yeniden başlatma: iki kayıt da geri gelir
        await tf.save_open()
        tf.F.open.clear()
        tf.F.older.clear()
        tf.F.kslices.clear()
        assert await tf.load_open(now) == 2
        assert tf.F.open[KEY]["started"] == s1 and set(tf.F.older[KEY]) == {s0}
        assert tf.F.known[KEY] == s1 + 600 + 120
        # #1: ESKİ emir iptal → ⛔ (yenisi sürüyor: bekler); susturma yenisinin bitişine iner
        st[s0] = "terminated"
        cancel_at = s0 + 400
        n = await tf.lifecycle(cfg, notifier, col, cancel_at)
        ends = [(c, t) for c, t in bot.sent if "TWAP iptal edildi" in t]
        assert n == 1 and [c for c, _ in ends] == ["-100", "-200"], bot.sent
        assert "plan $600K" in ends[0][1], ends[0][1]
        assert not tf.F.open[KEY]["ended"] and tf.F.known[KEY] == s1 + 600 + 120
        # yenisi de iptal → kendi ⛔ notu; susturma kalkar
        st[s1] = "terminated"
        for r in (tf.F.open[KEY],):
            r["lookup_ts"] = 0
        assert await tf.lifecycle(cfg, notifier, col, cancel_at + 60) == 1
        ends = [t for c, t in bot.sent if "TWAP iptal edildi" in t and c == "-100"]
        assert len(ends) == 2 and "plan $900K" in ends[1], ends
        assert KEY not in tf.F.known
        assert await tf.lifecycle(cfg, notifier, col, cancel_at + 120) == 0, "not bir kez"
        await tf.lifecycle(cfg, notifier, col, cancel_at + 60 + 601)
        assert not tf.F.older and KEY not in tf.F.open, "biten kayıtlar 10 dk sonra düşer"
    asyncio.run(run())
    print("✅ örtüşen iki emir) tek yeniden sorgu; ikisi de bildirildi; ikisinin de ⛔ notu; yeniden başlatmada ikisi")


def test_relook_backoff_when_nothing_new():
    async def run():
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        s0 = now - 1300
        col = Col(lambda: [twap_order(s0, minutes=60, plan=600_000)])   # HL geçmişinde yalnız bilinen emir
        bot = Bot()
        notifier = Notifier(cfg, bot)
        x = _slice_sz(600_000, 60)
        await _play(cfg, notifier, col, [(s0 + 1, x)])
        assert len(col.calls) == 1 and KEY in tf.F.known, "emir okundu (kapı dışı: dakika hızı düşük), anahtar susturuldu"
        # (a) evre: 15 sn'de bir dilim (iki evre) ama sorgu bilinen emri buluyor → tek yeniden sorgu, sonra iki evre bilinir
        await _play(cfg, notifier, col, [(s0 + 1 + 15 * i, x) for i in range(2, 30)])
        assert tf.F.stats["relook"] == 1 and len(col.calls) == 2, (tf.F.stats["relook"], col.calls)
        # (b) boyut: tek evrede ikişer büyük dilim (HL telafi dilimi gibi) — yeni emir yok → aralık katlanır
        ev, t0 = [], s0 + 1 + 15 * 30
        for i in range(40):
            ev.append((t0 + 30 * i, x * (2.5 if i % 4 in (2, 3) else 1.0)))
        await _play(cfg, notifier, col, ev)
        n = tf.F.stats["relook"] - 1
        assert 1 <= n <= 4 and tf.F.kslices[KEY]["gap"] > tf.RELOOK_MIN_GAP, (n, tf.F.kslices[KEY]["gap"])
        assert bot.sent == [], "bilinen emir yeniden değerlendirilir ama yazılmaz"
    asyncio.run(run())
    print("✅ yeniden sorgu) yeni evre bir kez sorulur, sonra bilinir; boyut sezgisi yeni emir bulamazsa aralık katlanır")


def test_coalesced_second_order_by_slice_size():
    async def run():
        cfg = _cfg()
        cfg.twap_alert_cooldown = 0
        await _fresh(cfg)
        now = dbm.now()
        s0 = now - 400
        s1 = s0 + 60                                          # dilimleri ilkinin dilimleriyle AYNI saniyede
        col = Col(lambda: [twap_order(s1, minutes=10, plan=900_000), twap_order(s0, minutes=10, plan=600_000)])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        x, y = _slice_sz(600_000, 10), _slice_sz(900_000, 10)
        await _play(cfg, notifier, col, [(s0 + 1, x)])
        ev = [(s0 + 1 + 30 * i, x) for i in range(1, 13)] + [(s1 + 1 + 30 * j, y) for j in range(0, 10)]
        ev.sort(key=lambda e: (e[0], -e[1]))
        first = await _play(cfg, notifier, col, ev)
        assert first is not None and first - s1 <= 40, (first, s1)
        assert tf.F.stats["relook"] == 1 and len(col.calls) == 2, (tf.F.stats["relook"], col.calls)
        assert "plan <b>$900K</b>" in bot.sent[2][1]
        # tek emirde olağan ±%20 dilim yeniden sorgu doğurmaz
        await _fresh(cfg)
        col2 = Col(lambda: [twap_order(s0, minutes=10, plan=600_000)])
        bot2 = Bot()
        n2 = Notifier(cfg, bot2)
        await _play(cfg, n2, col2, [(s0 + 1, x)] + [(s0 + 1 + 30 * i, x * (1.2 if i % 2 else 0.8))
                                                    for i in range(1, 13)])
        assert tf.F.stats["relook"] == 0 and len(col2.calls) == 1
    asyncio.run(run())
    print("✅ aynı saniyede ikinci emir) 2 sn'lik küme bilinenin 1,6 katını iki kez aşınca sorulur; tek emir sorulmaz")


def test_slow_path_cache_and_sat_floor():
    async def run():
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        started = now - 360
        # #3: aynı adresin başka coin'deki erken sorgusu (PUMP emri yokken) önbellekte
        tl._lookup_cache[A] = (started - 60, [])
        col = Col(lambda: [twap_order(started, minutes=60, plan=600_000)])
        for i in range(12):
            tl.observe("PUMP", A, "buy", PX, 1_000_000, 5_000, started + 1 + 30 * i, 1, True)
        run_ = tl.REG.runs[KEY]
        o = await tl.lookup_order(col, run_, now, cfg, {})
        assert col.calls == [A] and o and o["started_ts"] == started, (col.calls, o)
        assert await tl.lookup_order(col, run_, now + 5, cfg, {}) and col.calls == [A], "taze önbellek kullanılır"
        # #4: Cumartesi hisse tabanı yokken ön süzgeç her dilimi geçirir — taban altı ortalama yine sorulmaz
        orig = tl.sat_missing
        tl.sat_missing = lambda *a, **k: True
        try:
            tl.observe("PUMP", "0x" + "c" * 40, "buy", PX, 8_000, 40, started + 1, 1, True)
            tiny = tl.REG.runs[("PUMP", "0x" + "c" * 40, "buy")]
            assert not tl._slice_could_pass(cfg, tiny, now), "ortalama $40 < taban"
            tl.observe("PUMP", "0x" + "d" * 40, "buy", PX, 400_000, 2_000, started + 1, 1, True)
            ok = tl.REG.runs[("PUMP", "0x" + "d" * 40, "buy")]
            assert tl._slice_could_pass(cfg, ok, now), "tabanın üstü sorulur"
        finally:
            tl.sat_missing = orig
        # taban altı dilim (ilk dilim yolu hiç sormaz) Cumartesi dışında ön süzgeçle yine erken sorulur
        tl.VOL_CACHE.update(ts=now, v=await tl.volumes(None))
        tl.observe("PUMP", "0x" + "e" * 40, "buy", PX, 160_000, 800, started + 1, 1, True)
        below = tl.REG.runs[("PUMP", "0x" + "e" * 40, "buy")]
        assert cfg.twap_fast_min_slice_usd == 1000 and tl._slice_could_pass(cfg, below, now), "$800 < $1K taban: erken"
        # #5: /tani
        from app import diag
        await dbm.kv_set("twaplive_stats", {"ts": now, "keys": 3, "cands": 1, "early": 2})
        text = "\n".join(await diag._subsystems(cfg))
        assert "2 erken sorgu" in text, [ln for ln in text.splitlines() if "canlı twap" in ln]
    asyncio.run(run())
    print("✅ eski yol) önbellek dizinin ilk alt emrinden eskiyse yeniden sorulur; Cumartesi taban altı dilim sorulmaz;"
          " /tani erken sorgu")


def test_untracked_orders_counted_and_released():
    """İnceleme (b2271cf): (a) anahtarda zaten iki emir varken (biri taban altı dilimli) yeni emir 5 dk gecikiyordu —
    artık A'nın evresi bir kez sorulup bilinir, aynı saniyeye düşen C dilim boyundan sezilir; (b) izlenmeyen (kapı dışı)
    iptal edilmiş emrin planlanan bitişi, bildirilen emrin ⛔ notundan sonra susturmayı uzatıp yeni emri gizliyordu."""
    async def run():
        # (a)
        cfg = _cfg()
        cfg.twap_alert_cooldown = 0
        await _fresh(cfg)
        now = dbm.now()
        s0 = now - 1347
        sb, sc = s0 + 47, now - 100
        col = Col(lambda: [twap_order(sc, minutes=5, plan=900_000), twap_order(sb, minutes=60, plan=2_000_000),
                           twap_order(s0, minutes=60, plan=96_000)])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        a, b, c = _slice_sz(96_000, 60), _slice_sz(2_000_000, 60), _slice_sz(900_000, 5)
        ev = [(s0 + 1 + 30 * i, a) for i in range(0, 45) if s0 + 1 + 30 * i <= now] \
            + [(sb + 1 + 30 * j, b) for j in range(0, 45) if sb + 1 + 30 * j <= now] \
            + [(sc + 1 + 30 * k, c) for k in range(0, 4)]
        ev.sort()
        first = await _play(cfg, notifier, col, [e for e in ev if e[0] < sc])
        assert first is not None and len(bot.sent) == 2 and "plan <b>$2.0M</b>" in bot.sent[0][1], bot.sent
        assert tf.F.stats["relook"] == 1 and len(col.calls) == 2, "A'nın evresi bir kez sorulur, sonra bilinir"
        first = await _play(cfg, notifier, col, [e for e in ev if e[0] >= sc])
        assert first is not None and first - sc <= 70 and "plan <b>$900K</b>" in bot.sent[2][1], (first, sc, bot.sent)
        assert tf.F.stats["relook"] == 2
        # (b)
        await _fresh(cfg)
        now = dbm.now()
        sx, so = now - 600, now - 553
        hist = {"h": [twap_order(so, minutes=10, plan=900_000),
                      twap_order(sx, minutes=60, plan=600_000)]}
        col = Col(lambda: hist["h"])
        bot = Bot()
        notifier = Notifier(cfg, bot)
        x, o = _slice_sz(600_000, 60), _slice_sz(900_000, 10)
        await _play(cfg, notifier, col, [(sx + 1, x)])
        assert bot.sent == [] and tf.F.known[KEY] == sx + 3600 + 120, "X kapı dışı (yavaş), anahtar susturuldu"
        ev = sorted([(sx + 1 + 30 * i, x) for i in range(1, 6)] + [(so + 1 + 30 * j, o) for j in range(0, 4)])
        await _play(cfg, notifier, col, ev)
        assert len(bot.sent) == 2 and "plan <b>$900K</b>" in bot.sent[0][1], bot.sent
        assert tf.F.known[KEY] == so + 600 + 120, "susturma bulunan emrin KENDİ bitişi (kapı dışı X'inki değil)"
        hist["h"] = [twap_order(so, minutes=10, plan=900_000, status="terminated"),
                     twap_order(sx, minutes=60, plan=600_000, status="terminated")]
        tf.F.open[KEY]["lookup_ts"] = 0
        assert await tf.lifecycle(cfg, notifier, col, now - 300) == 1 and "TWAP iptal edildi" in bot.sent[-1][1]
        assert KEY not in tf.F.known, "izlenen tek emir bitti: susturma yok (kapı dışı X'in planı uzatmaz)"
        sp = now - 200
        hist["h"] = [twap_order(sp, minutes=60, plan=2_400_000)] + hist["h"]
        n = len(bot.sent)
        assert await _play(cfg, notifier, col, [(sp + 1, _slice_sz(2_400_000, 60))]) == sp + 1
        assert len(bot.sent) == n + 2 and "plan <b>$2.4M</b>" in bot.sent[n][1], bot.sent[n:]
    asyncio.run(run())
    print("✅ inceleme) örtüşen tüm emirler bilinir (yeni emir gecikmez); izlenmeyen iptal edilmiş emir susturmayı uzatmaz")


def test_loose_known_own_end_and_mm_no_relook():
    from test_twap_loose import _fresh_l, _lcfg
    from app.radar import twaploose as lo

    async def run():
        # 🔸 yalnız-gevşek çözüm twaploose.KNOWN'u bulduğu emrin KENDİ bitişine yazar (ana yolun uzun emrine değil)
        cfg = _lcfg()
        await _fresh_l(cfg)
        now = dbm.now()
        so, sx = now - 300, now - 60
        col = Col(lambda: [twap_order(sx, minutes=5, plan=80_000), twap_order(so, minutes=60, plan=2_000_000)])
        p = {"coin": "PUMP", "addr": A, "side": "buy", "t0": sx + 1, "ts": sx + 1, "usd": 2_000.0, "sz": 2_000 / PX,
             "px": PX, "px0": PX, "n": 1, "max": 2_000.0, "tot": 2_000.0, "szt": 2_000 / PX, "queued": now, "tries": 0,
             "not_before": 0, "loose": True}
        tf.F.pending[KEY] = p
        await tf.resolve(cfg, None, Notifier(cfg, Bot()), col, KEY, p, tf.F.vols)
        assert lo.KNOWN[KEY] == sx + 300 + 120 and KEY not in tf.F.known, (lo.KNOWN.get(KEY), tf.F.known.get(KEY))
        # mm / vault: iki örtüşen emir dakikada bir yeniden sorgu doğurmaz (susturma adres yüzünden)
        cfg = _cfg()
        await _fresh(cfg)
        async with dbm.db() as c:
            await c.execute("INSERT INTO addresses(address, first_seen, entity) VALUES(?,?,?)", (A, now - 9000, "vault"))
            await c.commit()
        s0 = now - 2400
        col = Col(lambda: [twap_order(s0 + 15, minutes=60, plan=900_000), twap_order(s0, minutes=60, plan=900_000)])
        bot = Bot()
        x = _slice_sz(900_000, 60)
        ev = sorted([(s0 + 1 + 30 * i, x) for i in range(0, 80)] + [(s0 + 16 + 30 * i, x) for i in range(0, 80)])
        await _play(cfg, Notifier(cfg, bot), col, ev)
        assert tf.F.stats["mm"] == 1 and tf.F.stats["relook"] == 0 and bot.sent == [], tf.F.stats
    asyncio.run(run())
    print("✅ inceleme) 🔸 susturma kendi emrinin bitişi; mm / vault anahtarında yeniden sorgu yok")


def test_early_counter_counts_real_lookups():
    async def run():
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        started = now - 400
        tl.VOL_CACHE.update(ts=now, v=await tl.volumes(None))
        col = Col(lambda: [twap_order(started, minutes=60, plan=600_000)])
        for i in range(13):
            tl.observe("PUMP", A, "buy", PX, 160_000, 800, started + 2 + 30 * i, 1, True)
        outs = [await tl.evaluate(cfg, Notifier(cfg, Bot()), None, col) for _ in range(3)]
        assert [o.get("early", 0) for o in outs] == [1, 0, 0] and col.calls == [A], ([o.get("early") for o in outs],
                                                                                  col.calls)
    asyncio.run(run())
    print("✅ /tani) erken sorgu sayacı yalnız HL'ye gerçekten sorulanları sayar (önbellekten dönen tur sayılmaz)")


def _hist(spec, st):
    """spec: [(başlangıç, dk, plan)], st: {başlangıç: durum} → HL geçmişi (en yeni önce)."""
    return [twap_order(a, minutes=m, plan=pl, status=st.get(a, "activated")) for a, m, pl in sorted(spec, reverse=True)]


def _slices(start, mins, plan, until, stop=None):
    return [(start + 1 + 30 * i, _slice_sz(plan, mins)) for i in range(0, 2 * int(mins) + 1)
            if start + 1 + 30 * i <= until and (stop is None or start + 1 + 30 * i <= stop)]


def test_phase_detection_review_cases():
    """Son inceleme (d4c8028..f52109a): emir SAYIMI yerine gözlenen dilim EVRESİ (an mod 30).
    (a) izlenmeyen emir iptal → evresi dilim kesilince düşer; C hangi anda başlarsa başlasın sorulur
    (b) aynı saniyede başlamış iki bilinen emir (tek evre) yeni evredeki C'yi gizlemez
    (c) aynı saniyede iki emir: olağan dilim ikisinin toplamı — boşa yeniden sorgu yok
    (d) bilinen emir bitip yerine hemen yenisi (roll-over): bitişte boşa sorgu yok, yenisi ≤ ~1 dk"""
    async def scenario(spec, cancel, c_spec, until_off=None):
        cfg = _cfg()
        cfg.twap_alert_cooldown = 0
        await _fresh(cfg)
        now = dbm.now()
        st = {}
        allspec = spec + [c_spec]
        col = Col(lambda: _hist([x for x in allspec if x[0] <= dbm.now() + 99999 and (x is not c_spec or C["on"])], st))
        C = {"on": False}
        bot = Bot()
        notifier = Notifier(cfg, bot)
        ev = []
        for a, m, pl in spec:
            ev += _slices(a, m, pl, now, cancel.get(a))
        ev.sort()
        cs = c_spec[0]
        await _play(cfg, notifier, col, [e for e in ev if e[0] < cs])
        for a in cancel:
            st[a] = "terminated"
        C["on"] = True
        ev2 = sorted([e for e in ev if e[0] >= cs] + _slices(cs, c_spec[1], c_spec[2], now))
        first = await _play(cfg, notifier, col, ev2)
        return first, bot, col

    async def run():
        # (a) A, B kapı dışı 90 dk $900K (47 sn arayla); A 300. sn'de iptal; C (10 dk $300K, ⚡) iptalden 5..50 sn sonra
        for off in range(5, 55, 5):
            now = dbm.now()
            sa = now - 900
            first, bot, col = await scenario([(sa, 90, 900_000), (sa + 47, 90, 900_000)], {sa: sa + 300},
                                             (sa + 300 + off, 10, 300_000))
            cs = sa + 300 + off
            assert first is not None and first - cs <= 70 and "plan <b>$300K</b>" in bot.sent[0][1], (off, first, cs)
        # (b) A, B aynı evrede (1 sn arayla), C başka evrede
        now = dbm.now()
        sa = now - 900
        first, bot, col = await scenario([(sa, 90, 900_000), (sa + 1, 90, 900_000)], {}, (sa + 314, 10, 300_000))
        assert first is not None and first - (sa + 314) <= 70, first
        # (c) A, B aynı saniyede: 5 dk boyunca boşa yeniden sorgu yok; C yine ≤ ~1 dk
        now = dbm.now()
        sa = now - 900
        first, bot, col = await scenario([(sa, 90, 900_000), (sa + 0, 90, 899_000)], {}, (sa + 614, 10, 300_000))
        assert first is not None and first - (sa + 614) <= 70, first
        assert tf.F.stats["relook"] <= 1 and len(col.calls) <= 2, (tf.F.stats["relook"], col.calls)
        # (d) A (10 dk) + B (90 dk), ikisi kapı dışı; A bitince 12 sn sonra A2 (5 dk $900K)
        now = dbm.now()
        sa = now - 900
        first, bot, col = await scenario([(sa, 10, 200_000), (sa + 47, 90, 900_000)], {},
                                         (sa + 600 + 12, 5, 900_000))
        assert first is not None and first - (sa + 612) <= 70, first
        assert tf.F.stats["relook"] == 2 and len(col.calls) == 3, "B'nin evresi + A2 (A'nın bitişinde sorgu yok)"
    asyncio.run(run())
    print("✅ evre sezgisi) iptal edilen emrin evresi düşer; aynı evrede / aynı saniyede bilinen emirler yeni emri gizlemez;"
          " roll-over ≤ ~1 dk")


def test_lifecycle_two_tracked_orders():
    """(e) eski kardeş iptal edildiyse yenisi bitince susturma kalkar ve kardeşin ⛔ notu hemen yazılır; (f) 1 sn arayla
    başlamış iki bildirilmiş emir: her kaydın notu KENDİ emrinin rakamlarıyla."""
    async def run():
        cfg = _cfg()
        cfg.twap_alert_cooldown = 0
        await _fresh(cfg)
        now = dbm.now()
        # (e) A 120 dk $4M (bildirilir), B 10 dk $900K (bildirilir); A, B'nin bitişinden 20 sn önce iptal
        sa = now - 900
        sb = sa + 47
        spec, st = [(sa, 120, 4_000_000), (sb, 10, 900_000)], {}
        col = Col(lambda: _hist(spec, st))
        bot = Bot()
        notifier = Notifier(cfg, bot)
        await _play(cfg, notifier, col, sorted(_slices(sa, 120, 4_000_000, sb + 120) + _slices(sb, 10, 900_000,
                                                                                               sb + 120)))
        assert len(bot.sent) == 4 and tf.F.open[KEY]["started"] == sb and set(tf.F.older[KEY]) == {sa}, bot.sent
        t1 = sb + 600 + 31
        for r in tf.F.records():                        # B'nin dilimleri A'nın kaydını da taze tuttu; son yoklama yakın:
            r[1]["lookup_ts"], r[1]["last_ts"] = t1 - 40, t1 - 10   # A 5 dk'lık yoklamayı bekliyor olurdu
        st[sa], st[sb] = "terminated", "finished"
        # B normal bitti (not yok, varsayılan); sorgusu A'yı iptal gösterdi → A aynı turda yeniden sorulur, ⛔ hemen
        assert await tf.lifecycle(cfg, notifier, col, t1) == 1
        assert "TWAP iptal edildi" in bot.sent[-2][1] and "plan $4.0M" in bot.sent[-2][1], bot.sent[-2:]
        assert not [t for _, t in bot.sent if "TWAP bitti" in t]
        assert KEY not in tf.F.known, "A HL'de sürmüyor: susturma A'nın planına uzamaz"
        # (f) A 60 dk $2.4M ve B 10 dk $1.5M, 1 sn arayla; ikisi bildirilir; B iptal → B'nin ⛔ notu B'nin rakamlarıyla
        await _fresh(cfg)
        now = dbm.now()
        sa = now - 500
        sb = sa + 1
        spec, st = [(sa, 60, 2_400_000)], {}
        bot = Bot()
        notifier = Notifier(cfg, bot)
        await _play(cfg, notifier, col, [(sa + 1, _slice_sz(2_400_000, 60))])
        spec.append((sb, 10, 1_500_000))                # B (1 sn sonra) de bildirilmiş olsun
        tf.F.known.pop(KEY, None)
        await _play(cfg, notifier, col, [(sb + 1, _slice_sz(1_500_000, 10))])
        assert len(bot.sent) == 4 and tf.F.open[KEY]["started"] == sb and set(tf.F.older[KEY]) == {sa}, \
            [t[:60] for _, t in bot.sent]
        st[sb] = "terminated"
        for r in tf.F.records():
            r[1]["lookup_ts"] = 0
        assert await tf.lifecycle(cfg, notifier, col, sa + 300) == 1
        ends = [t for c, t in bot.sent if "TWAP iptal edildi" in t and c == "-100"]
        assert len(ends) == 1 and "plan $1.5M" in ends[0], ends
        assert not tf.F.find(KEY, sa)["ended"], "A sürüyor"
    asyncio.run(run())
    print("✅ ömür) iptal edilmiş kardeş susturmayı uzatmaz, ⛔ hemen; 1 sn arayla iki emirde not doğru emirle")


def test_phase_model_review_fixes():
    """Evre modelinin incelemesi (a5d8e8a): (1) yeniden sorgu olağan dilimleri sıfırlamaz — hemen sonra aynı evreye düşen
    yeni emir olağan sayılmaz; (2) yeni emrin bir alt emri dolmasa da sayılır (~1 dk); (3) F.older'daki kaydın sorgusu
    kardeşi iptal gösterirse kardeşin ⛔ notu aynı turda."""
    async def run():
        cfg = _cfg()
        cfg.twap_alert_cooldown = 0
        # (1) X (kapı dışı) bilinir; Z başka evrede → tek yeniden sorgu (sa+338); Y (⚡, dilimi X'in 3 katı) X'in evresinde
        await _fresh(cfg)
        now = dbm.now()
        sa = now - 1200
        spec = [(sa, 90, 900_000), (sa + 307, 90, 900_000), (sa + 360, 10, 300_000)]
        col = Col(lambda: _hist([x for x in spec if x[0] <= cur["t"]], {}))
        cur = {"t": 0}
        bot = Bot()
        notifier = Notifier(cfg, bot)
        ev = sorted(_slices(sa, 90, 900_000, sa + 700) + _slices(sa + 307, 90, 900_000, sa + 700)
                    + _slices(sa + 360, 10, 300_000, sa + 700))
        first = None
        for e in ev:
            cur["t"] = e[0]
            f = await _play(cfg, notifier, col, [e])
            first = first or f
        assert first is not None and first - (sa + 360) <= 70 and "plan <b>$300K</b>" in bot.sent[0][1], (first, bot.sent)
        # (2) X bilinir; Y başka evrede, 2. alt emri dolmuyor (3.sü 2 kat)
        for off in (3, 14, 27):
            await _fresh(cfg)
            now = dbm.now()
            sa = now - 1200
            cs = sa + 610 + off
            spec = [(sa, 90, 900_000), (cs, 10, 300_000)]
            col = Col(lambda: _hist([x for x in spec if x[0] <= cur["t"]], {}))
            bot = Bot()
            notifier = Notifier(cfg, bot)
            y = _slices(cs, 10, 300_000, cs + 200)
            y = [y[0], (y[2][0], y[2][1] * 2)] + y[3:]
            ev = sorted(_slices(sa, 90, 900_000, cs + 200) + y)
            first = None
            for e in ev:
                cur["t"] = e[0]
                f = await _play(cfg, notifier, col, [e])
                first = first or f
            assert first is not None and first - cs <= 70, (off, first, cs)
        # (3) A (10 dk) F.older'da, B (120 dk) F.open'da; A bitince sorgusu B'yi iptal gösteriyor → B'nin ⛔'u aynı turda
        await _fresh(cfg)
        now = dbm.now()
        sa = now - 900
        sb = sa + 47
        spec, st = [(sa, 10, 900_000), (sb, 120, 4_000_000)], {}
        col = Col(lambda: _hist(spec, st))
        bot = Bot()
        notifier = Notifier(cfg, bot)
        await _play(cfg, notifier, col, [(sa + 1, _slice_sz(900_000, 10))])
        tf.F.known.pop(KEY, None)
        await _play(cfg, notifier, col, [(sb + 1, _slice_sz(4_000_000, 120))])
        assert tf.F.open[KEY]["started"] == sb and set(tf.F.older[KEY]) == {sa} and len(bot.sent) == 4
        t1 = sa + 600 + 31
        tf.F.open[KEY]["lookup_ts"], tf.F.open[KEY]["last_ts"] = t1 - 40, t1 - 10
        tf.F.older[KEY][sa]["lookup_ts"], tf.F.older[KEY][sa]["last_ts"] = t1 - 40, t1 - 10
        st[sa], st[sb] = "finished", "terminated"
        assert await tf.lifecycle(cfg, notifier, col, t1) == 1
        assert "TWAP iptal edildi" in bot.sent[-2][1] and "plan $4.0M" in bot.sent[-2][1], bot.sent[-2:]
        assert KEY not in tf.F.known
    asyncio.run(run())
    print("✅ evre modeli incelemesi) yeniden sorgu olağan dilimi korur; atlanan alt emir sayılır; kardeşin ⛔'u aynı turda")


def test_saturday_base_loaded_for_excluded_runs():
    async def run():
        cfg = _cfg()
        await _fresh(cfg)
        now = dbm.now()
        asked = []
        orig_m, orig_e = tl.sat_missing, tl.ensure_sat

        async def ens(cfg_, client, coins, ts, max_fetch=12):
            asked.extend(coins)
            return 0
        tl.sat_missing, tl.ensure_sat = (lambda *a, **k: True), ens
        try:
            for i in range(13):
                tl.observe("PUMP", A, "buy", PX, 8_000, 40, now - 400 + 30 * i, 1, True)
            out = await tl.evaluate(cfg, Notifier(cfg, Bot()), None, Col(lambda: []))
            assert out["cands"] == 0 and asked == ["PUMP"], (out.get("cands"), asked)
        finally:
            tl.sat_missing, tl.ensure_sat = orig_m, orig_e
    asyncio.run(run())
    print("✅ Cumartesi) taban yüzünden elenen dizinin coin'i için taban yine okunur (kısıt geçici)")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
