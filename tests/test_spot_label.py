"""🪙 Spot çifti okunur adıyla görünür — ve ince spot çifti kanalı doldurmaz.

Kullanıcı ilk canlı spot TWAP alarmında: "bi tane twap bildirimi geldi ama hangi
coine neye belli değil". Mesaj `⏳ TWAP · @272` diyordu: `@272` HL'nin spot BORSA
KİMLİĞİ, insana hiçbir şey anlatmıyor. Okunur ad ('@107' → 'PURR/USDC') kv'de
zaten vardı ama yalnız /twaplar okuyordu; `symbol_of` yalnız HIP-3'ün ':' önekini
soyuyor, '@N' biçimini bilmiyordu.

Çözüm TEK MERKEZ: `assets.SPOT_NAMES` + senkron `assets.label()` — Telegram
biçimleyicileri, şablon macro'ları ve teşhis satırları await edemez, bu yüzden
`CRYPTO_DEX_SYMBOLS` ile aynı desen (modül önbelleği + kv'den açılışta yükleme).

Pinlenenler:
  • label/is_spot/spot_coin_of; perp ve HIP-3 davranışı DEĞİŞMEZ (gerileme)
  • kv'den açılış yüklemesi; evren yenilemesi önbelleği doldurur
  • twap_alert/progress/end okunur ad yazar; '@' hiç geçmez; 🪙 işareti var
  • spot'ta "açık perp pozisyonu yok — kapatıyor ya da hedge olabilir" YAZILMAZ
    (spot'ta pozisyon diye bir şey yok; o cümle okuyanı yanıltıyordu)
  • assets.kind('@272') 'equity' DEĞİL → bilanço takvimi aranmaz; klass 'kripto' kalır
  • /twap sayfası ve _macros.coin() ham kimlik ya da ölü /t/@272 linki üretmez
  • kapı: 24s hacmi `spot_twap_min_day_vol` altındaki spot çifti alarm üretmez
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-spotlabel.db")
from app import assets, db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.hl.universe import symbol_of  # noqa: E402
from app.radar import twaplive as tl  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

A = "0xf704" + "0" * 31 + "7f8d"
NAMES = {"@107": "PURR/USDC", "@272": "USDT0/USDC"}


def _msg(coin="@272", vol_pct=799.0, day_vol=238_000):
    m = {"coin": coin, "address": A, "side": "sell", "n": 32, "avg_slice": 2000,
         "median_gap": 30, "px_first": 1131, "px_last": 1130, "px_chg_pct": -0.1,
         "taker_pct": 100, "sz_total": 1700, "total": 1_610_000, "dur": 3600}
    o = {"planned_sz": 1700, "planned_usd": 1_900_000, "executed_usd": 0, "filled_pct": 0,
         "remaining_usd": 1_900_000, "minutes": 600, "left_sec": 32040,
         "started_ts": dbm.now(), "status": "activated"}
    return m, {"order": o, "day_vol": day_vol, "vol_pct": vol_pct, "pos": {"none": True},
               "klass": "kripto", "entity": ""}


def test_label_helpers():
    assets.set_spot_names(NAMES)
    assert assets.label("@107") == "PURR/USDC" and assets.label("@272") == "USDT0/USDC"
    assert assets.is_spot("@107") and not assets.is_spot("INJ") and not assets.is_spot("")
    # gerileme: perp ve HIP-3 yolu aynı kalmalı
    assert assets.label("INJ") == "INJ" and assets.label("para:UANSEM") == "UANSEM"
    assert symbol_of("para:UANSEM") == "UANSEM" and symbol_of("INJ") == "INJ"
    # symbol_of spot'ta okunur ad → fanout abonelik anahtarı elle yazılabilir olur
    assert symbol_of("@107") == "PURR/USDC"
    assert assets.spot_coin_of("purr/usdc") == "@107" and assets.spot_coin_of("@107") == "@107"
    assert assets.spot_coin_of("INJ") == "" and assets.spot_coin_of("") == ""
    # ad henüz gelmediyse ham kimlik döner — mesaj yine gider, kilitlenmez
    assets.set_spot_names({})
    assert assets.label("@272") == "@272" and symbol_of("@272") == "@272"
    assets.set_spot_names(NAMES)
    print("✅ etiket) '@107' → 'PURR/USDC'; perp/HIP-3 aynı; ters arama; ad yoksa ham kimlik")


def test_cache_is_loaded_and_filled():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "sl.db"))
        assets.set_spot_names({})
        await dbm.kv_set(assets.SPOT_NAMES_KV, {"coins": ["@107"], "names": {"@107": "PURR/USDC"},
                                                "vols": {}, "want": 40, "ts": dbm.now()})
        assert await assets.load_spot_names() == 1, "açılışta kv'den yüklenir"
        assert assets.label("@107") == "PURR/USDC"
        # evren yenilemesi de senkron önbelleği doldurur (kv beklemeden)
        assets.set_spot_names({})
        from app.hl.universe import top_spot_coins

        class Cli:
            async def spot_meta_and_ctxs(self):
                return [{"tokens": [{"name": "PURR"}, {"name": "USDC"}],
                         "universe": [{"name": "@107", "tokens": [0, 1]}]},
                        [{"dayNtlVlm": "3200000"}]]
        await dbm.kv_set(assets.SPOT_NAMES_KV, {})
        await top_spot_coins(Cli(), 40)
        assert assets.SPOT_NAMES["@107"] == "PURR/USDC", "evren yenilemesi önbelleği doldurur"
        assert assets.SPOT_NAMES_KV == "spot_top_coins", "kaynak hl.universe.SPOT_KV ile aynı"
        print("✅ önbellek) açılışta kv'den yüklenir, evren yenilemesi tazeler; tek kv anahtarı")
    asyncio.run(run())


def test_telegram_messages():
    assets.set_spot_names(NAMES)
    m, ctx = _msg()
    t = fmt.twap_alert(m, ctx)
    assert "@" not in t, f"ham borsa kimliği sızdı:\n{t}"
    assert "🪙" in t and "<b>USDT0/USDC</b>" in t, t
    assert "1.7K USDT0/USDC ≈ $1.9M" in t, "emir satırı da okunur adla"
    assert "📍 spot çifti — pozisyon, likidasyon ve funding yok" in t
    assert "kapatıyor ya da hedge olabilir" not in t, \
        "spot'ta pozisyon yok; 'kapatıyor' çıkarımı yanıltıcıydı"
    p = fmt.twap_progress(m, ctx)
    e = fmt.twap_end(m, {"order": {}, "day_vol": 238_000})
    assert "USDT0/USDC" in p and "@" not in p, p
    assert "USDT0/USDC" in e and "@" not in e, e
    # gerileme: perp mesajı bire bir aynı kalır
    pm, pctx = _msg(coin="INJ")
    assert "<b>INJ</b>" in fmt.twap_alert(pm, pctx) and "🪙" not in fmt.twap_alert(pm, pctx)
    assert "📍 pozisyon: bu coinde açık perp pozisyonu yok" in fmt.twap_alert(pm, pctx)
    print("✅ telegram) alarm/yarılandı/bitti okunur adla; 🪙 + dürüst spot notu; perp değişmedi")


def test_asset_classification():
    assert assets.kind("@272") != "equity" and assets.has_earnings("@272") is False, \
        "spot çiftinin bilanço takvimi yok"
    assert assets.klass("@272") == "kripto", "yönlendirme bozulmadı — kripto kanalı"
    assert assets.is_index_perp("@272") is False
    assert tl.chat_for(Config(), "@272") == tl.chat_for(Config(), "PUMP"), \
        "spot da ana dex kriptoyla aynı kanala gider"
    # gerileme
    assert assets.kind("AAPL") == "equity" and assets.has_earnings("AAPL") is True
    assert assets.klass("xyz:SP500") == "endeks" and assets.kind("para:ANSEM") == "crypto"
    print("✅ sınıf) kind('@272') 'equity' değil → takvim aranmaz; klass 'kripto' kalır")


def test_gate_floor_for_thin_pairs():
    cfg, ts = Config(), dbm.now()
    o = {"status": "activated", "planned_usd": 1_900_000, "remaining_usd": 1_900_000}
    assert cfg.spot_twap_min_day_vol == 1_000_000.0
    # kullanıcının aldığı alarm: $238K hacimli çiftte $1,9M emir = hacmin %799'u.
    # Yüzde kuralı burada işe yaramaz (oran küçülmez, patlar) — taban hacimdir.
    assert tl.order_gate(cfg, o, 238_000, ts, ts, "@272") == "spot_thin"
    assert tl.order_gate(cfg, o, 3_200_000, ts, ts, "@272") == "ok"
    assert tl.order_gate(cfg, o, 238_000, ts, ts, "PUMP") == "ok", "perp'e dokunulmaz"
    cfg.spot_twap_min_day_vol = 0
    assert tl.order_gate(cfg, o, 238_000, ts, ts, "@272") == "ok", "0 = taban kapalı"
    cfg.spot_twap_min_day_vol = 1_000_000
    d = tl.gate_detail(cfg, "@272", o, 238_000, ts, ts)
    assert d["reason"] == "spot_thin" and d["vol_need"] == 1_000_000.0
    assert tl.gate_detail(cfg, "PUMP", o, 238_000, ts, ts)["vol_need"] is None
    # kaçan emir GİZLENMEZ: /tani nedeni okur, sayaç sayar
    assert tl.REASON_TR["spot_thin"] == "spot çifti ince (24s hacim taban altı)"
    src = open(os.path.join(ROOT, "app", "radar", "twaplive.py"), encoding="utf-8").read()
    assert '"spot_thin": 0' in src, "tur sayacında kendi kalemi var"
    # ayar künyesi
    assert EDITABLE_FIELDS["spot_twap_min_day_vol"]["group"] == "Spot"
    assert all(EDITABLE_FIELDS["spot_twap_min_day_vol"].get(x)
               for x in ("type", "label", "group", "desc"))
    assert "SPOT_TWAP_MIN_DAY_VOL=" in open(os.path.join(ROOT, ".env.example"), encoding="utf-8").read()
    print("✅ kapı) ince spot çifti (24s hacim < taban) alarm üretmez; 0 = kapalı;"
          " neden /tani'de sayılır; ayar künyeli")


def test_web_surfaces():
    async def run():
        from test_routes_smoke import _fresh, get
        app = await _fresh()
        await dbm.kv_set(assets.SPOT_NAMES_KV, {"coins": ["@107"], "names": {"@107": "PURR/USDC"},
                                                "vols": {}, "want": 40, "ts": dbm.now()})
        await assets.load_spot_names()
        t = dbm.now()
        async with dbm.db() as c:
            await c.execute(
                "INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,avg_slice,"
                "avg_gap,cv_gap,cv_size,taker_pct,ts,src) VALUES('@107',?,'buy',?,?,180,900000,5000,"
                "30,0.1,0.1,50,?,'live')", (A, t - 7200, t - 60, t))
        from app.radar import twap
        r = next(r for r in await twap.recent() if r["coin"] == "@107")
        assert r["symbol"] == "PURR/USDC" and r["spot"] is True, r
        st, body = await get(app, "/twap")
        assert st == 200 and "PURR/USDC" in body and "@107" not in body, body[-500:]
        # macro: spot'un coin sayfası yok → ölü /t/@ linki üretilmez
        mac = open(os.path.join(ROOT, "app", "web", "templates", "_macros.html"), encoding="utf-8").read()
        assert "is_spot(c)" in mac and "coin_label(c)" in mac, "macro merkezi etiketi okur"
        rt = open(os.path.join(ROOT, "app", "web", "templates", "twap.html"), encoding="utf-8").read()
        assert "{{ coin_label(d.coin) }}" in rt, "teşhis satırı da okunur adla"
        print("✅ web) /twap ve macro okunur ad gösterir; ölü /t/@272 linki üretilmez")
    asyncio.run(run())


def test_diag_accepts_readable_name():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "dg.db"))
        assets.set_spot_names(NAMES)
        await dbm.kv_set("ws_universe", {"crypto": ["PUMP"], "spot": ["@107"], "ts": dbm.now()})
        await dbm.kv_set("spot_top_coins", {"coins": ["@107"], "names": NAMES,
                                            "vols": {"@107": 238_000}, "want": 40, "ts": dbm.now()})
        out = await tl.diag_coin(Config(), "PURR/USDC")
        assert "PURR/USDC" in out and "dinleniyor ✓" in out, out
        assert "spot" in out and "@107" not in out, out
        assert "24s hacmi ≥" in out and "taban altı: alarm yok" in out, \
            "ince çift olduğunu ve neden susturulduğunu söyler"
        print("✅ teşhis) '/twap PURR/USDC' borsa kimliğini bulur; ince çift susturmasını açıklar")
    asyncio.run(run())
