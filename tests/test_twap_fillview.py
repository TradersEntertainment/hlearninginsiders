"""📋 Web TWAP tabloları: Doldu / Kalan / Durum yalnız ölçülen — "%0" yok; süresi geçmiş kayıt HL'den kapatılır.

Kullanıcı (18.10, coin sayfası ekran görüntüsü): bitmiş emirler "Doldu %0 · Kalan $2.99M · 🏁 bitti". Kök neden (canlı):
HL emir verildiği anki 'activated' kaydında dolanı HEP 0 yazar; gerçek dolan yalnız 'finished'/'terminated' kaydında.
Tur bellekten düşünce (restart / 4 sa pencere) DB satırı o 0'da takılı kalıyordu; web şablonları 0'ı ölçüm sayıyordu.

Kararlar: tek yer `twap.fill_view` (coin sayfası, /twaplar, /twap); `twaplive.close_stale` süresi geçmiş 'activated'
satırların HL bitişini WS'ten çeker; coin sayfasının "📡 oluşuyor" satırları en az LIVE_ROW_MIN dilim ya da HL emri.

Pinlenenler:
  • HL bitiş: yüzde HL'den ("%100", "%0.4", "<%0.1"), dolmadıysa "dolmadı"; iptalde kalan = dolmayan; 🏁 yalnız 'finished'
  • 'activated' (HL 0): "bilinmiyor"; süresi geçtiyse "⌛ süresi doldu", geçmediyse "⏸ dilim gelmiyor"; emir yok → "⏹"
  • canlı satır: radarın gördüğü HL dilimleri "≈ %N" (kalan ≈ plan − dolan) / "≥ $X"
  • close_stale: eşleşen bitiş kaydıyla günceller, bulamazsa lookup_ts (yeniden deneme bekler), CLOSE_EVERY'de bir
  • üç şablonda "%0" basan eski ifade yok; /tani satırı; evaluate kancası; README
"""
import asyncio
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twap-fillview.db")
from app import db as dbm  # noqa: E402
from app.radar import twap, twaplive as tl  # noqa: E402

A, B = "0x" + "a" * 40, "0x" + "b" * 40


def test_fill_view_measured_only():
    t = 1_800_000_000
    v = twap.fill_view
    r = v({"planned_usd": 2.99e6, "executed_usd": 2.99e6, "order_status": "finished"}, t)
    assert (r["fill_txt"], r["left_txt"], r["state_txt"]) == ("%100", "—", "🏁 bitti"), r
    r = v({"planned_usd": 1e6, "executed_usd": 4_000, "order_status": "terminated", "remaining_usd": 996_000}, t)
    assert (r["fill_txt"], r["left_txt"], r["state"]) == ("%0.4", "$996K", "cancelled"), r
    r = v({"planned_usd": 1e6, "executed_usd": 300, "order_status": "error", "remaining_usd": 999_700}, t)
    assert r["fill_txt"] == "<%0.1" and r["state_txt"] == "⛔ iptal", r
    r = v({"planned_usd": 1e6, "executed_usd": 0, "order_status": "terminated", "remaining_usd": 1e6}, t)
    assert r["fill_txt"] == "dolmadı" and r["left_txt"] == "$1.0M", r
    # ekran görüntüsündeki satır: 'activated' (HL 0), dilimler durmuş, süre geçmiş
    r = v({"planned_usd": 2.99e6, "executed_usd": 0, "remaining_usd": 2.99e6, "order_status": "activated",
           "order_ts": t - 7200, "order_min": 60, "running": False}, t)
    assert (r["fill_txt"], r["left_txt"], r["state_txt"]) == ("bilinmiyor", "—", "⌛ süresi doldu"), r
    r = v({"planned_usd": 2.99e6, "executed_usd": 0, "order_status": "activated", "order_ts": t - 600,
           "order_min": 60, "running": False}, t)
    assert r["state_txt"] == "⏸ dilim gelmiyor" and r["fill_txt"] == "bilinmiyor", r
    r = v({"planned_usd": 2.99e6, "executed_usd": 0, "order_status": "activated", "running": True}, t)
    assert r["state_txt"] == "▶ sürüyor" and r["fill_txt"] == "bilinmiyor"
    r = v({"planned_usd": 1e6, "executed_usd": 380_000, "remaining_usd": 620_000, "order_status": "activated",
           "running": True}, t)                         # HL kaydı dolanı yazmışsa ölçümdür (fill_amount ile aynı)
    assert (r["fill_txt"], r["left_txt"]) == ("%38", "$620K"), r
    r = v({"total": 50_000, "running": False}, t)
    assert r["fill_txt"] == "—" and r["state_txt"] == "⏹ dizi durdu", r
    # canlı satır: radarın gördüğü HL dilimleri
    r = v({"planned_usd": 1e6, "planned_sz": 100, "order_status": "activated", "fill_hl0": True, "fill_seen": 710_000,
           "fill_full": True, "fill_seen_sz": 71, "forming": True}, t)
    assert (r["fill_txt"], r["left_txt"], r["state_txt"]) == ("≈ %71", "≈ $290K", "📡 oluşuyor"), r
    r = v({"planned_usd": 1e6, "order_status": "activated", "fill_hl0": True, "fill_seen": 120_000, "fill_full": False}, t)
    assert r["fill_txt"] == "≥ $120K" and r["left_txt"] == "—", r
    r = v({"planned_usd": 1e6, "order_status": "activated", "fill_hl0": True, "fill_seen": None}, t)
    assert r["fill_txt"] == "bilinmiyor"
    print("✅ görünüm) HL bitişi → HL rakamı; 'activated' 0 → bilinmiyor; canlı → ≈/≥; 🏁 yalnız HL 'finished'; %0 yok")


