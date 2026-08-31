"""Örüntü tarayıcı — sinyal üret, KAYDET, vadesinde NOTLA.

Kaydetme kısmı süs değil, özelliğin belkemiği: bir tahmin aracının işe yarayıp
yaramadığı ancak kendi karnesiyle bilinir. `ai_hypotheses`'te kurduğumuz
desenin aynısı — sinyal ve sonucu tek satırda, vadesi gelince otomatik ölçüm,
ölçülemiyorsa `unresolvable` (sessizce "tuttu" demek yok).

SAYFA ÇOĞUNU GÖSTERİR, KANAL SEÇİCİDİR: zayıf sinyaller de yazılır (sekmede
bağlam), Telegram'a yalnız üç koşulu birden geçenler düşer.

ÇOKLU DENEME UYARISI: ~175 sembol × 2 dilim × 3 vade = binin üzerinde test
her turda. z≥2 eşiği tek testte %5 yanlış pozitif demek; binde onlarca
"kayda değer" şansa çıkar. Bu yüzden (a) mesaj bunu söylüyor, (b) asıl ölçü
karnedeki gerçekleşen isabet oranı.
"""
import asyncio
import logging

from ..db import alert_log, alert_recent, db, kv_set, now
from . import analog, bars

log = logging.getLogger("radar.patterns")

TF_SEC = bars.TF_SEC
RETENTION_D = 120


def horizons(cfg) -> list[int]:
    raw = str(getattr(cfg, "pattern_horizons", "4,12,24") or "")
    out = []
    for p in raw.split(","):
        try:
            v = int(p.strip())
        except ValueError:
            continue
        if 0 < v <= 500:
            out.append(v)
    return out or [12]


def win_for(cfg, tf: str) -> int:
    return int(getattr(cfg, "pattern_win_1h", 24) if tf == "1h"
               else getattr(cfg, "pattern_win_15m", 32))


async def analyze_coin(cfg, coin: str, tf: str, horizon: int) -> dict | None:
    """Bir sembol+dilim+vade için tam çözümleme. Ağır kısım thread'de."""
    win = win_for(cfg, tf)
    closes, ts = await bars.series(coin, tf)
    if len(closes) < win + horizon + 2:
        return {"coin": coin, "tf": tf, "horizon": horizon, "win": win,
                "label": "yetersiz", "n": 0,
                "note": f"arşivde yalnız {len(closes)} bar var"}
    # numpy döngüsü event loop'u kilitlemesin.
    res = await asyncio.to_thread(
        analog.analyze, closes[-win:], closes, ts, win, horizon,
        int(getattr(cfg, "pattern_top_k", 50)),
        float(getattr(cfg, "pattern_min_corr", 0.5)),
        int(getattr(cfg, "pattern_min_matches", 20)),
        int(ts[-win - 1]))                 # sorgu penceresinden ÖNCE bitenler
    # `pattern_signals` KOLON ADLARIYLA aynı takma adlar: sayfa aynı makroyu
    # hem canlı sonuç hem kayıtlı satır için kullanabilsin (şablonda kaynak
    # ayrımı yapmak, iki kaynak ayrışınca sessizce bozulurdu).
    return {**res, "coin": coin, "tf": tf, "horizon": horizon, "win": win,
            "px": closes[-1], "bars": len(closes), "last_ts": ts[-1],
            "med_move": res.get("med"), "n_match": res.get("n")}


def alertable(cfg, r: dict) -> bool:
    """Üç koşul BİRDEN: yeterli örneklem, istatistiksel ayrışma, anlamlı fark.

    z tek başına yetmez — çok büyük n'de 3 puanlık fark bile "anlamlı" çıkar
    ama üzerine bahis oynanmaz.
    """
    if r.get("label") != "kayda değer":
        return False
    n = r.get("n") or 0
    z, edge = r.get("z"), r.get("edge")
    return (n >= int(getattr(cfg, "pattern_min_matches", 20))
            and z is not None and abs(z) >= float(getattr(cfg, "pattern_z_alert", 2.0))
            and edge is not None and abs(edge) >= float(getattr(cfg, "pattern_edge_min", 10)))


