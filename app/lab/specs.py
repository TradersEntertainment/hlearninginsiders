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


def _fwd(rid: str, family: str, title: str, src: str, where: list, side: str, unit_s: int,
         horizons: tuple, primary_h: int, cluster_s: int, n_max: int, latency_s: int = 60,
         max_day: int = 200) -> dict:
    """İleri (forward) kural: kayıttan SONRAKİ veride, önceden yazılı iki bakış (%50, %100 n_max);
    birincil ölçü birincil ufukta maliyet sonrası piyasa düzeltmeli net getiri (> 0)."""
    return {"id": rid, "ver": 1, "family": family, "title": title, "evidence": "forward", "metric": "net",
            "src": src, "where": where, "side": side, "unit_s": unit_s,
            "horizons_s": list(horizons), "primary_h": primary_h, "exit": None, "latency_s": latency_s,
            "cluster_s": cluster_s, "looks": [0.5, 1.0], "n_max": n_max, "min_g": 20, "bt_alpha_share": 0.0,
            "max_events_day": max_day, "cfg_keys": []}


D = 86400
# İlk dalga ileri kurallar (08.10, veri görülmeden kaydedildi — akış aileleri aynı gün başladı).
# Her yön ayrı kural, ayrı yuva (α = LAB_ALPHA / K). Eşikler sabit; radarın ayarlı tabanı
# eşikten yüksekse olay türemez (taban koşulu) — ayar oynatmak kuralı sessizce değiştirmez.
_VOL_K = [["piyasa", "==", "crypto"], ["usd", ">=", 1_000_000], ["taban", "<=", 1_000_000]]
_VOL_H = [["piyasa", "==", "equity"], ["usd", ">=", 1_000_000], ["taban", "<=", 1_000_000],
          ["kova", "et_not_in", ["09:30", "09:35"]]]
_OPEN5 = [["dk", "==", 30], ["sira", "<=", 5]]
RULES = RULES + (
    _fwd("HAC-KRP-F", "hacim", "Kripto 5 dk hacim rekoru ≥ $1M → kova yönünde, 1 sa", "LOG-VOL", _VOL_K,
         "follow", 4 * H, (15 * 60, H, 4 * H), H, 4 * H, 120),
    _fwd("HAC-KRP-T", "hacim", "Kripto 5 dk hacim rekoru ≥ $1M → kova tersine, 1 sa", "LOG-VOL", _VOL_K,
         "fade", 4 * H, (15 * 60, H, 4 * H), H, 4 * H, 120),
    _fwd("HAC-HSS-F", "hacim", "Hisse 5 dk hacim rekoru ≥ $1M (09:30 kovası hariç) → kova yönünde, 1 sa",
         "LOG-VOL", _VOL_H, "follow", 4 * H, (15 * 60, H, 4 * H), H, 4 * H, 80),
    _fwd("HAC-HSS-T", "hacim", "Hisse 5 dk hacim rekoru ≥ $1M (09:30 kovası hariç) → kova tersine, 1 sa",
         "LOG-VOL", _VOL_H, "fade", 4 * H, (15 * 60, H, 4 * H), H, 4 * H, 80),
    _fwd("ACI-12-F", "acilis", "Açılışın ilk 30 dk en hareketli 5'i → aynı yönde, 10:01–11:59 ET", "LOG-OPEN",
         _OPEN5, "follow", D, (7080,), 7080, D, 60),
    _fwd("ACI-12-T", "acilis", "Açılışın ilk 30 dk en hareketli 5'i → ters yönde, 10:01–11:59 ET", "LOG-OPEN",
         _OPEN5, "fade", D, (7080,), 7080, D, 60),
    _fwd("ACI-16-F", "acilis", "Açılışın ilk 30 dk en hareketli 5'i → aynı yönde, 10:01–15:59 ET", "LOG-OPEN",
         _OPEN5, "follow", D, (21480,), 21480, D, 60),
    _fwd("ACI-16-T", "acilis", "Açılışın ilk 30 dk en hareketli 5'i → ters yönde, 10:01–15:59 ET", "LOG-OPEN",
         _OPEN5, "fade", D, (21480,), 21480, D, 60),
    _fwd("AKI-TWAP-F", "akis", "Kapıdan geçen TWAP emri → emrin yönünde, 4 sa", "LOG-TWAP",
         [["kapi", "in", ["ok", "big"]]], "follow", D, (H, 4 * H, 24 * H), 4 * H, D, 60),
    _fwd("AKI-WFILL-F", "akis", "Hissede ≥ $500K balina dolumu → dolum yönünde, 4 sa", "LOG-WFILL",
         [["kripto", "==", 0], ["usd", ">=", 500_000]], "follow", 4 * H, (H, 4 * H), 4 * H, D, 60),
    _fwd("AKI-NEWBIG-F", "akis", "≥ $1M yeni büyük pozisyon → pozisyon yönünde, 24 sa", "LOG-NEWBIG",
         [["usd", ">=", 1_000_000], ["age_s", "<=", 6 * H]], "follow", D, (4 * H, 24 * H), 24 * H, D, 60),
    _fwd("AKI-STICKY-F", "akis", "Emriyle doğrulanan ≥ $1M yapışkan duvar (açılış) → duvar yönünde, 4 sa",
         "LOG-STICKY", [["sahip", "==", "order"], ["etki", "==", "open"], ["usd", ">=", 1_000_000],
                        ["taban", "<=", 1_000_000]], "follow", D, (H, 4 * H), 4 * H, D, 60),
    _fwd("LIQ-S3-F", "liq", "Kripto liq'e ≤ %0.5 kalan dev pozisyon → liq yönünde, 1 sa", "LOG-CLIQ",
         [["mesafe", "<=", 0.5], ["usd", ">=", 500_000], ["taban", "<=", 500_000]], "follow", D,
         (15 * 60, H), H, 4 * H, 120),
    _fwd("ANO-FUND-T", "anomali", "Funding ≥ %0.05/sa (aşırı) → ödeyen tarafın tersine, 24 sa", "LOG-ANOM",
         [["fund", "abs>=", 0.0005]], "-sign:fund", D, (4 * H, 24 * H), 24 * H, D, 40),
)


