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
    for r in out:
        try:
            r["progress"] = await progress(r, r["spec_d"], ts)
        except Exception as e:                         # noqa: BLE001 — bir kural sayfayı düşürmesin
            r["progress"] = {"line": f"ilerleme hesaplanamadı: {type(e).__name__}", "steps": [], "rank": None}
    ranked = [r for r in out if (r["progress"] or {}).get("rank") is not None and r["status"] in ("aday", "kagit", "gecti")]
    closest = min(ranked, key=lambda r: r["progress"]["rank"]) if ranked else None
    st = await kv_get("lab_stats") or {}
    v = {"rules": out, "trail": trail, "stats": st, "ts": ts, "note": NOTE,
         "closest": ({"rule_id": closest["rule_id"], "ver": closest["ver"], "line": closest["progress"]["line"]}
                     if closest else None),
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


# ---------------- sinyale ne kaldı (yalnız SAYIM — ara p/z gösterilmez) ----------------

def _z(a: float) -> float | None:
    from statistics import NormalDist
    return NormalDist().inv_cdf(1 - a) if a and 0 < a < 1 else None


def _days_txt(d: float) -> str:
    if d < 1:
        return f"~{max(1, round(d * 24))} sa"
    return f"~{d:.0f} gün" if d >= 2 else f"~{d:.1f} gün"


async def progress(r: dict, spec: dict, ts: int) -> dict:
    """Kural sinyale ne kadar uzak: adımlar, sıradaki bakışa kalan küme, bu hızla en erken ne zaman.
    Kapının saydığı kümeler (looks.clusters_for) — aynı fonksiyon, aynı sayı. Test istatistiği YOK."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from . import gate, looks
    tr = ZoneInfo("Europe/Istanbul")
    st, ev = r["status"], r["evidence"]
    out = {"line": "", "steps": [], "rank": None, "guard": "", "pipe": 0}
    if ev == "log":
        out["line"] = "yalnız kayıt — sinyal üretemez (yuvasız)"
        return out
    if st == "emekli":
        out["line"] = f"emekli — {r.get('status_note') or ''}".strip(" —")
        return out
    rule = {**spec, "alpha": float(r["alpha"] or 0)}
    thr, inc = gate.thresholds(rule), gate.alpha_increments(rule)
    zs = [_z(a) for a in inc]
    async with db() as conn:
        cur = await conn.execute("SELECT look_no, decision FROM lab_tests WHERE rule_id=? AND ver=?"
                                 " AND kind IN ('backtest', 'look') ORDER BY look_no", (r["rule_id"], r["ver"]))
        done = {int(x["look_no"]): x["decision"] for x in await cur.fetchall()}
        cur = await conn.execute("SELECT COUNT(*) n FROM strat_events WHERE rule_id=? AND rule_ver=?"
                                 " AND status IN ('pending', 'open')", (r["rule_id"], r["ver"]))
        out["pipe"] = int((await cur.fetchone())["n"])
    g, fc = 0, None
    steps = [{"label": "kayıt", "state": "done", "note": datetime.fromtimestamp(int(r["registered_ts"]), tr)
              .strftime("%d.%m")}]
    if ev == "frozen_backtest":
        from .specs import STAGE_A_WAIT
        if 0 in done:
            steps.append({"label": "Aşama A (geçmiş)", "state": "done", "note": done[0]})
        else:
            left = int(r["registered_ts"]) + STAGE_A_WAIT - ts
            steps.append({"label": "Aşama A (geçmiş)", "state": "now",
                          "note": f"{max(0, left) // 3600} sa sonra" if left > 0 else "sıradaki saatlik turda"})
    if st in ("kagit", "gecti", "canli", "durdu") and (ev == "forward" or 0 in done):
        fc = await looks.clusters_for(r["rule_id"], int(r["ver"]), spec, int(r["registered_ts"]), ts)
        g = len(fc["xc"])
        out["unres_share"] = fc.get("unres_share")
    nxt = max([k for k in done if k > 0], default=0) + 1
    for k, (t, z) in enumerate(zip(thr, zs), start=1):
        state = "done" if k in done else ("now" if (st == "kagit" and k == nxt and (ev == "forward" or 0 in done))
                                           else "todo")
        steps.append({"label": f"{k}. bakış", "state": state,
                      "note": (done[k] if k in done else f"{min(g, t)}/{t} küme · eşik z ≥ {z:.2f}" if z else f"{t} küme")})
    steps.append({"label": "sahip onayı", "state": "now" if st == "gecti" else ("done" if st == "canli" else "todo"),
                  "note": "/strat KURAL ya da onay mesajı"})
    steps.append({"label": "canlı (mesaj)", "state": "done" if st == "canli" else "todo", "note": ""})
    out["steps"] = steps
    if st == "aday":
        out["line"] = next(s["note"] for s in steps if s["label"].startswith("Aşama A"))
        out["line"] = f"Aşama A (geçmiş veri testi) {out['line']}"
        out["rank"] = max(0, int(r["registered_ts"]) + 2 * 86400 - ts) / 86400
    elif st == "kagit" and nxt <= len(thr):
        t = thr[nxt - 1]
        rem = max(0, t - g)
        line = f"{nxt}. bakışa {rem} küme kaldı ({g}/{t})"
        if g and fc and len(fc["keys"]):
            width = int(spec.get("cluster_s") or 86400)
            first = int(min(fc["keys"])) * width
            # kümeler pencere + birincil ufuk + pay geçince sayılır → hız, sayılabilen zaman aralığından
            span = int(spec.get("primary_h") or 0) or int((spec.get("exit") or {}).get("timeout") or 0)
            lag = 0 if spec.get("custom") else span + looks.SETTLE + width
            rate = g / max((ts - lag - first) / 86400, 1 / 24)
            days = rem / rate if rate > 0 else None
            if days is not None:
                when = datetime.fromtimestamp(ts + days * 86400, tr).strftime("%d.%m")
                line += f" · bu hızla en erken {_days_txt(days)} ({when})" if rem else " · bakış sıradaki turda"
                out["rank"] = days
        else:
            line += " · henüz kapanmış küme yok"
        if out["pipe"]:
            line += f" · ölçümde {out['pipe']} olay"
        out["line"] = line
        z = zs[nxt - 1]
        out["guard"] = (f"geçmek için: ortalama net > 0 ve üç test birlikte tek yönlü z ≥ {z:.2f}; iki yarı aynı"
                        f" işaret; ölçülemeyen ≤ %20; 60 kümeden azsa sola çarpık değil") if z else ""
    elif st == "gecti":
        out["line"] = "kapıyı geçti — onay sorusu gönderildi (/strat KURAL'dan da onaylanır)"
        out["rank"] = 0
    elif st == "canli":
        out["line"] = "canlı — yeni olayları mesaj olarak geliyor"
    elif st == "durdu":
        out["line"] = f"durduruldu — {r.get('status_note') or ''}".strip(" —")
    return out


# ---------------- açık kâğıt pozisyonlar ----------------

async def prices_now(coins: list[str]) -> dict[str, tuple[float, int]]:
    """AĞSIZ şimdiki fiyat: ana dex kv (metrik döngüsünün yazdığı mark), HIP-3 asset_metrics son satırı."""
    from ..hl.universe import MAIN_CTX_KV
    out: dict[str, tuple[float, int]] = {}
    ctx = await kv_get(MAIN_CTX_KV) or {}
    for c in coins:
        rec = (ctx.get("c") or {}).get(c)
        if ":" not in c and rec and rec.get("m"):
            out[c] = (float(rec["m"]), int(ctx.get("ts") or 0))
    rest = [c for c in coins if c not in out]
    if rest:
        async with db() as conn:
            for c in rest:
                cur = await conn.execute("SELECT mark_px, ts FROM asset_metrics WHERE coin=? ORDER BY ts DESC LIMIT 1",
                                         (c,))
                x = await cur.fetchone()
                if x and x["mark_px"]:
                    out[c] = (float(x["mark_px"]), int(x["ts"]))
    return out


async def open_positions(ts: int | None = None, limit: int = 200) -> dict:
    """Girişi yapılmış, ufukları sürmekte olan kâğıt pozisyonlar (türetilmiş kurallar; LOG-* aileleri hariç)."""
    from . import costs, resolver
    ts = int(ts or now())
    async with db() as conn:
        cur = await conn.execute(
            "SELECT e.*, MIN(o.due_ts) next_due, COUNT(o.h) n_open FROM strat_events e"
            " JOIN strat_outcomes o ON o.event_id = e.id AND o.status='open'"
            " WHERE e.status='open' AND e.rule_id NOT LIKE 'LOG-%' GROUP BY e.id ORDER BY e.entry_ts DESC LIMIT ?",
            (limit,))
        rows = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT COUNT(*) n FROM strat_events WHERE status='pending' AND rule_id NOT LIKE 'LOG-%'")
        pending = int((await cur.fetchone())["n"])
        cur = await conn.execute("SELECT rule_id, ver, title, evidence FROM lab_rules")
        meta = {(m["rule_id"], int(m["ver"])): dict(m) for m in await cur.fetchall()}
    px = await prices_now(sorted({r["coin"] for r in rows}))
    for r in rows:
        m = meta.get((r["rule_id"], int(r["rule_ver"]))) or {}
        r["title"], r["izleme"] = m.get("title") or "", m.get("evidence") == "log"
        side = r["side"] if r["side"] in (1, -1) else 1
        p = px.get(r["coin"])
        r["px_now"], r["px_age"] = (p[0], ts - p[1]) if p else (None, None)
        if p and r["entry_px"]:
            ret = side * (p[0] / float(r["entry_px"]) - 1)
            c = costs.cost(resolver.cost_class(r["coin"], r["klass"]),
                           resolver.off_hours(r["klass"] or "hisse", int(r["entry_ts"]), ts),
                           max(0, ts - int(r["entry_ts"])), side, None)
            r["ret_now"], r["net_now"] = ret, ret - c["total"]
        else:
            r["ret_now"] = r["net_now"] = None
        r["left_s"] = (int(r["next_due"]) - ts) if r["next_due"] else None
    return {"rows": rows, "pending": pending, "ts": ts}


# ---------------- kâğıt hesap ----------------

async def paper_for(r: dict, spec: dict, cfg) -> dict | None:
    """Yuvalı kuralın kayıttan sonraki birincil ufuk işlemleri → sim ayarıyla kâğıt hesap (betimleme)."""
    from . import paper
    from ..radar import sim
    if spec.get("custom") or r["evidence"] == "log" or r["status"] == "emekli":
        return None
    h = int(spec.get("primary_h") or 0)
    async with db() as conn:
        cur = await conn.execute(
            "SELECT e.coin, e.entry_ts, o.exit_ts, o.ret_raw, o.cost FROM strat_outcomes o JOIN strat_events e"
            " ON e.id = o.event_id WHERE e.rule_id=? AND e.rule_ver=? AND e.origin='live' AND o.h=?"
            " AND o.status='done' AND o.ret_raw IS NOT NULL AND o.cost IS NOT NULL AND e.ts_decision >= ?",
            (r["rule_id"], r["ver"], h, r["registered_ts"]))
        rows = [dict(x) for x in await cur.fetchall()]
    trades = [{"entry_ts": x["entry_ts"], "exit_ts": x["exit_ts"], "net": x["ret_raw"] - x["cost"], "coin": x["coin"]}
              for x in rows]
    start = float(getattr(cfg, "sim_start_balance", 10_000) or 10_000)
    res = paper.replay(trades, start, float(getattr(cfg, "sim_leverage", 5) or 5),
                       float(getattr(cfg, "sim_margin_pct", 33) or 33), int(r["registered_ts"]))
    res["svg"] = sim.svg_curve(res["points"], start) if res["n"] else ""
    res.pop("points", None)
    return res


# ---------------- sonuçlanan işlemler ----------------

TRADE_COLS = ["kural", "sürüm", "coin", "yön", "karar_tsi", "giriş_tsi", "giriş_px", "ufuk", "çıkış_tsi",
              "çıkış_px", "çıkış_nedeni", "ham_getiri", "kıyas_getirisi", "düzeltilmiş", "maliyet", "net",
              "mfe", "mae", "durum", "köken"]


async def trades(rule: str = "", coin: str = "", h: int | None = None, limit: int = 500) -> list[dict]:
    """Sonuçlanan (ya da ölçülemeyen) ufuk satırları — yeni → eski. LOG-* aileleri yalnız açıkça istenirse."""
    q = ("SELECT e.rule_id, e.rule_ver, e.coin, e.side, e.ts_decision, e.entry_ts, e.entry_px, e.origin,"
         " o.h, o.exit_ts, o.exit_px, o.exit_reason, o.ret_raw, o.ret_bench, o.ret_adj, o.cost, o.net, o.mfe, o.mae,"
         " o.status FROM strat_outcomes o JOIN strat_events e ON e.id = o.event_id"
         " WHERE o.status IN ('done', 'unresolvable')")
    args: list = []
    if rule:
        q += " AND e.rule_id = ?"
        args.append(rule)
    else:
        q += " AND e.rule_id NOT LIKE 'LOG-%'"
    if coin:
        q += " AND (e.coin = ? OR e.coin LIKE ?)"
        args += [coin, f"%:{coin}"]
    if h is not None:
        q += " AND o.h = ?"
        args.append(int(h))
    q += " ORDER BY COALESCE(o.exit_ts, e.entry_ts) DESC LIMIT ?"
    args.append(int(limit))
    async with db() as conn:
        cur = await conn.execute(q, args)
        return [dict(r) for r in await cur.fetchall()]


def trades_csv(rows: list[dict]) -> str:
    import csv
    import io
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tr = ZoneInfo("Europe/Istanbul")

    def t(x):
        return datetime.fromtimestamp(int(x), tr).strftime("%Y-%m-%d %H:%M:%S") if x else ""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(TRADE_COLS)
    for r in rows:
        w.writerow([r["rule_id"], r["rule_ver"], r["coin"], {1: "long", -1: "short"}.get(r["side"], ""),
                    t(r["ts_decision"]), t(r["entry_ts"]), r["entry_px"], h_label(int(r["h"])), t(r["exit_ts"]),
                    r["exit_px"], r["exit_reason"], r["ret_raw"], r["ret_bench"], r["ret_adj"], r["cost"], r["net"],
                    r["mfe"], r["mae"], r["status"], r["origin"]])
    return buf.getvalue()


# ---------------- veri sağlığı ----------------

_HEALTH: dict = {"ts": 0, "v": None}
HEALTH_S = 600


async def data_health(cfg=None, ts: int | None = None) -> dict:
    """Lab'ın topladığı veri ve döngünün durumu. Pahalı sayımlar 10 dk önbellekli."""
    from . import data
    ts = int(ts or now())
    if _HEALTH["v"] is not None and 0 <= ts - _HEALTH["ts"] < HEALTH_S:
        heavy = _HEALTH["v"]
    else:
        heavy = {}
        async with db() as conn:
            cur = await conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT coin) c, MAX(ts) m FROM metrics_hourly")
            heavy["mh"] = dict(await cur.fetchone())
            cur = await conn.execute("SELECT tf, COUNT(*) n, COUNT(DISTINCT coin) c, MAX(ts) m FROM lab_candles"
                                     " GROUP BY tf ORDER BY tf")
            heavy["lc"] = [dict(r) for r in await cur.fetchall()]
            cur = await conn.execute("SELECT COUNT(*) n, COUNT(DISTINCT coin) c, MAX(ts) m FROM seans_bars")
            heavy["sb"] = dict(await cur.fetchone())
            cur = await conn.execute("SELECT (rule_id LIKE 'LOG-%') lg, status, COUNT(*) n FROM strat_events"
                                     " GROUP BY lg, status")
            heavy["ev"] = [dict(r) for r in await cur.fetchall()]
        _HEALTH.update(ts=ts, v=heavy)
    st = await kv_get("lab_stats") or {}
    deep = await kv_get(data.DEEP_KV) or {}
    try:
        uni = len(await data.lab_coins(cfg, ts))
    except Exception:                                  # noqa: BLE001
        uni = 0
    by_tf = {}
    for k in deep:
        tf = k.rsplit("|", 1)[-1]
        by_tf[tf] = by_tf.get(tf, 0) + 1
    ev = {}
    for r in heavy.get("ev") or []:
        d = ev.setdefault("aile" if r["lg"] else "kural", {})
        d[r["status"]] = int(r["n"])
    rs = st.get("resolve") or {}
    warns = []
    mh_last = (heavy.get("mh") or {}).get("m")
    if not mh_last or ts - int(mh_last) > 2 * 3600 + 3600:
        warns.append("saatlik metrik 2 saatten eski")
    if int(rs.get("backlog") or 0) > 500:
        warns.append(f"çözücüde {rs.get('backlog')} iş bekliyor")
    if st.get("err"):
        warns.append(f"son tur hatası: {st['err']}")
    if st.get("ts") and ts - int(st["ts"]) > 600:
        warns.append(f"lab döngüsü {(ts - int(st['ts'])) // 60} dk'dır tur atmadı")
    return {"heavy": heavy, "stats": st, "deep": {"by_tf": by_tf, "universe": uni}, "ev": ev, "warns": warns,
            "cached_at": _HEALTH["ts"], "ts": ts}
