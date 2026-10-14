"""Süren TWAP'ın DOLANI — HL'nin 'activated' kaydı yerleştirme anının görüntüsüdür (executedSz / executedNtl HEP 0).

Kullanıcı (14.10): "4. HYPE 🔴 SATIŞ · plan $1.6M · doldu %0 · 45 dk kaldı · 0xedcd..d05a — bu niye gelmedi". HL
twapHistory: aynı emrin 'activated' kaydı executedSz "0.0"; userTwapSliceFills: 107 dk'da 14.285,71 HYPE ≈ $1,17M (%71).
HL dolanı yalnız bitiş / iptal kaydında yazar. Süren emrin dolanı radarın gördüğü HL alt emri dolumlarından ölçülür.

Pinlenenler:
  • dilimler emrin başından beri görülüyorsa "doldu ≈%71" (liste) / "doldu ≈ $1.17M (%71) · kalan ≈ …" (mesaj)
  • radar emrin başını görmediyse ALT SINIR ("≥"); dilim bölümü emirden önce başladıysa (önceki emirle karışık) ya da
    coin dinlenmiyorsa dolan YAZILMAZ ("%0" asla) — tahmin yok
  • bitmiş / iptal kaydı (HL dolanı yazar) aynen; kapı (kalan kuralı) HL kaydıyla bugünkü gibi
  • 👁 takip: yarılanma notu radarın gördüğü dolanla (yalnız baştan beri görülüyorsa)
"""
import asyncio
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twapfill.db")
from app import db as dbm  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A = "0xedcdcaa1f18350c50c10bef860e64daa9785d05a"
PX = 81.56


def hl_order(started, status="activated", ex_sz=0.0, sz=20000.0, minutes=150):
    return {"time": started, "twapId": 2314331, "status": {"status": status},
            "state": {"coin": "HYPE", "user": A, "side": "A", "sz": str(sz), "executedSz": str(ex_sz),
                      "executedNtl": str(ex_sz * PX), "minutes": minutes, "reduceOnly": False, "randomize": False,
                      "timestamp": started * 1000}}


def slices(t0, n, usd=5470.0, gap=30, native=True):
    for i in range(n):
        tl.observe("HYPE", A, "sell", PX, usd / PX, usd, t0 + i * gap, 1, native)


def test_running_order_fill_measured():
    tl.REG.clear()
    now = dbm.now()
    started = now - 107 * 60
    o = tl.parse_twap_orders([hl_order(started)], "HYPE", "sell", PX, now)[0]
    assert o["status"] == "activated" and o["executed_usd"] == 0 and o["filled_pct"] == 0, "HL: yerleştirme görüntüsü"
    # coin dinlenmiyor / dilim yok → dolan yazılmaz ("%0" değil)
    w = tl.with_seen(o, "HYPE", A, "sell", now)
    assert w["fill_hl0"] and w["fill_seen"] is None
    assert fmt.fill_amount(w)[2] == "none" and "%0" not in fmt._fill_line(w)
    e = {"coin": "HYPE", "side": "sell", "planned_usd": o["planned_usd"], "fill_hl0": True, "fill_seen": None,
         "fill_full": False, "filled_pct": 0.0, "minutes": 150, "left_sec": 2580}
    assert "doldu" not in fmt._live_row(4, e)
    # dilimler emrin başından beri: 214 × $5.47K ≈ $1.17M → %72
    slices(started + 2, 214)
    w = tl.with_seen(o, "HYPE", A, "sell", now)
    assert w["fill_full"] and abs(w["fill_seen"] - 214 * 5470) < 1
    line = fmt._fill_line(w)
    assert line.startswith("doldu ≈ <b>$1.2M</b> (%72) · kalan ≈ $461K"), line
    e.update(fill_seen=w["fill_seen"], fill_full=True)
    row = fmt._live_row(4, e)
    assert "doldu ≈%72" in row and "doldu %0" not in row, row
    assert fmt.fill_short(w).startswith("doldu ≈ $1.2M (%72)")
    # kapı HL kaydıyla bugünkü gibi (kalan = plan): bu düzeltme bildirim kararını DEĞİŞTİRMEZ
    assert o["remaining_usd"] == o["planned_usd"]
    print("✅ süren emir) HL 0 yazar → dolan radarın gördüğü HL dilimlerinden (≈%72); dilim yoksa yazılmaz; kapı aynı")