# ---------------- türetilmiş kurallar (akış ailesinden süzgeçle) ----------------
# Türetilmiş kural: "src" (akış ailesi) + "where" (DONMUŞ eşikler, ham özellik üzerinde) + "side"
# (follow = olayın yönü, fade = tersi, "-sign:ALAN" = özelliğin işaretinin tersi) + "unit_s" (coin
# başına zaman birimi: birimde ilk olay). Radarın ayarlı tabanı yerine sabit eşik — ayar değişse de
# kural değişmez. match/side_of'un kaynağı türetilmiş kuralın hash'ine girer.

def _et_hm(ts: int) -> str:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.fromtimestamp(int(ts), ZoneInfo("America/New_York")).strftime("%H:%M")


def match(where, feats: dict, ts: int) -> bool:
    """Saf süzgeç: her koşul [alan, op, değer]; eksik alan → eşleşmez. Op: == != >= <= > < in abs>=
    et_not_in ([ss:dd, ss:dd) ET saat aralığı dışında; alan '_ts' = karar anı)."""
    for f, op, v in where or ():
        x = ts if f == "_ts" else feats.get(f)
        if x is None:
            return False
        try:
            ok = {"==": lambda: x == v, "!=": lambda: x != v, ">=": lambda: x >= v, "<=": lambda: x <= v,
                  ">": lambda: x > v, "<": lambda: x < v, "in": lambda: x in v,
                  "abs>=": lambda: abs(x) >= v,
                  "et_not_in": lambda: not (v[0] <= _et_hm(x) < v[1])}[op]()
        except (KeyError, TypeError, ValueError):
            return False
        if not ok:
            return False
    return True


def side_of(mode: str, side: int, feats: dict) -> int:
    """follow → olayın yönü; fade → tersi; '+sign:ALAN' / '-sign:ALAN' → özelliğin işareti. 0 = yön yok."""
    if mode == "follow":
        return side if side in (1, -1) else 0
    if mode == "fade":
        return -side if side in (1, -1) else 0
    if mode[:6] in ("+sign:", "-sign:"):
        x = feats.get(mode[6:])
        if not isinstance(x, (int, float)) or x == 0:
            return 0
        s = 1 if x > 0 else -1
        return s if mode[0] == "+" else -s
    return 0


def validate(spec: dict) -> list[str]:
    """Kayıttan ÖNCE tanım denetimi — hatalı kural yuva harcamaz, 'emekli' (tanım geçersiz) kaydolur."""
    errs: list[str] = []
    ev, metric = spec.get("evidence"), spec.get("metric")
    if ev not in ("log", "forward", "frozen_backtest"):
        errs.append(f"evidence {ev!r}")
    if metric not in ("net", "barrier"):
        errs.append(f"metric {metric!r}")
    hs = [int(h) for h in spec.get("horizons_s") or []]
    ex = spec.get("exit") or None
    ph = int(spec.get("primary_h") if spec.get("primary_h") is not None else -1)
    if ph not in hs and not (ph == 0 and ex):
        errs.append("birincil ufuk listede yok")
    span = int(ex["timeout"]) if (ph == 0 and ex) else ph
    if int(spec.get("cluster_s") or 0) < max(span, 1):
        errs.append("küme genişliği birincil ufuktan kısa (örtüşen olaylar ayrı sayılırdı)")
    if ex and not int(ex.get("timeout") or 0) > 0:
        errs.append("çıkış kuralında zaman aşımı yok")
    if ev in ("forward", "frozen_backtest"):
        looks = [float(x) for x in spec.get("looks") or []]
        if not looks or looks != sorted(looks) or looks[0] <= 0 or abs(looks[-1] - 1.0) > 1e-9:
            errs.append("bakış oranları artan ve 1 ile bitmeli")
        n_max, min_g = int(spec.get("n_max") or 0), int(spec.get("min_g") or 20)
        if looks and -(-looks[0] * n_max // 1) < min_g:
            errs.append(f"ilk bakış {min_g} kümeden önce düşüyor (n_max {n_max})")
        if metric == "barrier" and not (ex and ex.get("tp") and ex.get("sl") and ph == 0):
            errs.append("barrier metriği TP + SL + birincil ufuk 0 ister")
        if ev == "frozen_backtest" and not 0 < float(spec.get("bt_alpha_share") or 0) < 1:
            errs.append("bt_alpha_share (0, 1) dışında")
    if spec.get("src"):
        if spec["src"] not in {r["id"] for r in RULES if r.get("evidence") == "log"}:
            errs.append(f"kaynak aile yok: {spec['src']}")
        if spec.get("side") not in ("follow", "fade") and str(spec.get("side"))[:6] not in ("+sign:", "-sign:"):
            errs.append(f"yön kuralı {spec.get('side')!r}")
        if not int(spec.get("unit_s") or 0) > 0:
            errs.append("unit_s yok")
    return errs


def by_id() -> dict[str, dict]:
    return {r["id"]: r for r in RULES}
