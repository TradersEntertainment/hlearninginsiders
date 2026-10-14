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
        s0 = now - 700
        col = Col(lambda: [twap_order(s0, minutes=30, plan=600_000)])   # HL geçmişinde yalnız bilinen emir
        bot = Bot()
        notifier = Notifier(cfg, bot)
        x = _slice_sz(600_000, 30)
        await _play(cfg, notifier, col, [(s0 + 1, x)])
        assert len(col.calls) == 1 and KEY in tf.F.known, "emir okundu (kapı dışı: dakika hızı düşük), anahtar susturuldu"
        # 10 dk boyunca 15 sn'de bir dilim (tempo iki emir gibi) ama sorgu hep bilinen emri buluyor
        ev = [(s0 + 1 + 15 * i, x) for i in range(2, 42)]
        await _play(cfg, notifier, col, ev)
        # aralık 60 → 120 → 240 → 480: 10 dk'da en çok 3 yeniden sorgu (eskiden ~10)
        assert 1 <= tf.F.stats["relook"] <= 3 and len(col.calls) == 1 + tf.F.stats["relook"], \
            (tf.F.stats["relook"], col.calls)
        assert tf.F.kslices[KEY]["gap"] > tf.RELOOK_MIN_GAP
        assert bot.sent == [], "bilinen emir yeniden değerlendirilir ama yazılmaz"
    asyncio.run(run())
    print("✅ yeniden sorgu) yeni emir bulamazsa aralık katlanır")


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
            out = {}
            assert tl._slice_could_pass(cfg, ok, now, out) and out["early"] == 1, "tabanın üstü sorulur"
        finally:
            tl.sat_missing = orig
        # #5: /tani
        from app import diag
        await dbm.kv_set("twaplive_stats", {"ts": now, "keys": 3, "cands": 1, "early": 2})
        text = "\n".join(await diag._subsystems(cfg))
        assert "2 erken sorgu" in text, [ln for ln in text.splitlines() if "canlı twap" in ln]
    asyncio.run(run())
    print("✅ eski yol) önbellek dizinin ilk alt emrinden eskiyse yeniden sorulur; Cumartesi taban altı dilim sorulmaz;"
          " /tani erken sorgu")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