def test_partial_mixed_and_finished():
    tl.REG.clear()
    now = dbm.now()
    started = now - 107 * 60
    o = tl.parse_twap_orders([hl_order(started)], "HYPE", "sell", PX, now)[0]
    slices(now - 40 * 60, 80)                                  # radar emrin başını görmedi (ör. yeniden başlatma)
    w = tl.with_seen(o, "HYPE", A, "sell", now)
    assert w["fill_seen"] and not w["fill_full"]
    assert fmt._fill_line(w).startswith("gördüğümüz dolum en az <b>$438K</b> (%27) (radar emrin başını görmedi)"), fmt._fill_line(w)
    e = {"coin": "HYPE", "side": "sell", "planned_usd": o["planned_usd"], "fill_hl0": True,
         "fill_seen": w["fill_seen"], "fill_full": False, "minutes": 150}
    assert "doldu ≥%27" in fmt._live_row(1, e)
    # dilim bölümü bu emirden ÖNCE başlamış (önceki emir aralıksız) → karışık, yazılmaz
    tl.REG.clear()
    slices(started - 3600, 120 + 214)
    assert tl.with_seen(o, "HYPE", A, "sell", now)["fill_seen"] is None
    # dilimler sustu (5 dk'dan uzun) → süren dolan olarak sayılmaz
    tl.REG.clear()
    slices(started + 2, 100)
    assert tl.with_seen(o, "HYPE", A, "sell", now)["fill_seen"] is None
    # elle (sıfırsız hash) işlemler dolan sayılmaz
    tl.REG.clear()
    slices(started + 2, 214, native=False)
    assert tl.with_seen(o, "HYPE", A, "sell", now)["fill_seen"] is None
    # aynı coin + yönde aynı anda iki süren emir (son HL cevabında): dilimler ayrışmaz → yazılmaz
    tl.REG.clear()
    slices(started + 2, 214)
    two = [hl_order(started), {**hl_order(now - 600, sz=5000.0, minutes=60), "twapId": 2314999}]
    tl._LIVE_Q.clear()
    tl.live_q_put(A, now, two)
    assert tl.with_seen(o, "HYPE", A, "sell", now)["fill_seen"] is None
    tl._LIVE_Q.clear()
    tl.live_q_put(A, now, [hl_order(started)])
    assert tl.with_seen(o, "HYPE", A, "sell", now)["fill_full"]
    tl._LIVE_Q.clear()
    # bitmiş kayıt HL'nin kendi dolanını yazar → aynen
    fin = tl.parse_twap_orders([hl_order(started, "finished", ex_sz=20000.0)], "HYPE", "sell", PX, now)[0]
    assert tl.with_seen(fin, "HYPE", A, "sell", now) is fin and fmt.fill_amount(fin)[2] == "hl"
    print("✅ kenarlar) baştan görülmediyse alt sınır (≥); önceki emirle karışık / susmuş / elle işlem / eşzamanlı iki emir → yazılmaz;"
          " bitmiş kayıt HL'nin")


def test_follow_half_note_uses_measured_fill():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "f.db"))
        from app.config import Config
        from app.radar import twapfollow
        tl.REG.clear()
        now = dbm.now()
        started = now - 107 * 60
        slices(started + 2, 214)
        cfg = Config()
        cfg.twap_follow_progress = True
        sent = []

        class N:
            async def send(self, kind, text, **k):
                sent.append(text)
                return True
        async with dbm.db() as c:
            cur = await c.execute(
                "INSERT INTO twap_follows(coin,address,side,first_ts,chat_id,active,created_ts) VALUES(?,?,?,?,?,1,?)",
                ("HYPE", A, "sell", started, "111", now))
            fid = cur.lastrowid
            f = dict(await (await c.execute("SELECT * FROM twap_follows WHERE id=?", (fid,))).fetchone())
        out = {k: 0 for k in ("no_order", "failed", "cancelled", "ended", "progress")}
        await twapfollow._one(cfg, f, [hl_order(started)], now, N(), fmt, tl, out)
        assert out["progress"] == 1 and "TWAP yarılandı" in sent[-1] and "doldu ≈ <b>$1.2M</b>" in sent[-1], sent
    asyncio.run(run())
    print("✅ 👁 takip) yarılanma notu radarın gördüğü dolanla (HL süren emirde 0 yazdığı için eskiden hiç gelmiyordu)")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
