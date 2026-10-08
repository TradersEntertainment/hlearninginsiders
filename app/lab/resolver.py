"""🧪 Çözücü — olayın girişini ve ufuk sonuçlarını İLERİYE BAKIŞSIZ ölçer (5 dk'da bir).

1) Giriş: t* = karar + gecikme; t* ≤ t olan ilk işlemli mumun AÇILIŞI (execsim.entry). Mum dilimi
   yaşa göre: 1m (HL ~3.4 gün tutar) → 5m (~17 gün) → 1h. Beklemede işlem yoksa "işlem yok".
   β (kıyasa duyarlılık): kararDAN ÖNCEKİ 30 günün 1h getirileriyle (Dimson, 1'e büzülmüş).
2) Ufuk: vade sonrası ilk işlemli mumun açılışı (tolerans max(2 mum, 5 dk)); yoksa "çıkışta işlem
   yok" — hiçbir zaman "tutmadı" sayılmaz. h=0: kuralın çıkış tanımı (TP içinden geçmeli, stop
   boşlukta açılıştan, aynı mumda ikisi → stop, tam süre zaman aşımı).
3) Getiri aritmetik: ret_raw = yön·(çıkış/giriş − 1); ret_adj = ret_raw − yön·β·kıyas getirisi
   (aynı [giriş, çıkış]); net = ret_adj − maliyet (donmuş tablo; funding = geçilen saat sınırlarının
   metrics_hourly oranı, eksik saat → temkinli yer tutucu oran — gerçek tavan DEĞİL, HL %4/sa'e
   kadar izin verir). Yönsüz olay (0) long yönünde ölçülür. HIP-3 kripto dex'i (para:…) ana dex
   ücretiyle değil HIP-3 yer tutucusuyla ücretlenir.
Bütçe yetmezse iş sonraki tura kalır; eksik veriyle sonuç uydurulmaz.
"""
from __future__ import annotations

import json
import logging

from ..db import db, now
from . import costs, data, execsim

log = logging.getLogger("lab.resolver")

SETTLE = 90                         # mum kapanış payı + gecikme
BETA_DAYS = 30


def tf_for(age_s: int) -> int:
    """Giriş anının yaşına göre HL'nin hâlâ verdiği en ince dilim."""
    if age_s < int(3.3 * 86400):
        return 60
    if age_s < int(16.5 * 86400):
        return 300
    return 3600


def tol_of(tf: int) -> int:
    return max(2 * tf, 300)


def off_hours(klass: str, t0: int, t1: int) -> bool:
    """Hisse/endeks: giriş ya da çıkış ABD nakit seansı (09:30–16:00 ET, işlem günü) dışındaysa
    pahalı kayma. Kripto 7/24 → hep seans içi."""
    if klass == "kripto":
        return False
    from ..radar.seans import trading_day_at
    return trading_day_at(int(t0))[1] != "ny" or trading_day_at(int(t1))[1] != "ny"


