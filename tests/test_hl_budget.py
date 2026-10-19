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
  • "user" şeridi (ekrandaki canlı defter, 19.10): ortak pencerede yer varsa (tavan + USER_EXTRA) geçer, yoksa ayrılmış
    kotası USER_RPM ile (AIMD inişine, 429 duraklamasına bakmaz; ortak pencereye yine yazılır); ikisi de doluysa hemen
    BudgetBusy ("botun HL istek kotası dolu"); kendi kapısı USER_CONC (429 fırtınasında ortak kapıda uyuyan arka plan
    istekleri defteri bekletmez); normal şerit tavanda beklemeye devam eder; /tani satırı şeridi yazar
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
        # candleSnapshot: dönen her 60 mum +1 (5000 mumluk dolum ≈ 104); aralık bozuksa düz 20
        day = 86_400_000
        assert weight_of({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1h", "startTime": 0, "endTime": day}}) == 21
        assert weight_of({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "5m", "startTime": 0, "endTime": 30 * day}}) == 20 + 84
        assert weight_of({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1m", "startTime": 0, "endTime": 30 * day}}) == 20 + 84
        assert weight_of({"type": "candleSnapshot", "req": {"interval": "1h", "startTime": "x"}}) == 20
        assert hlc.HL_WEIGHT_LIMIT == 1200
        print("✅ bütçe) düşük şerit %70'te bekler, 429 sonrası susar; normal geçer; stats kendi 429'u; bağlam önceliği; ağırlıklar")
    asyncio.run(run())


def test_aimd_cap():
    import time as _t
    cli = HLClient(Sess(), "https://x/", "https://lb", min_interval=0, max_rpm=550)
    assert cli.usage()["cap"] == 550
    cli._note_429()
    assert cli.usage()["cap"] == 440 and cli.n_429 == 1, "429 → ×0.8"
    cli._note_429()
    assert cli.usage()["cap"] == 440, "10 sn içinde ikinci 429 tek adım sayılır"
    cli._cap_down_ts -= hlc.CAP_DOWN_MIN_GAP
    cli._note_429()
    assert cli.usage()["cap"] == 352
    for _ in range(40):
        cli._cap_down_ts -= hlc.CAP_DOWN_MIN_GAP
        cli._note_429()
    assert cli.usage()["cap"] == hlc.CAP_FLOOR, "taban"
    # sessizlik: son 429'dan ve son artıştan 60 sn geçince +25; 429 taze ise çıkmaz
    cli._last_429 = _t.monotonic() - hlc.CAP_UP_EVERY - 1
    cli._cap_up_ts = _t.monotonic() - hlc.CAP_UP_EVERY - 1
    assert cli.usage()["cap"] == hlc.CAP_FLOOR + hlc.CAP_UP_STEP
    assert cli.usage()["cap"] == hlc.CAP_FLOOR + hlc.CAP_UP_STEP, "dakikada bir adım"
    cli._cap_up_ts = _t.monotonic() - hlc.CAP_UP_EVERY - 1
    assert cli.usage()["cap"] == hlc.CAP_FLOOR + 2 * hlc.CAP_UP_STEP
    cli._cap = 540.0
    cli._cap_up_ts = _t.monotonic() - hlc.CAP_UP_EVERY - 1
    assert cli.usage()["cap"] == 550, "hl_max_rpm üst sınır"
    # şeritler etkin tavana göre
    cli2 = HLClient(Sess(), "https://x/", "https://lb", min_interval=0, max_rpm=10)
    cli2._cap = 4.0
    cli2._last_429 = 0.0

    async def run():
        for _ in range(3):
            await cli2._acquire_budget("normal", 2)
        assert not await _passes(cli2._acquire_budget("low", 2)), "düşük: 4×0.7=2.8 < 3"
        assert await _passes(cli2._acquire_budget("normal", 2))
        assert not await _passes(cli2._acquire_budget("normal", 2)), "normal: etkin tavan 4"
    asyncio.run(run())
    # süpürücü yetişme: etkin tavana bakar; 429/duraklamada taban parti
    from app.config import Config
    from app.radar import sweeper

    class U:
        def __init__(self, d):
            self.d = d

        def usage(self):
            return self.d
    cfg = Config()
    cfg.sweep_catchup, cfg.sweep_batch_size, cfg.sweep_interval_sec, cfg.sweep_batch_max = True, 40, 90, 250
    cfg.sweep_rpm_headroom = 0.85
    base_u = {"rpm": 50, "max": 550, "cap": 300, "window": 60.0, "low_paused": 0, "last_429_ago": None}
    n, info = sweeper._adaptive_batch(cfg, U(base_u), per_addr=2.0)
    assert n == int((300 * 0.85 - 50) * 1.5 / 2) and info["rpm_max"] == 300 and "catchup_off" not in info, (n, info)
    n, info = sweeper._adaptive_batch(cfg, U({**base_u, "low_paused": 30}), per_addr=2.0)
    assert n == 40 and info["catchup_off"] == "429"
    n, info = sweeper._adaptive_batch(cfg, U({**base_u, "last_429_ago": 20}), per_addr=2.0)
    assert n == 40 and info["catchup_off"] == "429"
    n, info = sweeper._adaptive_batch(cfg, U({**base_u, "last_429_ago": 120}), per_addr=2.0)
    assert n > 40
    print("✅ AIMD) 429 → tavan ×0.8 (10 sn'de bir), taban 120; sessizlikte +25/dk; şeritler etkin tavana göre; yetişme 429'da kapalı")


def test_diag_budget_line():
    from app import diag

    class St:
        client = HLClient(Sess(), "https://x/", "https://lb", min_interval=0, max_rpm=550)
    line = diag._budget_line(St())
    assert line.startswith("  HL bütçesi: 0/550 istek/dk (etkin tavan 550, AIMD) · ~0 ağırlık/dk (tahmini; HL sınırı 1200) · 429 toplam 0, hiç · düşük şerit"), line
    assert "açık (kullanım %70 üstünde bekler)" in line
    St.client._last_429 = __import__("time").monotonic()
    St.client.n_429 = 3
    line = diag._budget_line(St())
    assert "429 toplam 3, son 0sn önce" in line and "DURAKLI" in line, line
    assert "canlı defter şeridi 0/dk (ayrılmış 60)" in line and "gönderilmedi" not in line, line
    St.client.user_busy = 4
    assert "⚠️ kota dolu diye 4 istek gönderilmedi" in diag._budget_line(St())
    assert diag._budget_line(None) is None and diag._budget_line(object()) is None
    print("✅ tani) HL bütçesi satırı: istek/ağırlık/429/düşük şerit durumu")


def test_user_lane():
    """Kullanıcı (19.10): "canlı defter canlı değil? sadece işlemler canlı geliyor" — defter düşük şeritteydi, arka plan
    yükünde hiç sıraya giremedi. "user" şeridi: kendi kotası (USER_RPM) + kendi kapısı (USER_CONC).

    İnceleme (19.10, yeniden üretildi): ilk sürüm "tavan + 30" idi — (1) 429'da tavan ×0,8 inince pencere eski tavanla
    dolu kalıyor, defter ~8 sn bekliyordu; (2) ortak kapıda (8 yer) 429 tekrarında uyuyan arka plan istekleri defteri
    4 sn'lik süresine kadar bekletiyordu (yalnız mesaj değişmişti)."""
    import time as _t
    async def run():
        cli = HLClient(Sess(), "https://x/", "https://lb", min_interval=0, max_rpm=10)
        for _ in range(10):
            await cli._acquire_budget("normal", 20)
        assert not await _passes(cli._acquire_budget("normal", 2)), "tavan: normal bekler"
        cli._last_429 = _t.monotonic()                             # 429 az önce: düşük şerit susuyor
        cli._cap = 8.0                                             # AIMD indi: pencere (10) eski tavanla dolu
        assert cli.low_paused() > 0 and not await _passes(cli._acquire_budget("low", 2))
        for _ in range(hlc.USER_RPM):
            assert await _passes(cli._acquire_budget("user", 2)), "user şeridi ayrılmış kotasıyla geçer (tavan/pencereye bakmaz)"
        # ikisi de dolu: uyuyup 4 sn'lik süreye takılmaz, hemen BudgetBusy ("HL cevap vermedi" diye yazılmasın)
        t0 = _t.monotonic()
        try:
            await cli._acquire_budget("user", 2)
            raise AssertionError("BudgetBusy bekleniyordu")
        except hlc.BudgetBusy as e:
            assert "yerel istek kotası dolu" in str(e) and _t.monotonic() - t0 < 0.5
        u = cli.usage()
        assert u["rpm"] == 10 + hlc.USER_RPM and u["user"] == hlc.USER_RPM and u["user_busy"] == 1, u
        from app.radar import livefeed as lf
        assert lf.fail_reason(hlc.BudgetBusy("x")) == "botun HL istek kotası dolu (çok sayfa açık)"
        # ortak pencerede yer varken ayrılmış kota TAVAN değil: 6+ açık coin sayfası (coin başına ~10/dk) eskimesin
        roomy = HLClient(Sess(), "https://x/", "https://lb", min_interval=0, max_rpm=550)
        for _ in range(hlc.USER_RPM * 3):
            assert await _passes(roomy._acquire_budget("user", 2))
        assert roomy.usage()["user"] == hlc.USER_RPM * 3
        # l2_book priority'yi geçirir
        seen = {}

        async def info(p, retries=4, priority=None, stats=None):
            seen.update(p=p, priority=priority, retries=retries)
            return {"levels": [[], []]}
        cli.info = info
        await cli.l2_book("PUMP", 3, retries=1, priority="user")
        assert seen == {"p": {"type": "l2Book", "coin": "PUMP", "nSigFigs": 3}, "priority": "user", "retries": 1}, seen

        # 429 fırtınası: 8 arka plan isteği 429 alıp ortak kapıda geri-çekilmede uyuyor; defter yine 4 sn'de gelir
        class Storm:
            def __init__(self):
                self.l2 = 0

            def post(self, url, json=None, timeout=None):
                if (json or {}).get("type") == "l2Book":
                    self.l2 += 1
                    return Resp(200, {"levels": [[{"px": "1", "sz": "1", "n": 1}], [{"px": "1.01", "sz": "1", "n": 1}]]})
                return Resp(429, None)
        st = Storm()
        c2 = HLClient(st, "https://x/", "https://lb", concurrency=8, min_interval=0, max_rpm=550)
        bg = [asyncio.ensure_future(c2.clearinghouse("0x" + "a" * 40)) for _ in range(8)]
        await asyncio.sleep(0.05)
        t0 = _t.monotonic()
        from app.radar import livefeed as lf
        lf._DEPTH.clear()
        lf._DEPTH_WHY.clear()
        await lf._fetch_depth(c2, "PUMP")
        assert st.l2 == 1 and "PUMP" in lf._DEPTH and _t.monotonic() - t0 < 1.0, (st.l2, lf._DEPTH_WHY)
        from app.radar import bookowners as bo
        assert (await bo._book(c2, "PUMP", None))["levels"][0], "bookowners defteri de bekletilmez"
        for b in bg:
            b.cancel()
        await asyncio.gather(*bg, return_exceptions=True)
        lf._DEPTH.clear()
    asyncio.run(run())
    print("✅ user şeridi) kendi kotası (tavan/429/pencereden bağımsız), kendi kapısı (429 fırtınasında defter <1 sn);"
          " l2_book önceliği geçirir")
