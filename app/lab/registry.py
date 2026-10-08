"""🧪 Kayıt defteri — kural kaydı (yuva + α dondurma) ve karar anı olay kuyruğu.

Kayıt (`sync`, açılışta): specs.RULES'taki her (id, ver) ilk görüldüğünde `registered_ts` = şimdi
(ileri saat buradan başlar), bir sonraki yuva ve α = LAB_ALPHA / LAB_K_BUDGET kayda DONDURULUR.
Yuva geri dönmez; bütçe dolarsa yeni kural `emekli` (sahip yeni dönem ilan edene dek). Tanım, maliyet
tablosu ya da bağlı ayar değişirse (hash) kural `emekli` — sessizce yeniden tanımlanmış kural olmaz.
'log' kuralları yuva/α almaz, kapıya hiç girmez.

Olay (`emit`): SENKRON, G/Ç yok — radarların sıcak yollarından (WS toplayıcı dahil) çağrılır.
Doğrular, gün tavanını uygular, sınırlı kuyruğa ekler; taşma ve tavan düşüşleri SAYILIR. Tüm DB
yazımı lab döngüsünün `flush`'ında.
"""
from __future__ import annotations

import hashlib
import json
import logging
from collections import OrderedDict, deque

from ..db import db, now
from . import specs
from .costs import COSTS_SHA

log = logging.getLogger("lab.registry")

QUEUE_MAX = 5000
FEATURES_MAX = 512

_Q: deque = deque(maxlen=QUEUE_MAX)
LIVE_OUT: deque = deque(maxlen=200)          # canlı (onaylı) kuralların YENİ yazılan olayları → teslim
ACTIVE: dict[str, dict] = {}                 # rule_id → {spec, ver, status, alpha, registered_ts}
DERIVED: dict[str, list[str]] = {}           # akış ailesi → ondan türetilen etkin kurallar
STATS = {"emitted": 0, "rejected": 0, "overflow": 0, "cap_drop": 0, "flushed": 0, "dup": 0, "seen": 0}
CAP_DROPS: dict[str, int] = {}               # rule_id → gün tavanı yüzünden düşen (toplam)
_DAY: dict[tuple[str, int], int] = {}
# Aynı tetik (kural, coin, anahtar) tekrar tekrar gelir (tarama her turda aynı pozisyonu görür): bellekte
# tekilleştirilir ki gün tavanını tekrarlar doldurmasın. Sınırlı; taşarsa DB'nin UNIQUE'i yine korur.
_SEEN: "OrderedDict[tuple, None]" = OrderedDict()
SEEN_MAX = 50_000


def canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def cfg_snapshot(spec: dict, cfg) -> dict:
    return {k: getattr(cfg, k, None) for k in spec.get("cfg_keys") or []}


def _derive_code_sha() -> str:
    import inspect
    src = "".join(inspect.getsource(f) for f in (specs.match, specs.side_of, specs._et_hm))
    return hashlib.sha256(src.encode()).hexdigest()


def spec_sha(spec: dict, cfg=None) -> str:
    body = {"spec": spec, "cfg": cfg_snapshot(spec, cfg) if cfg is not None else {}, "costs": COSTS_SHA}
    if spec.get("src"):
        body["derive"] = _derive_code_sha()        # süzgeç anlamı değişirse türetilmiş kural emekli
    return hashlib.sha256(canon(body).encode()).hexdigest()


async def _status_row(conn, rule_id: str, ver: int, status: str, note: str, ts: int, by: str = "lab") -> None:
    """Durum izi lab_tests'te (kind='status', hiç budanmaz): onay / emeklilik kimin, ne zaman."""
    await conn.execute(
        "INSERT INTO lab_tests(ts, rule_id, ver, kind, decision, payload) VALUES(?,?,?,?,?,?)",
        (ts, rule_id, ver, "status", status, canon({"note": note, "by": by})))


async def set_status(rule_id: str, ver: int, status: str, note: str = "", by: str = "lab",
                     ts: int | None = None) -> None:
    assert status in specs.STATUSES, status
    ts = int(ts or now())
    async with db() as conn:
        await conn.execute("UPDATE lab_rules SET status=?, status_ts=?, status_note=?"
                           + (", approved_ts=?" if status == "canli" else "")
                           + " WHERE rule_id=? AND ver=?",
                           (status, ts, note, *((ts,) if status == "canli" else ()), rule_id, ver))
        await _status_row(conn, rule_id, ver, status, note, ts, by)
    a = ACTIVE.get(rule_id)
    if a and a["ver"] == ver:
        if status == "emekli":
            ACTIVE.pop(rule_id, None)
        else:
            a["status"] = status


