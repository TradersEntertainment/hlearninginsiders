"""🏓 Dar koridor (app/radar/koridor.py) — PROPR coininde dar koridorda gidip gelme (ölçüm, sinyal değil).

Kullanıcı (19.10): "bu tarz bir bot istiyorum böyle coinlerde bi tık hacimli olanlar tabi pons gibi böyle sürekli gitgel
yapanlar dar bi alanda" → araştırma (koridorlar rastgele fiyatta da aynı sıklıkta; kenar alım-satımı komisyon öncesi
≈ 0) → "dar bant botunu kuralım".

Kararlar: araştırmanın "pivot" dedektörü, kullanıcının kuralı (her kenara ≥4 sıralı temas, %0,7–2, ≥60 dk, ≥$5M, PROPR);
WS'ten 1 dk mum; tek bildirim + tek kırılış notu; yalnız RANGE_CHAT_ID; ölçülü dil (öneri / yön yok).

Pinlenenler:
  • detect: gidip gelen seride koridor (kenarlar, ≥3+3 (toplam ≥7) temas, genişlik); tek yönlü yürüyüşte / genişlik dışında /
    seyrek işlemde yok; yalnız geçmiş mumlar (son mumdan sonrası eklenince t0 aynı kalır)
  • broke: kapanış kenarın %25 W dışında → yön; içeride → None
  • observe / close_minutes: evren dışı coin yok sayılır; dakika mumu (o/h/l/c/n); işlemsiz dakika düz n=0 mum;
    uzun boşluk (> MAX_GAP_FILL) eski mumları atar
  • scan: RANGE_CHAT_ID'ye bir kez; chat yoksa mesaj yok ama koridor kaydedilir; aynı koridor ikinci kez gelmez;
    kırılınca tek "kırıldı" notu, bildirilmemiş koridorun kırılışı yazılmaz
  • mesaj: ilk 4 satır (ne/genişlik/yaş · PROPR · kenarlar+temas · eğim+hacim), geçmiş ölçüm satırı, dipnot; yasak dil yok
  • kablolama: config env-only (EDITABLE_FIELDS'ta yok), KINDS (PUBLIC'te yok), spawn, kollektör kancası, health,
    TASK_TR, /tani satırı, /koridor (COMMANDS + help), _own_chats, README + .env
"""
import asyncio
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import koridor as k  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
T0 = 1_792_380_000 // 60 * 60


def _zigzag_bars(n=400, mid=1.0, amp=0.0045, period=40, drift=0.0, noise=0.0004):
    """Kenardan kenara gidip gelen fiyat (üçgen dalga, ~%0,9 genişlik), her dakika işlem var."""
    out = []
    for i in range(n):
        ph = (i % period) / period
        tri = 4 * ph - 1 if ph < 0.5 else 3 - 4 * ph            # −1 … +1
        c = mid * math.exp(amp * tri + drift * i + noise * math.sin(i * 1.7))
        out.append([T0 + 60 * i, c, c * 1.0006, c * 0.9994, c, 5])
    return out


def _reset():
    k.BARS.clear(); k.CUR.clear(); k.ELIGIBLE.clear(); k.ACTIVE.clear()       # noqa: E702
    for x in ("sent", "broken", "found", "skipped_nochat", "sent_day"):
        k.STATS[x] = 0


