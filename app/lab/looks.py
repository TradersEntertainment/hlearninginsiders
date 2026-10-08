"""🧪 Planlı bakışlar — kuralın durumu YALNIZ burada değişir (sahip onayı hariç).

Her tur (saatlik) her ileri kural için kayıttan SONRAKİ, penceresi tamamen kapanmış kümeler
sayılır; önceden yazılı eşik (gate.look_due) aşıldıysa o bakış BİR kez yapılır ve lab_tests'e
yazılır (UNIQUE: aynı bakış iki kez olmaz). Sık kontrol "isteğe bağlı durdurma" değildir: bakış
sonuçtan değil küme SAYISINDAN tetiklenir. Ek engelleyen koruma: ölçülemeyen payı > %20 → geçemez.
Canlı kural: onaydan sonraki her 20 yeni kümede düşüş denetimi (ortalama net < 0 anlamlıysa durdu).
"""
from __future__ import annotations

import json
import logging

import numpy as np

from ..db import db, now
from . import gate, registry, stats

log = logging.getLogger("lab.looks")

SETTLE = 2 * 3600                  # küme kapanışı + birincil ufuk + bu pay geçmeden küme sayılmaz
UNRES_MAX = 0.20
DEMOTE_EVERY = 20


async def forward_clusters(rule_id: str, ver: int, spec: dict, since: int, ts: int, until: int | None = None) -> dict:
    """Kayıttan (since) sonraki canlı olaylar → birincil ufukta net; coin başına örtüşmeyen inceltme;
    penceresi kapanmış takvim kümeleri (zaman sıralı ortalamalar)."""
    h = int(spec.get("primary_h") or 0)
    width = int(spec["cluster_s"])
    span = int(spec["exit"]["timeout"]) if (h == 0 and spec.get("exit")) else h
    last_key = (ts - span - SETTLE) // width - 1               # bu anahtara kadar kümeler kapandı
    async with db() as conn:
        cur = await conn.execute(
            "SELECT e.coin, e.ts_decision, e.cluster, e.status, o.status ostatus, o.net, o.exit_reason"
            " FROM strat_events e LEFT JOIN strat_outcomes o ON o.event_id = e.id AND o.h = ?"
            " WHERE e.rule_id=? AND e.rule_ver=? AND e.origin='live' AND e.ts_decision >= ?"
            + (" AND e.ts_decision < ?" if until else "") + " ORDER BY e.ts_decision",
            (h, rule_id, ver, since, *((until,) if until else ())))
        rows = [dict(r) for r in await cur.fetchall()]
    lat = int(spec.get("latency_s") or 0)
    rows = [r for r in rows if r["cluster"] is not None and int(r["cluster"]) <= last_key
            # penceresi küme sınırını aşan olay atılır: komşu kümelerin pencereleri örtüşmez → küme
            # ortalamaları bağımsız (aksi hâlde ortak piyasa hareketi iki kümeye birden yazılırdı)
            and int(r["ts_decision"]) + lat + span < (int(r["cluster"]) + 1) * width]
    n_all = len(rows)
    # ölçülemeyen: olay ya da birincil ufuk ölçülemedi, ya da "done" ama net yok (eski kayıt) — hepsi sayılır
    unres = sum(1 for r in rows if r["status"] == "unresolvable" or r["ostatus"] == "unresolvable"
                or (r["ostatus"] == "done" and r["net"] is None))
    done = [r for r in rows if r["ostatus"] == "done" and r["net"] is not None]
    keep = stats.thin_nonoverlap([r["ts_decision"] for r in done], [r["coin"] for r in done], max(span, 1))
    done = [r for r, k in zip(done, keep) if k]
    xc, sizes, keys = stats.cluster_means([r["net"] for r in done], [int(r["cluster"]) for r in done])
    return {"xc": xc, "keys": keys, "n_ev": len(done), "n_all": n_all, "unres": unres,
            "unres_share": (unres / n_all) if n_all else 0.0,
            "outcomes": [r["exit_reason"] for r in done]}


def _seed(rule_id: str, ver: int, look_no: int) -> int:
    import hashlib
    return int(hashlib.sha256(f"{rule_id}:{ver}:{look_no}".encode()).hexdigest()[:8], 16)


def _payload(res: dict, fc: dict) -> str:
    keep = {k: v for k, v in res.items() if k in ("why", "guards", "frac", "test", "alpha_k", "mean")}
    keep.update(n_ev=fc["n_ev"], unres=fc["unres"], unres_share=round(fc["unres_share"], 4))
    return registry.canon(keep)