def hour_marks(t0: int, t1: int) -> list[int]:
    """[t0, t1] arasında geçilen saat sınırları — her biri bir funding ödemesi (HL saatlik)."""
    first = (int(t0) // 3600 + 1) * 3600
    return list(range(first, int(t1) + 1, 3600))


async def funding_list(coin: str, t0: int, t1: int) -> list[float | None]:
    """Her saat sınırı için ÖNCEKİ saatin ortalama ctx oranı; yoksa None (maliyet temkinli yer tutucu oranla)."""
    marks = hour_marks(t0, t1)
    if not marks:
        return []
    async with db() as conn:
        cur = await conn.execute("SELECT ts, funding_avg FROM metrics_hourly WHERE coin=? AND ts>=? AND ts<?",
                                 (coin, marks[0] - 3600, marks[-1]))
        got = {int(r["ts"]): r["funding_avg"] for r in await cur.fetchall()}
    return [got.get(m - 3600) for m in marks]


def hourly_returns(a: dict[int, float], b: dict[int, float]) -> tuple[list[float], list[float]]:
    """Saf: iki seri (saat → kapanış) ortak saat ızgarasında, işlemsiz saat önceki kapanışla doldurulur
    (getiri 0 — ince kitapta gecikmeli uyum Dimson'un gecikme terimine kalır). Izgara bitişik:
    gecikme terimi gerçekten bir önceki saati eşler."""
    if not a or not b:
        return [], []
    t0 = max(min(a), min(b))
    t1 = min(max(a), max(b))
    r, rb = [], []
    pa = pb = None
    for t in range(t0, t1 + 1, 3600):
        ca, cb = a.get(t, pa), b.get(t, pb)
        if pa and pb and ca and cb:
            r.append(ca / pa - 1)
            rb.append(cb / pb - 1)
        pa, pb = ca, cb
    return r, rb


async def beta_for(coin: str, bench: str | None, t_dec: int) -> float:
    """Karardan önceki 30 günün 1h getirileri (önbellek) → Dimson β; veri azsa 1.0."""
    if not bench:
        return 0.0
    from .stats import beta_dimson
    t0 = int(t_dec) - BETA_DAYS * 86400
    a = {c["t"]: c["c"] for c in await data.candles(coin, 3600, t0, int(t_dec) - 3600)}
    b = {c["t"]: c["c"] for c in await data.candles(bench, 3600, t0, int(t_dec) - 3600)}
    r, rb = hourly_returns(a, b)
    return float(beta_dimson(r, rb))


def cost_class(coin: str, klass: str | None) -> str:
    """Maliyet satırı: HIP-3 kripto dex'i (para:ANSEM) ana dex değil — HIP-3 yer tutucusu."""
    k = klass or "hisse"
    return "hip3_kripto" if (k == "kripto" and ":" in coin) else k


class _Win:
    """Bir çağrı içinde (coin, tf) başına tek pencere isteği."""

    def __init__(self, client, budget, now_ts: int):
        self.client, self.budget, self.now = client, budget, now_ts
        self.memo: dict[tuple[str, int], tuple[int, int, list | None]] = {}

    async def get(self, coin: str, tf: int, t0: int, t1: int) -> list[dict] | None:
        m = self.memo.get((coin, tf))
        if m and m[0] <= t0 and m[1] >= t1:
            return m[2]
        rows = await data.ensure_window(self.client, coin, tf, t0, t1, self.budget, self.now)
        self.memo[(coin, tf)] = (t0, t1, rows)
        return rows


async def _entries(win: _Win, now_ts: int, limit: int) -> dict:
    from .registry import ACTIVE
    from .specs import by_id
    out = {"opened": 0, "unres": 0, "wait": 0}
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM strat_events WHERE status='pending' AND ts_decision + COALESCE(latency_s, 0) + ? <= ?"
            " ORDER BY ts_decision LIMIT ?", (SETTLE, now_ts, limit))
        evs = [dict(r) for r in await cur.fetchall()]
    specs_by = by_id()
    by_coin: dict[str, list[dict]] = {}
    for e in evs:
        by_coin.setdefault(e["coin"], []).append(e)
    for coin, rows in by_coin.items():
        for e in rows:
            t_star = int(e["ts_decision"]) + int(e["latency_s"] or 0)
            tf = tf_for(now_ts - t_star)
            tol = tol_of(tf)
            cands = await win.get(coin, tf, t_star - tf, t_star + tol + tf)
            if cands is None:
                out["wait"] += 1
                continue
            ent = execsim.entry(cands, t_star, tol)
            if ent is None:
                if now_ts > t_star + tol + tf + data.CLOSED_MARGIN + 300:
                    await _set_event(e["id"], "unresolvable", "girişte işlem yok / bayat veri", now_ts)
                    out["unres"] += 1
                else:
                    out["wait"] += 1
                continue
            spec = (ACTIVE.get(e["rule_id"]) or {}).get("spec") or specs_by.get(e["rule_id"]) or {}
            if not spec:
                await _set_event(e["id"], "void", "kural tanımı yok", now_ts)
                continue
            beta = await beta_for(coin, e["bench"], int(e["ts_decision"]))
            hs = [int(h) for h in spec.get("horizons_s") or []]
            ex = spec.get("exit") or None
            rows_o = [(e["id"], h, ent[0] + h) for h in hs]
            if ex and e["side"] in (1, -1):
                rows_o.append((e["id"], 0, ent[0] + int(ex["timeout"])))
            async with db() as conn:
                await conn.execute(
                    "UPDATE strat_events SET entry_ts=?, entry_px=?, entry_src=?, beta=?, status='open' WHERE id=?",
                    (ent[0], ent[1], data.TF_NAME[tf], beta, e["id"]))
                await conn.executemany("INSERT OR IGNORE INTO strat_outcomes(event_id, h, due_ts) VALUES(?,?,?)",
                                       rows_o)
            out["opened"] += 1
    return out