def test_detect_and_break():
    P = k.params(Config())
    bars = _zigzag_bars()
    f = k.detect(bars, P)
    assert f and f["nL"] >= 3 and f["nU"] >= 3 and f["nL"] + f["nU"] >= 7 and 0.007 <= f["W"] <= 0.02 \
        and f["age_min"] >= 60, f
    assert f["lower"] < bars[-1][4] * 1.01 and f["upper"] > bars[-1][4] * 0.99
    assert "yatay" in fmt._slope_txt(f["slope"], f["W"])
    # yalnız geçmiş: seriyi kesip yeniden sormak aynı son-mum sonucunu verir
    f2 = k.detect(bars[:300], P)
    assert f2 and f2["t0"] == bars[299][0]
    # tek yönlü yürüyüş: koridor yok
    assert k.detect(_zigzag_bars(amp=0.0045, drift=0.0004), P) is None
    # çok dar (%0,3) ya da çok geniş (%5): yok
    assert k.detect(_zigzag_bars(amp=0.0015), P) is None and k.detect(_zigzag_bars(amp=0.025), P) is None
    # seyrek işlem (dakikaların %50'si n=0): yok
    sparse = [b[:5] + [0 if i % 2 else 3] for i, b in enumerate(_zigzag_bars())]
    assert k.detect(sparse, P) is None
    # kırılış
    up = math.exp(f["u0"] + f["W"] * 0.4)
    assert k.broke(f, [f["t0"] + 60, up, up, up, up, 3], P) == "up"
    dn = math.exp(f["l0"] - f["W"] * 0.4)
    assert k.broke(f, [f["t0"] + 60, dn, dn, dn, dn, 3], P) == "down"
    mid = math.exp((f["u0"] + f["l0"]) / 2)
    assert k.broke(f, [f["t0"] + 60, mid, mid, mid, mid, 3], P) is None
    print("✅ tespit) gidip gelen seride ≥3+3 (toplam ≥7) temas; trend / dar / geniş / seyrek → yok; kırılış yönü")


def test_bars_from_trades():
    _reset()
    k.ELIGIBLE.add("PONS")
    k.observe("OTHER", 1.0, T0)                         # evren dışı
    k.observe("PONS", 1.00, T0 + 1)
    k.observe("PONS", 1.02, T0 + 20)
    k.observe("PONS", 0.99, T0 + 40)
    k.observe("PONS", 1.01, T0 + 59)
    k.observe("PONS", 1.05, T0 + 61)                    # yeni dakika: önceki kapanır
    b = list(k.BARS["PONS"])[0]
    assert b == [T0, 1.00, 1.02, 0.99, 1.01, 4] and "OTHER" not in k.BARS and "OTHER" not in k.CUR, b
    k.close_minutes(T0 + 60 * 4 + 5)                    # T0+60 kapanır, T0+120 ve T0+180 işlemsiz → düz n=0
    bars = list(k.BARS["PONS"])
    assert [x[0] for x in bars] == [T0, T0 + 60, T0 + 120, T0 + 180] and bars[2][1:] == [1.05, 1.05, 1.05, 1.05, 0]
    # uzun boşluk (WS kopukluğu): eski mumlar atılır
    k.observe("PONS", 1.1, T0 + 60 * (5 + k.MAX_GAP_FILL + 2))
    k.observe("PONS", 1.1, T0 + 60 * (6 + k.MAX_GAP_FILL + 2))
    assert len(k.BARS["PONS"]) == 1
    print("✅ mum) WS işleminden 1 dk mum; işlemsiz dakika düz n=0; uzun boşluk eski mumları atar; evren dışı yok")


class _Notifier:
    def __init__(self):
        self.sent = []

    async def send(self, kind, text, **kw):
        self.sent.append((kind, text, kw))
        return True


