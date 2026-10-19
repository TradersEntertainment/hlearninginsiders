"""🏓 Dar koridor (app/radar/koridor.py) — PROPR coininde dar koridorda gidip gelme (ölçüm, sinyal değil).

Kullanıcı (19.10): "bu tarz bir bot istiyorum böyle coinlerde bi tık hacimli olanlar tabi pons gibi böyle sürekli gitgel
yapanlar dar bi alanda" → araştırma (koridorlar rastgele fiyatta da aynı sıklıkta; kenar alım-satımı komisyon öncesi
≈ 0) → "dar bant botunu kuralım".

Kararlar: araştırmanın "pivot" dedektörü, kullanıcının kuralı (her kenara ≥4 sıralı temas, %0,7–2, ≥60 dk, ≥$5M, PROPR);
WS'ten 1 dk mum; tek bildirim + tek kırılış notu; yalnız RANGE_CHAT_ID; ölçülü dil (öneri / yön yok).

Pinlenenler:
  • detect: gidip gelen seride koridor (kenarlar, ≥4+4 temas, genişlik); tek yönlü yürüyüşte / genişlik dışında /
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
    assert f and f["nL"] >= 4 and f["nU"] >= 4 and 0.007 <= f["W"] <= 0.02 and f["age_min"] >= 60, f
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
    print("✅ tespit) gidip gelen seride ≥4+4 temas; trend / dar / geniş / seyrek → yok; kırılış yönü")


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
        bars2 = _zigzag_bars(mid=0.2635, n=420)
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


def test_wiring():
    async def run():
        from app import diag, health, notify
        src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
        assert '_spawn("koridor", lambda: koridor.loop(cfg, client, notifier, collector), notifier)' in src("app/main.py")
        assert "koridor.observe(coin, px, ts)" in src("app/hl/collector.py")
        assert notify.KINDS["koridor"][0] == "" and "koridor" not in notify.PUBLIC_KINDS
        cfg = Config()
        assert cfg.range_enabled and cfg.range_chat_id == "" and cfg.range_min_touches == 4
        assert cfg.range_min_vol_usd == 5e6 and cfg.range_min_width_pct == 0.7 and cfg.range_max_width_pct == 2.0
        assert not any(f.startswith("range_") for f in EDITABLE_FIELDS), "env-only"
        assert health.limits(cfg)["koridor"] == 1200 and health.periods(cfg)["koridor"] == 60
        assert fmt.TASK_TR["koridor"] == "dar koridor" and "/koridor" in fmt.help_text()
        assert any(c[0] == "/koridor" for _, cmds in fmt.COMMANDS for c in cmds)
        bot = src("app/telegram/bot.py")
        assert 'getattr(self.cfg, "range_chat_id", "")' in bot and bot.count('elif cmd in ("koridor"') == 2
        readme, env = src("README.md"), src(".env.example")
        assert "## 🏓 Dar koridor (`RANGE_CHAT_ID`)" in readme and "| `/koridor` |" in readme
        assert "**Kör nokta, açıkça:** yalnız WS'te dinlenen coinler" in readme
        for v in ("RANGE_CHAT_ID=", "RANGE_ENABLED=1", "RANGE_MIN_VOL_USD=5000000", "RANGE_MIN_TOUCHES=4",
                  "RANGE_BREAK_NOTE=1"):
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
    test_wiring()
