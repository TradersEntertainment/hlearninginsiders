"""⚖️ HL istek bütçesi — öncelik şeritleri, kendi-429 ataması, ağırlık sayacı.

Pinlenenler:
  • normal şerit bugünkü gibi (tavana kadar geçer); düşük şerit kullanım LOW_SHARE (%70)
    üstündeyken bekler, altındayken geçer; herhangi bir 429'dan sonra LOW_PAUSE_SEC susar,
    normal şerit susmaz
  • öncelik çağrıda (priority=) ya da görev bağlamında (PRIORITY contextvar) verilir;
    gather'daki her coroutine kendi bağlam kopyasını alır (süpürücü partisi)
  • stats: 429 çağıranın sözlüğüne yazılır (kendi 429'u), küresel n_429 ve last_429 da güncellenir
  • ağırlık: belge tablosu (clearinghouseState 2, candleSnapshot 20…), usage() son dakikayı toplar
  • /tani "HL bütçesi" satırı
"""
import asyncio
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-budget.db")
from app.hl import client as hlc  # noqa: E402
from app.hl.client import LOW_PAUSE_SEC, LOW_SHARE, PRIORITY, HLClient, weight_of  # noqa: E402


class Resp:
    def __init__(self, status, body):
        self.status, self.body = status, body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self.body

    async def text(self):
        return "x"


class Sess:
    def __init__(self):
        self.posts, self.queue = [], []

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        return self.queue.pop(0) if self.queue else Resp(200, {"assetPositions": []})


async def _passes(coro, timeout=0.3) -> bool:
    try:
        await asyncio.wait_for(coro, timeout)
        return True
    except asyncio.TimeoutError:
        return False


def test_lanes_and_stats():
    async def run():
        cli = HLClient(Sess(), "https://x/", "https://lb", min_interval=0, max_rpm=10)
        # boş pencere: ikisi de geçer
        assert await _passes(cli._acquire_budget("low", 2)) and await _passes(cli._acquire_budget("normal", 20))
        u = cli.usage()
        assert u["rpm"] == 2 and u["weight"] == 22 and u["low_paused"] == 0 and u["n_429"] == 0 and u["last_429_ago"] is None
        # kullanım %70'e gelince düşük bekler, normal geçer
        for _ in range(5):
            await cli._acquire_budget("normal", 2)
        assert cli.usage()["rpm"] == 7 and 7 >= 10 * LOW_SHARE
        assert not await _passes(cli._acquire_budget("low", 2)), "düşük şerit %70 üstünde beklemeli"
        assert await _passes(cli._acquire_budget("normal", 2))
        # tavan: normal de bekler
        for _ in range(2):
            await cli._acquire_budget("normal", 2)
        assert cli.usage()["rpm"] == 10 and not await _passes(cli._acquire_budget("normal", 2))
        # 429 sonrası: düşük şerit LOW_PAUSE_SEC susar (boş pencerede bile), normal geçer
        cli2 = HLClient(Sess(), "https://x/", "https://lb", min_interval=0, max_rpm=10)
        cli2.session.queue = [Resp(429, None), Resp(200, {"assetPositions": []})]
        own = {}
        out = await cli2.clearinghouse("0x" + "a" * 40, priority="normal", stats=own)
        assert out == {"assetPositions": []} and own == {"429": 1} and cli2.n_429 == 1
        u = cli2.usage()
        assert 0 < u["low_paused"] <= LOW_PAUSE_SEC and u["last_429_ago"] <= 3 and u["weight"] == 2, u   # retry 1-2 sn uyur
        assert not await _passes(cli2._acquire_budget("low", 2)) and await _passes(cli2._acquire_budget("normal", 2))
        other = {}
        await cli2.clearinghouse("0x" + "b" * 40, stats=other)
        assert other == {} and own == {"429": 1}, "başkasının isteği kendi sayacına yazılmaz"
        # bağlam önceliği: gather'daki her coroutine kendi kopyasını alır
        cli3 = HLClient(Sess(), "https://x/", "https://lb", min_interval=0, max_rpm=10)
        for _ in range(7):
            await cli3._acquire_budget("normal", 2)
        seen = {}

        async def low_task():
            PRIORITY.set("low")
            seen["low"] = await _passes(cli3.info({"type": "l2Book", "coin": "X"}))

        async def normal_task():
            seen["normal"] = await _passes(cli3.info({"type": "l2Book", "coin": "Y"}))
        await asyncio.gather(low_task(), normal_task())
        assert seen == {"low": False, "normal": True} and PRIORITY.get() == "normal", seen
        # ağırlık tablosu
        assert weight_of({"type": "clearinghouseState"}) == 2 and weight_of({"type": "candleSnapshot"}) == 20
        assert weight_of({"type": "userRole"}) == 60 and weight_of({"type": "batchClearinghouseStates"}) == 20 and weight_of({}) == 20
        assert hlc.HL_WEIGHT_LIMIT == 1200
        print("✅ bütçe) düşük şerit %70'te bekler, 429 sonrası susar; normal geçer; stats kendi 429'u; bağlam önceliği; ağırlıklar")
    asyncio.run(run())


def test_diag_budget_line():
    from app import diag

    class St:
        client = HLClient(Sess(), "https://x/", "https://lb", min_interval=0, max_rpm=550)
    line = diag._budget_line(St())
    assert line.startswith("  HL bütçesi: 0/550 istek/dk · ~0 ağırlık/dk (tahmini; HL sınırı 1200) · 429 toplam 0, hiç · düşük şerit"), line
    assert "açık (kullanım %70 üstünde bekler)" in line
    St.client._last_429 = __import__("time").monotonic()
    St.client.n_429 = 3
    line = diag._budget_line(St())
    assert "429 toplam 3, son 0sn önce" in line and "DURAKLI" in line, line
    assert diag._budget_line(None) is None and diag._budget_line(object()) is None
    print("✅ tani) HL bütçesi satırı: istek/ağırlık/429/düşük şerit durumu")