def test_scan_send_once_and_break():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "kor.db"))
        from app.radar import twaplive
        twaplive.VOL_CACHE.update(ts=dbm.now(), v={"PONS": (13.1e6, dbm.now())})
        _reset()
        cfg = Config()
        cfg.range_chat_id = ""
        n = _Notifier()
        bars = _zigzag_bars(mid=0.2635)
        k.ELIGIBLE.add("PONS")
        k.BARS["PONS"] = __import__("collections").deque(bars, maxlen=k.KEEP)
        t = bars[-1][0] + 65
        # chat yok: mesaj yok, koridor kaydedilir (bildirilmedi)
        assert await k.scan(cfg, n, t=t) == [] and not n.sent and k.ACTIVE["PONS"]["sent"] is False
        assert k.STATS["skipped_nochat"] == 1
        # kırılış (bildirilmemiş): not yok, koridor düşer
        cor = k.ACTIVE["PONS"]
        up = math.exp(cor["u0"] + cor["W"])
        k.BARS["PONS"].append([k.BARS["PONS"][-1][0] + 60, up, up, up, up, 4])
        await k.scan(cfg, n, t=t + 60)
        assert "PONS" not in k.ACTIVE and not n.sent
        # chat var: bir kez gönderilir
        _reset()
        k.ELIGIBLE.add("PONS")
        cfg.range_chat_id = "-100777"
        bars2 = _zigzag_bars(mid=0.2635, n=430)        # son kapanış bandın ortasında (kenar payı şartı)
        k.BARS["PONS"] = __import__("collections").deque([[b[0] + 86400] + b[1:] for b in bars2], maxlen=k.KEEP)
        t2 = k.BARS["PONS"][-1][0] + 65
        out = await k.scan(cfg, n, t=t2)
        assert len(out) == 1 and n.sent[-1][0] == "koridor" and n.sent[-1][2]["chat_id"] == "-100777"
        assert n.sent[-1][2]["public"] is False
        txt = out[0].split("\n")
        assert txt[0].startswith("🏓 <b>PONS</b> · dar koridor %") and "sürüyor" in txt[0]
        assert txt[1] == "✅ <b>PROPR'da listeli</b>" and txt[2].startswith("alt ") and "temas" in txt[2]
        assert "24s hacim $13.1M" in txt[3] and "Geçmiş ölçüm" in out[0] and "Gözlem aracıdır" in out[0]
        # açıkken yeniden aranmaz; kv'de kayıtlı (restart: ikinci kez gelmez)
        await k.scan(cfg, n, t=t2 + 1)
        assert len(n.sent) == 1 and (await dbm.kv_get(k.ACTIVE_KV))["PONS"]["sent"] is True
        k.ACTIVE.clear()
        k.ACTIVE.update(await dbm.kv_get(k.ACTIVE_KV))
        # kırılış: tek not
        cor = k.ACTIVE["PONS"]
        dn = math.exp(cor["l0"] - cor["W"])
        k.BARS["PONS"].append([k.BARS["PONS"][-1][0] + 60, dn, dn, dn, dn, 4])
        out = await k.scan(cfg, n, t=t2 + 60)
        assert len(out) == 1 and out[0].startswith("🏓✂️ <b>PONS</b> koridoru kırıldı (aşağı)") and "alt kenarın %" in out[0]
        assert k.STATS["broken"] == 1 and "PONS" not in k.ACTIVE
        # aynı koridor (alert_log) yeniden bulunursa ikinci kez gönderilmez
        k.BARS["PONS"] = __import__("collections").deque([[b[0] + 86400] + b[1:] for b in bars2], maxlen=k.KEEP)
        await k.scan(cfg, n, t=t2)
        assert len(n.sent) == 2, [x[1][:40] for x in n.sent]
        from tests.test_wording import hits
        assert not hits(out[0]) and not hits(fmt.koridor_alert("PONS", cor, 13.1e6))
        assert not hits(fmt.koridor_list(k.active_rows(), k.STATS, 3, 2, False))
        _reset()
    asyncio.run(run())
    print("✅ tarama) chat yoksa mesaj yok; bir kez; restart'ta ikinci kez yok; tek kırılış notu; ölçülü dil")


