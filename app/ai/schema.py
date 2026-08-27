"""Hipotez şeması — "ölçülemiyorsa hipotez değildir".

Modelin ürettiği her iddia buradan geçer. Geçemeyen ATILMAZ, **gözlem** olarak
kaydedilir: sitede görünür ama sicile girmez. Böylece model serbestçe
konuşabilir, ama yalnız sınanabilir dediklerinden sorumlu tutulur.
"""

# metrik -> (hangi konu türüne uyar, kısa açıklama)
#
# Liste KAPALI ve kısa: hepsi zaten periyodik yazdığımız veriden ölçülebiliyor.
# Yeni metrik eklemek demek, önce onu ölçen kodu yazmak demektir.
METRICS: dict[str, tuple[str, str]] = {
    # asset_metrics'ten (mark_px / oi / day_volume)
    "price_move_pct": ("coin", "baz fiyata göre % değişim"),
    "oi_change_pct": ("coin", "baz OI'ye göre % değişim"),
    "volume_ratio": ("coin", "günlük hacmin baz hacme oranı"),
    # positions_current / hl_positions'tan
    "position_closed": ("position", "pozisyon kapandı mı (1 = kapandı)"),
    "position_grew_pct": ("position", "pozisyon boyutunun % değişimi"),
}

OPS = (">=", "<=")
MIN_HORIZON_H = 1
# asset_metrics 45 gün saklanıyor; 7 gün ölçüm için fazlasıyla güvenli sınır.
MAX_HORIZON_H = 168
MAX_CLAIM = 400
MAX_RATIONALE = 600


class Rejected(Exception):
    """Hipotez ölçülebilir değil — gözleme düşecek. Mesaj sebebi söyler."""


def _num(v, name: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise Rejected(f"{name} sayı değil: {v!r}")


def validate(raw: dict, coins: set[str], addresses: set[str]) -> dict:
    """Ham LLM önerisi -> kayda hazır hipotez. Uymuyorsa `Rejected` fırlatır.

    `coins`: `tickers` tablosundaki coin'ler. Kripto BİLEREK dışarıda: fiyat
    metrikleri `asset_metrics`'ten ölçülüyor, o tablo yalnız hisse dex'lerini
    topluyor. BTC hakkında bir hipotezi kaydedip sonra ölçememek, sicili
    "ölçülemedi" ile doldurmaktan başka işe yaramazdı.
    """
    if not isinstance(raw, dict):
        raise Rejected("hipotez sözlük değil")

    metric = str(raw.get("metric") or "").strip()
    if metric not in METRICS:
        raise Rejected(f"bilinmeyen metrik: {metric!r}")
    want_kind, _ = METRICS[metric]

    kind = str(raw.get("subject_kind") or "").strip()
    if kind != want_kind:
        raise Rejected(f"{metric} için konu türü {want_kind!r} olmalı, {kind!r} geldi")

    op = str(raw.get("op") or "").strip()
    if op not in OPS:
        raise Rejected(f"karşılaştırma {OPS} olmalı, {op!r} geldi")

    value = _num(raw.get("value"), "value")

    horizon = raw.get("horizon_h")
    try:
        horizon = int(float(horizon))
    except (TypeError, ValueError):
        raise Rejected(f"horizon_h sayı değil: {horizon!r}")
    if not MIN_HORIZON_H <= horizon <= MAX_HORIZON_H:
        raise Rejected(f"horizon_h {MIN_HORIZON_H}-{MAX_HORIZON_H} olmalı, {horizon} geldi")

    subject = str(raw.get("subject") or "").strip()
    subject_coin = str(raw.get("subject_coin") or "").strip()
    if kind == "coin":
        if subject not in coins:
            raise Rejected(f"evrende olmayan coin: {subject!r}")
        subject_coin = subject
    else:                                     # position
        subject = subject.lower()
        if not subject.startswith("0x") or len(subject) < 10:
            raise Rejected(f"adres gibi görünmüyor: {subject!r}")
        if addresses and subject not in addresses:
            raise Rejected(f"havuzda olmayan adres: {subject!r}")
        if subject_coin not in coins:
            raise Rejected(f"evrende olmayan coin: {subject_coin!r}")

    claim = str(raw.get("claim") or "").strip()
    if len(claim) < 10:
        raise Rejected("iddia metni yok/çok kısa")

    conf = raw.get("confidence")
    conf = _num(conf, "confidence") if conf is not None else 0.5
    conf = min(1.0, max(0.0, conf))

    return {
        "claim": claim[:MAX_CLAIM],
        "rationale": str(raw.get("rationale") or "").strip()[:MAX_RATIONALE],
        "confidence": conf,
        "subject_kind": kind,
        "subject": subject,
        "subject_coin": subject_coin,
        "metric": metric,
        "op": op,
        "value": value,
        "horizon_h": horizon,
    }


def prompt_spec(max_hyp: int) -> str:
    """Sistem promptuna gömülecek şema tarifi — tek kaynaktan üretilir ki
    kod ile promptun anlattığı şey birbirinden ayrı düşmesin."""
    lines = [f"{m}: {desc} (konu türü: {kind})" for m, (kind, desc) in METRICS.items()]
    return (
        "Yalnız GEÇERLİ JSON döndür, başka hiçbir şey yazma. Biçim:\n"
        '{"observations": [{"subject_kind": "coin|position|global", '
        '"subject": "", "text": ""}],\n'
        f' "hypotheses": [ ... en fazla {max_hyp} tane ... ]}}\n\n'
        "Her hipotez ŞU alanlara sahip olmalı:\n"
        '  claim         : iddia (Türkçe, tek cümle)\n'
        '  subject_kind  : "coin" veya "position"\n'
        '  subject       : coin adı (ör. "xyz:NVDA"), position ise adres (0x…)\n'
        '  subject_coin  : position hipotezinde coin adı\n'
        f'  horizon_h     : {MIN_HORIZON_H}-{MAX_HORIZON_H} arası tam sayı (kaç saat sonra ölçülecek)\n'
        '  metric        : ' + " | ".join(METRICS) + "\n"
        '  op            : ">=" veya "<="\n'
        '  value         : sayı (eşik)\n'
        '  rationale     : neden (kısa)\n'
        '  confidence    : 0-1\n\n'
        "Metrikler:\n  " + "\n  ".join(lines) + "\n\n"
        "Hipotez SINANACAK: vadesi gelince aynı veriden ölçülüp tuttu/tutmadı "
        "diye kaydedilecek ve senin sicilin olarak gösterilecek. Bu yüzden "
        "ölçülebilir, dar ve iddialı olanı seç; emin değilsen hipotez yerine "
        "gözlem yaz. Sadece brifingteki coin ve adresleri kullan."
    )
