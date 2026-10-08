"""🧪 Önceden kayıt — laboratuvarın kuralları BURADA dondurulur (git commit = kayıt belgesi).

Kural sözlüğü (registry.sync hash'ler; tek alan değişirse kural `emekli`, yeni `ver` = yeni yuva):
  id, ver, family, title       kimlik; title ölçülü Türkçe (tahmin/tavsiye yok)
  evidence                     'log' (yalnız kayıt, α yok, yuva yok) | 'forward' (yalnız ileri veri) |
                               'frozen_backtest' (Aşama A: donmuş geçmişte BİR kez, sonra ileri)
  metric                       'net' (maliyet sonrası piyasa düzeltmeli getiri > 0) | 'barrier' (TP/SL)
  horizons_s, primary_h        ölçülen ufuklar (sn); kapı birincil ufukta
  exit                         None ya da {tp, sl, timeout} kesir/sn — h=0 satırı (birim işlem)
  latency_s                    karar → giriş gecikmesi (kaynak radarın tarama süresi + pay)
  cluster_s                    takvim bloğu genişliği ≥ birincil ufuk (örtüşen olaylar tek küme)
  looks, n_max, min_g          ileri bakış oranları, küme sayısı tavanı, bakış için en az küme
  bt_alpha_share               Aşama A'nın α payı (frozen_backtest)
  max_events_day               gün başına olay tavanı (taşan SAYILIR, kartta görünür)
  cfg_keys                     kurala bağlı ayarlar — anlık değerleri hash'e girer
Akış aileleri önce 'log' olarak girer: karar noktasında ham özellik yazılır, eşik lab içinde
uygulanır. Sayım yetince kendi 'forward' kuralı kaydedilir (yeni id, yeni yuva).
"""
from __future__ import annotations

H = 3600
STATUSES = ("aday", "kagit", "gecti", "canli", "durdu", "emekli")
STATUS_TR = {"aday": "aday (Aşama A bekliyor)", "kagit": "kâğıt üstünde ölçülüyor",
             "gecti": "kapıyı geçti — onay bekliyor", "canli": "canlı (mesaj atar)",
             "durdu": "durduruldu", "emekli": "emekli"}


def _log(rid: str, family: str, title: str, horizons=(15 * 60, H, 4 * H), latency_s: int = 60,
         max_day: int = 400, ver: int = 2) -> dict:
    return {"id": rid, "ver": ver, "family": family, "title": title, "evidence": "log", "metric": "net",
            "horizons_s": list(horizons), "primary_h": max(horizons), "exit": None,
            "latency_s": latency_s, "cluster_s": max(86400, max(horizons)), "looks": [], "n_max": 0,
            "min_g": 20, "bt_alpha_share": 0.0, "max_events_day": max_day, "cfg_keys": []}


# Akış aileleri: kapılardan ÖNCE ham özellik kaydı (radarın kendi eşiği, bildirim ayarı ve
# Telegram teslimi örneklemi seçmesin). Yön: olayın kendi yönü (+1 alış / −1 satış), yoksa 0.
# ts_decision = radarın olayı GÖRDÜĞÜ an (tarama / akış anı); latency_s = tepki süresi (mesaj + el).
# v2 (08.10): v1 gecikmeyi tarama süresi sanıyordu — kayıt noktaları gerçek görme anını yazıyor.
RULES: tuple[dict, ...] = (
    _log("LOG-TWAP", "akis", "TWAP kararı (uyarılan + kapıdan kalan)"),
    _log("LOG-STICKY", "akis", "Yapışkan duvar (sahibi bilinen emir)"),
    _log("LOG-NEWBIG", "akis", "Yeni büyük pozisyon (oto tarama)", horizons=(H, 4 * H, 24 * H)),
    _log("LOG-WFILL", "akis", "Balina dolumu (canlı akış)", latency_s=30, max_day=1500),
    _log("LOG-ANOM", "akis", "OI / funding anomalisi", horizons=(H, 4 * H, 24 * H)),
    _log("LOG-VOL", "akis", "5 dk hacim rekoru"),
    _log("LOG-OPEN", "akis", "Açılışın en hareketlisi (ilk 5 / 30 dk)", horizons=(30 * 60, 2 * H, 6 * H),
         max_day=40),
    _log("LOG-CLIQ", "akis", "Kripto likidasyon aşama geçişi (doğrulanmamış)", horizons=(15 * 60, H, 4 * H)),
)


def by_id() -> dict[str, dict]:
    return {r["id"]: r for r in RULES}
