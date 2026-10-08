"""🧪 Laboratuvar görünümü (/lab sayfası, /strat komutu) — YALNIZ BETİMLEME.

Burada hesaplanan hiçbir sayı kuralın durumunu değiştirmez: durum yalnız planlı bakışta (gate) ve
sahibin onayıyla değişir. Kayıttan sonraki (ileri) veri ÖNCE gösterilir; geçmiş veri "seçim örneği"
etiketiyle. Her satırda n, küme sayısı, ortalama net ve %95 GA; tek bir "isabet" yüzdesi asla
tek başına yazılmaz.
"""
from __future__ import annotations

import json
import math

from ..db import db, kv_get, now
from . import specs

LABEL_LOG = "yalnız kayıt — kapıya girmez, mesaj atmaz"
NOTE = "betimleme — karar yalnız önceden yazılı bakış noktasında"


def _f(x):
    return None if x is None or (isinstance(x, float) and not math.isfinite(x)) else x


def h_label(h: int) -> str:
    if h == 0:
        return "çıkış kuralı"
    if h % 3600 == 0:
        return f"{h // 3600} sa"
    return f"{h // 60} dk"


async def _summ(rows: list[dict]) -> dict:
    """Ufuk başına: küme ortalamaları üzerinden betimleme (net, maliyet sonrası)."""
    from .stats import cluster_means, effect_summary
    vals = [r["net"] for r in rows if r["net"] is not None]
    keys = [r["cluster"] for r in rows if r["net"] is not None]
    if not vals:
        return {"n_ev": 0, "n_c": 0}
    xc, _sizes, _ks = cluster_means(vals, keys)
    e = effect_summary(xc, vals)
    lo, hi = e["ci"]
    return {"n_ev": e.get("n_ev", len(vals)), "n_c": e["n_c"], "mean": _f(e["mean"]), "lo": _f(lo), "hi": _f(hi),
            "hit": _f(e.get("hit")), "worst": _f(e.get("worst_ev")),
            "raw": _f(sum(r["ret_raw"] for r in rows if r["ret_raw"] is not None)
                      / max(1, sum(1 for r in rows if r["ret_raw"] is not None)))}


async def overview(ts: int | None = None) -> dict:
    ts = int(ts or now())
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM lab_rules ORDER BY (status='emekli'), registered_ts, rule_id, ver")
        rules = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute(
            "SELECT rule_id, rule_ver, status, COUNT(*) n, MIN(ts_decision) first, MAX(ts_decision) last"
            " FROM strat_events GROUP BY rule_id, rule_ver, status")
        counts: dict = {}
        for r in await cur.fetchall():
            c = counts.setdefault((r["rule_id"], int(r["rule_ver"])), {"n": 0, "first": None, "last": None})
            c[r["status"]] = int(r["n"])
            c["n"] += int(r["n"])
            c["first"] = min(x for x in (c["first"], r["first"]) if x is not None) if r["first"] else c["first"]
            c["last"] = max(x for x in (c["last"], r["last"]) if x is not None) if r["last"] else c["last"]
        cur = await conn.execute(
            "SELECT e.rule_id, e.rule_ver, e.cluster, e.origin, o.h, o.net, o.ret_raw FROM strat_outcomes o"
            " JOIN strat_events e ON e.id = o.event_id WHERE o.status='done'")
        outs: dict = {}
        for r in await cur.fetchall():
            outs.setdefault((r["rule_id"], int(r["rule_ver"]), int(r["h"]), r["origin"] or "live"), []).append(dict(r))
        cur = await conn.execute("SELECT * FROM lab_tests ORDER BY ts DESC, id DESC LIMIT 30")
        trail = [dict(r) for r in await cur.fetchall()]
    code = specs.by_id()
    out = []
    for r in rules:
        key = (r["rule_id"], int(r["ver"]))
        try:
            spec = json.loads(r["spec"] or "{}")
        except (TypeError, ValueError):
            spec = {}
        hs = []
        for h in [*(spec.get("horizons_s") or []), *([0] if spec.get("exit") else [])]:
            fwd = await _summ(outs.get((*key, int(h), "live"), []))
            bt = await _summ(outs.get((*key, int(h), "backfill"), []))
            hs.append({"h": int(h), "label": h_label(int(h)), "fwd": fwd, "bt": bt,
                       "primary": int(h) == int(spec.get("primary_h") or -1)})
        c = counts.get(key, {"n": 0})
        out.append({**r, "spec_d": spec, "counts": c, "horizons": hs,
                    "status_tr": specs.STATUS_TR.get(r["status"], r["status"]),
                    "evidence_tr": {"log": LABEL_LOG, "forward": "yalnız ileri veri (kayıttan sonra)",
                                    "frozen_backtest": "Aşama A donmuş geçmişte bir kez + ileri veri"}
                    .get(r["evidence"], r["evidence"]),
                    "in_code": r["rule_id"] in code and int(code[r["rule_id"]]["ver"]) == int(r["ver"])})
    st = await kv_get("lab_stats") or {}
    return {"rules": out, "trail": trail, "stats": st, "ts": ts, "note": NOTE,
            "n_active": sum(1 for r in out if r["status"] != "emekli"),
            "n_live": sum(1 for r in out if r["status"] == "canli")}


async def recent_events(rule_id: str, limit: int = 30) -> list[dict]:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM strat_events WHERE rule_id=? ORDER BY ts_decision DESC LIMIT ?", (rule_id, limit))
        evs = [dict(r) for r in await cur.fetchall()]
        if evs:
            q = ",".join("?" * len(evs))
            cur = await conn.execute(f"SELECT * FROM strat_outcomes WHERE event_id IN ({q})", [e["id"] for e in evs])
            by: dict = {}
            for o in await cur.fetchall():
                by.setdefault(o["event_id"], []).append(dict(o))
            for e in evs:
                e["outs"] = sorted(by.get(e["id"], []), key=lambda o: o["h"])
                try:
                    e["feat"] = json.loads(e["features"] or "{}")
                except (TypeError, ValueError):
                    e["feat"] = {}
    return evs
