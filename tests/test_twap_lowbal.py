"""⚠️ TWAP düşük bakiye satırı.

Kullanıcı (15.10): "update yapmamız lazım milyonluk twaplarda iptal gelebiliyor eğer cüzdanda 200-300k varsa sadece o
yüzden twapı atanın cüzdan düşük bütçeliyse uyarı versin ilk satırların oralarda düşükten kastım 500k altındaysa"

Pinlenenler:
  • eşik TWAP_LOW_BALANCE_USD (vars. $500K, env-only; 0 = kapalı → satır yok, ek istek yok)
  • bakiye ölçümdür ("cüzdanda"): perp = cüzdanın TÜM perp dex'lerindeki accountValue toplamı (TWAP'ın dex'i pozisyon
    satırının ZATEN sorduğu yanıttan; diğerleri + tek spotClearinghouseState aynı anda) + spot'taki SERBEST dolar
    stabilleri (total − hold: birleşik hesapta perp teminatı spot USDC'nin hold'unda durur → çift sayım yok);
    spot alış = serbest karşı token; spot satış = serbest adet × fiyat
  • kapatan emir uyarılmaz (reduce-only ya da ters pozisyon ≥ planın yarısı); bakiye bilinmiyor / herhangi bir parça
    okunamadı / WALLET_TIMEOUT → satır yok (eksik okumayla "düşük" denmez)
  • satır mesajın 2. satırı (ilk dilim ve eski yol biçimleri); ölçülü dil
  • altı çağrı yeri (ana, ilk dilim, 🔸 ×2, 🕐 ×2) bağlamı geçirir; /tani satırı, README, .env.example
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-twap-lowbal.db")
from app import assets, db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A = "0x" + "ab" * 20


def _cfg(lim=500_000):
    c = Config()
    c.twap_low_balance_usd = lim
    return c


def _w(perp=240_000.0, spot=None):
    return {"none": True, "src": "canlı", "wallet": {"perp": perp, "spot": {"USDC": 10_000.0} if spot is None else spot}}


def _order(plan=1_200_000, **kw):
    return {"planned_usd": plan, "planned_sz": 1000, "minutes": 300, "left_sec": 14000,
            "started_ts": dbm.now() - 600, "executed_usd": 0, "filled_pct": 0, "status": "activated", **kw}


def _m(coin="HYPE"):
    return {"coin": coin, "address": A, "side": "buy", "n": 20, "avg_slice": 4000, "median_gap": 30,
            "px_first": 40.0, "px_last": 40.4, "px_chg_pct": 1.0, "taker_pct": 100}


def test_wallet_budget_kinds():
    assets.set_spot_names({"@107": "PURR/USDC"})
    o = _order()
    b = tl.wallet_budget("HYPE", "buy", o, _w(), 40.0)
    assert b == {"kind": "perp", "usd": 250_000, "perp": 240_000, "stable": 10_000, "plan": 1_200_000}, b
    # HIP-3 aynı hesap (cüzdan toplamı); stabillerin eksisi (hold > total) 0 sayılır, stabil olmayan token sayılmaz
    b = tl.wallet_budget("xyz:TSLA", "sell", o, {"none": True, "wallet": {"perp": 300_000.0,
                                                                          "spot": {"USDC": -5e6, "USDH": 2_000.0,
                                                                                   "HYPE": 1e6}}}, 250.0)
    assert b["usd"] == 302_000 and b["stable"] == 2_000, b
    # spot okunamadı → bilinmiyor (yalnız perp'le "düşük" denmez)
    assert tl.wallet_budget("xyz:TSLA", "sell", o, {"none": True, "wallet": {"perp": 300_000.0, "spot": None}}, 250.0) is None
    # kapatan emir: reduce-only ya da ters pozisyon ≥ planın yarısı → 'closing'
    assert tl.wallet_budget("HYPE", "sell", _order(reduce_only=True), _w(), 40.0)["kind"] == "closing"
    long_pos = {"side": "long", "notional": 700_000, "src": "canlı", "wallet": _w()["wallet"]}
    assert tl.wallet_budget("HYPE", "sell", o, long_pos, 40.0)["kind"] == "closing"
    small = {**long_pos, "notional": 100_000}
    assert tl.wallet_budget("HYPE", "sell", o, small, 40.0)["kind"] == "perp"      # çevirme: yeni teminat ister
    assert tl.wallet_budget("HYPE", "buy", o, long_pos, 40.0)["kind"] == "perp"    # aynı yön: ekleme
    # spot alış: karşı token; spot satış: adet × fiyat
    sp = {"none": True, "wallet": {"perp": 5e6, "spot": {"USDC": 120_000.0, "PURR": 1_000_000.0}}}
    b = tl.wallet_budget("@107", "buy", o, sp, 0.2)
    assert b == {"kind": "spot_buy", "usd": 120_000.0, "token": "USDC", "plan": 1_200_000}, b
    b = tl.wallet_budget("@107", "sell", o, sp, 0.2)
    assert b["kind"] == "spot_sell" and abs(b["usd"] - 200_000) < 1e-6 and b["token"] == "PURR", b
    # bilinmeyen: bakiye yok, spot adı yok, spot okunamadı, fiyat yok
    assert tl.wallet_budget("HYPE", "buy", o, {"none": True, "src": "canlı"}, 40.0) is None
    assert tl.wallet_budget("HYPE", "buy", o, None, 40.0) is None
    assert tl.wallet_budget("@999", "buy", o, sp, 0.2) is None
    assert tl.wallet_budget("@107", "buy", o, {"wallet": {"perp": 1.0, "spot": None}}, 0.2) is None
    assert tl.wallet_budget("@107", "sell", o, sp, None) is None
    assert tl.wallet_budget("HYPE", "buy", o, {"wallet": {"perp": None, "spot": {}}}, 40.0) is None
    print("✅ bakiye) cüzdan = tüm dex'ler + serbest spot stabil; spot alış/satış; kapatan emir; eksik okuma → yok")


def test_threshold_and_counters():
    for k in ("checked", "warned", "closing", "unknown", "spot_fail"):
        tl.LOW_BAL[k] = 0
    tl.LOW_BAL["last"] = None
    o = _order()
    lb = tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, _w(), 40.0)
    assert lb and lb["limit"] == 500_000 and lb["usd"] == 250_000, lb
    assert tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, _w(perp=490_000.0), 40.0) is None        # 490K + 10K = sınır
    assert tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, _w(perp=489_999.0), 40.0)
    assert tl.low_bal_ctx(_cfg(), "HYPE", "sell", _order(reduce_only=True), _w(), 40.0) is None
    assert tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, {"none": True}, 40.0) is None
    before = dict(tl.LOW_BAL)
    assert tl.low_bal_ctx(_cfg(0), "HYPE", "buy", o, _w(), 40.0) is None                     # kapalı: sayaç da yok
    assert tl.LOW_BAL["checked"] == before["checked"]
    s = tl.LOW_BAL
    assert (s["checked"], s["warned"], s["closing"], s["unknown"]) == (5, 2, 1, 1), s
    assert s["last"]["coin"] == "HYPE" and s["last"]["plan"] == 1_200_000
    d = tl.low_bal_diag(_cfg())
    assert "eşik $500,000" in d and "5 TWAP mesajında bakıldı, 2 uyarı" in d and "1 kapatan emir" in d
    assert "1 bakiye bilinmiyor" in d and "son: HYPE bakiye $499,999 / plan $1,200,000" in d, d
    assert "kapalı" in tl.low_bal_diag(_cfg(0))
    assert tl.low_balance(_cfg(-5)) == 0
    print("✅ eşik) < $500K uyarı, sınırda yok; kapalıyken sayaç yok; /tani sayaçları")


def test_message_line():
    sys.path.insert(0, HERE)
    import test_wording as tw
    assets.set_spot_names({"@107": "PURR/USDC"})
    o = _order()
    lb = tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, _w(), 40.0)
    base = {"order": o, "day_vol": 50e6, "vol_pct": 2.4, "pos": _w(), "klass": "kripto", "entity": ""}
    txt = fmt.twap_alert(_m(), {**base, "low_bal": lb})
    lines = txt.split("\n")
    assert lines[0].startswith("⏳ <b>TWAP</b>") and lines[1].startswith("⚠️ <b>Düşük bakiye</b> (&lt; $500K): "), lines[:3]
    assert lines[1].endswith("cüzdanda <b>$250K</b> (perp $240K + spot $10K) · plan bunun <b>4.8 katı</b>"), lines[1]
    assert lines[2].startswith("🐘 emir 24s hacmin") and lines[4].startswith("👤 "), lines
    assert not tw.hits(txt), tw.hits(txt)
    plain = fmt.twap_alert(_m(), base)
    assert "Düşük bakiye" not in plain and plain.split("\n")[1].startswith("🐘")
    # ilk dilim biçimi: başlıktan hemen sonra
    ctx = {"coin": "xyz:TSLA", "address": A, "side": "sell", "gate": "ok", "day_vol": 80e6, "vol_pct": 1.5,
           "per_min": 4000, "rate_x": 2.5, "slice_usd": 4000, "slice_px": 250.0, "klass": "hisse",
           "pos": {"none": True, "wallet": {"perp": 300_000.0, "spot": {"USDC": 0.4}}}, "now": dbm.now()}
    ctx["low_bal"] = tl.low_bal_ctx(_cfg(), "xyz:TSLA", "sell", o, ctx["pos"], 250.0)
    ftxt = fmt.twap_fast(o, ctx)
    fl = ftxt.split("\n")
    assert fl[0].startswith("⏳ <b>TWAP başladı</b>") and fl[1] == (
        "⚠️ <b>Düşük bakiye</b> (&lt; $500K): cüzdanda <b>$300K</b> · plan bunun <b>4.0 katı</b>"), fl[1]
    assert fl[2].startswith("⏱ ") and not tw.hits(ftxt), tw.hits(ftxt)
    # spot alış / satış; plan bakiyeden küçükse yüzde
    sp = {"none": True, "wallet": {"perp": 0.0, "spot": {"USDC": 120_000.0, "PURR": 1_000_000.0}}}
    assert fmt.low_bal_line(tl.low_bal_ctx(_cfg(), "@107", "buy", o, sp, 0.2)) == (
        "⚠️ <b>Düşük bakiye</b> (&lt; $500K): cüzdanda <b>$120K</b> USDC · plan bunun <b>10.0 katı</b>")
    assert fmt.low_bal_line(tl.low_bal_ctx(_cfg(), "@107", "sell", _order(plan=150_000), sp, 0.2)) == (
        "⚠️ <b>Düşük bakiye</b> (&lt; $500K): cüzdanda <b>$200K</b> değerinde PURR · plan bunun %75'i")
    zero = fmt.low_bal_line(tl.low_bal_ctx(_cfg(), "HYPE", "buy", o, _w(perp=0.0, spot={}), 40.0))
    assert zero == "⚠️ <b>Düşük bakiye</b> (&lt; $500K): cüzdanda <b>$0</b>", zero
    assert fmt.low_bal_line(None) == ""
    print("✅ mesaj) 2. satır (eski yol + ilk dilim); cüzdan toplamı; spot alış/satış; ölçülü dil")


class _Cli:
    def __init__(self, states=None, spot=None, fail=(), spot_fail=False, slow=()):
        self.states, self.spot, self.fail, self.spot_fail, self.slow = states or {}, spot, set(fail), spot_fail, set(slow)
        self.calls = []

    async def clearinghouse(self, user, dex="", priority=None, stats=None):
        self.calls.append(("ch", dex))
        await asyncio.sleep(5 if dex in self.slow else 0.01)
        if dex in self.fail:
            raise RuntimeError("ağ yok")
        return self.states.get(dex, {"marginSummary": {"accountValue": "0"}, "assetPositions": []})

    async def spot_clearinghouse(self, user, priority=None, stats=None):
        self.calls.append(("spot", ""))
        await asyncio.sleep(5 if "spot" in self.slow else 0.01)
        if self.spot_fail:
            raise RuntimeError("spot yok")
        return self.spot


def test_position_ctx_wallet():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "lowbal.db"))
        tsla = {"marginSummary": {"accountValue": "245000.5"},
                "assetPositions": [{"position": {"coin": "xyz:TSLA", "szi": "-100", "positionValue": "25000",
                                                 "entryPx": "250", "unrealizedPnl": "0", "leverage": {"value": 5}}}]}
        main = {"marginSummary": {"accountValue": "4575.6"}, "assetPositions": []}
        # birleşik hesap (canlı örnek 15.10): perp teminatı spot USDC'nin hold'unda → serbest = total − hold
        sp = {"balances": [{"coin": "USDC", "total": "26420.13", "hold": "4575.6"}, {"coin": "PURR", "total": "10"},
                           {"coin": "usdt0", "total": "100", "hold": "-31798"}, {"bozuk": True}, "x"]}
        dexes = ["", "xyz", "para"]
        c = _Cli({"xyz": tsla, "": main}, sp)
        pos = await tl.position_ctx(c, "xyz:TSLA", A, wallet=dexes)
        assert pos["side"] == "short" and pos["notional"] == 25000 and pos["src"] == "canlı", pos
        w = pos["wallet"]
        assert abs(w["perp"] - (245000.5 + 4575.6)) < 1e-6 and w["spot"] == {"USDC": 26420.13 - 4575.6, "PURR": 10.0,
                                                                            "USDT0": 100.0}, w
        assert sorted(c.calls) == [("ch", ""), ("ch", "para"), ("ch", "xyz"), ("spot", "")], c.calls
        b = tl.wallet_budget("xyz:TSLA", "buy", {"planned_usd": 1e6}, pos, 250.0)
        assert abs(b["usd"] - (245000.5 + 4575.6 + 26420.13 - 4575.6 + 100)) < 1e-6, b       # çift sayım yok
        # kapalıyken ([]) ek istek yok, 'wallet' yok (eski davranış)
        c = _Cli({"xyz": tsla}, sp)
        pos = await tl.position_ctx(c, "xyz:TSLA", A)
        assert "wallet" not in pos and c.calls == [("ch", "xyz")], (pos, c.calls)
        pos = await tl.position_ctx(_Cli({"xyz": tsla}, sp), "xyz:TSLA", A, wallet=[])
        assert "wallet" not in pos
        # bir parça düşer → o alan None → satır yok; pozisyon yine canlı
        pos = await tl.position_ctx(_Cli({"xyz": tsla}, sp, spot_fail=True), "xyz:TSLA", A, wallet=dexes)
        assert pos["side"] == "short" and pos["wallet"]["spot"] is None and pos["wallet"]["perp"] is not None
        pos = await tl.position_ctx(_Cli({"xyz": tsla}, sp, fail=["para"]), "xyz:TSLA", A, wallet=dexes)
        assert pos["side"] == "short" and pos["wallet"]["perp"] is None and pos["wallet"]["spot"] is not None
        assert tl.wallet_budget("xyz:TSLA", "buy", {"planned_usd": 1e6}, pos, 250.0) is None
        # yavaş parça: WALLET_TIMEOUT'ta kesilir, pozisyon satırı beklemez
        old = tl.WALLET_TIMEOUT
        tl.WALLET_TIMEOUT = 0.3
        try:
            t0 = asyncio.get_running_loop().time()
            pos = await tl.position_ctx(_Cli({"xyz": tsla}, sp, slow=["spot"]), "xyz:TSLA", A, wallet=dexes)
            assert asyncio.get_running_loop().time() - t0 < 1.5 and pos["side"] == "short"
            assert pos["wallet"]["spot"] is None and pos["wallet"]["perp"] is not None
        finally:
            tl.WALLET_TIMEOUT = old
        # spot isteği bilmeyen istemci (eski sahte) → spot None
        class Old:
            async def clearinghouse(self, user, dex=""):
                return {"marginSummary": {"accountValue": "7"}, "assetPositions": []}
        pos = await tl.position_ctx(Old(), "HYPE", A, wallet=[""])
        assert pos == {"none": True, "src": "canlı", "wallet": {"perp": 7.0, "spot": None}}, pos
        # bakiye alanı yok → perp None (0 değil); canlı sorgu düşerse süpürme kaydı, bakiye yok
        pos = await tl.position_ctx(_Cli({"": {"assetPositions": []}}, sp), "HYPE", A, wallet=dexes)
        assert pos["wallet"]["perp"] is None and tl.wallet_budget("HYPE", "buy", _order(), pos, 40.0) is None
        assert await tl.position_ctx(_Cli({}, sp, fail=[""]), "HYPE", A, wallet=dexes) is None
        assert tl.parse_spot_balances({"balances": "x"}) is None and tl.parse_spot_balances(None) is None
        assert tl.parse_account_value({"marginSummary": {"accountValue": "abc"}}) is None
        # dex listesi: kapalıyken boş
        assert tl.wallet_dexes(_cfg(0)) == [] and tl.wallet_dexes(_cfg())[0] == ""
    asyncio.run(run())
    print("✅ sorgu) tüm dex'ler + tek spot isteği pozisyonla aynı anda; hold düşülür (çift sayım yok); kapalıyken ek"
          " istek yok; düşen / yavaş parça → satır yok, pozisyon beklemez")


def test_wiring():
    assert Config().twap_low_balance_usd == 500_000
    os.environ["TWAP_LOW_BALANCE_USD"] = "0"
    try:
        assert Config().twap_low_balance_usd == 0
    finally:
        del os.environ["TWAP_LOW_BALANCE_USD"]
    assert "twap_low_balance_usd" not in EDITABLE_FIELDS                       # env-only
    src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()       # noqa: E731
    assert src("app/radar/twaplive.py").count("low_bal_ctx(cfg, run.coin, run.side, order, pos, m.get(\"px_last\"))") == 1
    assert src("app/radar/twapfast.py").count("twaplive.low_bal_ctx(cfg, coin, side, order, pos, p.get(\"px\"))") == 1
    lo = src("app/radar/twaploose.py")
    assert lo.count("twaplive.low_bal_ctx(") == 4 and lo.count("wallet=twaplive.wallet_dexes(cfg)") == 4
    assert "wallet=twaplive.wallet_dexes(cfg)" in src("app/radar/twapfast.py")
    assert "wallet=wallet_dexes(cfg)" in src("app/radar/twaplive.py")
    assert "low_bal_diag(cfg)" in src("app/diag.py")
    assert "TWAP_LOW_BALANCE_USD=500000" in src(".env.example")
    readme = src("README.md")
    assert "TWAP_LOW_BALANCE_USD" in readme and "⚠️ Düşük bakiye" in readme and "Kapatan emir uyarılmaz" in readme
    print("✅ bağlantı) env-only eşik; altı çağrı yeri; /tani; README; .env.example")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
