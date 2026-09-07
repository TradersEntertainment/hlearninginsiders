"""📏 Kapsama — havuz / HL OI yüzdesi (sayfa, PNG, mesaj, /tani).

Pinlenenler:
  • pct: oran; OI yoksa None (≠ %0)
  • HIP-3 (para:ANSEM): positions_current toplamları / asset_metrics OI; scans sayımı; over=False
  • ana dex (HYPE): addr_positions açık satırlar / main_dex_ctx oi×m; kapalı satır sayılmaz; %100 üstü over
  • OI yoksa pct None, txt boş, coverage_ctx None
  • overview: OI ≥ $250K, en düşük önce, ortanca
  • liqchart.render(coverage_txt=…) PNG; format alarm/anlık mesajı kapsama satırı; /tani satırı
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-coverage.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.radar import coverage as cov  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

ADDR = "0x" + "a" * 40


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cov.db"))
    t = dbm.now()
    async with dbm.db() as c:
        for coin, dex, sym in (("para:ANSEM", "para", "ANSEM"), ("xyz:TSLA", "xyz", "TSLA"), ("xyz:SNDK", "xyz", "SNDK")):
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
        # asset_metrics: ANSEM oi 10M birim × 0.2 = $2M; TSLA 10K × 500 = $5M; SNDK küçük ($100K)
        for coin, mark, oi in (("para:ANSEM", 0.2, 10_000_000), ("xyz:TSLA", 500.0, 10_000), ("xyz:SNDK", 100.0, 1_000)):
            await c.execute("INSERT INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume) VALUES(?,?,?,?,?,?)",
                            (coin, t - 60, mark, oi, 0.0, 1.0))
            await c.execute("INSERT INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume) VALUES(?,?,?,?,?,?)",
                            (coin, t - 7200, mark * 2, oi * 5, 0.0, 1.0))     # eski satır — MAX(ts) kazanmalı
        rows = [("para:ANSEM", "0x" + "1" * 40, "long", 200_000.0), ("para:ANSEM", "0x" + "2" * 40, "long", 40_000.0),
                ("para:ANSEM", "0x" + "3" * 40, "short", 100_000.0),
                ("xyz:TSLA", "0x" + "4" * 40, "long", 4_000_000.0), ("xyz:TSLA", "0x" + "5" * 40, "short", 500_000.0),
                ("xyz:SNDK", "0x" + "6" * 40, "long", 50_000.0)]
        for coin, addr, side, ntl in rows:
            await c.execute("INSERT INTO positions_current(coin,address,ts,side,szi,notional,liq_px) VALUES(?,?,?,?,?,?,?)",
                            (coin, addr, t - 30, side, 1, ntl, 0.1))
        await c.execute("INSERT INTO scans(coin,ts,n_addrs,n_found) VALUES('para:ANSEM',?,312,41)", (t - 10,))
        # ana dex: HYPE oi 50K × 40 = $2M; açık long 300K, kapalı long 1M (sayılmaz), açık short 2.5M (→ %125)
        for addr, side, ntl, closed in (("0x" + "7" * 40, "long", 300_000.0, None), ("0x" + "8" * 40, "long", 1_000_000.0, t - 5),
                                        ("0x" + "9" * 40, "short", 2_500_000.0, None)):
            await c.execute("INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,ts,closed_ts)"
                            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("HYPE", addr, "", side, 1, 40, 5, 30, 0, ntl, t - 30, closed))
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HYPE": {"m": 40.0, "oi": 50_000}}, "ts": t})
    return t


def test_pct_and_coverage():
    async def run():
        t = await _fresh()
        assert cov.pct(50, 200) == 25 and cov.pct(1, 0) is None and cov.pct(None, 5) == 0 and cov.pct("x", 5) is None
        c = await cov.coverage("para:ANSEM", "equity")
        assert c["long"] == 240_000 and c["short"] == 100_000 and c["oi_ntl"] == 2_000_000, c
        assert c["pct_long"] == 12 and c["pct_short"] == 5 and c["n"] == 3 and c["over"] is False
        assert c["scan"] == {"ts": t - 10, "n_addrs": 312, "n_found": 41} and c["latest_ts"] == t - 30
        assert cov.txt(c) == "kapsama L %12 · S %5"
        # sayfa özeti verilirse OI oradan (ikinci okuma yok)
        c2 = await cov.coverage("para:ANSEM", "equity", summ={"oi_ntl": 4_000_000})
        assert c2["pct_long"] == 6 and c2["oi_ntl"] == 4_000_000
        # ana dex: kapalı satır sayılmaz, %125 → over
        h = await cov.coverage("HYPE", "crypto")
        assert h["long"] == 300_000 and h["short"] == 2_500_000 and h["oi_ntl"] == 2_000_000, h
        assert h["pct_long"] == 15 and h["pct_short"] == 125 and h["over"] is True and h["scan"] is None
        h2 = await cov.coverage("HYPE", "crypto", ctx={"c": {"HYPE": {"m": 40.0, "oi": 100_000}}})
        assert h2["oi_ntl"] == 4_000_000, "verilen ctx kv'yi ezer"
        # OI yok → oran yok (≠ %0), metin boş
        n = await cov.coverage("xyz:NOPE", "equity")
        assert n["pct_long"] is None and n["pct_short"] is None and n["over"] is False and cov.txt(n) == ""
        assert fmt.coverage_ctx(n) is None and fmt.coverage_ctx(None) is None
        assert (await cov.coverage("NOPE", "crypto"))["oi_ntl"] is None
        # overview: SNDK ($100K OI) taban altı; en düşük ANSEM (5) sonra TSLA (10); ortanca 7.5
        ov = await cov.overview(limit=5)
        assert [r["symbol"] for r in ov["worst"]] == ["ANSEM", "TSLA"] and ov["n"] == 2 and ov["median"] == 7.5, ov
        assert ov["worst"][0]["dex"] == "para" and ov["worst"][1]["pct_long"] == 80 and ov["worst"][1]["pct_short"] == 10
        print("✅ kapsama) HIP-3 ve ana dex oranları, kapalı satır dışarıda, %100 üstü ⚠️, OI yoksa None, overview sırası")
    asyncio.run(run())


def test_png_message_diag():
    async def run():
        t = await _fresh()
        cfg = Config()
        cfg.telegram_chat_id = "111"
        # PNG alt başlığı
        from app.radar import liqchart
        cands = []
        px = 0.2
        for i in range(60):
            o = px
            px = px * (1 + (0.002 if i % 3 else -0.003))
            cands.append({"t": t - (60 - i) * 900, "o": o, "h": max(o, px) * 1.001, "l": min(o, px) * 0.999, "c": px})
        levels = [{"px": 0.13, "side": "long", "notional": 11_000, "dist": 37.7, "main": True}]
        png = liqchart.render("para:ANSEM", cands, 0.2119, levels, coverage_txt="kapsama L %12 · S %5")
        png0 = liqchart.render("para:ANSEM", cands, 0.2119, levels)
        assert (png is None and png0 is None) or (png and png0 and png[:8] == b"\x89PNG\r\n\x1a\n" and png != png0)
        # mesajlar
        c = await cov.coverage("para:ANSEM", "equity")
        assert fmt.coverage_ctx(c) == "kapsama long %12 · short %5 (havuz / HL OI)"
        assert "⚠️ %100 üstü" in fmt.coverage_ctx({"pct_long": 15.0, "pct_short": 125.0, "over": True})
        row = {"side": "long", "notional": 11_000.0, "liq_px": 0.1321, "dist": 37.67, "address": ADDR, "ts": t - 30,
               "leverage": 3, "entry_px": 0.2, "mark": 0.2119, "need": 1, "verified": True, "coin": "para:ANSEM"}
        snap = fmt.crypto_liq_snapshot({"coin": "para:ANSEM", "mark": 0.2119, "rows": [row], "n_all": 1, "n_big": 0,
                                        "min_usd": 500_000, "coverage": c})
        assert "HL'nin tamamı değil · kapsama long %12 · short %5 (havuz / HL OI)" in snap, snap
        snap0 = fmt.crypto_liq_snapshot({"coin": "para:ANSEM", "mark": 0.2119, "rows": [row], "n_all": 1, "n_big": 0,
                                         "min_usd": 500_000})
        assert snap0.count("HL'nin tamamı değil") == 1 and "kapsama" not in snap0
        alert = fmt.crypto_liq_alert("HYPE", 40.0, [{**row, "coin": "HYPE", "liq_px": 39.0, "dist": 2.5, "mark": 40.0}], [],
                                     2.5, 1, coverage=await cov.coverage("HYPE", "crypto"))
        assert "havuzdaki adresler — HL'nin tamamı değil · kapsama long %15 · short %125 (havuz / HL OI) ⚠️" in alert, alert
        # /tani
        from app import diag
        txt = await diag.report(cfg, None)
        line = next((ln for ln in txt.splitlines() if ln.strip().startswith("kapsama (havuz/HL OI")), "")
        assert "en düşük ANSEM %12/%5 (para) · TSLA %80/%10 (xyz)" in line and "ortanca %8" in line and "2 coin" in line, line
        async with dbm.db() as cc:
            await cc.execute("DELETE FROM asset_metrics")
        txt = await diag.report(cfg, None)
        assert "OI verisi yok — metrik turu koşmadı" in txt
        # bağlantı
        rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
        assert "coverage_txt=_coverage.txt(cov)" in rd("app", "radar", "cryptoliq.py") and '"coverage": cov' in rd("app", "web", "routes.py")
        assert "Kapsama yüzdesi (havuz / HL OI)" in rd("README.md")
        print("✅ PNG/mesaj/tanı) alt başlıkta kapsama; anlık ve alarm mesajında kapsama satırı; /tani en düşükler + ortanca")
    asyncio.run(run())
