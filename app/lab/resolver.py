"""🧪 Çözücü — olayın girişini ve ufuk sonuçlarını İLERİYE BAKIŞSIZ ölçer (iş birikince her turda).

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
Bütçe yetmezse iş sonraki tura kalır; eksik veriyle sonuç uydurulmaz. Olay KAYITTA DONMUŞ kural
tanımıyla (kural + sürüm) ölçülür. Coin başına tek pencere, en eski ihtiyaç önce; bir coinin HL hatası
turu düşürmez (art arda 6 kez → o coinin bekleyen işi "veri alınamadı").
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
    """Bir tur içinde (coin, tf) başına TEK pencere isteği; data.ensure_window önbelleği turlar arası
    yalnız eksik kuyruğu çeker. None = bütçe izin vermedi (bekle — asla sonuç uydurma)."""

    def __init__(self, client, budget, now_ts: int):
        self.client, self.budget, self.now = client, budget, now_ts
        self.memo: dict[tuple[str, int], tuple[int, int, list | None]] = {}

    async def get(self, coin: str, tf: int, t0: int, t1: int) -> list[dict] | None:
        m = self.memo.get((coin, tf))
        if m and m[2] is not None and m[0] <= t0 and m[1] >= t1:
            return m[2]
        if m:
            t0, t1 = min(t0, m[0]), max(t1, m[1])
        rows = await data.ensure_window(self.client, coin, tf, t0, t1, self.budget, self.now)
        self.memo[(coin, tf)] = (t0, t1, rows)
        return rows


class _Specs:
    """Olayın KAYITTA DONMUŞ tanımı (lab_rules.spec, kural + sürüm) — kod listesi değişse de eski sürümün
    olayı kendi tanımıyla ölçülür."""

    def __init__(self):
        self.memo: dict[tuple[str, int], dict] = {}

    async def get(self, rid: str, ver: int) -> dict:
        k = (rid, int(ver))
        if k not in self.memo:
            async with db() as conn:
                cur = await conn.execute("SELECT spec FROM lab_rules WHERE rule_id=? AND ver=?", k)
                r = await cur.fetchone()
            try:
                self.memo[k] = json.loads(r["spec"]) if r and r["spec"] else {}
            except (TypeError, ValueError):
                self.memo[k] = {}
        return self.memo[k]


FAIL_MAX = 6                        # aynı coinde art arda bu kadar HL hatası → bekleyen iş "veri alınamadı"
_FAILS: dict[str, int] = {}


def measure(cands: list[dict], e: dict, h: int, due: int, tf: int, spec: dict) -> dict | None:
    """Saf ölçüm: None = henüz değil; {"status": "unresolvable", "note"} ya da tam sonuç sözlüğü."""
    side = e["side"] if e["side"] in (1, -1) else 1
    entry_ts, entry_px = int(e["entry_ts"]), float(e["entry_px"])
    tol = tol_of(tf)
    if h == 0:
        ex = spec.get("exit") or {}
        if not ex.get("timeout"):
            return {"status": "unresolvable", "note": "çıkış kuralı yok"}
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


def _late(now_ts: int, due: int, tf: int) -> bool:
    return now_ts > due + tol_of(tf) + tf + data.CLOSED_MARGIN + 300


async def _entry_coin(win: _Win, specs_c: _Specs, coin: str, evs: list[dict], now_ts: int, out: dict) -> None:
    """Bir coinin bekleyen girişleri: dilim başına tek pencere."""
    by_tf: dict[int, list[dict]] = {}
    for e in evs:
        t_star = int(e["ts_decision"]) + int(e["latency_s"] or 0)
        by_tf.setdefault(tf_for(now_ts - t_star), []).append({**e, "t_star": t_star})
    writes, outs, unres = [], [], []
    for tf, lst in by_tf.items():
        tol = tol_of(tf)
        cands = await win.get(coin, tf, min(e["t_star"] for e in lst) - tf, max(e["t_star"] for e in lst) + tol + tf)
        if cands is None:
            out["wait"] += len(lst)
            continue
        for e in lst:
            ent = execsim.entry(cands, e["t_star"], tol)
            if ent is None:
                if _late(now_ts, e["t_star"], tf):
                    unres.append((e["id"], "girişte işlem yok / bayat veri"))
                    out["unres"] += 1
                else:
                    out["wait"] += 1
                continue
            spec = await specs_c.get(e["rule_id"], e["rule_ver"])
            if not spec:
                unres.append((e["id"], "kural tanımı yok"))
                continue
            beta = await beta_for(coin, e["bench"], int(e["ts_decision"]))
            rows_o = [(e["id"], int(h), ent[0] + int(h)) for h in spec.get("horizons_s") or []]
            ex = spec.get("exit") or None
            if ex and ex.get("timeout") and e["side"] in (1, -1):
                rows_o.append((e["id"], 0, ent[0] + int(ex["timeout"])))
            writes.append((ent[0], ent[1], data.TF_NAME[tf], beta, e["id"]))
            outs.extend(rows_o)
            out["opened"] += 1
    if writes or outs or unres:
        async with db() as conn:
            await conn.executemany("UPDATE strat_events SET entry_ts=?, entry_px=?, entry_src=?, beta=?, status='open'"
                                   " WHERE id=?", writes)
            await conn.executemany("INSERT OR IGNORE INTO strat_outcomes(event_id, h, due_ts) VALUES(?,?,?)", outs)
            await conn.executemany("UPDATE strat_events SET status='unresolvable', status_note=?, resolved_ts=?"
                                   " WHERE id=?", [(note, now_ts, eid) for eid, note in unres])


async def _outcome_coin(win: _Win, specs_c: _Specs, coin: str, rows: list[dict], now_ts: int, out: dict) -> set[int]:
    """Bir coinin vadesi gelmiş ufukları: giriş dilimi başına tek pencere, kıyas başına tek pencere."""
    by_tf: dict[int, list[dict]] = {}
    for o in rows:
        by_tf.setdefault(data.NAME_TF.get(o["entry_src"] or "", 3600), []).append(o)
    writes, touched = [], set()
    for tf, lst in by_tf.items():
        tol = tol_of(tf)
        cands = await win.get(coin, tf, min(int(o["entry_ts"]) for o in lst), max(int(o["due_ts"]) for o in lst) + tol + tf)
        if cands is None:
            out["wait"] += len(lst)
            continue
        for o in lst:
            spec = await specs_c.get(o["rule_id"], o["rule_ver"])
            due, h = int(o["due_ts"]), int(o["h"])
            r = measure(cands, o, h, due, tf, spec)
            late = _late(now_ts, due, tf)
            if (r is None or r.get("status") == "unresolvable") and late:
                tf2 = tf_for(now_ts - int(o["entry_ts"]))      # ince dilim HL'den düşmüş olabilir: kaba dilimle dene
                if tf2 > tf and due < data.hl_start(tf, now_ts) + tol:
                    c2 = await win.get(coin, tf2, int(o["entry_ts"]) - tf2, due + tol_of(tf2) + tf2)
                    if c2 is None:
                        out["wait"] += 1
                        continue
                    r2 = measure(c2, o, h, due, tf2, spec)
                    if r2 is not None:
                        r, tf = r2, tf2
            if r is None:
                if late:
                    r = {"status": "unresolvable", "note": "çıkışta işlem yok" if h else "veri yok"}
                else:
                    out["wait"] += 1
                    continue
            if r.get("status") == "unresolvable":
                if not late:
                    out["wait"] += 1
                    continue
                writes.append(("unresolvable", None, None, r["note"], tf, None, None, None, None, None, None, None,
                               now_ts, o["event_id"], h))
                touched.add(o["event_id"])
                out["unres"] += 1
                continue
            side = o["side"] if o["side"] in (1, -1) else 1
            rb, adj = None, r["ret"]
            if o["bench"]:
                t_tol = tol_of(tf)
                bc = await win.get(o["bench"], tf, int(o["entry_ts"]), int(r["exit_ts"]) + t_tol + tf)
                if bc is None:
                    out["wait"] += 1                          # bütçe: bekle — asla kıyassız sonuçlandırma
                    continue
                b0 = execsim.entry(bc, int(o["entry_ts"]), t_tol)
                b1 = execsim.exit_at(bc, int(r["exit_ts"]), t_tol)
                if not (b0 and b1):
                    if not _late(now_ts, int(r["exit_ts"]), tf):
                        out["wait"] += 1
                        continue
                    writes.append(("unresolvable", r["exit_ts"], r["exit_px"], "kıyasta işlem yok", tf, r["ret"],
                                   None, None, None, None, r["mfe"], r["mae"], now_ts, o["event_id"], h))
                    touched.add(o["event_id"])
                    out["unres"] += 1
                    continue
                rb = b1[1] / b0[1] - 1
                adj = r["ret"] - side * float(o["beta"] if o["beta"] is not None else 1.0) * rb
            fl = await funding_list(o["coin"], int(o["entry_ts"]), int(r["exit_ts"]))
            c = costs.cost(cost_class(o["coin"], o["klass"]),
                           off_hours(o["klass"] or "hisse", int(o["entry_ts"]), int(r["exit_ts"])),
                           int(r["exit_ts"]) - int(o["entry_ts"]), side, fl)
            writes.append(("done", r["exit_ts"], r["exit_px"], r["reason"], tf, r["ret"], rb, adj, c["total"],
                           costs.net(adj, c), r["mfe"], r["mae"], now_ts, o["event_id"], h))
            touched.add(o["event_id"])
            out["done"] += 1
    if writes:
        async with db() as conn:
            await conn.executemany(
                "UPDATE strat_outcomes SET status=?, exit_ts=?, exit_px=?, exit_reason=?, tf_used=?, ret_raw=?,"
                " ret_bench=?, ret_adj=?, cost=?, net=?, mfe=?, mae=?, resolved_ts=? WHERE event_id=? AND h=?", writes)
    return touched


async def _close_events(ids: list[int], now_ts: int, specs_c: _Specs) -> None:
    """Tüm ufukları sonuçlanan olay 'done'; birincil ufuk ölçülemediyse 'unresolvable'."""
    async with db() as conn:
        for i in range(0, len(ids), 200):
            part = ids[i:i + 200]
            q = ",".join("?" * len(part))
            cur = await conn.execute(
                f"SELECT e.id, e.rule_id, e.rule_ver, o.h, o.status FROM strat_events e"
                f" JOIN strat_outcomes o ON o.event_id=e.id WHERE e.id IN ({q})", part)
            by: dict[int, list] = {}
            meta: dict[int, tuple] = {}
            for r in await cur.fetchall():
                by.setdefault(r["id"], []).append((int(r["h"]), r["status"]))
                meta[r["id"]] = (r["rule_id"], int(r["rule_ver"]))
            for eid, outs in by.items():
                if any(s == "open" for _, s in outs):
                    continue
                spec = await specs_c.get(*meta[eid])
                prim = int(spec.get("primary_h") or 0)
                bad = any(h == prim and s == "unresolvable" for h, s in outs)
                await conn.execute("UPDATE strat_events SET status=?, status_note=?, resolved_ts=? WHERE id=?",
                                   ("unresolvable" if bad else "done",
                                    "birincil ufuk ölçülemedi" if bad else None, now_ts, eid))


async def _give_up(coin: str, now_ts: int, err: str) -> None:
    """Coin art arda FAIL_MAX turdur okunamıyor (HL 500: kaldırılmış varlık): bekleyen iş ölçülemedi."""
    note = f"veri alınamadı ({err})"[:120]
    async with db() as conn:
        await conn.execute("UPDATE strat_outcomes SET status='unresolvable', exit_reason=?, resolved_ts=?"
                           " WHERE status='open' AND event_id IN (SELECT id FROM strat_events WHERE coin=?)",
                           (note, now_ts, coin))
        await conn.execute("UPDATE strat_events SET status='unresolvable', status_note=?, resolved_ts=?"
                           " WHERE coin=? AND status IN ('pending','open')", (note, now_ts, coin))


async def resolve_due(client, budget, now_ts: int | None = None, limit: int = 600) -> dict:
    """Bir tur: önce girişler, sonra (yeni açılanlar dahil) vadesi gelen ufuklar; ikisinde de coin başına
    tek pencere, en eski ihtiyaç önce. Coin başına hata yalıtılır (bir coinin HL hatası turu düşürmez,
    o coinde yapılan ölçümler yazılır)."""
    now_ts = int(now_ts or now())
    win, specs_c = _Win(client, budget, now_ts), _Specs()
    ent = {"opened": 0, "unres": 0, "wait": 0}
    oc = {"done": 0, "unres": 0, "wait": 0}
    st = {"errors": 0}
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM strat_events WHERE status='pending' AND ts_decision + COALESCE(latency_s, 0) + ? <= ?"
            " ORDER BY ts_decision LIMIT ?", (SETTLE, now_ts, limit))
        evs = [dict(r) for r in await cur.fetchall()]
    await _per_coin(evs, "ts_decision", lambda coin, lst: _entry_coin(win, specs_c, coin, lst, now_ts, ent), now_ts, st)
    async with db() as conn:
        cur = await conn.execute(
            "SELECT o.event_id, o.h, o.due_ts, e.* FROM strat_outcomes o JOIN strat_events e ON e.id = o.event_id"
            " WHERE o.status='open' AND o.due_ts + ? <= ? ORDER BY o.due_ts LIMIT ?", (SETTLE, now_ts, limit))
        outs = [dict(r) for r in await cur.fetchall()]
    touched: set[int] = set()

    async def _oc(coin, lst):
        touched.update(await _outcome_coin(win, specs_c, coin, lst, now_ts, oc))
    await _per_coin(outs, "due_ts", _oc, now_ts, st)
    if touched:
        await _close_events(sorted(touched), now_ts, specs_c)
    return {"entries": ent, "outcomes": oc, "errors": st["errors"], "backlog": ent["wait"] + oc["wait"]}


async def _per_coin(rows: list[dict], tkey: str, fn, now_ts: int, st: dict) -> None:
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["coin"], []).append(r)
    for coin in sorted(by, key=lambda c: min(int(r[tkey]) for r in by[c])):   # en eski ihtiyaç önce
        try:
            await fn(coin, by[coin])
            _FAILS.pop(coin, None)
        except Exception as e:                                    # noqa: BLE001 — coin başına yalıtım
            st["errors"] += 1
            _FAILS[coin] = _FAILS.get(coin, 0) + 1
            log.warning("lab çözücü %s: %s (%d. kez)", coin, e, _FAILS[coin])
            if _FAILS[coin] >= FAIL_MAX:
                await _give_up(coin, now_ts, type(e).__name__)
                _FAILS.pop(coin, None)


def features_of(row: dict) -> dict:
    try:
        return json.loads(row.get("features") or "{}")
    except (TypeError, ValueError):
        return {}