async def _set_event(eid: int, status: str, note: str, ts: int) -> None:
    async with db() as conn:
        await conn.execute("UPDATE strat_events SET status=?, status_note=?, resolved_ts=? WHERE id=?",
                           (status, note, ts, eid))


def measure(cands: list[dict], e: dict, h: int, due: int, tf: int, spec: dict) -> dict | None:
    """Saf ölçüm: None = henüz değil; {"status": "unresolvable", "note"} ya da tam sonuç sözlüğü."""
    side = e["side"] if e["side"] in (1, -1) else 1
    entry_ts, entry_px = int(e["entry_ts"]), float(e["entry_px"])
    tol = tol_of(tf)
    if h == 0:
        ex = spec.get("exit") or {}
        r = execsim.path_exit(cands, entry_ts, entry_px, side, ex.get("tp"), ex.get("sl"), int(ex["timeout"]))
        if r["reason"] == "open":
            return None
        if r["reason"] == "timeout" and r["exit_ts"] - due > tol:
            return {"status": "unresolvable", "note": "zaman aşımında işlem yok"}
        return r
    r = execsim.path_exit(cands, entry_ts, entry_px, side, None, None, h)
    if r["reason"] == "open":
        return None
    if r["exit_ts"] - due > tol:
        return {"status": "unresolvable", "note": "çıkışta işlem yok"}
    return r


