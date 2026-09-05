"""Liq attack radarı — skor, pencere, bildirim, geçmiş tespiti, karne.

Pinlenenler:
  • defter maliyeti yalnız d içindeki seviyeleri sayar; defter d'ye varmıyorsa
    `thin` işaretlenir (gerçek maliyet bundan BÜYÜK olamaz)
  • d*, L ≥ eşik olan d'ler arasında oranı maksimize eder; eşik altı → None
  • hafta sonu değilse tarama İSTEK ATMAZ ve 'pencere kapalı' yazar
  • bildirim markerı yalnız gönderim BAŞARILIYSA yazılır (kapalı seans dersi)
  • geçmiş tespiti: sıçra-ve-dön + liq onayı; dönmeyen hareket ve liq'siz
    fitil saldırı DEĞİL
  • karne: önceden işaretlenen saldırı isabet, gerçekleşmeyen aday yanlış alarm
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db as dbm
from app.config import Config
from app.radar import hourstats as hs
from app.radar import liqattack as la

MARK = 100.0
T0 = 1_800_000_000


def book(bid_levels, ask_levels):
    """[(px, sz)] listelerinden bid/ask sözlükleri."""
    return ([{"px": p, "sz": s} for p, s in bid_levels],
            [{"px": p, "sz": s} for p, s in ask_levels])


def pos(addr, side, ntl, dist):
    liq = MARK * (1 - dist / 100) if side == "long" else MARK * (1 + dist / 100)
    return {"address": addr, "side": side, "notional": float(ntl), "liq_px": liq,
            "coin": "xyz:SNDK", "symbol": "SNDK"}


# ------------------------------------------------ 1) defter maliyeti
def test_depth_cost():
    bids, asks = book([(99.9, 100), (99.5, 200), (99.0, 300), (97.0, 1000)],
                      [(100.1, 50), (100.6, 100), (101.5, 400)])
    c, thin = la.depth_cost(bids, asks, MARK, "down", 1.0)     # 99.0'a kadar
    assert abs(c - (99.9 * 100 + 99.5 * 200 + 99.0 * 300)) < 1e-6, c
    assert thin is False, "97.0'da seviye var, defter 1%'i geçiyor"
    c2, thin2 = la.depth_cost(bids, asks, MARK, "down", 5.0)    # 95'e kadar
    assert thin2 is True, "görünen defter 97'de bitiyor → ince"
    c3, thin3 = la.depth_cost(bids, asks, MARK, "up", 1.0)      # 101'e kadar
    assert abs(c3 - (100.1 * 50 + 100.6 * 100)) < 1e-6 and thin3 is False
    assert la.depth_cost([], [], MARK, "down", 1.0) == (0.0, True)
    print("✅ defter) yalnız d içindeki seviyeler sayılıyor; defter d'ye"
          " varmıyorsa 'ince' — maliyet bundan büyük olamaz")


# ------------------------------------------------ 2) d* seçimi
def test_score_direction():
    rows = [pos("0xa", "long", 1_500_000, 0.8), pos("0xb", "long", 900_000, 1.4),
            pos("0xc", "long", 3_000_000, 3.6), pos("0xs", "short", 5_000_000, 1.0)]
    bids, asks = book([(99.5, 2000), (99.0, 2000), (98.5, 2000), (96.5, 20000)],
                      [(100.5, 100)])
    s = la.score_direction(rows, bids, asks, MARK, "down", 2_000_000, 4.0)
    assert s and s["direction"] == "down"
    # L ≥ $2M ilk kez d=1.5'te (0xa+0xb=$2.4M); defter 96.5'e kadar $ ~600K.
    # d=3.75'te $5.4M ama 96.5 seviyesi ($1.93M) yenir → oran düşer.
    assert 1.4 <= s["dist_pct"] <= 1.5, s["dist_pct"]
    assert abs(s["liq_usd"] - 2_400_000) < 1e-6
    assert s["score"] > 3, s["score"]
    assert s["targets"][0]["address"] == "0xa"
    assert la.score_direction(rows, bids, asks, MARK, "down", 10_000_000, 4.0) is None
    # up tarafı: $5M short 1%'de, ask defteri sadece $50K → ince ve devasa oran
    u = la.score_direction(rows, bids, asks, MARK, "up", 2_000_000, 4.0)
    assert u and u["book_thin"] is True and u["score"] > 50, u
    print("✅ d*) eşiği geçen en ucuz uzaklık seçiliyor; eşik altı None;"
          " ince ask defterinde oran fırlıyor")


# ------------------------------------------------ 3) pencere kapalıysa istek yok
def test_window_gate():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "la.db"))
        cfg = Config()
        calls = []

        class C:
            async def l2_book(self, coin):
                calls.append(coin); return {"levels": [[], []]}
        la.hourstats.weekend_window = lambda *a, **k: None
        out = await la.scan(cfg, C(), None)
        assert out["window"] is False and "kapalı" in out["skipped"]
        assert calls == [], "pencere kapalıyken l2Book çekilmemeli"
        st = await dbm.kv_get("liqattack_stats")
        assert st and st["skipped"], "atlanma sebebi kv'ye yazılmalı"
        print("✅ pencere) hafta sonu değilse sıfır istek, sebep kaydediliyor")
    asyncio.run(run())


async def _seed_weekend_db(cfg, ntl_long=2_500_000):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "la2.db"))
    now = dbm.now()
    async with dbm.db() as c:
        await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:SNDK','SNDK')")
        await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:MU','MU')")
        for coin in ("xyz:SNDK", "xyz:MU"):
            await c.execute("INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,"
                            "funding,day_volume) VALUES(?,?,?,1,0,0)", (coin, now, MARK))
        for i, (side, ntl, dist) in enumerate([("long", ntl_long, 1.2),
                                              ("long", 400_000, 2.5),
                                              ("short", 300_000, 1.0)]):
            liq = MARK * (1 - dist / 100) if side == "long" else MARK * (1 + dist / 100)
            await c.execute(
                "INSERT INTO positions_current(coin,address,ts,side,szi,entry_px,"
                "leverage,liq_px,upnl,notional) VALUES('xyz:SNDK',?,?,?,1,100,3,?,0,?)",
                (f"0x{i}", now, side, liq, ntl))
        # MU: uzakta, ön elemeyi geçmemeli
        await c.execute(
            "INSERT INTO positions_current(coin,address,ts,side,szi,entry_px,"
            "leverage,liq_px,upnl,notional) VALUES('xyz:MU','0xm',?,'long',1,100,3,?,0,?)",
            (now, MARK * 0.85, 9_000_000))
    return now


# ------------------------------------------------ 4) tarama + ön eleme + bildirim
def test_scan_alert():
    async def run():
        cfg = Config(); cfg.telegram_chat_id = "-1"; cfg.alert_forensics = False
        await _seed_weekend_db(cfg)
        la.hourstats.weekend_window = lambda *a, **k: (T0, T0 + 2 * 86400)
        calls = []

        class C:
            async def l2_book(self, coin):
                calls.append(coin)
                return {"levels": [[{"px": "99.7", "sz": "500"}, {"px": "99.2", "sz": "500"}],
                                   [{"px": "100.3", "sz": "500"}]]}

        class Bot:
            def __init__(self, ok): self.ok, self.sent = ok, []
            async def send(self, text, chat_id=None):
                if self.ok: self.sent.append(text)
                return self.ok
        from app.notify import Notifier
        # (a) gönderim BAŞARISIZ → marker yok, failed sayılır, sonraki tur dener
        bad = Bot(False)
        out = await la.scan(cfg, C(), Notifier(cfg, bad))
        assert calls == ["xyz:SNDK"], f"MU ön elemeyi geçmemeliydi: {calls}"
        assert out["candidates"] == 1 and out["failed"] == 1 and out["alerted"] == 0, out
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind='liqattack'")
            assert (await cur.fetchone())["n"] == 0, "başarısız gönderim marker yazdı"
        # (b) başarılı → marker var, tekrar etmez
        good = Bot(True)
        out2 = await la.scan(cfg, C(), Notifier(cfg, good))
        assert out2["alerted"] == 1 and len(good.sent) == 1, out2
        assert "LIQ ATTACK ADAYI" in good.sent[0] and "SNDK" in good.sent[0]
        assert "↓" in good.sent[0] and "long patlar" in good.sent[0]
        out3 = await la.scan(cfg, C(), Notifier(cfg, good))
        assert out3["alerted"] == 0 and len(good.sent) == 1, "cooldown çalışmadı"
        # adaylar tabloya yazıldı, sayfa okuyor
        pg = await la.page(cfg)
        assert pg["cands"] and pg["cands"][0]["symbol"] == "SNDK"
        assert pg["cands"][0]["targets"][0]["address"] == "0x0"
        assert pg["cands"][0]["hot"] == 1
        print("✅ tarama) ön eleme uzak coini eledi; başarısız gönderim marker"
              " yazmadı; başarılı gönderim cooldown'a girdi; sayfa hedefleri buluyor")
    asyncio.run(run())


# ------------------------------------------------ 5) geçmiş tespiti (saf)
def test_find_spikes():
    # 40 dk düz, sonra 5 dk'da %-2.2'ye in, 20 dk'da geri dön
    series = [(T0 + i * 60, MARK) for i in range(40)]
    dip = [MARK * (1 - x / 100) for x in (0.8, 1.6, 2.2, 2.0, 1.1, 0.6, 0.3, 0.1)]
    series += [(T0 + (40 + i) * 60, p) for i, p in enumerate(dip)]
    series += [(T0 + (48 + i) * 60, MARK) for i in range(30)]
    ev = la.find_spikes(series, 1.5, 90)
    assert len(ev) == 1 and ev[0]["direction"] == "down", ev
    assert abs(ev[0]["move_pct"] - 2.2) < 0.05
    assert ev[0]["ts_end"] > ev[0]["ts_peak"] > ev[0]["ts_start"]
    # geri DÖNMEYEN hareket olay değil
    trend = [(T0 + i * 60, MARK * (1 - min(i, 60) * 0.05 / 100)) for i in range(120)]
    assert la.find_spikes(trend, 1.5, 90) == []
    print("✅ sıçrama) sıçra-ve-dön tek olay, uç ve dönüş sıralı;"
          " geri dönmeyen hareket olay DEĞİL")


# ------------------------------------------------ 6) tespit + liq onayı + karne
def test_detect_record():
    async def run():
        cfg = Config()
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "la3.db"))
        # En yakın hafta sonu penceresi (bugün de olabilir): detect() hafta
        # sonu dışındaki örnekleri ATIYOR, fikstür hafta içine düşmemeli.
        wk0 = None
        for back in range(0, 10):
            wk0 = hs.weekend_window(dbm.now() - back * 86400, 0)
            if wk0:
                break
        assert wk0, "10 gün içinde hafta sonu bulunamadı"
        base = int(wk0[0]) + 3 * 3600                 # Cmt 03:00 TSİ
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:SNDK','SNDK')")
            await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:MU','MU')")
            for coin, dip_ok in (("xyz:SNDK", True), ("xyz:MU", True)):
                for i in range(120):
                    px = MARK
                    if 60 <= i < 66:
                        px = MARK * (1 - [1.0, 1.8, 2.3, 1.9, 0.9, 0.3][i - 60] / 100)
                    await c.execute("INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,"
                                    "oi,funding,day_volume) VALUES(?,?,?,1,0,0)",
                                    (coin, base + i * 60, px))
            # SNDK: sıçramanın içinde liq olmuş pozisyon → SALDIRI
            await c.execute(
                "INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,"
                "liq_px,upnl,notional,ts,closed_ts) VALUES('xyz:SNDK','0xv','xyz','long',"
                "1,100,5,?,0,1800000,?,?)", (MARK * 0.985, base + 61 * 60, base + 63 * 60))
            # MU: aynı fitil ama liq YOK → saldırı değil
            # SNDK için olaydan ÖNCE aday vardı (skor 4.2) → isabet
            await c.execute(
                "INSERT INTO liq_attack_candidates(coin,direction,ts,weekend_ts,mark,"
                "dist_pct,liq_usd,cost_usd,score,book_thin,n_pos,target_px,dev_close,hot)"
                " VALUES('xyz:SNDK','down',?,?,100,1.5,1800000,400000,4.2,0,1,98.5,0.1,1)",
                (base + 30 * 60, int(wk0[0])))
            # boşa çıkan aday (başka hafta sonu, saldırı yok)
            await c.execute(
                "INSERT INTO liq_attack_candidates(coin,direction,ts,weekend_ts,mark,"
                "dist_pct,liq_usd,cost_usd,score,book_thin,n_pos,target_px,dev_close,hot)"
                " VALUES('xyz:MU','up',?,?,100,2,2500000,300000,8.3,1,2,102,0,1)",
                (base - 7 * 86400, int(wk0[0]) - 7 * 86400))
        n = await la.detect(cfg)
        assert n == 1, f"yalnız liq onaylı olay saldırı sayılmalı: {n}"
        async with dbm.db() as c:
            cur = await c.execute("SELECT * FROM liq_attacks")
            rows = [dict(r) for r in await cur.fetchall()]
        assert rows[0]["coin"] == "xyz:SNDK" and rows[0]["n_liq"] == 1
        assert abs(rows[0]["predicted_score"] - 4.2) < 1e-6, rows[0]
        assert abs(rows[0]["liq_usd"] - 1_800_000) < 1e-6
        assert await la.detect(cfg) == 0, "tekrar taramada çift kayıt olmamalı"
        rec = await la.record(cfg)
        assert rec["attacks"] == 1 and rec["predicted"] == 1 and rec["hit_rate"] == 100.0
        # Yanlış alarm yalnız BİTMİŞ hafta sonları için sayılır (pencere
        # sürerken "gerçekleşmedi" denemez). Bu hafta sonu hâlâ sürüyorsa SNDK
        # adayı henüz sayılmaz — geçen haftanın MU adayı ise boşa çıkmış.
        wk_over = int(wk0[0]) < dbm.now() - 2 * 86400
        exp_flagged, exp_hit = (2, 1) if wk_over else (1, 0)
        assert rec["flagged"] == exp_flagged and rec["flagged_hit"] == exp_hit, rec
        assert rec["false_alarm"] == 1, rec
        print("✅ karne) liq onaylı fitil saldırı, onaysız fitil değil; önceden"
              " işaretlenen isabet, boşa çıkan aday yanlış alarm; idempotent")
    asyncio.run(run())


# ------------------------------------------------ 7) ayar/bekçi/format
def test_wiring():
    from app.config import EDITABLE_FIELDS
    c = Config()
    for f in ("liq_attack_min_usd", "liq_attack_max_dist_pct", "liq_attack_min_score",
              "liq_attack_scan_sec", "liq_attack_cooldown", "liq_attack_spike_pct",
              "liq_attack_revert_min"):
        assert f in EDITABLE_FIELDS and hasattr(c, f), f
        assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
    assert "liq_attack_chat_id" not in EDITABLE_FIELDS, "chat id env-only kalmalı"
    assert c.liq_attack_min_usd == 2_000_000 and c.notify_liqattack is True
    from app.health import limits, periods
    assert "liqattack" in limits(c) and "liqattack" in periods(c)
    from app.notify import KINDS
    assert KINDS["liqattack"][2] == "high"
    src = open("/home/user/hlearninginsiders/app/main.py", encoding="utf-8").read()
    assert '_spawn("liqattack"' in src
    print("✅ bağlantı) 7 ayar künyeli, chat id env-only, bekçi/tip/döngü kayıtlı")


# ------------------------------------------------ 8) elle tarama + hata görünür
def test_manual_and_error():
    async def run():
        cfg = Config(); cfg.telegram_chat_id = "-1"; cfg.alert_forensics = False
        await _seed_weekend_db(cfg)
        hs.weekend_window = lambda *a, **k: (T0, T0 + 2 * 86400)
        calls = []

        class C:
            async def l2_book(self, coin):
                calls.append(coin)
                return {"levels": [[{"px": "99.7", "sz": "500"}], [{"px": "100.3", "sz": "5"}]]}
        out = await la.manual_scan(cfg, C(), None)
        assert out.get("window") is True and calls == ["xyz:SNDK"], out
        again = await la.manual_scan(cfg, C(), None)
        assert again.get("skipped"), "60 sn içinde ikinci tıklama atlanmalı"
        assert calls == ["xyz:SNDK"], "atlanan tıklama istek atmamalı"
        # döngü hata verirse kv'ye yazılır → sayfa 'bekleniyor' değil 'hata' der
        await la._stats({"error": "RuntimeError: ağ yok"})
        pg = await la.page(cfg)
        assert pg["stats"].get("error") == "RuntimeError: ağ yok"
        print("✅ elle) 🔄 tarama döngüyü beklemiyor, 60 sn'de bir; tur hatası"
              " sayfada görünüyor")
    asyncio.run(run())


_REAL_WW = hs.weekend_window

def _restore():
    hs.weekend_window = _REAL_WW

test_depth_cost()
test_score_direction()
test_window_gate();  _restore()
test_scan_alert();   _restore()
test_find_spikes()
test_detect_record()
test_wiring()
test_manual_and_error(); _restore()
print("\n✅ LIQ ATTACK TESTLERİ GEÇTİ")