def test_review_fixes():
    """İnceleme (19.10, yeniden üretildi): kırılış yalnız son mumda aranıyordu; evren restart'ta 10 dk boş kalıyordu;
    okunamayan geri doldurma eski mumları düz mumla birleştiriyordu; RANGE_ENABLED=0 bekçiyi alarma geçiriyordu."""
    async def run():
        import collections
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "kor3.db"))
        _reset()
        P = k.params(Config())
        cfg = Config()
        cfg.range_chat_id = "-1"
        n = _Notifier()
        # 1) toplu gelen mumlarda aradaki kırılış (+18 dışarıda, +19..+20 içeride) yakalanır, doğru mumla yazılır
        cor = {"t0": T0, "start_ts": T0 - 3600, "u0": math.log(101), "l0": math.log(99), "W": math.log(101 / 99),
               "slope": 0.0, "sent": True, "checked": T0, "nU": 4, "nL": 4, "lower": 99, "upper": 101}
        k.ELIGIBLE.add("X")
        k.ACTIVE["X"] = dict(cor)
        bars = [[T0 + 60 * i, 100, 100, 100, 100, 3] for i in range(1, 21)]
        bars[17] = [T0 + 60 * 18, 110, 110, 110, 110, 3]
        k.BARS["X"] = collections.deque(bars, maxlen=k.KEEP)
        out = await k.scan(cfg, n, t=T0 + 60 * 21 + 5)
        assert len(out) == 1 and "kırıldı (yukarı)" in out[0] and fmt.tr_time(T0 + 60 * 18) in out[0], out
        assert "X" not in k.ACTIVE
        # 2) evren: kollektör henüz abone değilken boş → her turda yeniden bakılır (10 dk beklenmez)
        _reset()

        class Col:
            connected, connected_since, valid_coins = False, None, set()
        from app.radar import twaplive
        twaplive.VOL_CACHE.update(ts=dbm.now(), v={"PONS": (60e6, dbm.now())})
        assert await k._refresh_universe(cfg, Col()) == set() and not k.ELIGIBLE
        Col.valid_coins = {"PONS"}
        await k._refresh_universe(cfg, Col())
        assert k.ELIGIBLE == {"PONS"}
        src = open(os.path.join(ROOT, "app/radar/koridor.py"), encoding="utf-8").read()
        assert "if tm - last_uni >= UNIVERSE_SEC or not ELIGIBLE:" in src and "filled -= gone" in src
        # 3) geri doldurma okunamazsa eski mumlar atılır, başarılı sayılmaz; okunursa WS kuyruğu korunur
        k.BARS["PONS"] = collections.deque([[T0, 1, 1, 1, 1, 1]], maxlen=k.KEEP)

        class Bad:
            async def candles(self, *a):
                raise TimeoutError("x")
        old = k.BACKFILL_GAP_SEC
        k.BACKFILL_GAP_SEC = 0
        try:
            assert await k._backfill(Bad(), ["PONS"], cfg) == set() and "PONS" not in k.BARS
            end = int(__import__("time").time()) // 60 * 60
            k.BARS["PONS"] = collections.deque([[end, 2, 2, 2, 2, 7]], maxlen=k.KEEP)     # WS'ten gelen yeni dakika

            class Good:
                async def candles(self, coin, iv, s, e):
                    return [{"t": (end - 60 * i) * 1000, "o": 1, "h": 1, "l": 1, "c": 1, "n": 2} for i in range(3, 0, -1)]
            assert await k._backfill(Good(), ["PONS"], cfg) == {"PONS"}
            assert [b[0] for b in k.BARS["PONS"]] == [end - 180, end - 120, end - 60, end] and k.BARS["PONS"][-1][5] == 7
        finally:
            k.BACKFILL_GAP_SEC = old
        # 4) kapalıyken de nabız
        assert "while True:                                     # kapalıyken de nabız" in src
        # 5) metin: "%100'inde" ek hatası yok; geçmiş satırı rastgele karşılaştırmasını ve örneklemi söyler
        assert "işlemli dakika %" in fmt.koridor_alert("PONS", {**cor, "age_min": 60, "close": 100, "act_pct": 100}, 1e7)
        assert "ortanca 45 dk" in k.HIST_LINE and "%41'inde kenardan kenara" in k.HIST_LINE
        assert "rastgele fiyatla aynı" in k.HIST_LINE and "%0" not in k.HIST_LINE
        _reset()
    asyncio.run(run())
    print("✅ inceleme) toplu mumda kırılış; boş evren yeniden bakılır; okunamayan doldurma eski mumu atar, WS kuyruğu"
          " korunur; kapalıyken nabız; metin")