async def sync(cfg, ts: int | None = None, rules=None) -> dict:
    """Kod listesi ↔ lab_rules. Döner: {new, retired, active, slots_used}."""
    ts = int(ts or now())
    rules = specs.RULES if rules is None else rules
    k_budget = max(1, int(getattr(cfg, "lab_k_budget", 40)))
    alpha_lab = float(getattr(cfg, "lab_alpha", 0.05))
    epoch = int(getattr(cfg, "lab_epoch", 1))
    out = {"new": [], "retired": [], "active": 0, "slots_used": 0}
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM lab_rules")
        have = {(r["rule_id"], int(r["ver"])): dict(r) for r in await cur.fetchall()}
        cur = await conn.execute("SELECT COUNT(*) n FROM lab_rules WHERE epoch=? AND slot IS NOT NULL", (epoch,))
        used = int((await cur.fetchone())["n"])
        live_ids = {(r["id"], int(r["ver"])) for r in rules}
        ACTIVE.clear()
        for spec in rules:
            rid, ver = spec["id"], int(spec["ver"])
            sha = spec_sha(spec, cfg)
            row = have.get((rid, ver))
            if row is None:
                slot, alpha, status, note = None, 0.0, "kagit", "kayıt"
                errs = specs.validate(spec)
                if spec.get("evidence") == "frozen_backtest":
                    status = "aday"
                if errs:
                    status, note = "emekli", "tanım geçersiz: " + "; ".join(errs)
                    log.error("lab kuralı %s/v%d geçersiz: %s", rid, ver, note)
                elif spec["evidence"] != "log":
                    if used >= k_budget:
                        status, note = "emekli", f"K bütçesi ({k_budget}) dolu — yeni dönem gerekir"
                    else:
                        used += 1
                        slot, alpha = used, alpha_lab / k_budget
                await conn.execute(
                    "INSERT INTO lab_rules(rule_id, ver, family, title, spec, spec_sha, registered_ts, epoch,"
                    " slot, alpha, evidence, status, status_ts, status_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, ver, spec["family"], spec["title"], canon(spec), sha, ts, epoch, slot, alpha,
                     spec["evidence"], status, ts, note))
                await _status_row(conn, rid, ver, status, note, ts)
                row = {"status": status, "alpha": alpha, "registered_ts": ts, "spec_sha": sha}
                out["new"].append(f"{rid}/v{ver}")
            elif row["spec_sha"] != sha and row["status"] != "emekli":
                note = "tanım, maliyet tablosu ya da bağlı ayar değişti (hash) — yeni sürüm gerekir"
                await conn.execute("UPDATE lab_rules SET status='emekli', status_ts=?, status_note=?"
                                   " WHERE rule_id=? AND ver=?", (ts, note, rid, ver))
                await _status_row(conn, rid, ver, "emekli", note, ts)
                log.error("lab kuralı %s/v%d hash değişti → emekli", rid, ver)
                out["retired"].append(f"{rid}/v{ver}")
                continue
            if row["status"] == "emekli" or row["spec_sha"] != sha:
                continue
            ACTIVE[rid] = {"spec": spec, "ver": ver, "status": row["status"], "alpha": float(row["alpha"] or 0),
                           "registered_ts": int(row["registered_ts"])}
        for (rid, ver), row in have.items():
            if (rid, ver) not in live_ids and row["status"] != "emekli":
                note = "kod listesinde yok"
                await conn.execute("UPDATE lab_rules SET status='emekli', status_ts=?, status_note=?"
                                   " WHERE rule_id=? AND ver=?", (ts, note, rid, ver))
                await _status_row(conn, rid, ver, "emekli", note, ts)
                out["retired"].append(f"{rid}/v{ver}")
    DERIVED.clear()
    for rid, a in ACTIVE.items():
        if a["spec"].get("src"):
            DERIVED.setdefault(a["spec"]["src"], []).append(rid)
    out["active"], out["slots_used"] = len(ACTIVE), used
    return out


def _features(features) -> str | None:
    if not features:
        return None
    s = canon(features)
    if len(s.encode()) <= FEATURES_MAX:
        return s
    keep = dict(features)
    for k in sorted(keep, key=lambda k: -len(canon(keep[k]))):
        keep.pop(k)
        keep["_kirpildi"] = 1
        s = canon(keep)
        if len(s.encode()) <= FEATURES_MAX:
            return s
    return canon({"_kirpildi": 1})


