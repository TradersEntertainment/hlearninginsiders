"""🪶 WS mum ön-süzgeci — canlı akışta hacmi olmayan coine 5dk mum sorulmaz (HL ağırlık 20).

Pinlenenler:
  • Collector.flow_notional: WS bağlı değil / coin abone değil / bağlantı pencereden yeni → None
    (bilinmiyor → mum sor); abone + pencere dolu + işlem yok → 0.0; işlemler pencerede toplanır,
    eskiler düşer; yeniden bağlanınca pencere sıfırlanır; LIVE = çalışan collector
  • prefilter_skip: akış < sayfa tabanı → atla; ≥ taban / None / ayar kapalı / LIVE yok → sor
  • cryptovol.scan: atlanan coin mum istemez, stats "prefiltered"; /tani satırı; equityvol aynı yardımcı
"""
import asyncio
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-prefilter.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl import collector as colmod  # noqa: E402
from app.hl.collector import FLOW_WINDOW, Collector  # noqa: E402
from app.radar import cryptovol as cv  # noqa: E402
from app.radar import equityvol as ev  # noqa: E402
from test_volchart import Bot, Client, _cfg, raw_candles  # noqa: E402

Z, Y = "0x" + "c" * 40, "0x" + "b" * 40


def trade(tid, sz, ts, coin="PUMP"):
    return {"coin": coin, "px": "0.0043", "sz": str(sz), "time": ts * 1000, "tid": tid, "side": "B", "users": [Z, Y]}


def test_flow_window():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "flow.db"))
        cfg = Config()
        cfg.census_enabled = False
        col = Collector(cfg, None)
        assert colmod.LIVE is col
        await _flow_checks(col)

    async def _flow_checks(col):
        t = int(time.time())
        assert col.flow_notional("PUMP") is None, "WS bağlı değil → bilinmiyor"
        col.connected, col.connected_since = True, time.time()
        col.subscribed = {"PUMP"}
        assert col.flow_notional("PUMP") is None, "pencere dolmadı → bilinmiyor"
        col.connected_since = time.time() - FLOW_WINDOW - 5
        assert col.flow_notional("PUMP") == 0.0, "abone + pencere dolu + işlem yok = gerçek 0"
        assert col.flow_notional("HYPE") is None, "abone değil"
        await col._handle(json.dumps({"channel": "trades", "data": [trade(2, 5_000_000, t - FLOW_WINDOW - 50),
                                                                    trade(1, 2_000_000, t - 100)]}))
        f = col.flow_notional("PUMP")
        assert f is not None and abs(f - 0.0043 * 2_000_000) < 1e-6, f
        await col._handle(json.dumps({"channel": "trades", "data": [trade(3, 100, t - 10)]}))   # toz bile sayılır (fills'e girmez)
        assert abs(col.flow_notional("PUMP") - (0.0043 * 2_000_100)) < 1e-6
        assert len(col.flow["PUMP"]) == 2, "pencere dışı düştü"
        col.flow.clear()                                    # yeniden bağlanma davranışı
        assert col.flow_notional("PUMP") == 0.0
        col.connected = False
        assert col.flow_notional("PUMP") is None
        print("✅ akış) None/0.0 ayrımı; pencere toplamı; eskiler düşer; LIVE")
    try:
        asyncio.run(run())
    finally:
        colmod.LIVE = None                  # modül durumu başka test dosyalarına sızmasın


class Fake:
    def __init__(self, val):
        self.val, self.calls = val, 0

    def flow_notional(self, coin, window=FLOW_WINDOW):
        self.calls += 1
        return self.val


class Counting(Client):
    def __init__(self, candles):
        super().__init__(candles)
        self.n = 0

    async def candles(self, coin, interval, start_ms, end_ms):
        self.n += 1
        return await super().candles(coin, interval, start_ms, end_ms)


def test_prefilter_and_scan():
    async def run():
        from app.notify import Notifier
        try:
            cfg = _cfg()
            assert cfg.vol_ws_prefilter is True and "vol_ws_prefilter" in EDITABLE_FIELDS
            e = EDITABLE_FIELDS["vol_ws_prefilter"]
            assert all(e.get(x) for x in ("type", "label", "group", "desc"))
            colmod.LIVE = Fake(30_000.0)
            assert cv.prefilter_skip(cfg, "PUMP", 50_000) is True
            colmod.LIVE = Fake(60_000.0)
            assert cv.prefilter_skip(cfg, "PUMP", 50_000) is False
            colmod.LIVE = Fake(None)
            assert cv.prefilter_skip(cfg, "PUMP", 50_000) is False, "bilinmiyor → sor"
            colmod.LIVE = Fake(0.0)
            assert cv.prefilter_skip(cfg, "PUMP", 50_000) is True, "gerçek 0 → atla"
            cfg.vol_ws_prefilter = False
            assert cv.prefilter_skip(cfg, "PUMP", 50_000) is False, "ayar kapalı"
            cfg.vol_ws_prefilter = True
            colmod.LIVE = None
            assert cv.prefilter_skip(cfg, "PUMP", 50_000) is False, "collector yok"
            assert ev.prefilter_skip is cv.prefilter_skip
            # tarama: atlanan coin mum istemez; /tani satırı
            await dbm.init_db(os.path.join(tempfile.mkdtemp(), "pf.db"))
            colmod.LIVE = Fake(30_000.0)
            cli = Counting(raw_candles(300))
            out = await cv.scan(cfg, cli, Notifier(cfg, Bot()))
            assert out["coins"] == 1 and out["prefiltered"] == 1 and out["checked"] == 0 and cli.n == 0, out
            from app import diag
            txt = await diag.report(cfg, None)
            assert "kripto hacim: 1 sembol · 0 tarandı" in txt and "ön-süzgeç: 1 coin atlandı (WS)" in txt, txt
            colmod.LIVE = Fake(5_000_000.0)
            cli = Counting(raw_candles(300))
            out = await cv.scan(cfg, cli, Notifier(cfg, Bot()))
            assert out["prefiltered"] == 0 and out["checked"] == 1 and cli.n == 1 and out["events"] == 1, out
            print("✅ ön-süzgeç) akış < taban → mum yok; ≥ taban / bilinmiyor / kapalı → sorulur; stats + /tani")
        finally:
            colmod.LIVE = None
    asyncio.run(run())