def test_skhx_regression():
    """Kullanıcı (20.10, SKHX ekranı): "bu koridor botu hiç olmadı btw çok kötü neden skhx kaçırdı mesela ? mis gibi
    kanal". Gerçek HL 1 dk mumları (tests/fixtures): dipler 912,0 / 913,3 / 912,4 / 912,2, tepeler 920,5 / 923,7 / 924,3.

    Pinlenenler: yeni kural (3+3, %25 tolerans, toplam ≥7, paralellik %50, uyum ≤%10, en iyi oturan aday) 09:17'de
    GÖRÜLEN kanalı (912,4–926) yakalar; eski kural (4+4, %15, %30) 09:30'a kadar yakalamaz. 08:59'da yalnız 05:22'den
    başlayan eğik, kötü oturan aday vardı (906–922) — uyum şartı onu eler (eskiden en uzun aday seçiliyordu)."""
    import json as _json
    import time as _time
    d = _json.load(open(os.path.join(ROOT, "tests", "fixtures", "skhx_20_10_1m.json")))
    bars = d["bars"]
    tsi = lambda t: _time.strftime("%H:%M", _time.gmtime(t + 3 * 3600))   # noqa: E731
    P = k.params(Config())
    assert P["min_touch"] == 3 and P["touch_tol"] == 0.25 and P["min_total"] == 7
    assert P["parallel_tol"] == 0.5 and P["fit_max"] == 0.1 and P["min_w_rv"] == 1.0 and P["edge_gap"] == 0.1
    first = None
    for t in range(300, len(bars)):
        f = k.detect(bars[max(0, t - 719):t + 1], P)
        if f:
            first = f
            break
    assert first and tsi(first["t0"]) == "09:17", (first and tsi(first["t0"]))
    assert 912 <= first["lower"] <= 913 and 925 <= first["upper"] <= 927 and first["nL"] + first["nU"] >= 7, first
    assert tsi(first["start_ts"]) == "07:27", tsi(first["start_ts"])
    # uyum şartı olmasa 08:59'da eğik aday (05:22'den, alt ~906) gönderilirdi
    i859 = next(i for i, b in enumerate(bars) if tsi(b[0]) == "08:59")
    loose = k.detect(bars[max(0, i859 - 719):i859 + 1], {**P, "fit_max": 1.0})
    assert loose and loose["lower"] < 908 and tsi(loose["start_ts"]) == "05:22", loose
    assert k.detect(bars[max(0, i859 - 719):i859 + 1], P) is None
    old = {**P, "min_touch": 4, "touch_tol": 0.15, "min_total": 0, "parallel_tol": 0.3, "fit_max": 1.0}
    assert not any(k.detect(bars[max(0, t - 719):t + 1], old) for t in range(300, len(bars))), "eski kural yakalamazdı"
    print("✅ SKHX 20.10) yeni kural 09:17'de görülen kanal (912,4–926, 07:27'den); eski 4+4 / %15 kural yakalamıyordu")


