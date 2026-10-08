"""🧪 Donmuş, temkinli maliyet tablosu — her olayın net getirisi buradan düşülür (08.10).

Sınıf başına gidiş-dönüş (giriş + çıkış) kesirler:
  • kripto  (HL ana dex): taker %0.045 × 2 = 0.0009; kayma 5 bp/taraf → 0.0010, seans dışı aynı
  • hisse / endeks (HIP-3 xyz): taker %0.09 × 2 = 0.0018 — DOĞRULANMADI: sahip PROPR ücretini
    verene dek temkinli YER TUTUCU; kayma seans içi 5 bp/taraf 0.0010, seans dışı 15 bp/taraf 0.0030
  • emtia / doviz = hisse ile aynı (aynı dex, aynı yer tutucu)
  • funding_bound_h = 0.00002 (%0.002/sa) hepsinde: funding bilinmezse saatlik en kötü sınır —
    YER TUTUCU, doğrulanmadı.

Tablo donmuştur: kanonik JSON'unun sha256'sı (COSTS_SHA) kuralın spec hash'ine girer; tablo
değişirse kural yeniden kaydolur (yeni sürüm) — eski sonuçlar sessizce yeni maliyetle karışmaz.
Bilinmeyen sınıf "hisse" sayılır (en pahalı satırlardan biri — temkinli; büyük/küçük harf
düzeltilmez, yanlış yazılan "KRIPTO" ucuz satıra kaçmaz)."""
from __future__ import annotations

import hashlib
import json
import math

_HIP3 = {"fee_rt": 0.0018, "slip_rt_on": 0.0010, "slip_rt_off": 0.0030, "funding_bound_h": 0.00002}

COSTS: dict[str, dict[str, float]] = {
    "kripto": {"fee_rt": 0.0009, "slip_rt_on": 0.0010, "slip_rt_off": 0.0010, "funding_bound_h": 0.00002},
    "hisse": dict(_HIP3),
    "endeks": dict(_HIP3),
    "emtia": dict(_HIP3),
    "doviz": dict(_HIP3),
}
FALLBACK = "hisse"          # bilinmeyen sınıf → bu satır


def table_sha(table: dict) -> str:
    """Tablonun kanonik JSON'unun (sıralı anahtar, boşluksuz, ASCII) sha256'sı — anahtar sırasından
    bağımsız, süreçten bağımsız."""
    blob = json.dumps(table, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


COSTS_SHA: str = table_sha(COSTS)


def cost(klass: str, off_hours: bool, hold_s: int, side: int,
         funding_h: list[float] | None = None) -> dict:
    """Bir gidiş-dönüş işlemin maliyeti (kesir): {"fee", "slip", "funding", "total"}.

    funding = side·Σ funding_h (pozitif oranda long öder → maliyet artar; short alır → azalır).
    funding_h None → funding_bound_h·(hold_s/3600) — yönden bağımsız MALİYET (temkinli). Listede
    None/NaN olan saat de sınırla (yön fark etmeksizin maliyet) doldurulur. off_hours None (seans
    bilinmiyor) → seans dışı sayılır (pahalı kayma, temkinli). funding_h None iken hold_s sonlu
    olmalı (NaN sessizce sıfır funding'e dönmesin) → değilse ValueError."""
    if side not in (1, -1):
        raise ValueError(f"side ±1 olmalı: {side!r}")
    row = COSTS.get(klass) or COSTS[FALLBACK]
    fee = row["fee_rt"]
    slip = row["slip_rt_off"] if (off_hours is None or off_hours) else row["slip_rt_on"]
    bound = row["funding_bound_h"]
    if funding_h is None:
        if hold_s is None or not math.isfinite(hold_s):
            raise ValueError(f"hold_s sonlu olmalı: {hold_s!r}")
        funding = bound * max(0, hold_s) / 3600.0
    else:
        funding = 0.0
        for f in funding_h:
            if f is None or not math.isfinite(f):
                funding += bound
            else:
                funding += side * float(f)
    return {"fee": fee, "slip": slip, "funding": funding, "total": fee + slip + funding}


def net(ret_adj: float, c: dict) -> float:
    """Maliyet sonrası getiri: ret_adj − c["total"]."""
    return ret_adj - c["total"]