def emit(rule_id: str, coin: str, ts_decision: int, side: int = 0, px_ref: float | None = None,
         trig_key: str | None = None, features: dict | None = None, gated: bool | None = None,
         origin: str = "live") -> bool:
    """Karar anı olayı — SENKRON, G/Ç yok, asla istisna atmaz. Kapılardan (taban, bekleme, ayar,
    bildirim) ÖNCE çağrılır. Döner: kuyruğa girdi mi."""
    try:
        a = ACTIVE.get(rule_id)
        if a is None or not coin:
            STATS["rejected"] += 1
            return False
        ts_d = int(ts_decision)
        tk = str(trig_key if trig_key is not None else ts_d)
        sk = (rule_id, a["ver"], coin, tk)
        if sk in _SEEN:
            STATS["seen"] += 1
            return False
        for did in DERIVED.get(rule_id, ()):          # türetilmiş kurallar: aynı an, donmuş süzgeç
            d = ACTIVE.get(did)
            if d is None:
                continue
            ds = d["spec"]
            if specs.match(ds.get("where"), features or {}, ts_d):
                sd = specs.side_of(ds["side"], int(side or 0), features or {})
                if sd:
                    emit(did, coin, ts_d, side=sd, px_ref=px_ref, trig_key=str(ts_d // int(ds["unit_s"])),
                         features=features, gated=gated, origin=origin)
        day = (rule_id, ts_d // 86400)
        cap = int(a["spec"].get("max_events_day") or 0)
        if cap and _DAY.get(day, 0) >= cap:
            STATS["cap_drop"] += 1
            CAP_DROPS[rule_id] = CAP_DROPS.get(rule_id, 0) + 1
            return False
        _SEEN[sk] = None
        if len(_SEEN) > SEEN_MAX:
            _SEEN.popitem(last=False)
        _DAY[day] = _DAY.get(day, 0) + 1
        if len(_DAY) > 4096:                       # eski günler
            for k in sorted(_DAY, key=lambda k: k[1])[:2048]:
                _DAY.pop(k, None)
        if len(_Q) >= QUEUE_MAX:
            STATS["overflow"] += 1                 # deque en eskiyi atar — sayılır, kartta görünür
        _Q.append({"rule_id": rule_id, "ver": a["ver"], "family": a["spec"]["family"], "origin": origin,
                   "coin": coin, "ts_decision": ts_d, "trig_key": tk,
                   "side": int(side) if side in (1, -1) else 0,
                   "px_ref": float(px_ref) if px_ref else None, "latency_s": int(a["spec"]["latency_s"]),
                   "cluster": ts_d // int(a["spec"]["cluster_s"]), "gated": None if gated is None else int(bool(gated)),
                   "features": _features(features)})
        STATS["emitted"] += 1
        return True
    except Exception:                              # noqa: BLE001 — radar asla lab yüzünden düşmez
        STATS["rejected"] += 1
        log.debug("lab emit hatası", exc_info=True)
        return False


def pending() -> int:
    return len(_Q)


async def flush(limit: int = 2000, ts: int | None = None) -> int:
    """Kuyruğu tek işlemde yaz (INSERT OR IGNORE: aynı kural+coin+tetik anahtarı bir kez)."""
    if not _Q:
        return 0
    from .data import bench_of
    from .. import assets
    ts = int(ts or now())
    items = []
    while _Q and len(items) < limit:
        items.append(_Q.popleft())
    rows, live = [], []
    for e in items:
        klass = assets.klass(e["coin"])
        row = (e["rule_id"], e["ver"], e["family"], e["origin"], e["coin"], klass, e["ts_decision"], ts,
               e["trig_key"], e["side"], e["px_ref"], e["latency_s"], bench_of(e["coin"], klass),
               e["cluster"], e["gated"], e["features"])
        a = ACTIVE.get(e["rule_id"])
        (live if (a and a["status"] == "canli" and e["origin"] == "live") else rows).append((row, e))
    sql = ("INSERT OR IGNORE INTO strat_events(rule_id, rule_ver, family, origin, coin, klass, ts_decision,"
           " ts_logged, trig_key, side, px_ref, latency_s, bench, cluster, gated, features)"
           " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)")
    try:
        async with db() as conn:
            before = conn.total_changes
            await conn.executemany(sql, [r for r, _ in rows])
            fresh = []
            for row, e in live:                        # canlı kural: yalnız GERÇEKTEN yeni olan teslim edilir
                cur = await conn.execute(sql, row)
                if (cur.rowcount or 0) > 0:
                    fresh.append(dict(e))
            wrote = conn.total_changes - before
    except Exception:
        for e in reversed(items):                  # yazılamadı: geri koy (taşarsa sayılır)
            _Q.appendleft(e)
        raise
    LIVE_OUT.extend(fresh)                             # yalnız işlem başarıyla kapandıktan sonra
    STATS["flushed"] += wrote
    STATS["dup"] += len(items) - wrote
    return wrote


def take_live() -> list[dict]:
    out = list(LIVE_OUT)
    LIVE_OUT.clear()
    return out


def stats_line() -> dict:
    return {**STATS, "pending": len(_Q), "active": len(ACTIVE), "cap_by_rule": dict(CAP_DROPS)}
