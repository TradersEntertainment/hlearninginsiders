"""🧪 Strateji laboratuvarı önkoşulları (Faz 1) — veri kaybını durdur, ölçüm bütçesini doğru say.

Kullanıcı (08.10): "strateji mi üretsek, her data var artık neredeyse". Araştırma: veri zengin ama
geçmişi kısa, bazı satırlar her gün atılıyor. Bu dosya laboratuvardan ÖNCEKİ hazırlığı pinler.

Pinlenenler:
  • Yahoo'nun geçmiş bilanço satırları (tarih, saat, EPS tahmini / gerçekleşen / sürpriz %) artık
    atılmaz: earnings_history'ye yazılır, yeniden gelişte boş olmayan alan korunur, tablo budanmaz;
    gelecek bilanço seçimi aynen sürer
  • candleSnapshot ağırlığı dönen mum sayısıyla artar (test_hl_budget de pinler)
  • /tani lab sayımı: yalnız sayı ve en eski tarih, sonuç yok; eksik tablo satırı düşürmez
"""
import asyncio
import os
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-labprep.db")
from app import db as dbm  # noqa: E402
from app.earnings import calendar as cal  # noqa: E402


def _fake_yf(rows_by_sym):
    """yfinance taklidi: get_earnings_dates → gerçek pandas DataFrame (Yahoo biçimi)."""
    import pandas as pd

    class Ticker:
        def __init__(self, sym):
            self.sym = sym

        def get_earnings_dates(self, limit=12):
            rows = rows_by_sym.get(self.sym) or []
            if not rows:
                return pd.DataFrame()
            idx = pd.DatetimeIndex([pd.Timestamp(r[0], tz="America/New_York") for r in rows])
            return pd.DataFrame({"EPS Estimate": [r[1] for r in rows], "Reported EPS": [r[2] for r in rows],
                                 "Surprise(%)": [r[3] for r in rows]}, index=idx)
    return types.SimpleNamespace(Ticker=Ticker)


def test_yahoo_past_rows_kept():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "labprep.db"))
        import datetime as dt
        today = dt.datetime.now(cal.ET)
        fut = (today + dt.timedelta(days=5)).replace(hour=16, minute=5, second=0, microsecond=0)
        rows = {"SNDK": [(fut.strftime("%Y-%m-%d %H:%M"), 1.5, float("nan"), float("nan")),
                         ("2026-08-14 16:05", 1.2, 1.5, 25.0),
                         ("2026-05-08 07:00", 0.9, 0.8, -11.1),
                         ("2026-02-06 00:00", 0.7, float("nan"), float("nan"))]}
        real = sys.modules.get("yfinance")
        sys.modules["yfinance"] = _fake_yf(rows)
        real_sleep = cal._time.sleep
        cal._time.sleep = lambda s: None
        try:
            past: list[dict] = []
            out = cal._fetch_yahoo_sync(["SNDK"], 14, {}, past)
        finally:
            cal._time.sleep = real_sleep
            if real is not None:
                sys.modules["yfinance"] = real
            else:
                sys.modules.pop("yfinance", None)
        assert out["SNDK"]["date_et"] == fut.strftime("%Y-%m-%d") and out["SNDK"]["hour_hint"] == "amc"
        by = {r["date_et"]: r for r in past}
        assert set(by) == {"2026-08-14", "2026-05-08", "2026-02-06"}
        a = by["2026-08-14"]
        assert (a["hour_hint"], a["eps_est"], a["eps_actual"], a["surprise_pct"]) == ("amc", 1.2, 1.5, 25.0)
        assert by["2026-05-08"]["hour_hint"] == "bmo" and by["2026-02-06"]["exact_ts"] is None, "00:00 yer tutucu"
        assert by["2026-02-06"]["eps_actual"] is None, "NaN → None"
        assert await cal.save_history(past, "yahoo") == 3
        # yeniden gelişte boş alan eskisini silmez
        await cal.save_history([{**a, "eps_actual": None, "surprise_pct": None}], "yahoo")
        async with dbm.db() as c:
            cur = await c.execute("SELECT * FROM earnings_history ORDER BY date_et")
            got = [dict(r) for r in await cur.fetchall()]
        assert [g["date_et"] for g in got] == ["2026-02-06", "2026-05-08", "2026-08-14"]
        assert got[2]["eps_actual"] == 1.5 and got[2]["surprise_pct"] == 25.0 and got[2]["source"] == "yahoo"
        assert await cal.save_history([], "yahoo") == 0
        src = open(os.path.join(ROOT, "app/radar/sweeper.py"), encoding="utf-8").read()
        assert "earnings_history" not in src, "geçmiş bilanço tablosu budanmaz"
    asyncio.run(run())
    print("✅ geçmiş bilanço) Yahoo'nun geçmiş satırları earnings_history'ye; boş alan eskisini silmez;"
          " budanmaz; gelecek seçimi aynı")


def test_lab_counts_line():
    async def run():
        from app import diag
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "labcount.db"))
        async with dbm.db() as c:
            await c.execute("INSERT INTO vol_events(coin, ts, market) VALUES('BTC', 1791379800, NULL),"
                            " ('xyz:SNDK', 1791379900, 'equity'), ('ETH', 1791380000, 'crypto')")
            # aynı 1h barı iki 30 dk'lık taramada kaydedilmiş (aynı vade) + bir sonraki bar
            for ts, res in ((1791000000, 1791086400), (1791001800, 1791086400), (1791003600, 1791090000)):
                await c.execute("INSERT INTO pattern_signals(ts, coin, tf, horizon, resolve_ts) VALUES(?,?,?,?,?)",
                                (ts, "xyz:SNDK", "1h", 24, res))
            await c.commit()
        line = "\n".join(await diag._lab_counts())
        assert line.startswith("  🧪 lab sayımı (yalnız sayı, sonuç yok): ")
        assert "hacim rekoru (kripto) 2 (en eski" in line and "hacim rekoru (hisse) 1 (en eski" in line
        assert "örüntü sinyali (tekil vade) 2" in line, "aynı vade iki kez sayılmaz"
        assert "?" not in line, line
        diag.LAB_COUNTS.append(("yok", "SELECT COUNT(*), MIN(ts) FROM olmayan_tablo"))
        try:
            assert "yok ? (OperationalError)" in "\n".join(await diag._lab_counts())
        finally:
            diag.LAB_COUNTS.pop()
        assert "_lab_counts()" in open(os.path.join(ROOT, "app/diag.py"), encoding="utf-8").read()
    asyncio.run(run())
    print("✅ lab sayımı) yalnız sayı + en eski tarih; mükerrer örüntü vadesi tek; eksik tablo satırı düşürmez")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