async def _outcomes(win: _Win, now_ts: int, limit: int) -> dict:
    from .registry import ACTIVE
    from .specs import by_id
    out = {"done": 0, "unres": 0, "wait": 0}
    async with db() as conn:
        cur = await conn.execute(
            "SELECT o.event_id, o.h, o.due_ts, e.* FROM strat_outcomes o JOIN strat_events e ON e.id = o.event_id"
            " WHERE o.status='open' AND o.due_ts + ? <= ? ORDER BY o.due_ts LIMIT ?", (SETTLE, now_ts, limit))
        rows = [dict(r) for r in await cur.fetchall()]
    specs_by = by_id()
    touched: set[int] = set()
    writes = []
    for o in rows:
        spec = (ACTIVE.get(o["rule_id"]) or {}).get("spec") or specs_by.get(o["rule_id"]) or {}
        tf = data.NAME_TF.get(o["entry_src"] or "", 3600)
        tol = tol_of(tf)
        due = int(o["due_ts"])
        cands = await win.get(o["coin"], tf, int(o["entry_ts"]), due + tol + tf)
        if cands is None:
            out["wait"] += 1
            continue
        r = measure(cands, o, int(o["h"]), due, tf, spec)
        late = now_ts > due + tol + tf + data.CLOSED_MARGIN + 300
        if r is None:
            if late:
                r = {"status": "unresolvable", "note": "çıkışta işlem yok" if int(o["h"]) else "veri yok"}
            else:
                out["wait"] += 1
                continue
        if r.get("status") == "unresolvable":
            if not late:
                out["wait"] += 1
                continue
            writes.append(("unresolvable", None, None, r["note"], tf, None, None, None, None, None, None, None,
                           now_ts, o["event_id"], o["h"]))
            touched.add(o["event_id"])
            out["unres"] += 1
            continue
        side = o["side"] if o["side"] in (1, -1) else 1
        rb, adj = None, r["ret"]
        if o["bench"]:
            bc = await win.get(o["bench"], tf, int(o["entry_ts"]), int(r["exit_ts"]) + tol + tf)
            b0 = execsim.entry(bc or [], int(o["entry_ts"]), tol) if bc is not None else None
            b1 = execsim.exit_at(bc or [], int(r["exit_ts"]), tol) if bc is not None else None
            if b0 and b1:
                rb = b1[1] / b0[1] - 1
                adj = r["ret"] - side * float(o["beta"] if o["beta"] is not None else 1.0) * rb
            elif not late:
                out["wait"] += 1
                continue
            else:
                adj = None                           # kıyas yok: net yazılmaz, sayılır
        fl = await funding_list(o["coin"], int(o["entry_ts"]), int(r["exit_ts"]))
        c = costs.cost(cost_class(o["coin"], o["klass"]),
                       off_hours(o["klass"] or "hisse", int(o["entry_ts"]), int(r["exit_ts"])),
                       int(r["exit_ts"]) - int(o["entry_ts"]), side, fl)
        net = costs.net(adj, c) if adj is not None else None
        writes.append(("done", r["exit_ts"], r["exit_px"], r["reason"], tf, r["ret"], rb, adj, c["total"], net,
                       r["mfe"], r["mae"], now_ts, o["event_id"], o["h"]))
        touched.add(o["event_id"])
        out["done"] += 1
    for i in range(0, len(writes), 200):
        async with db() as conn:
            await conn.executemany(
                "UPDATE strat_outcomes SET status=?, exit_ts=?, exit_px=?, exit_reason=?, tf_used=?, ret_raw=?,"
                " ret_bench=?, ret_adj=?, cost=?, net=?, mfe=?, mae=?, resolved_ts=? WHERE event_id=? AND h=?",
                writes[i:i + 200])
    if touched:
        await _close_events(sorted(touched), now_ts)
    return out


async def _close_events(ids: list[int], now_ts: int) -> None:
    """Tüm ufukları sonuçlanan olay 'done'; birincil ufuk ölçülemediyse 'unresolvable'."""
    from .registry import ACTIVE
    from .specs import by_id
    specs_by = by_id()
    async with db() as conn:
        for i in range(0, len(ids), 200):
            part = ids[i:i + 200]
            q = ",".join("?" * len(part))
            cur = await conn.execute(
                f"SELECT e.id, e.rule_id, o.h, o.status FROM strat_events e JOIN strat_outcomes o ON o.event_id=e.id"
                f" WHERE e.id IN ({q})", part)
            by: dict[int, list] = {}
            rid: dict[int, str] = {}
            for r in await cur.fetchall():
                by.setdefault(r["id"], []).append((int(r["h"]), r["status"]))
                rid[r["id"]] = r["rule_id"]
            for eid, outs in by.items():
                if any(s == "open" for _, s in outs):
                    continue
                spec = (ACTIVE.get(rid[eid]) or {}).get("spec") or specs_by.get(rid[eid]) or {}
                prim = int(spec.get("primary_h") or 0)
                bad = any(h == prim and s == "unresolvable" for h, s in outs)
                await conn.execute("UPDATE strat_events SET status=?, status_note=?, resolved_ts=? WHERE id=?",
                                   ("unresolvable" if bad else "done",
                                    "birincil ufuk ölçülemedi" if bad else None, now_ts, eid))


async def resolve_due(client, budget, now_ts: int | None = None, limit: int = 300) -> dict:
    now_ts = int(now_ts or now())
    win = _Win(client, budget, now_ts)
    a = await _entries(win, now_ts, limit)
    b = await _outcomes(win, now_ts, limit)
    return {"entries": a, "outcomes": b}


def features_of(row: dict) -> dict:
    try:
        return json.loads(row.get("features") or "{}")
    except (TypeError, ValueError):
        return {}
