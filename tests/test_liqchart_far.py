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
        # band tek pozisyonluysa satır POZİSYONun kendisidir (adres + kaldıraç), "band" soyutlaması yok
        assert "liq bantları" in txt and "🟢 LONG <b>$20K</b>" in txt and "bandı" not in txt, txt
        assert "👤" in txt and txt.count("🟢 LONG <b>$20K</b>") == 1 and "⭐" in txt, "tek satır, tekrar yok"
        assert "Büyük tekler" not in txt and "1 pozisyon %50'den uzak" in txt, txt
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


MARKH = 0.00888


class ClientH:
    async def meta_and_ctxs(self, dex=""):
        return [{"universe": [{"name": "HEMI"}]},
                [{"markPx": str(MARKH), "openInterest": str(1.95e6 / MARKH), "funding": "0", "dayNtlVlm": "4450000"}]]

    async def l2_book(self, coin, n_sig_figs=None):
        return {"levels": [[{"px": str(MARKH * 0.995), "sz": "1e8"}, {"px": str(MARKH * 0.98), "sz": "1e8"}],
                           [{"px": str(MARKH * 1.005), "sz": "1e8"}, {"px": str(MARKH * 1.02), "sz": "1e8"}]]}

    async def candles(self, coin, interval, start_ms, end_ms):
        return [{"t": c["t"] * 1000, "o": str(c["o"]), "h": str(c["h"]), "l": str(c["l"]), "c": str(c["c"]), "v": "1"}
                for c in candles(MARKH)]


def test_hemi_clusters():
    """Pozisyon tavanlı coin: ≥ $500K tek pozisyon yok; toz ($136) başlığa çıkmaz, %17-19 aşağıdaki
    40 küçük pozisyonun kümesi ($210K) başlık olur; grafik bandı çizer."""
    async def run():
        from app.radar import cryptoliq, liqmap
        from app.telegram import format as fmt
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "hemi.db"))
        cfg = Config()
        cfg.max_liq_distance_pct, cfg.crypto_liq_min_usd = 50.0, 500_000
        t = dbm.now()
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HEMI": {"m": MARKH, "oi": 1.95e6 / MARKH, "v": 4.45e6}}, "ts": t})
        rows = []
        for i in range(40):                                   # $5.25K × 40 = $210K, liq 0.0072–0.0074
            rows.append(("0x" + f"{i:040x}", "long", 5_250.0, 0.0072 + 0.0002 * i / 39))
        rows += [("0x" + "e" * 40, "long", 136.0, 0.00879), ("0x" + "f" * 40, "long", 388.0, 0.00870),
                 ("0x" + "d" * 40, "short", 3_000.0, 0.0090)]
        async with dbm.db() as c:
            for addr, side, ntl, liq in rows:
                await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", ("HEMI", addr, "", side, 1, MARKH, 2, liq, 0, ntl, t))
        # saf küme seçimi
        cands = [{"side": s, "notional": n, "liq_px": l, "dist": abs(MARKH - l) / MARKH * 100} for _a, s, n, l in rows if n >= 1950]
        cl = liqmap.clusters(cands, MARKH, 50)
        assert cl[0]["side"] == "long" and abs(cl[0]["total"] - 210_000) < 1 and cl[0]["n"] == 40, cl[0]
        assert abs(cl[0]["px_lo"] - 0.0072) < 1e-9 and abs(cl[0]["px_hi"] - 0.0074) < 1e-9 and cl[0]["px"] == cl[0]["px_hi"]
        assert 16.6 < cl[0]["dist_lo"] < 16.8 and 18.8 < cl[0]["dist_hi"] < 19.0 and len(cl[0]["top"]) == 3
        assert cl[1]["side"] == "short" and cl[1]["total"] == 3_000 and len(cl) == 2
        # anlık görüntü uçtan uca
        s = await cryptoliq.snapshot(cfg, ClientH(), "HEMI")
        longb = max((c for c in s["clusters"] if c["side"] == "long"), key=lambda c: c["total"])
        assert longb["n"] == 40 and s["main_band"] is longb and s["n_dust"] == 2 and abs(s["dust"] - 1_950) < 1, (s["n_dust"], s["dust"])
        dl = [c["dist_lo"] for c in s["clusters"]]
        assert dl == sorted(dl) and s["clusters"][0]["total"] == 136, "mesajda bantlar mesafeye göre (toz long %0.9 önce)"
        # toz bantlara DAHİL (kendi kovalarında $136/$388'lık ayrı bantlar), tek listesinde yok
        assert sum(c["n"] for c in s["clusters"]) == 43 and all(c["total"] >= 100 for c in s["clusters"]), s["clusters"]
        assert s["rows"][0]["notional"] == 5_250 and all(r["notional"] >= 1_950 for r in s["rows"]) and s["n_big"] == 0
        txt = fmt.crypto_liq_snapshot(s)
        assert "LONG bandı <b>$210K</b>" in txt and "40 pozisyon ⭐" in txt and "altta" in txt and "En büyük tekler" in txt, txt
        assert "$136" in txt and "2 toz pozisyon" in txt and "bantlarda var" in txt and "zorunlu <b>SATIŞ</b> ~$211K" in txt, txt
        assert "👤" not in txt.split("En büyük tekler")[0], "tek listesi bantlardan sonra; tozlar tek listesinde yok"
        assert all(("$136" not in ln and "$388" not in ln) for ln in txt.splitlines() if "👤" in ln), txt
        if s["png"] is not None:
            assert s["png"][:8] == b"\x89PNG\r\n\x1a\n"
            sp = os.environ.get("HLR_PNG_OUT")
            if sp:
                open(os.path.join(sp, "hemi_cluster.png"), "wb").write(s["png"])
        # tek büyük pozisyon varsa eski davranış (küme yok)
        async with dbm.db() as c:
            await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", ("HEMI", "0x" + "1" * 40, "", "long", 1, MARKH, 2, 0.0085, 0, 600_000.0, t))
        s2 = await cryptoliq.snapshot(cfg, ClientH(), "HEMI")
        assert s2["clusters"] and s2["rows"][0]["notional"] == 600_000 and s2["n_big"] == 1
        t2 = fmt.crypto_liq_snapshot(s2)
        # tek pozisyonluk band ARTIK pozisyonun kendisi olarak yazılır (adres + kaldıraç),
        # "Büyük tekler" başlığı yalnız bandına girmeyen tekler kalınca çıkar
        assert "liq bantları" in t2 and "👤" in t2 and "Büyük tekler" not in t2, t2
        # render: küme bandı başlık ve etiket
        lv = [{"px": 0.0074, "px_lo": 0.0072, "px_hi": 0.0074, "side": "long", "notional": 210_000, "dist": 16.7, "main": True,
               "cluster": True, "n": 40}]
        p = liqchart.plan_levels(candles(MARKH), MARKH, lv, None, 50)
        assert p["lo"] < 0.0072 and p["draw"][0]["cluster"] is True
        png = liqchart.render("HEMI", candles(MARKH), MARKH, lv)
        assert png is None or png[:8] == b"\x89PNG\r\n\x1a\n"
        assert "Toz değil küme" in open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
        print("✅ HEMI) toz elenir; 40 pozisyonluk $210K küme başlık; grafik bandı; tek büyük varsa eski davranış")
    asyncio.run(run())