class _Col:
    connected = True

    def __init__(self, hist):
        self.hist, self.asked = hist, []

    async def fetch_twap_history(self, addr):
        self.asked.append(addr)
        return self.hist.get(addr)


def _rec(coin, side, t0, minutes, sz, ex_sz, ex_ntl, status, tm, twap_id=1):
    return {"time": tm, "twapId": twap_id, "status": {"status": status},
            "state": {"coin": coin, "side": side, "sz": str(sz), "executedSz": str(ex_sz), "executedNtl": str(ex_ntl),
                      "minutes": minutes, "timestamp": t0 * 1000, "reduceOnly": False, "randomize": True}}


def test_close_stale_from_hl_history():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cl.db"))
        t = dbm.now()
        async with dbm.db() as c:
            for addr, coin, side, t0 in ((A, "PUMP", "sell", t - 7200), (B, "BTC", "buy", t - 9000)):
                await c.execute(
                    "INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,planned_usd,planned_sz,"
                    "executed_usd,remaining_usd,order_ts,order_min,order_status,src) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (coin, addr, side, t0 + 5, t0 + 3500, 11, 2.98e6, 2.99e6, 1000.0, 0.0, 2.99e6, t0, 60, "activated",
                     "live"))
            await c.commit()
        hist = {A: [_rec("PUMP", "A", t - 7200, 60, 1000, 0, 0, "activated", t - 7200),
                    _rec("PUMP", "A", t - 7200, 60, 1000, 1000, 2.98e6, "finished", t - 3600)],
                B: [_rec("ETH", "B", t - 9000, 60, 5, 5, 1e4, "finished", t - 5400, 9)]}   # B: eşleşen kayıt yok
        col = _Col(hist)
        tl.CLOSE.update(last=0, asked=0, fixed=0, miss=0)
        assert await tl.close_stale(col, t) == 1 and sorted(col.asked) == sorted([A, B])
        rows = {r["address"]: r for r in await twap.all_orders(hours=0)}
        a, b = rows[A], rows[B]
        assert (a["fill_txt"], a["left_txt"], a["state_txt"]) == ("%100", "—", "🏁 bitti"), a
        assert a["order_status"] == "finished" and round(a["executed_usd"]) == 2_980_000
        assert b["order_status"] == "activated" and b["fill_txt"] == "bilinmiyor" and b["state_txt"] == "⌛ süresi doldu"
        # sıklık: CLOSE_EVERY dolmadan yeniden taramaz; B CLOSE_RETRY dolmadan yeniden sorulmaz
        col.asked.clear()
        assert await tl.close_stale(col, t + 10) == 0 and not col.asked
        assert await tl.close_stale(col, t + tl.CLOSE_EVERY) == 0 and not col.asked, "B lookup_ts: bekler"
        assert await tl.close_stale(col, t + tl.CLOSE_RETRY + 1) == 0 and col.asked == [B]
        # kollektör bağlı değilse hiç sormaz
        tl.CLOSE["last"] = 0
        off = _Col(hist)
        off.connected = False
        assert await tl.close_stale(off, t + 5000) == 0 and not off.asked
        assert "1 satır HL bitiş kaydıyla güncellendi" in tl.close_diag()
    asyncio.run(run())
    print("✅ kapatma) süresi geçmiş 'activated' satır HL bitiş kaydıyla güncellenir; bulunamayan bekler; sık sormaz")


def test_live_rows_and_templates():
    tl.REG.runs.clear()
    t = dbm.now()
    for i in range(2):                                   # 2 işlemlik tur: dizi değil → listelenmez
        tl.REG.observe("PUMP", A, "buy", 0.004, 1000, 4, t - 60 + i)
    for i in range(4):
        tl.REG.observe("PUMP", B, "sell", 0.004, 1000, 4, t - 120 + i * 30)
    rows = tl.live_runs("PUMP")
    assert [r["address"] for r in rows] == [B] and rows[0]["state_txt"] == "📡 oluşuyor" and rows[0]["fill_txt"] == "—"
    tl.REG.runs.clear()
    bad = re.compile(r"'%\.0f'\|format\(r\.(filled_pct|order_pct)\)")
    for p in ("coin.html", "twaplar.html", "twap.html"):
        src = open(os.path.join(ROOT, "app/web/templates", p), encoding="utf-8").read()
        assert not bad.search(src), p
        assert "r.fill_txt" in src and ("r.state_txt" in src), p
    diag = open(os.path.join(ROOT, "app/diag.py"), encoding="utf-8").read()
    assert "close_diag()" in diag
    src = open(os.path.join(ROOT, "app/radar/twaplive.py"), encoding="utf-8").read()
    assert 'out["closed"] = await close_stale(collector, ts)' in src
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    assert "twap.fill_view" in readme and "twaplive.close_stale" in readme and "⌛ süresi doldu" in readme
    print("✅ canlı satır + şablonlar) tek işlem dizi değil; üç şablonda %0 basan ifade yok; /tani; kanca; README")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
