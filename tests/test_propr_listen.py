"""📡 PROPR'da listeli her ana dex coini canlı akışta dinlenir (hacim sırası ne olursa olsun).

Kullanıcı (19.10): "bu 58 coin dediğin proprda listeli değilşse anlamı yok ama proprda listeli her coini
dinleyebiliyor olmalıyız". Ölçüm (19.10): PROPR'daki 114 ana dex coininin 9'u (SUSHI, BOME, PEOPLE, ZEN,
GRIFFAIN, ACE, STABLE, HEMI, SAGA) hacimce ilk 120'nin dışındaydı → WS'e abone değildi → HL TWAP'ları
(twapfast/twaplive) ve "ne oldu" kayıtları bu coinlerde körse.

Kararlar: ilk CRYPTO_WATCH_TOP aynen kalır (kullanıcı ayarı), PROPR'dakiler sona EKLENİR; yalnız HL'de
işlem gören (silinmemiş) adlar eklenir; CRYPTO_WATCH_TOP=0 hâlâ kripto dinlemeyi tamamen kapatır.

Pinlenenler:
  • top_crypto_coins(also=…): ilk N + sıralamada geride kalan PROPR coinleri (hacim sırasıyla), tekrarsız;
    silinmiş / HL'de olmayan eklenmez; kv önbelleği tüm sıralamayı ("all") tutar, TTL içinde istek yok;
    "all"suz eski önbellek yeniden çekilir; istek düşerse bayat listeyle yine eklenir; also hata atarsa yok sayılır
  • collector._crypto_coins propr.is_listed'i geçirir; top=0 → []
  • /twap COIN satırı ve README/.env/ayar metni PROPR'ı anar
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db as dbm
from app import propr
from app.hl import universe as uni

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Client:
    def __init__(self):
        self.calls = 0
        self.fail = False
        names = [f"C{i:03d}" for i in range(60)] + ["SUSHI", "OLD", "kPEPE"]
        self.meta_ = {"universe": [{"name": n, "isDelisted": n == "OLD"} for n in names]}
        # hacim: C000 en büyük … SUSHI ve kPEPE en küçükler
        self.ctx = [{"dayNtlVlm": str(1e9 - i * 1e6)} for i in range(60)] + [{"dayNtlVlm": "5"}, {"dayNtlVlm": "9"},
                                                                             {"dayNtlVlm": "3"}]

    async def meta_and_ctxs(self, dex=""):
        self.calls += 1
        if self.fail:
            raise RuntimeError("HL 500")
        return [self.meta_, self.ctx]


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "plisten.db"))


def test_top_plus_propr():
    async def run():
        await _fresh()
        c = Client()
        also = {"SUSHI", "OLD", "KPEPE", "C001", "NOPE"}.__contains__
        f = lambda n: also(n.upper())      # noqa: E731 — propr.is_listed gibi büyük/küçük harfe duyarsız
        out = await uni.top_crypto_coins(c, 3, also=f)
        assert out == ["C000", "C001", "C002", "SUSHI", "kPEPE"], out    # OLD silinmiş, NOPE HL'de yok, C001 tekrar etmez
        assert await uni.top_crypto_coins(c, 3) == ["C000", "C001", "C002"]
        assert c.calls == 1                                                 # TTL içinde önbellekten
        assert (await dbm.kv_get(uni.CRYPTO_KV))["all"][-2:] == ["SUSHI", "kPEPE"]
        # istek düşse de (bayat önbellek) PROPR eklemesi sürer
        await dbm.kv_set(uni.CRYPTO_KV, {**(await dbm.kv_get(uni.CRYPTO_KV)), "ts": 0})
        c.fail = True
        assert await uni.top_crypto_coins(c, 3, also=f) == out
        # "all"suz eski önbellek: also istenince yeniden çekilir
        c.fail = False
        await dbm.kv_set(uni.CRYPTO_KV, {"coins": ["C000", "C001", "C002"], "want": 50, "ts": dbm.now()})
        n0 = c.calls
        assert await uni.top_crypto_coins(c, 3, also=f) == out and c.calls == n0 + 1
        # also hata atarsa dinleme kesilmez
        def boom(n):
            raise ValueError("x")
        assert await uni.top_crypto_coins(c, 3, also=boom) == ["C000", "C001", "C002"]
        print("✅ ilk N + PROPR (hacim sırasıyla, silinmiş/yok hariç); önbellek 'all'; bayat/eski önbellek; hata yutulur")
    asyncio.run(run())


def test_collector_uses_propr():
    async def run():
        await _fresh()
        from app.config import Config
        from app.hl.collector import Collector
        cfg = Config()
        cfg.crypto_watch_top = 2
        col = Collector.__new__(Collector)
        col.cfg, col.client, col.crypto_err = cfg, Client(), ""
        propr.set_live([])
        try:
            propr.set_live(set(propr.DEFAULT_PROPR) | {"SUSHI"})
            out = await col._crypto_coins()
            assert out[:2] == ["C000", "C001"] and "SUSHI" in out and "C002" not in out, out
            cfg.crypto_watch_top = 0
            assert await col._crypto_coins() == []                       # 0 = kripto dinleme kapalı (PROPR dahil)
        finally:
            propr.set_live([])
        print("✅ collector: propr.is_listed eklenir; CRYPTO_WATCH_TOP=0 hepsini kapatır")
    asyncio.run(run())


def test_wiring_texts():
    rd = lambda *p: open(os.path.join(ROOT, *p), encoding="utf-8").read()  # noqa: E731
    assert "also=propr.is_listed" in rd("app", "hl", "collector.py")
    assert "PROPR'da listeli her ana dex coini" in rd("README.md")
    assert "PROPR'da listeli ana dex coinleri sıradan bağımsız EKLENİR" in rd(".env.example")
    from app.config import EDITABLE_FIELDS
    assert "PROPR" in EDITABLE_FIELDS["crypto_watch_top"]["desc"]
    assert "PROPR'da listeliler" in rd("app", "radar", "twaplive.py")
    print("✅ README/.env/ayar/twap metni PROPR'ı anıyor")


if __name__ == "__main__":
    test_top_plus_propr()
    test_collector_uses_propr()
    test_wiring_texts()
