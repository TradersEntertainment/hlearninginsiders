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


def _summ(rows: list[dict]) -> dict:
    """Ufuk başına betimleme — girdi SQL'de küme başına toplanmış satırlar (s = Σnet, n, mn = en kötü,
    pos = pozitif sayısı, sr/nr = maliyet öncesi). Bootstrap küme ortalamaları üzerinde."""
    from .stats import effect_summary
    import numpy as np
    rows = [r for r in rows if r["n"]]
    if not rows:
        return {"n_ev": 0, "n_c": 0}
    xc = np.array([r["s"] / r["n"] for r in rows], dtype=float)
    e = effect_summary(xc)
    lo, hi = e["ci"]
    n_ev = sum(int(r["n"]) for r in rows)
    nr = sum(int(r["nr"] or 0) for r in rows)
    return {"n_ev": n_ev, "n_c": e["n_c"], "mean": _f(e["mean"]), "lo": _f(lo), "hi": _f(hi),
            "hit": sum(int(r["pos"] or 0) for r in rows) / n_ev, "worst": _f(min(r["mn"] for r in rows)),
            "raw": (sum(float(r["sr"] or 0) for r in rows) / nr) if nr else None}


AGG = ("SELECT e.rule_id, e.rule_ver, o.h, COALESCE(e.origin, 'live') origin, e.cluster,"
       " SUM(o.net) s, COUNT(o.net) n, MIN(o.net) mn, SUM(CASE WHEN o.net > 0 THEN 1 ELSE 0 END) pos,"
       " SUM(o.ret_raw) sr, COUNT(o.ret_raw) nr"
       " FROM strat_outcomes o JOIN strat_events e ON e.id = o.event_id WHERE o.status='done'")
AGG_GROUP = " GROUP BY e.rule_id, e.rule_ver, o.h, origin, e.cluster"
_CACHE: dict = {"ts": 0, "v": None}
CACHE_S = 60


async def _agg(where: str = "", args: tuple = ()) -> dict:
    async with db() as conn:
        cur = await conn.execute(AGG + where + AGG_GROUP, args)
        out: dict = {}
        for r in await cur.fetchall():
            out.setdefault((r["rule_id"], int(r["rule_ver"]), int(r["h"]), r["origin"]), []).append(dict(r))
    return out


def _horizons(spec: dict, key: tuple, outs: dict) -> list[dict]:
    hs = []
    for h in [*(spec.get("horizons_s") or []), *([0] if spec.get("exit") else [])]:
        hs.append({"h": int(h), "label": h_label(int(h)), "fwd": _summ(outs.get((*key, int(h), "live"), [])),
                   "bt": _summ(outs.get((*key, int(h), "backfill"), [])),
                   "primary": int(h) == int(spec.get("primary_h") or -1)})
    return hs


async def rule_card(rule_id: str, ver: int) -> dict | None:
    """Tek kuralın karnesi (canlı olay mesajı için) — tüm tabloyu taramaz."""
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM lab_rules WHERE rule_id=? AND ver=?", (rule_id, ver))
        r = await cur.fetchone()
    if not r:
        return None
    r = dict(r)
    spec = json.loads(r["spec"] or "{}")
    outs = await _agg(" AND e.rule_id=? AND e.rule_ver=?", (rule_id, ver))
    return {**r, "spec_d": spec, "horizons": _horizons(spec, (rule_id, int(ver)), outs),
            "status_tr": specs.STATUS_TR.get(r["status"], r["status"])}


async def overview(ts: int | None = None, fresh: bool = False) -> dict:
    """Tüm kurallar — 60 sn önbellekli (sayfa yenilemesi tabloyu her seferinde taramasın)."""
    ts = int(ts or now())
    if not fresh and _CACHE["v"] is not None and ts - _CACHE["ts"] < CACHE_S and _CACHE["ts"] <= ts:
        return _CACHE["v"]
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
        cur = await conn.execute("SELECT * FROM lab_tests ORDER BY ts DESC, id DESC LIMIT 30")
        trail = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT * FROM lab_tests WHERE kind IN ('backtest', 'look', 'demote')"
                                 " ORDER BY rule_id, ver, ts")
        tests: dict = {}
        for r in await cur.fetchall():
            tests.setdefault((r["rule_id"], int(r["ver"])), []).append(dict(r))
    outs = await _agg()
    code = specs.by_id()
    out = []
    for r in rules:
        key = (r["rule_id"], int(r["ver"]))
        try:
            spec = json.loads(r["spec"] or "{}")
        except (TypeError, ValueError):
            spec = {}
        c = counts.get(key, {"n": 0})
        out.append({**r, "spec_d": spec, "counts": c, "tests": tests.get(key, []),
                    "horizons": _horizons(spec, key, outs) if r["status"] != "emekli" else [],
                    "status_tr": specs.STATUS_TR.get(r["status"], r["status"]),
                    "evidence_tr": {"log": LABEL_LOG, "forward": "yalnız ileri veri (kayıttan sonra)",
                                    "frozen_backtest": "Aşama A donmuş geçmişte bir kez + ileri veri"}
                    .get(r["evidence"], r["evidence"]) + (" · örüntü sinyalleri üzerinden denetim (olay kaydı yok)"
                                                           if spec.get("custom") == "oru" else ""),
                    "in_code": r["rule_id"] in code and int(code[r["rule_id"]]["ver"]) == int(r["ver"])})
    st = await kv_get("lab_stats") or {}
    v = {"rules": out, "trail": trail, "stats": st, "ts": ts, "note": NOTE,
         "n_active": sum(1 for r in out if r["status"] != "emekli"),
         "n_live": sum(1 for r in out if r["status"] == "canli")}
    _CACHE.update(ts=ts, v=v)
    return v


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