def test_lit_late_gates():
    """Kullanıcı (20.10, LIT ekranı): "bu bildirim niye iş işten geçtikten sonra geliyor bot tamamen kullanışsız".
    LIT: başlangıç 09:13, bildirim 10:14, kırılış 10:16 (bildirimden sonra 3 dk). Teknik gecikme yok (mum kapanışından
    ~2–4 sn sonra yazıldı); 60 dk şartı yalnız 8 dk geciktirdi. Ölçüm (72 coin, 17–20.10): erken uyarı işe yaramıyor —
    15–30. dakikada bulunan koridorların ortanca kalan ömrü 9–10 dk; geç gelen uyarıların ortak izi (1) koridor son 60
    dk'nın oynaklığından dar (W/RV < 1: ortanca 15–19 dk), (2) son kapanış kenarda ya da dışında (ortanca 7–23 dk).
    Kararlar: RANGE_MIN_WIDTH_RV=1,0 ve RANGE_EDGE_GAP_PCT=10 (0 = kapalı); 60 dk / temas şartları aynı.

    Pinlenenler: kapılar kapalıyken 10:13 mumunda canlı mesajın koridoru (09:13, 3,1936–3,2198, 4+4; W/RV 0,66, konum
    0,01) çıkar; kapılarla çıkmaz. Kapılarla LIT'te ilk koridor 09:18'de (08:07'den, 3,2064–3,2580) ve 10:16'da kırılır
    (bildirimden 58 dk sonra). Kenar payı / oynaklık kapısı 0'la kapanır."""
    import json as _json
    import time as _time
    bars = _json.load(open(os.path.join(ROOT, "tests", "fixtures", "lit_20_10_1m.json")))["bars"]
    tsi = lambda t: _time.strftime("%H:%M", _time.gmtime(t + 3 * 3600))   # noqa: E731
    P = k.params(Config())
    off = {**P, "min_w_rv": 0.0, "edge_gap": 0.0}
    j = next(i for i, b in enumerate(bars) if tsi(b[0]) == "10:13")
    f = k.detect(bars[j - 719:j + 1], off)
    assert f and tsi(f["start_ts"]) == "09:13" and abs(f["lower"] - 3.1936) < 1e-3 and abs(f["upper"] - 3.2198) < 1e-3
    assert f["nL"] == 4 and f["nU"] == 4 and f["w_rv"] < 0.8 and f["pos"] < 0.1, f
    assert k.detect(bars[j - 719:j + 1], P) is None
    # kapılarla: daha geniş ve oynaklığa göre rahat koridor 09:18'de, 58 dk yaşıyor
    first = None
    for i in range(j - 70, len(bars)):
        g = k.detect(bars[max(0, i - 719):i + 1], P)
        if g:
            first = g
            break
    assert first and tsi(first["t0"]) == "09:18" and tsi(first["start_ts"]) == "08:07", first and tsi(first["t0"])
    assert abs(first["lower"] - 3.2064) < 1e-3 and abs(first["upper"] - 3.258) < 1e-3
    assert first["w_rv"] >= 1.0 and 0.1 <= first["pos"] <= 0.9
    brk = next(b for b in bars if b[0] > first["t0"] and k.broke(first, b, P))
    assert tsi(brk[0]) == "10:16" and (brk[0] - first["t0"]) // 60 == 58
    # kenar payı kapısı: zigzag'ın son kapanışı üst kenarda (konum 0,98) → elenir; 0 = kapalı → geçer
    z = _zigzag_bars(mid=0.2635, n=420)
    assert k.detect(z, P) is None and k.detect(z, {**P, "edge_gap": 0.0})["pos"] > 0.9
    cfg = Config()
    cfg.range_min_width_rv, cfg.range_edge_gap_pct = 0.0, 0.0
    assert k.params(cfg)["min_w_rv"] == 0.0 and k.params(cfg)["edge_gap"] == 0.0
    print("✅ LIT 20.10) 10:14'teki geç uyarı (W/RV 0,66, kenarda) elenir; kapılarla 09:18'de 58 dk yaşayan koridor")


def test_cooldown_not_stored():
    """Bekleme süresinde (RANGE_COIN_COOLDOWN_MIN) bulunan koridor eskiden "bildirilmedi" diye ACTIVE'e yazılıyordu:
    coin o koridor kırılana dek kilitleniyor, bekleme bitince de yazılmıyordu (17–20.10: 80 koridor, 5842 coin-dk sessiz).

    Pinlenenler: bekleme içinde bulunan koridor kaydedilmez ve yazılmaz; bekleme bitince aynı koridor yazılır."""
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "kor3.db"))
        from app.radar import twaplive
        twaplive.VOL_CACHE.update(ts=dbm.now(), v={"PONS": (60e6, dbm.now())})
        _reset()
        cfg = Config()
        cfg.range_chat_id = "-100777"
        n = _Notifier()
        k.ELIGIBLE.add("PONS")
        bars = _zigzag_bars(mid=0.2635, n=430)
        k.BARS["PONS"] = __import__("collections").deque(bars, maxlen=k.KEEP)
        await dbm.alert_log("koridor_coin", "PONS", "")          # az önce başka bir koridor yazılmış
        t = bars[-1][0] + 65
        assert await k.scan(cfg, n, t=t) == [] and not n.sent and "PONS" not in k.ACTIVE
        assert k.STATS["found"] == 0
        async with dbm.db() as conn:                              # bekleme bitti (3 sa önce yazılmış gibi)
            await conn.execute("UPDATE alerts_log SET ts=ts-? WHERE kind='koridor_coin'", (181 * 60,))
            await conn.commit()
        out = await k.scan(cfg, n, t=t + 1)
        assert len(out) == 1 and len(n.sent) == 1 and k.ACTIVE["PONS"]["sent"] is True
        _reset()
    asyncio.run(run())
    print("✅ bekleme) bekleme içinde bulunan koridor kilitlemez; bekleme bitince yazılır")