def test_pump_near_band_beats_far_single():
    """PUMP vakası: %19'da $986K ve %24'te $5.2M tekler varken %5,4-6,3'teki 30 küçük pozisyonluk
    $1.1M band (tek kova: %5-7,5) ana başlık olur (en büyük bandın %10'undan büyük ve %10 içinde)."""
    async def run():
        from app.radar import cryptoliq
        from app.telegram import format as fmt
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "pumpband.db"))
        cfg = Config()
        cfg.max_liq_distance_pct, cfg.crypto_liq_min_usd = 50.0, 500_000
        t = dbm.now()
        M = 0.00443
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": M, "oi": 60e6 / M, "v": 1e8}}, "ts": t})
        rows = [("0x" + f"{i:040x}", "long", 36_000.0, 0.00415 + 0.00001 * (i % 5)) for i in range(30)]   # $1.08M @ %5,4-6,3 (tek kova)
        rows += [("0x" + "a" * 40, "long", 986_000.0, 0.00358), ("0x" + "b" * 40, "long", 5_200_000.0, 0.00336)]
        async with dbm.db() as c:
            for addr, side, ntl, liq in rows:
                await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                                " VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)", ("PUMP", addr, "", side, 1, M, 3, liq, 0, ntl, t))

        class Cli(ClientH):
            async def meta_and_ctxs(self, dex=""):
                return [{"universe": [{"name": "PUMP"}]},
                        [{"markPx": str(M), "openInterest": str(60e6 / M), "funding": "0", "dayNtlVlm": "1e8"}]]

            async def candles(self, coin, interval, start_ms, end_ms):
                return [{"t": c["t"] * 1000, "o": str(c["o"]), "h": str(c["h"]), "l": str(c["l"]), "c": str(c["c"]), "v": "1"}
                        for c in candles(M)]
        s = await cryptoliq.snapshot(cfg, Cli(), "PUMP")
        mb = s["main_band"]
        assert mb and mb["n"] == 30 and abs(mb["total"] - 1_080_000) < 1 and mb["dist_lo"] < 6, mb
        assert [r["notional"] for r in s["rows"]] == [986_000.0, 5_200_000.0] and s["n_big"] == 2, "büyük tekler yakından uzağa"
        assert [c["side"] for c in s["clusters"]] == ["long", "long", "long"] and s["clusters"][0] is mb
        txt = fmt.crypto_liq_snapshot(s, offers=[74, 75])
        assert "LONG bandı <b>$1.1M</b>" in txt and "30 pozisyon ⭐" in txt and "$5.2M" in txt, txt
        # uzak büyük tekler kendi bantları olarak, sahipleriyle birlikte (tekrar yok, /takip band satırında)
        assert txt.count("$986K") == 1 and txt.count("$5.2M") == 1, txt
        assert "/takip_74" in txt and "/takip_75" in txt and "Büyük tekler" not in txt, txt
        assert s["cascade"] is None or s["cascade"].get("direction") == "down"
        if s["png"]:
            assert s["png"][:8] == b"\x89PNG\r\n\x1a\n"
            sp = os.environ.get("HLR_PNG_OUT")
            if sp:
                open(os.path.join(sp, "pump_band.png"), "wb").write(s["png"])
        print("✅ PUMP) yakın $1.1M band ana başlık; uzak $986K/$5.2M tekler kendi bantlarında (adres + /takip, tekrar yok)")
    asyncio.run(run())
