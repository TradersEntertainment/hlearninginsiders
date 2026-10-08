"""🧪 Örüntü denetimi (R3) — kayıtlı pattern_signals satırları gerçekten para kazandırır mıydı?

Örüntü uyarısı "şu şekil geçmişte n kez görüldü, sonra yükselen pay %p, taban %b" diyordu; eski sicil
satırı mükerrer satır sayıyor ve p_up ≥ 50'yi isabet sayıyordu (piyasa yönünü ödüllendirir). Bu
denetim tek, dürüst sayı üretir: maliyet sonrası, yönlü, kümeli ortalama getiri.

Kurallar (DONMUŞ — specs'teki kuralın parçası, kod hash'i kurala girer):
  • yalnız çözülmüş satırlar (hit/miss); ölçülemeyen ayrı sayılır, "tutmadı" sayılmaz
  • sorgu barı qb = resolve_ts − ufuk·TF; (coin, tf, ufuk, qb) başına en erken satır (mükerrer yok)
  • (coin, tf, ufuk) içinde örtüşmeyen inceltme: sonraki satır ancak qb ≥ önceki resolve_ts + TF
  • küme: "alertable" = n ≥ 20, |z| ≥ 2, |fark| ≥ 10 (uyarı varsayılanlarının DONMUŞ kopyası — ayar
    oynasa da denetim aynı); "all" = hepsi
  • yön = farkın işareti (p_up − taban); giriş = satır yazıldıktan SONRA kapanan ilk barın kapanışı
    (kayıtlı px kullanılmaz: kapanmamış bar olabilir); çıkış = resolve_ts'te açılan barın kapanışı
  • getiri aritmetik, maliyet donmuş tablodan (funding bilinmez → temkinli yer tutucu); 2 günlük
    takvim kümeleri
Fiyat: bars arşivi (1h ~180 gün, 15m ~60 gün kapanışları).
"""
from __future__ import annotations

import logging

from ..db import db
from . import costs, stats

log = logging.getLogger("lab.audit")

TF_SEC = {"1h": 3600, "15m": 900}
ALERT_MIN_N, ALERT_Z, ALERT_EDGE = 20, 2.0, 10.0


def is_alertable(r: dict) -> bool:
    return ((r.get("n_match") or 0) >= ALERT_MIN_N and r.get("z") is not None and abs(r["z"]) >= ALERT_Z
            and r.get("edge") is not None and abs(r["edge"]) >= ALERT_EDGE)


def dedupe_thin(rows: list[dict]) -> list[dict]:
    """Saf: mükerrer sorgu barını at, (coin, tf, ufuk) içinde örtüşen pencereleri incelt. Zaman sıralı."""
    first: dict[tuple, dict] = {}
    for r in sorted(rows, key=lambda r: (int(r["ts"]), int(r["id"]))):
        tf = TF_SEC.get(r["tf"])
        if not tf or r.get("resolve_ts") is None:
            continue
        qb = int(r["resolve_ts"]) - int(r["horizon"]) * tf
        k = (r["coin"], r["tf"], int(r["horizon"]), qb)
        if k not in first:
            first[k] = {**r, "qb": qb, "tf_s": tf}
    out, last_end = [], {}
    for r in sorted(first.values(), key=lambda r: (r["qb"], int(r["ts"]))):
        g = (r["coin"], r["tf"], int(r["horizon"]))
        if g in last_end and r["qb"] < last_end[g] + r["tf_s"]:
            continue
        last_end[g] = int(r["resolve_ts"])
        out.append(r)
    return sorted(out, key=lambda r: (int(r["ts"]), r["coin"]))


def trade(r: dict, closes: dict[int, float], klass: str) -> dict | None:
    """Saf: tek satırın işlemi. closes: bar AÇILIŞ ts → kapanış. None = fiyat yok (ölçülemedi)."""
    tf = r["tf_s"]
    side = 1 if (r.get("edge") or 0) > 0 else -1
    t_in = (int(r["ts"]) - 1) // tf * tf                   # kapanışı (t_in + tf) ≥ ts olan ilk bar
    p_in = closes.get(t_in)
    p_out = closes.get(int(r["resolve_ts"]))
    if not p_in or not p_out or t_in >= int(r["resolve_ts"]):
        return None
    ret = side * (p_out / p_in - 1)
    hold = int(r["resolve_ts"]) + tf - (t_in + tf)
    # seans bilgisi yok: hisse/endeks seans DIŞI (pahalı kayma) sayılır — temkinli
    c = costs.cost(klass, klass != "kripto", hold, side, None)
    return {"side": side, "ret": ret, "net": ret - c["total"], "entry_ts": t_in + tf, "hold": hold}


async def _closes(coin: str, tf: str) -> dict[int, float]:
    async with db() as conn:
        cur = await conn.execute("SELECT ts, c FROM bars WHERE coin=? AND tf=?", (coin, tf))
        return {int(r["ts"]): float(r["c"]) for r in await cur.fetchall() if r["c"]}


async def clusters(spec: dict, since: int = 0, until: int | None = None, ts: int | None = None) -> dict:
    """Denetim örneği → kümeli net getiri. since/until satırın yazıldığı an (ts) üzerinden:
    Aşama A = kayıttan ÖNCE, ileri bakış = kayıttan SONRA. Yalnız çözülmüş (hit/miss) satır."""
    from .. import assets
    width = int(spec.get("cluster_s") or 2 * 86400)
    q = ("SELECT * FROM pattern_signals WHERE status IN ('hit','miss','unresolvable') AND ts >= ?"
         + (" AND ts < ?" if until else ""))
    async with db() as conn:
        cur = await conn.execute(q, (int(since), *((int(until),) if until else ())))
        rows = [dict(r) for r in await cur.fetchall()]
    if ts is not None:
        rows = [r for r in rows if int(r["resolve_ts"] or 0) + 2 * TF_SEC.get(r["tf"], 3600) <= int(ts)]
    rows = dedupe_thin(rows)
    if spec.get("set") == "alertable":
        rows = [r for r in rows if is_alertable(r)]
    unres = sum(1 for r in rows if r["status"] == "unresolvable")
    cache: dict[tuple, dict] = {}
    nets, keys, n_nopx = [], [], 0
    for r in rows:
        if r["status"] == "unresolvable":
            continue
        k = (r["coin"], r["tf"])
        if k not in cache:
            cache[k] = await _closes(*k)
        t = trade(r, cache[k], assets.klass(r["coin"]))
        if t is None:
            n_nopx += 1
            continue
        nets.append(t["net"])
        keys.append(stats.block_key(int(r["ts"]), width))
    xc, _sizes, ks = stats.cluster_means(nets, keys)
    n_all = len(rows)
    return {"xc": xc, "keys": ks, "n_ev": len(nets), "n_all": n_all, "unres": unres + n_nopx,
            "unres_share": ((unres + n_nopx) / n_all) if n_all else 0.0, "outcomes": None}
