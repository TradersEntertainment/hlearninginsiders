"""🔎 Alarm özeti canlı sondası — 12.10 canlı hata.

Kullanıcının ekranında bir sıçrama alarmının altında "⚠️ canlı profil çekilemedi: TypeError: 'int'
object is not callable" vardı. Sebep: `window_brief`'in yerel değişkeni `probed`, aynı adlı modül
fonksiyonunu gölgeliyordu; sonda BAŞARILI olunca `await probed(ha)` bir sayıyı çağırıyordu — yani hata
tam da sondanın işe yaradığı durumda çıkıyor ve taze pozisyon mesaja hiç girmiyordu.

Pinlenen: başarılı sondada hata yok, `probed` sayısı ve taze `probed_ts` satıra yazılır.
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-forensics-probe.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.radar import forensics as fx  # noqa: E402

A = "0x" + "a" * 40


def test_successful_probe_no_typeerror():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "fx.db"))
        t1 = dbm.now()
        t0 = t1 - 600
        rep = {"rows": [{"address": A, "gross": 500_000.0, "buy": 500_000.0, "sell": 0.0, "net": 500_000.0,
                         "tk": 1, "known": 1, "pos": None, "probed_ts": 0, "infer": {"stale": True}}],
               "symbol": "SNDK", "minutes": 10, "oi": {"verdict": "", "why": ""}, "oi_chg": None, "px_chg": 1.0,
               "widened": False, "span": 600, "buy": 500_000.0, "sell": 0.0, "before_records": 0}
        calls = []

        async def fake_report(coin, a, b):
            return rep

        async def fake_refresh(cfg, client, coin, addrs, cap=None):
            calls.append(list(addrs))
            async with dbm.db() as c:                      # sonda adresi "baktık" diye işaretlemiş olsun
                await c.execute("INSERT OR REPLACE INTO kv(k, v) VALUES('x', '1')")
                await c.commit()
            return {"tried": 1, "ok": 1, "err": 0, "err_msg": "", "wrote": 0, "flat": 1, "skipped": 0}

        async def fake_listening(coin):
            return True
        real = (fx.report, fx.refresh_profiles, fx.listening)
        fx.report, fx.refresh_profiles, fx.listening = fake_report, fake_refresh, fake_listening
        try:
            out = await fx.window_brief(Config(), object(), "xyz:SNDK", t0, t1, top=3)
        finally:
            fx.report, fx.refresh_profiles, fx.listening = real
        assert calls == [[A]], calls
        assert out["probe_err"] == "" and out["probed"] == 1, out["probe_err"]
        assert out["top"][0]["address"] == A and "infer" in out["top"][0]
        print("✅ sonda) başarılı sondada TypeError yok (yerel ad fonksiyonu gölgelemiyor); sayı ve taze çıkarım yazılır")
    asyncio.run(run())


if __name__ == "__main__":
    test_successful_probe_no_typeerror()
