"""📉 Liq grafiğinde uzak seviyeler ekseni bozmasın (INJ vakası: +%2297 short).

Pinlenenler:
  • plan_levels: pencere = mumlar ∪ fiyat ∪ ana ∪ (2×ana mesafe, en az %10, en çok far_pct);
    pencere dışı ≤ far_pct → kenar toplu; > far_pct → görünmez; zincir hedefi dışarıdaysa kenar
  • ana seviye far_pct'den uzaksa None (grafik çizilmez); yalnız ana %37,7 → çizilir
  • _pinned_text: tek/çoklu metin; render PNG üretir ve uzak seviye eklenince ölçek değişmez
  • cryptoliq.snapshot: önce %50 içindekiler; hepsi uzaksa all_far + PNG yok; mesaj notları
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-liqfar.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.radar import liqchart  # noqa: E402

MARK = 5.662


def candles(mark=MARK, n=192, spread=0.02):
    """15 dk sıkı mumlar (±%2), t saniye artan."""
    t0 = dbm.now() - n * 900
    out, px = [], mark * (1 - spread / 2)
    for i in range(n):
        o = px
        px = px * (1 + (0.0006 if i % 3 else -0.0011))
        px = min(max(px, mark * (1 - spread)), mark * (1 + spread))
        out.append({"t": t0 + i * 900, "o": o, "h": max(o, px) * 1.001, "l": min(o, px) * 0.999, "c": px})
    return out


def lvl(px, side, ntl, main=False):
    return {"px": px, "side": side, "notional": ntl, "dist": abs(px - MARK) / MARK * 100, "main": main}


MAIN = lvl(4.907, "long", 5.5e6, main=True)          # %13,3 altta
NEAR = lvl(6.2, "short", 200_000)                    # +%9,5 → çizilir
MID = lvl(9.1, "short", 678_000)                     # +%61 → kenar toplu (far_pct 100'de)
FAR = lvl(135.72, "short", 678_000)                  # +%2297 → görünmez


def test_plan_levels():
    cs = candles()
    p = liqchart.plan_levels(cs, MARK, [MAIN, NEAR, MID, FAR], ("3.70", "x"), far_pct=50)
    assert abs(p["win"] - 26.6) < 0.2, p["win"]
    assert [x["px"] for x in p["draw"]] == [4.907, 6.2] and p["pinned"]["top"] == [] and p["pinned"]["bottom"] == []
    assert [x["px"] for x in p["omitted"]] == [9.1, 135.72], "far_pct %50: 9.1 (+%61) de görünmez"
    assert p["hi"] <= MARK * 1.30 and p["lo"] < 4.907 and p["target_pinned"] is True, (p["lo"], p["hi"])
    # far_pct 100: 9.1 kenarda toplu, 135.72 hâlâ görünmez; ölçek değişmez
    p2 = liqchart.plan_levels(cs, MARK, [MAIN, NEAR, MID, FAR], ("3.70", "x"), far_pct=100)
    assert [x["px"] for x in p2["pinned"]["top"]] == [9.1] and [x["px"] for x in p2["omitted"]] == [135.72]
    assert p2["hi"] == p["hi"] and p2["lo"] == p["lo"]
    # hedef pencere içindeyse aralığa girer
    p3 = liqchart.plan_levels(cs, MARK, [MAIN, NEAR], (5.0, "x"), far_pct=50)
    assert p3["target_pinned"] is False and p3["lo"] < 4.907
    # yalnız ana %37,7'de → çizilir; ana %60'ta → None
    p4 = liqchart.plan_levels(candles(0.2119), 0.2119, [lvl(0.13, "long", 11_000, True)], None, far_pct=50)
    assert p4 and len(p4["draw"]) == 1 and p4["win"] >= 37
    assert liqchart.plan_levels(cs, MARK, [lvl(MARK * 0.4, "long", 1e6, True)], None, far_pct=50) is None
    # alt kenar: long liq %30 altta (pencere %26,6) → bottom
    p5 = liqchart.plan_levels(cs, MARK, [MAIN, lvl(MARK * 0.70, "long", 90_000)], None, far_pct=50)
    assert [x["px"] for x in p5["pinned"]["bottom"]] == [MARK * 0.70]
    # metinler
    t1 = liqchart._pinned_text(p2["pinned"]["top"], top=True)
    assert t1 == "▲ SHORT $678K · +%61 · liq 9.100", t1
    two = liqchart.plan_levels(cs, MARK, [MAIN, MID, lvl(12.4, "short", 500_000)], None, far_pct=150)["pinned"]["top"]
    assert liqchart._pinned_text(two, top=True) == "▲ 2 short $1.2M +%61…119"
    assert liqchart._pinned_text(p5["pinned"]["bottom"], top=False).startswith("▼ LONG $90K")
    print("✅ pencere) 2×ana/%10/far_pct; kenar toplu; >far_pct görünmez; hedef; ana uzaksa None; metinler")


def test_render_scale_and_png():
    cs = candles()
    png_near = liqchart.render("INJ", cs, MARK, [MAIN, NEAR])
    png_far = liqchart.render("INJ", cs, MARK, [MAIN, NEAR, FAR], target=(3.70, "zincir → 3.7000 · $5.5M"))
    png_pin = liqchart.render("INJ", cs, MARK, [MAIN, NEAR, MID, FAR], far_pct=100)
    if png_near is None:
        print("⚠️ Pillow yok, PNG atlandı")
        return
    for b in (png_near, png_far, png_pin):
        assert b and b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) > 5000
    assert png_far != png_near and png_pin != png_far
    assert liqchart.render("INJ", cs, MARK, [lvl(MARK * 0.4, "long", 1e6, True)]) is None, "ana uzak → grafik yok"
    sp = os.environ.get("HLR_PNG_OUT")
    if sp:
        open(os.path.join(sp, "inj_far.png"), "wb").write(png_far)
        open(os.path.join(sp, "inj_pin.png"), "wb").write(png_pin)
    print("✅ render) PNG üretildi; uzak seviye/hedef ölçeği değiştirmiyor; ana uzaksa None")


class Client:
    async def meta_and_ctxs(self, dex=""):
        return [{"universe": [{"name": "PUMP"}]},
                [{"markPx": str(MARK), "openInterest": "1", "funding": "0", "dayNtlVlm": "1"}]]

    async def l2_book(self, coin, n_sig_figs=None):
        return {"levels": [[{"px": str(MARK * 0.995), "sz": "1e6"}, {"px": str(MARK * 0.98), "sz": "1e6"}],
                           [{"px": str(MARK * 1.005), "sz": "1e6"}, {"px": str(MARK * 1.02), "sz": "1e6"}]]}

    async def candles(self, coin, interval, start_ms, end_ms):
        return [{"t": c["t"] * 1000, "o": str(c["o"]), "h": str(c["h"]), "l": str(c["l"]), "c": str(c["c"]), "v": "1"}
                for c in candles()]


def test_snapshot_prefers_near():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "liqfar.db"))
        cfg = Config()
        cfg.max_liq_distance_pct = 50.0
        cfg.crypto_liq_min_usd = 500_000
        t = dbm.now()
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": MARK, "oi": 1000.0}}, "ts": t})
        A, B = "0x" + "a" * 40, "0x" + "b" * 40
        async with dbm.db() as c:
            for addr, side, ntl, liq in ((A, "long", 20_000.0, MARK * 0.97), (B, "short", 900_000.0, 135.72)):
                await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", ("PUMP", addr, "", side, 1, MARK, 3, liq, 0, ntl, t))
        from app.radar import cryptoliq
        from app.telegram import format as fmt
        s = await cryptoliq.snapshot(cfg, Client(), "PUMP")
        assert [r["address"] for r in s["rows"]] == [A], s["rows"]            # uzak büyük short başa geçmez
        assert s["all_far"] is False and s["n_far"] == 1 and s["n_big"] == 0 and s["n_all"] == 2
        txt = fmt.crypto_liq_snapshot(s)
        assert "≥ $500K pozisyon yok — en yakın küçükler" in txt and "1 pozisyon %50'den uzak" in txt, txt
        assert s["png"] is None or s["png"][:8] == b"\x89PNG\r\n\x1a\n"
        # yalnız uzak: eski davranış (en yakın uzaklar) + all_far + grafik yok
        async with dbm.db() as c:
            await c.execute("DELETE FROM addr_positions WHERE address=?", (A,))
        s2 = await cryptoliq.snapshot(cfg, Client(), "PUMP")
        assert [r["address"] for r in s2["rows"]] == [B] and s2["all_far"] is True and s2["png"] is None, s2["rows"]
        txt2 = fmt.crypto_liq_snapshot(s2)
        assert "%50 içinde pozisyon yok — en yakın uzaklar (grafik çizilmez)" in txt2 and "uzak (listede" not in txt2, txt2
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "far_pct=_far_pct(cfg)" in rd("app", "radar", "cryptoliq.py") and "Uzak seviyeler ekseni bozmaz" in rd("README.md")
        print("✅ anlık) önce %50 içindekiler; uzak sayısı notu; hepsi uzaksa 'en yakın uzaklar' + grafik yok")
    asyncio.run(run())