async def save(r: dict) -> bool:
    """Sinyali (zayıf olanı da) yaz. Döner: yeni satır mı."""
    resolve_ts = int(r["last_ts"]) + r["horizon"] * TF_SEC[r["tf"]]
    async with db() as conn:
        cur = await conn.execute(
            """INSERT OR IGNORE INTO pattern_signals(ts,coin,tf,win,horizon,
                 n_match,p_up,base_up,edge,z,med_move,q25,q75,px,resolve_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now(), r["coin"], r["tf"], r["win"], r["horizon"], r.get("n") or 0,
             r.get("p_up"), r.get("base_up"), r.get("edge"), r.get("z"),
             r.get("med"), r.get("q25"), r.get("q75"), r.get("px"), resolve_ts))
        return (cur.rowcount or 0) > 0


async def scan_all(cfg, notifier=None) -> dict:
    out = {"coins": 0, "checked": 0, "signals": 0, "strong": 0, "alerted": 0,
           "thin": 0, "structural": 0, "skipped": "", "best": None}
    if not getattr(cfg, "pattern_enabled", True):
        out["skipped"] = "kapalı"
        return out
    coins = await bars.universe(None)              # arşivde ne varsa
    out["coins"] = len(coins)
    hs, chat = horizons(cfg), (getattr(cfg, "pattern_chat_id", "") or "").strip()
    cool = max(300, int(getattr(cfg, "pattern_scan_sec", 1800)) * 2)

    for coin in coins:
        for tf in bars.TFS:
            for h in hs:
                try:
                    r = await analyze_coin(cfg, coin, tf, h)
                except Exception as e:
                    log.warning("örüntü çözümlemesi patladı (%s/%s/%s): %s",
                                coin, tf, h, e)
                    continue
                if not r:
                    continue
                out["checked"] += 1
                if r["label"] == "yetersiz":
                    out["thin"] += 1
                    out["structural"] += 1 if r.get("structural") else 0
                    continue
                if await save(r):
                    out["signals"] += 1
                if r["label"] != "kayda değer":
                    continue
                out["strong"] += 1
                if not out["best"] or abs(r["z"]) > abs(out["best"]["z"]):
                    out["best"] = {"coin": coin, "tf": tf, "horizon": h,
                                   "z": r["z"], "edge": r["edge"],
                                   "p_up": r["p_up"]}
                if not alertable(cfg, r) or notifier is None or not chat:
                    continue
                key = f"pattern:{coin}:{tf}:{h}"
                if await alert_recent("pattern", key, cool):
                    continue
                from ..telegram import format as fmt
                text = fmt.pattern_alert({**r, "record": await record()})
                if await notifier.send("pattern", text, priority="high",
                                       key=f"{key}:{r['last_ts']}", chat_id=chat):
                    await alert_log("pattern", key, text)
                    async with db() as conn:
                        await conn.execute(
                            "UPDATE pattern_signals SET alerted=1 WHERE coin=? AND"
                            " tf=? AND horizon=? AND resolve_ts=?",
                            (coin, tf, h, int(r["last_ts"]) + h * TF_SEC[tf]))
                    out["alerted"] += 1

    await kv_set("patterns_stats", {**out, "ts": now(), "chat": bool(chat)})
    if out["strong"]:
        log.info("örüntü: %d çözümleme, %d sinyal, %d güçlü, %d bildirim",
                 out["checked"], out["signals"], out["strong"], out["alerted"])
    return out


async def resolve_due(limit: int = 200) -> dict:
    """Vadesi gelmiş sinyalleri arşivden ölç ve damgala."""
    ts = now()
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM pattern_signals WHERE status='open' AND resolve_ts <= ?"
            " ORDER BY resolve_ts LIMIT ?", (ts, limit))
        rows = [dict(r) for r in await cur.fetchall()]
    hit = miss = unres = 0
    for s in rows:
        closes, tss = await bars.series(s["coin"], s["tf"])
        px = None
        for c, t in zip(reversed(closes), reversed(tss)):
            if t <= int(s["resolve_ts"]) + TF_SEC[s["tf"]]:
                # Ölçüm vadeden ÇOK uzaksa ölçme: bayat barla "tuttu" demek yalan.
                px = c if t >= int(s["resolve_ts"]) - 3 * TF_SEC[s["tf"]] else None
                break
        if not px or not s["px"]:
            status, measured = "unresolvable", None
            unres += 1
        else:
            measured = (px / float(s["px"]) - 1) * 100
            up = measured > 0
            predicted_up = (s["p_up"] or 50) >= 50
            status = "hit" if up == predicted_up else "miss"
            hit, miss = (hit + 1, miss) if status == "hit" else (hit, miss + 1)
        async with db() as conn:
            await conn.execute(
                "UPDATE pattern_signals SET status=?, measured=?, resolved_ts=?"
                " WHERE id=?", (status, measured, ts, s["id"]))
    if rows:
        log.info("örüntü sicili: %d hit, %d miss, %d ölçülemedi", hit, miss, unres)
    return {"hit": hit, "miss": miss, "unresolvable": unres, "n": len(rows)}


async def record(only_alerted: bool = False) -> dict:
    """Karne. `only_alerted` = yalnız kanala düşenler (asıl merak edilen)."""
    q = ("SELECT status, COUNT(*) n FROM pattern_signals WHERE 1=1"
         + (" AND alerted=1" if only_alerted else "") + " GROUP BY status")
    async with db() as conn:
        cur = await conn.execute(q)
        st = {r["status"]: r["n"] for r in await cur.fetchall()}
    hit, miss = st.get("hit", 0), st.get("miss", 0)
    tot = hit + miss
    return {"hit": hit, "miss": miss, "open": st.get("open", 0),
            "unresolvable": st.get("unresolvable", 0), "n": tot,
            "rate": round(hit / tot * 100) if tot else None}


async def calibration(buckets=(50, 60, 70, 80, 101)) -> list[dict]:
    """Söylediğimiz olasılık ile GERÇEKLEŞEN oran. Aracın tek dürüst ölçüsü.

    Yön bilgisini "yukarı olasılığı"ndan alıyoruz; aşağı sinyallerde tahmin
    edilen olasılık 100−p_up olur, o yüzden ikisi de aynı kovaya düşsün diye
    güven = max(p, 100−p).
    """
    async with db() as conn:
        cur = await conn.execute(
            "SELECT p_up, status FROM pattern_signals"
            " WHERE status IN ('hit','miss') AND p_up IS NOT NULL")
        rows = [dict(r) for r in await cur.fetchall()]
    out, lo = [], 50
    for hi in buckets[1:]:
        sel = [r for r in rows
               if lo <= max(r["p_up"], 100 - r["p_up"]) < hi]
        n = len(sel)
        out.append({"lo": lo, "hi": min(hi, 100), "n": n,
                    "rate": round(sum(1 for r in sel if r["status"] == "hit")
                                  / n * 100) if n else None})
        lo = hi
    return out


async def recent(limit: int = 60, strong_only: bool = False) -> list[dict]:
    q = ("SELECT * FROM pattern_signals WHERE status='open'"
         + (" AND ABS(z) >= 2" if strong_only else "")
         + " ORDER BY ABS(z) DESC, ts DESC LIMIT ?")
    async with db() as conn:
        cur = await conn.execute(q, (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def history(limit: int = 40) -> list[dict]:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM pattern_signals WHERE status IN ('hit','miss')"
            " ORDER BY resolved_ts DESC LIMIT ?", (limit,))
        return [dict(r) for r in await cur.fetchall()]


async def prune(days: int = RETENTION_D) -> int:
    async with db() as conn:
        cur = await conn.execute(
            "DELETE FROM pattern_signals WHERE ts < ? AND status != 'open'",
            (now() - days * 86400,))
        return cur.rowcount or 0


async def loop(cfg, notifier) -> None:
    from ..health import beat
    await asyncio.sleep(240)                 # arşiv bir tur atsın
    while True:
        try:
            await beat("patterns")
            await resolve_due()
            await scan_all(cfg, notifier)
            await prune()
            await beat("patterns")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("örüntü turu hatası")
        await asyncio.sleep(max(300, int(getattr(cfg, "pattern_scan_sec", 1800))))