async def run(ts: int | None = None, on_pass=None, on_stop=None) -> list[dict]:
    """Bir tur: sırası gelen bakışlar + canlı kuralların düşüş denetimi. Döner: yapılan bakışlar."""
    ts = int(ts or now())
    done: list[dict] = []
    for rid, a in list(registry.ACTIVE.items()):
        spec = a["spec"]
        if spec.get("evidence") not in ("forward", "frozen_backtest"):
            continue
        if a["status"] == "canli":
            await _demote(rid, a, ts, on_stop)
            continue
        if a["status"] != "kagit":
            continue                                   # aday (Aşama A bekliyor), gecti (onay bekliyor), durdu
        rule = {**spec, "alpha": a["alpha"]}
        async with db() as conn:
            cur = await conn.execute("SELECT look_no FROM lab_tests WHERE rule_id=? AND ver=? AND kind='look'",
                                     (rid, a["ver"]))
            looks_done = [int(r["look_no"]) for r in await cur.fetchall()]
        fc = await forward_clusters(rid, a["ver"], spec, a["registered_ts"], ts)
        k = gate.look_due(rule, len(fc["xc"]), looks_done)
        if not k:
            continue
        import asyncio
        # CPU: erken bakışta eşik ~5e-6 → milyonlarca Monte Carlo tekrarı; olay döngüsünü (WS) bloklamasın
        res = await asyncio.to_thread(gate.stage_b, rule, k, fc["xc"],
                                      fc["outcomes"] if spec.get("metric") == "barrier" else None,
                                      seed=_seed(rid, a["ver"], k))
        dec = res["decision"]
        if dec == gate.GECTI and fc["unres_share"] > UNRES_MAX:
            res["guards"]["unres"] = False
            dec = gate.EMEKLI if k == len(rule["looks"]) else gate.DEVAM
            res["why"] = f"ölçülemeyen payı %{fc['unres_share'] * 100:.0f} > %{UNRES_MAX * 100:.0f}"
        mean = float(np.mean(fc["xc"])) if len(fc["xc"]) else None
        new = {gate.GECTI: "gecti", gate.EMEKLI: "emekli"}.get(dec)
        try:
            async with db() as conn:                   # bakış satırı + durum TEK işlemde
                await conn.execute(
                    "INSERT INTO lab_tests(ts, rule_id, ver, kind, look_no, n_events, n_clusters, est, p, alpha_k,"
                    " decision, payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, rid, a["ver"], "look", k, fc["n_ev"], len(fc["xc"]), mean, res["p"], res["alpha_k"],
                     dec, _payload(res, fc)))
                if new:
                    await registry.set_status(rid, a["ver"], new, f"bakış {k}: {res.get('why', '')}"[:200],
                                              ts=ts, conn=conn)
        except Exception:                              # UNIQUE: bu bakış zaten yapılmış (yarış) — dokunma
            log.warning("lab bakışı yazılamadı %s/v%d bakış %d", rid, a["ver"], k, exc_info=True)
            continue
        if new:                                        # işlem kapandı: bellekteki durum
            if new == "emekli":
                registry.ACTIVE.pop(rid, None)
            else:
                a["status"] = new
        rec = {"rule_id": rid, "ver": a["ver"], "look": k, "decision": dec, "p": res["p"], "alpha_k": res["alpha_k"],
               "n_c": len(fc["xc"]), "n_ev": fc["n_ev"], "mean": mean, "why": res.get("why", "")}
        done.append(rec)
        if dec == gate.GECTI and on_pass:
            await on_pass(rec)                         # teslim düşerse deliver.resend_pending yeniden dener
    return done


async def _demote(rid: str, a: dict, ts: int, on_stop) -> None:
    """Canlı kural: onaydan sonraki kümeler her DEMOTE_EVERY'de bir düşüş denetimi."""
    async with db() as conn:
        cur = await conn.execute("SELECT approved_ts FROM lab_rules WHERE rule_id=? AND ver=?", (rid, a["ver"]))
        r = await cur.fetchone()
        cur = await conn.execute("SELECT MAX(n_clusters) m FROM lab_tests WHERE rule_id=? AND ver=? AND kind='demote'",
                                 (rid, a["ver"]))
        last = int((await cur.fetchone())["m"] or 0)
    since = int((r["approved_ts"] if r and r["approved_ts"] else a["registered_ts"]))
    fc = await forward_clusters(rid, a["ver"], a["spec"], since, ts)
    g = len(fc["xc"])
    if g < DEMOTE_EVERY or g < last + DEMOTE_EVERY:
        return
    dec = gate.demote_check(fc["xc"], None)
    mean = float(np.mean(fc["xc"]))
    async with db() as conn:
        await conn.execute(
            "INSERT INTO lab_tests(ts, rule_id, ver, kind, n_events, n_clusters, est, decision, payload)"
            " VALUES(?,?,?,?,?,?,?,?,?)", (ts, rid, a["ver"], "demote", fc["n_ev"], g, mean, dec,
                                          json.dumps({"since": since})))
    if dec == gate.DURDU:
        await registry.set_status(rid, a["ver"], "durdu", f"canlıda düşüş: {g} küme, ortalama net {mean:+.4f}", ts=ts)
        if on_stop:
            await on_stop({"rule_id": rid, "ver": a["ver"], "n_c": g, "mean": mean})