def test_wiring():
    async def run():
        from app import diag, health, notify
        src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
        assert '_spawn("koridor", lambda: koridor.loop(cfg, client, notifier, collector), notifier)' in src("app/main.py")
        assert "koridor.observe(coin, px, ts)" in src("app/hl/collector.py")
        assert notify.KINDS["koridor"][0] == "" and "koridor" not in notify.PUBLIC_KINDS
        cfg = Config()
        assert cfg.range_enabled and cfg.range_chat_id == "" and cfg.range_min_touches == 3
        assert cfg.range_min_vol_usd == 50e6 and cfg.range_min_width_pct == 0.7 and cfg.range_max_width_pct == 2.0
        assert cfg.range_min_total_touches == 7 and cfg.range_touch_tol_pct == 25 and cfg.range_coin_cooldown_min == 180
        assert cfg.range_min_width_rv == 1.0 and cfg.range_edge_gap_pct == 10
        assert not any(f.startswith("range_") for f in EDITABLE_FIELDS), "env-only"
        assert health.limits(cfg)["koridor"] == 1200 and health.periods(cfg)["koridor"] == 60
        assert fmt.TASK_TR["koridor"] == "dar koridor" and "/koridor" in fmt.help_text()
        assert any(c[0] == "/koridor" for _, cmds in fmt.COMMANDS for c in cmds)
        bot = src("app/telegram/bot.py")
        assert 'getattr(self.cfg, "range_chat_id", "")' in bot and bot.count('elif cmd in ("koridor"') == 2
        readme, env = src("README.md"), src(".env.example")
        assert "## 🏓 Dar koridor (`RANGE_CHAT_ID`)" in readme and "| `/koridor` |" in readme
        assert "**Kör nokta, açıkça:** yalnız WS'te dinlenen coinler" in readme
        for v in ("RANGE_CHAT_ID=", "RANGE_ENABLED=1", "RANGE_MIN_VOL_USD=50000000", "RANGE_MIN_TOUCHES=3",
                  "RANGE_MIN_TOTAL_TOUCHES=7", "RANGE_TOUCH_TOL_PCT=25", "RANGE_COIN_COOLDOWN_MIN=180",
                  "RANGE_MIN_WIDTH_RV=1.0", "RANGE_EDGE_GAP_PCT=10", "RANGE_BREAK_NOTE=1"):
            assert v in env, v
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "kor2.db"))
        await dbm.kv_set(k.STATS_KV, {"coins": 38, "ready": 36, "active": 2, "sent_day": 5, "found": 9, "broken": 6,
                                      "errors": 0, "backfill": 38})
        txt = "\n".join(await diag._subsystems(cfg))
        line = [x for x in txt.split("\n") if "dar koridor:" in x]
        assert line and "38 coin izleniyor" in line[0] and "açık koridor 2" in line[0] \
            and "RANGE_CHAT_ID TANIMSIZ" in line[0], line
    asyncio.run(run())
    print("✅ kablolama) env-only ayarlar, KINDS (public değil), spawn, kanca, sağlık, TASK_TR, /koridor, _own_chats,"
          " README + .env, /tani satırı")


if __name__ == "__main__":
    test_detect_and_break()
    test_bars_from_trades()
    test_scan_send_once_and_break()
    test_review_fixes()
    test_skhx_regression()
    test_wiring()
