"""⚠️ TWAP düşük bakiye satırı.

Kullanıcı (15.10): "update yapmamız lazım milyonluk twaplarda iptal gelebiliyor eğer cüzdanda 200-300k varsa sadece o
yüzden twapı atanın cüzdan düşük bütçeliyse uyarı versin ilk satırların oralarda düşükten kastım 500k altındaysa"

Pinlenenler:
  • eşik TWAP_LOW_BALANCE_USD (vars. $500K, env-only; 0 = kapalı → satır yok, ek istek yok)
  • cüzdan = HL'nin kendi değeri: `portfolio` "day" serisinin son noktası (tüm dex'ler, spot tokenler, birleşik hesap,
    portföy marjin). İnceleme 15.10 (canlı veriyle): parçaları kendimiz toplamak birleşik hesapta çift sayıyor, portföy
    marjinde (borçlu USDC) $8.5M'lık cüzdana "$0" diyordu
  • istek pozisyon isteğiyle aynı anda, düşük şerit + tekrar denemesiz; pozisyon yanıtından sonra en çok WALLET_GRACE;
    düşen / geç kalan / bayat / bozuk yanıt → satır yok; pozisyon satırı bakiyeyi beklemez ve süre bütçesi içeride
  • kapatan emir uyarılmaz: reduce-only ya da ters pozisyon ≥ KALANIN yarısı; radar emri baştan görmediyse ve emrin
    %15'i geçtiyse ters pozisyonda kalan bilinmez → satır yok
  • satır mesajın 2. satırı (ilk dilim ve eski yol biçimleri); bakiye aşağı yuvarlanır (eşikle çelişmez); "%0" yok
  • sayaç: uyarı yalnız GÖNDERİLİNCE (low_bal_mark); altı çağrı yeri; /tani; README (kör nokta); .env.example
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twap-lowbal.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A = "0x" + "ab" * 20


def _cfg(lim=500_000):
    c = Config()
    c.twap_low_balance_usd = lim
    return c


def _w(usd=244_000.0, **pos):
    return {"none": True, "src": "canlı", **pos, "wallet": {"usd": usd}}


def _order(plan=1_200_000, **kw):
    return {"planned_usd": plan, "planned_sz": 1000, "minutes": 300, "left_sec": 14000,
            "started_ts": dbm.now() - 600, "executed_usd": 0, "filled_pct": 0, "status": "activated", **kw}


def _m(coin="HYPE"):
    return {"coin": coin, "address": A, "side": "buy", "n": 20, "avg_slice": 4000, "median_gap": 30,
            "px_first": 40.0, "px_last": 40.4, "px_chg_pct": 1.0, "taker_pct": 100}


def _pf(val, age=0, now_ts=None):
    """HL `portfolio` yanıtının şekli (canlı 15.10): [[dönem, {accountValueHistory: [[ms, "$"], …], …}], …]."""
    t = (now_ts or dbm.now()) - age
    return [["day", {"accountValueHistory": [[(t - 3600) * 1000, "1.0"], [t * 1000, str(val)]],
                     "pnlHistory": [], "vlm": "0"}],
            ["perpDay", {"accountValueHistory": [[t * 1000, "235.9"]], "pnlHistory": [], "vlm": "0"}]]


def test_portfolio_parse():
    assert tl.parse_portfolio_value(_pf(3452477.91)) == 3452477.91             # portföy marjin: HL'nin toplamı
    assert tl.parse_portfolio_value(_pf(0)) == 0.0                             # gerçekten boş ≠ bilinmiyor
    assert tl.parse_portfolio_value(_pf(9e5, age=tl.PORTFOLIO_MAX_AGE + 60)) is None      # bayat
    for bad in (None, {}, [], "x", [["day", {}]], [["day", {"accountValueHistory": []}]],
                [["day", {"accountValueHistory": [["x", "y"]]}]], [["day", "x"]], [1, 2, 3]):
        assert tl.parse_portfolio_value(bad) is None, bad
    print("✅ portfolio) 'day' son noktası; boş / bayat / bozuk → bilinmiyor")


def test_budget_and_closing():
    o = _order()
    assert tl.wallet_budget("HYPE", "buy", o, _w()) == {"kind": "wallet", "usd": 244_000.0, "plan": 1_200_000}
    assert tl.wallet_budget("@107", "buy", o, _w())["kind"] == "wallet"        # spot da aynı cüzdan değeri
    assert tl.wallet_budget("HYPE", "buy", o, {"none": True}) is None
    assert tl.wallet_budget("HYPE", "buy", o, None) is None
    assert tl.wallet_budget("HYPE", "buy", o, _w(usd=None)) is None
    # kapatan: reduce-only; ters pozisyon ≥ kalanın yarısı (plan değil)
    assert tl.wallet_budget("HYPE", "sell", _order(reduce_only=True), _w())["kind"] == "closing"
    long_pos = _w(side="long", notional=700_000)
    long_pos.pop("none")
    assert tl.wallet_budget("HYPE", "sell", o, long_pos)["kind"] == "closing"
    assert tl.wallet_budget("HYPE", "sell", o, {**long_pos, "notional": 100_000})["kind"] == "wallet"   # çevirme
    assert tl.wallet_budget("HYPE", "buy", o, long_pos)["kind"] == "wallet"                             # ekleme
    # emrin ortasında ilk uyarı (inceleme 15.10): $2M satışın %70'i görülmüş, $600K long kaldı → kapatan
    mid = {**_order(plan=2_000_000), "fill_hl0": True, "fill_seen": 1_400_000, "fill_full": True}
    assert tl.wallet_budget("HYPE", "sell", mid, {**long_pos, "notional": 600_000})["kind"] == "closing"
    # radar baştan görmedi, emrin %15'inden fazlası geçti, ters pozisyon var → kalan bilinmez → yok
    unsure = {**_order(plan=2_000_000, minutes=60), "started_ts": dbm.now() - 1800,
              "fill_hl0": True, "fill_seen": 100_000, "fill_full": False}
    assert tl.wallet_budget("HYPE", "sell", unsure, {**long_pos, "notional": 300_000}) is None
    early = {**unsure, "started_ts": dbm.now() - 300}                       # %8 geçti: kalan üst sınırıyla
    assert tl.wallet_budget("HYPE", "sell", early, {**long_pos, "notional": 300_000})["kind"] == "wallet"
    assert tl.wallet_budget("HYPE", "buy", unsure, long_pos)["kind"] == "wallet"   # aynı yön: kalan önemsiz
    # HL dolanı yazmışsa kalan = remaining_usd
    assert tl.wallet_budget("HYPE", "sell", _order(remaining_usd=900_000), {**long_pos, "notional": 450_000}
                            )["kind"] == "closing"
    print("✅ bakiye) HL cüzdan değeri; kapatan emir kalanla; baştan görülmeyen emirde ters pozisyon → yok")


def test_threshold_and_counters():
    for k in ("checked", "unknown", "closing", "warned"):
        tl.LOW_BAL[k] = 0
    tl.LOW_BAL["last"] = None
    o = _order()
    lb = tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, _w())
    assert lb == {"kind": "wallet", "usd": 244_000.0, "plan": 1_200_000, "limit": 500_000}, lb
    assert tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, _w(usd=500_000.0)) is None              # sınırda yok
    assert tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, _w(usd=499_999.0))
    assert tl.low_bal_ctx(_cfg(), "HYPE", "sell", _order(reduce_only=True), _w()) is None
    assert tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, {"none": True}) is None
    before = dict(tl.LOW_BAL)
    assert tl.low_bal_ctx(_cfg(0), "HYPE", "buy", o, _w()) is None                           # kapalı: sayaç yok
    assert tl.LOW_BAL == before
    s = tl.LOW_BAL
    assert (s["checked"], s["unknown"], s["closing"], s["warned"], s["last"]) == (5, 1, 1, 0, None), s
    tl.low_bal_mark(None, "HYPE")
    assert s["warned"] == 0
    tl.low_bal_mark(lb, "HYPE")                                                              # yalnız gönderilince
    assert s["warned"] == 1 and s["last"]["usd"] == 244_000 and s["last"]["plan"] == 1_200_000
    d = tl.low_bal_diag(_cfg())
    assert "eşik $500,000 · bu açılıştan beri 1 uyarılı mesaj gönderildi" in d, d
    assert "(son: HYPE cüzdan $244,000 / plan $1,200,000" in d and "hazırlıkta 5 bakış" in d, d
    assert "1 cüzdan okunamadı (yazılmaz)" in d and "1 kapatan emir (yazılmaz)" in d, d
    assert "kapalı" in tl.low_bal_diag(_cfg(0)) and tl.low_balance(_cfg(-5)) == 0 and not tl.wallet_on(_cfg(0))
    print("✅ eşik) < $500K, sınırda yok; kapalıyken sayaç yok; uyarı sayacı yalnız gönderimde; /tani")


def test_message_line():
    sys.path.insert(0, HERE)
    import test_wording as tw
    o = _order()
    lb = tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, _w())
    base = {"order": o, "day_vol": 50e6, "vol_pct": 2.4, "pos": _w(), "klass": "kripto", "entity": ""}
    txt = fmt.twap_alert(_m(), {**base, "low_bal": lb})
    lines = txt.split("\n")
    assert lines[0].startswith("⏳ <b>TWAP</b>") and lines[1] == (
        "⚠️ <b>Düşük bakiye</b> (&lt; $500K): cüzdanda <b>$244K</b> · plan bunun <b>4.9 katı</b>"), lines[:3]
    assert lines[2].startswith("🐘 emir 24s hacmin") and lines[4].startswith("👤 "), lines
    assert not tw.hits(txt), tw.hits(txt)
    plain = fmt.twap_alert(_m(), base)
    assert "Düşük bakiye" not in plain and plain.split("\n")[1].startswith("🐘")
    # ilk dilim biçimi: başlıktan hemen sonra
    ctx = {"coin": "xyz:TSLA", "address": A, "side": "sell", "gate": "ok", "day_vol": 80e6, "vol_pct": 1.5,
           "per_min": 4000, "rate_x": 2.5, "slice_usd": 4000, "slice_px": 250.0, "klass": "hisse",
           "pos": _w(usd=300_000.0), "now": dbm.now()}
    ctx["low_bal"] = tl.low_bal_ctx(_cfg(), "xyz:TSLA", "sell", o, ctx["pos"])
    fl = fmt.twap_fast(o, ctx).split("\n")
    assert fl[0].startswith("⏳ <b>TWAP başladı</b>") and fl[1] == (
        "⚠️ <b>Düşük bakiye</b> (&lt; $500K): cüzdanda <b>$300K</b> · plan bunun <b>4.0 katı</b>"), fl[1]
    assert fl[2].startswith("⏱ ") and not tw.hits("\n".join(fl))
    # yuvarlama eşiğe çıkmaz (inceleme 15.10); "%0" yok; $0 / $1 altı oran yazılmaz
    edge = fmt.low_bal_line({"usd": 499_600, "plan": 1.2e6, "limit": 500_000})
    assert "cüzdanda <b>$499K</b>" in edge and "$500K</b>" not in edge, edge
    assert fmt.low_bal_line({"usd": 400_000, "plan": 1_000, "limit": 500_000}).endswith("plan bunun %1'inden az")
    assert fmt.low_bal_line({"usd": 300_000, "plan": 150_000, "limit": 500_000}).endswith("plan bunun %50'i")
    assert fmt.low_bal_line({"usd": 0.3, "plan": 1e6, "limit": 500_000}).endswith("cüzdanda <b>$0</b>")
    assert fmt.low_bal_line({"usd": 2_399_999, "plan": 5e6, "limit": 3e6}).startswith(
        "⚠️ <b>Düşük bakiye</b> (&lt; $3.0M): cüzdanda <b>$2.3M</b>")
    assert fmt.low_bal_line(None) == ""
    print("✅ mesaj) 2. satır (eski yol + ilk dilim); aşağı yuvarlama; %0 yok; ölçülü dil")


class _Cli:
    def __init__(self, state=None, pf=None, fail=False, pf_fail=False, pf_delay=0.01, main_delay=0.01):
        self.state, self.pf, self.fail, self.pf_fail = state, pf, fail, pf_fail
        self.pf_delay, self.main_delay = pf_delay, main_delay
        self.calls, self.pf_kw, self.pf_cancelled = [], None, False

    async def clearinghouse(self, user, dex="", priority=None, stats=None):
        self.calls.append(("ch", dex))
        await asyncio.sleep(self.main_delay)
        if self.fail:
            raise RuntimeError("ağ yok")
        return self.state

    async def portfolio(self, user, priority=None, retries=4, stats=None):
        self.calls.append(("pf", ""))
        self.pf_kw = {"priority": priority, "retries": retries}
        try:
            await asyncio.sleep(self.pf_delay)
        except asyncio.CancelledError:
            self.pf_cancelled = True
            raise
        if self.pf_fail:
            raise RuntimeError("portfolio yok")
        return self.pf


def test_position_ctx_wallet():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "lowbal.db"))
        st = {"marginSummary": {"accountValue": "245000.5"},
              "assetPositions": [{"position": {"coin": "xyz:TSLA", "szi": "-100", "positionValue": "25000",
                                               "entryPx": "250", "unrealizedPnl": "0", "leverage": {"value": 5}}}]}
        c = _Cli(st, _pf(244_335.69))
        pos = await tl.position_ctx(c, "xyz:TSLA", A, wallet=True)
        assert pos["side"] == "short" and pos["notional"] == 25000 and pos["src"] == "canlı", pos
        assert pos["wallet"] == {"usd": 244_335.69}, pos
        assert sorted(c.calls) == [("ch", "xyz"), ("pf", "")], c.calls
        assert c.pf_kw == {"priority": "low", "retries": 0}, c.pf_kw          # sırayı kapmaz, tekrar denemez
        # kapalıyken ek istek yok, 'wallet' yok (eski davranış)
        c = _Cli(st, _pf(1))
        pos = await tl.position_ctx(c, "xyz:TSLA", A)
        assert "wallet" not in pos and c.calls == [("ch", "xyz")], (pos, c.calls)
        # düşen / bayat / geç kalan → usd None; pozisyon yine canlı; geç kalan iptal edilir
        pos = await tl.position_ctx(_Cli(st, _pf(1), pf_fail=True), "xyz:TSLA", A, wallet=True)
        assert pos["side"] == "short" and pos["wallet"] == {"usd": None}
        pos = await tl.position_ctx(_Cli(st, _pf(1, age=99_999)), "xyz:TSLA", A, wallet=True)
        assert pos["wallet"] == {"usd": None}
        old = tl.WALLET_GRACE
        tl.WALLET_GRACE = 0.2
        try:
            slow = _Cli(st, _pf(1), pf_delay=5)
            t0 = asyncio.get_running_loop().time()
            pos = await tl.position_ctx(slow, "xyz:TSLA", A, wallet=True)
            assert asyncio.get_running_loop().time() - t0 < 1.0 and pos["side"] == "short"
            assert pos["wallet"] == {"usd": None}
            await asyncio.sleep(0)
            assert slow.pf_cancelled, "geç kalan istek iptal edilmeli (sızmasın)"
        finally:
            tl.WALLET_GRACE = old
        # süre bütçesi içeride: pozisyon yavaşsa bakiyeye kalan süre o kadar azalır, toplam timeout'u aşmaz
        c = _Cli(st, _pf(1), pf_delay=3, main_delay=0.3)
        t0 = asyncio.get_running_loop().time()
        pos = await tl.position_ctx(c, "xyz:TSLA", A, wallet=True, timeout=0.4)
        assert asyncio.get_running_loop().time() - t0 < 0.6 and pos["side"] == "short" and pos["wallet"] == {"usd": None}
        # pozisyon timeout'u → süpürme kaydına düşer (kayıt yok → None), bakiye isteği iptal
        c = _Cli(st, _pf(1), pf_delay=3, main_delay=3)
        assert await tl.position_ctx(c, "xyz:TSLA", A, wallet=True, timeout=0.2) is None
        await asyncio.sleep(0)
        assert c.pf_cancelled
        # pozisyon isteği düşerse süpürme kaydı, bakiye yok
        assert await tl.position_ctx(_Cli(st, _pf(1), fail=True), "HYPE", A, wallet=True) is None
        # portfolio bilmeyen istemci (eski sahte) → usd None
        class Old:
            async def clearinghouse(self, user, dex=""):
                return {"marginSummary": {"accountValue": "7"}, "assetPositions": []}
        pos = await tl.position_ctx(Old(), "HYPE", A, wallet=True)
        assert pos == {"none": True, "src": "canlı", "wallet": {"usd": None}}, pos
        # twapfast._pos: süre bütçesini içeri verir
        from app.radar import twapfast
        pos = await twapfast._pos(_Cli(st, _pf(123_456.0)), "xyz:TSLA", A, wallet=True)
        assert pos["wallet"] == {"usd": 123_456.0} and pos["side"] == "short"
    asyncio.run(run())
    print("✅ sorgu) pozisyonla aynı anda tek portfolio (düşük şerit, tekrar yok); kapalıyken ek istek yok; düşen /"
          " bayat / geç kalan → yok ve iptal; süre bütçesi içeride")


def test_wiring():
    assert Config().twap_low_balance_usd == 500_000
    os.environ["TWAP_LOW_BALANCE_USD"] = "0"
    try:
        assert Config().twap_low_balance_usd == 0
    finally:
        del os.environ["TWAP_LOW_BALANCE_USD"]
    assert "twap_low_balance_usd" not in EDITABLE_FIELDS                       # env-only
    src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()       # noqa: E731
    live, fast, lo = src("app/radar/twaplive.py"), src("app/radar/twapfast.py"), src("app/radar/twaploose.py")
    assert live.count("low_bal_ctx(cfg, run.coin, run.side, wo, pos)") == 1 and live.count("low_bal_mark(") == 3
    assert fast.count("twaplive.low_bal_ctx(cfg, coin, side, wo, pos)") == 1 and fast.count("low_bal_mark(") == 1
    assert lo.count("twaplive.low_bal_ctx(") == 4 and lo.count("twaplive.low_bal_mark(") == 4
    assert lo.count("wallet=twaplive.wallet_on(cfg)") == 4 and "wallet=twaplive.wallet_on(cfg)" in fast
    assert "wallet=wallet_on(cfg)" in live and "timeout=POS_TIMEOUT" in fast
    assert "low_bal_diag(cfg)" in src("app/diag.py") and "async def portfolio(" in src("app/hl/client.py")
    assert "TWAP_LOW_BALANCE_USD=500000" in src(".env.example")
    readme = src("README.md")
    assert "TWAP_LOW_BALANCE_USD" in readme and "⚠️ Düşük bakiye" in readme and "Kapatan emir uyarılmaz" in readme
    assert "Cüzdan = HL'nin kendi hesabı" in readme and "satır cüzdanın büyüklüğünü ölçer" in readme
    print("✅ bağlantı) env-only eşik; altı çağrı yeri + gönderim işaretleri; /tani; README; .env.example")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
