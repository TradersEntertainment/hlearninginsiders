"""🧪 Kâğıt hesap — kuralın sonuçlanan işlemlerini sim ayarıyla ($10K / 5x / %33) yeniden oynatır (saf).

Yalnız betimleme: kapı bu hesaba bakmaz (kapı küme düzeyinde, piyasa düzeltmeli neti test eder). Hesap
"her olayda işleme girseydim" sorusunu cevaplar: gerçek işlem gibi piyasa düzeltmesiz net (ham getiri −
maliyet). Kurallar:
  • girişte marjin = min(bakiye × pay, serbest bakiye); serbest kalmadıysa işlem "yer yok" (sayılır)
  • kâr/zarar = marjin × kaldıraç × net; zarar marjini aşamaz (likidasyon = marjinin tamamı)
  • yol üstünde en kötü an (mae, yön düzeltmeli) kaldıraçla marjini bitirdiyse işlem likidasyon sayılır —
    sonradan toparlanan son net kazanç yazılmaz
  • kapanış sırası çıkış zamanına göre; aynı anda giriş varsa önce kapanışlar
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

TR = ZoneInfo("Europe/Istanbul")
MIN_MARGIN = 1.0


def replay(trades: list[dict], start: float, lev: float, margin_pct: float, start_ts: int = 0) -> dict:
    """trades: {"entry_ts", "exit_ts", "net", "coin", "mae"?, "rule"?}. Döner: eğri noktaları (sim.curve biçimi:
    (ts, bakiye, işlem|None)), son bakiye ve özet."""
    start = float(start)
    lev = max(1.0, float(lev or 1))
    pct = max(0.0, float(margin_pct or 0)) / 100.0
    bal = start
    opened: list[dict] = []
    closed: list[dict] = []
    skipped = 0
    pts: list[tuple] = [(int(start_ts or 0), bal, None)]

    def close_until(t: int | None) -> None:
        nonlocal bal
        opened.sort(key=lambda o: (o["exit_ts"], o["entry_ts"]))
        while opened and (t is None or opened[0]["exit_ts"] <= t):
            o = opened.pop(0)
            bal += o["pnl_usd"]
            closed.append(o)
            pts.append((o["exit_ts"], bal, o))

    ok = [t for t in trades if t.get("net") is not None and t.get("exit_ts") is not None
          and t.get("entry_ts") is not None]                  # ölçülmemiş satır sayılmaz
    for t in sorted(ok, key=lambda t: (int(t["entry_ts"]), int(t["exit_ts"]))):
        close_until(int(t["entry_ts"]))
        free = bal - sum(o["margin"] for o in opened)
        margin = min(max(0.0, bal) * pct, free)
        if margin < MIN_MARGIN:
            skipped += 1
            continue
        mae = t.get("mae")
        liq = mae is not None and lev * float(mae) <= -1.0           # yol üstünde marjin bitti
        pnl = -margin if liq else max(-margin, margin * lev * float(t["net"]))   # en çok marjin gider
        coin = str(t.get("coin") or "")
        opened.append({**t, "entry_ts": int(t["entry_ts"]), "exit_ts": int(t["exit_ts"]), "margin": margin,
                       "pnl_usd": pnl, "coin": coin.split(":")[-1], "liq": liq,
                       "title": f"{coin.split(':')[-1]} · net %{float(t['net']) * 100:+.2f}"
                                + (" · likidasyon" if liq else "") + f" · {pnl:+,.0f}$"})
    close_until(None)
    peak, dd = start, 0.0
    for _, b, _ in pts:
        peak = max(peak, b)
        if peak > 0:
            dd = min(dd, b / peak - 1)
    days: dict[str, float] = {}
    for o in closed:
        d = datetime.fromtimestamp(o["exit_ts"], TR).strftime("%d.%m.%Y")
        days[d] = days.get(d, 0.0) + o["pnl_usd"]
    n = len(closed)
    return {"points": pts, "balance": bal, "start": start, "ret_pct": (bal / start - 1) * 100 if start else None,
            "n": n, "skipped": skipped, "liq": sum(1 for o in closed if o["liq"]), "pos_share": (sum(1 for o in closed if o["pnl_usd"] > 0) / n) if n else None,
            "worst_trade": min((o["pnl_usd"] for o in closed), default=None),
            "worst_day": min(days.items(), key=lambda kv: kv[1]) if days else None,
            "max_dd_pct": dd * 100, "lev": lev, "margin_pct": pct * 100}


def thin(points: list, maxp: int = 300) -> list:
    """Çizim için seyreltme: kova başına en düşük ve en yüksek bakiye (zaman sırasıyla); işaretler düşer.
    Özet sayılar seyreltmeden ÖNCE hesaplanır — yalnız eğri kabalaşır."""
    if len(points) <= maxp:
        return points
    step = -(-(len(points) - 2) // (maxp // 2))
    keep = [points[0]]
    for i in range(1, len(points) - 1, step):
        b = points[i:min(i + step, len(points) - 1)]
        lo, hi = min(b, key=lambda p: p[1]), max(b, key=lambda p: p[1])
        for p in sorted({id(lo): lo, id(hi): hi}.values(), key=lambda p: p[0]):
            keep.append((p[0], p[1], None))
    keep.append((points[-1][0], points[-1][1], None))
    return keep
