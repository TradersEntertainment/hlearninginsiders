"""Liq simülasyonu — kâğıt üstünde iki bacaklı işlem defteri (gerçek emir YOK).

Kullanıcı kuralı: kripto liq radarı sondayla doğrulanmış SON UYARI verince
(liq'e ≤%0,5 kala, ≥$500K, BTC/ETH hariç) balinanın TERSİNE gir — short
balina patlayınca zorunlu ALIŞ gelir, biz LONG; long balina → SHORT. Stop
girişten %10. Hedef: zincir simülasyonunun "defter nereye kadar yenir"
fiyatı (`cascade.simulate` → end_px), limit emir. Liq gerçekleşip fiyat o
iğneye uzanırsa ön bacak orada kapanır ve AYNI fiyattan ters bacak açılır
("iğneden girebilirse girer, giremezse girmez"); ters bacak %0,75 geri
çekilince kapanır, stop %10. Liq oldu ama iğne 30 dk içinde hedefe gelmezse
ön bacak piyasadan kapanır, ters bacak hiç açılmaz.

Hesap: başlangıç $10K, 5x kaldıraç, bakiyenin %33'ü bir dilim (aynı anda en
çok 3 işlem; dilimler doluysa sinyal "bakiye bağlı" diye atlanır, son dilim
kalan bakiyeden küçük olabilir), ücretler HL taban (taker %0,045 / maker
%0,015), bakiye bileşik. Değerlendirme 1 dk mum uçlarıyla: seviyeye dokunan
limit dolmuş sayılır (iyimser), aynı mumda stop+hedef → stop (kötümser),
slippage/kısmi dolum yok. Sayfa /sim, Telegram SIM_CHAT_ID (env; boşsa mesaj
yok, defter yine ilerler).

Defter kuralı alarm kuralından BİLEREK ayrılır: işlem durumu her zaman
ilerler, Telegram düşerse `fail:sim` kaydı düşer ama işlem geri açılmaz —
bu bir simülasyon defteridir, mesaj değil.
"""
import asyncio
import logging

from ..db import alert_log, db, kv_get, kv_set, now
from ..hl.universe import main_dex_ctx

log = logging.getLogger("radar.sim")

INTERVAL = "1m"
CANDLE_SEC = 60
ENTRY_MARK_TTL = 30          # girişte fiyat bu kadar taze olsun (1 istek, sinyal nadir)
MARK_MAX_AGE = 600           # mum yoksa kullanılan kv fiyatı bundan bayatsa ERTELE
CANDLE_LOOKBACK = 6 * 3600   # tek istekte en çok bu kadar geriye mum
SKIP_DEDUPE_SEC = 3600       # aynı (coin, balina, neden) bu sürede tek atlanma satırı
MIN_AVAIL_FRAC = 0.05        # kullanılabilir bakiye bunun altındaysa "bakiye bağlı"
PAGE_CLOSED = 50
PAGE_SKIPPED = 20
ACCOUNT_KV = "sim_account"
STATS_KV = "sim_stats"

_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """Loop'a bağlı kilit: kanca (açılış), tick (kapanış) ve sıfırlama sıralanır.
    Testler `sim._lock = None` ile sıfırlar (sweeper._probe_sem deseni)."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


# ---------------- saf motor (ağ/DB yok) ----------------

def slots_of(margin_pct) -> int:
    """Marjin payından dilim sayısı: 33 → 3, 50 → 2, 100 → 1, 25 → 4."""
    pct = float(margin_pct or 0)
    return max(1, int(100 // pct)) if pct > 0 else 1


def sizing(available: float, margin_pct: float, leverage: float, entry: float,
           balance: float | None = None) -> tuple[float, float, float]:
    """(marjin, notional, adet). Marjin = BAKİYE × pay (dilim), kullanılabilir
    bakiyeyi aşamaz (son dilim küçük kalabilir); notional = marjin × kaldıraç.
    `balance` verilmezse kullanılabilir × pay (eski davranış)."""
    base = float(balance) if balance is not None else float(available or 0)
    margin = max(0.0, base) * max(0.0, float(margin_pct or 0)) / 100.0
    margin = min(margin, max(0.0, float(available or 0)))
    lev = max(1.0, float(leverage or 1))
    notional = margin * lev
    qty = notional / float(entry) if entry and float(entry) > 0 else 0.0
    return margin, notional, qty


def usd_short(v: float) -> str:
    v = float(v or 0)
    return f"${v / 1000:.1f}K" if abs(v) >= 1000 else f"${v:,.0f}"


def fee(notional: float, pct: float) -> float:
    return abs(float(notional or 0)) * float(pct or 0) / 100.0


def pnl_of(t: dict, exit_px: float, exit_fee_pct: float) -> tuple[float, float, float]:
    """(net kâr/zarar, toplam ücret, marjine göre %). Giriş ücreti `fee_usd`'de
    (açılışta yazılır); çıkış ücreti çıkış notional'ı üzerinden eklenir."""
    sign = 1 if t.get("side") == "long" else -1
    qty = float(t.get("qty") or 0)
    gross = sign * (float(exit_px) - float(t["entry_px"])) * qty
    exit_fee = fee(float(exit_px) * qty, exit_fee_pct)
    fee_total = float(t.get("fee_usd") or 0) + exit_fee
    net = gross - fee_total
    margin = float(t.get("margin") or 0)
    pct = net / margin * 100 if margin > 0 else 0.0
    return net, fee_total, pct


def plan_leg1(cfg, coin: str, mark, whale: dict, casc: dict | None,
              available: float, balance: float, n_open: int = 0) -> dict:
    """Ön bacak planı ya da {"skip": neden}. Yön balinanın tersi; hedef zincir
    sonu (defter yoksa liq fiyatı, ters bacak yok); stop girişten %X. Boyut:
    bakiyenin `sim_margin_pct`'i bir dilim, dilimler (`slots_of`) doluysa atlanır."""
    if not whale.get("verified"):
        return {"skip": "sonda teyidi yok"}
    try:
        mark = float(mark or 0)
    except (TypeError, ValueError):
        mark = 0.0
    if mark <= 0:
        return {"skip": "fiyat yok"}
    liq = float(whale.get("liq_px") or 0)
    if liq <= 0:
        return {"skip": "liq fiyatı yok"}
    side = "long" if whale.get("side") == "short" else "short"
    up = side == "long"
    notes: list[str] = []
    tp_src, tp = "liq", liq
    end = float((casc or {}).get("end_px") or 0)
    if casc and not casc.get("no_book") and end > 0 and ((up and end > liq) or (not up and end < liq)):
        tp_src, tp = "cascade", end
    cap = float(getattr(cfg, "sim_max_tp_pct", 0) or 0)
    if cap > 0:
        lim = mark * (1 + cap / 100) if up else mark * (1 - cap / 100)
        if (up and tp > lim) or (not up and tp < lim):
            notes.append(f"hedef %{cap:g}'de kırpıldı (zincir sonu {tp:.6g}), ters bacak yok")
            tp, tp_src = lim, "capped"
    dist_pct = (tp - mark) / mark * 100 * (1 if up else -1)
    min_tp = float(getattr(cfg, "sim_min_tp_pct", 0.2) or 0)
    if dist_pct < min_tp:
        return {"skip": f"hedef çok yakın (%{dist_pct:.2f} < %{min_tp:g})"}
    pct = float(getattr(cfg, "sim_margin_pct", 33) or 100)
    slots = slots_of(pct)
    if int(n_open or 0) >= slots:
        return {"skip": f"bakiye bağlı ({int(n_open)}/{slots} dilim dolu)"}
    if available <= 0 or available < float(balance or 0) * MIN_AVAIL_FRAC:
        return {"skip": f"bakiye bağlı (kullanılabilir ${max(0.0, float(available or 0)):,.0f})"}
    lev = float(getattr(cfg, "sim_leverage", 5) or 1)
    margin, notional, qty = sizing(available, pct, lev, mark, balance)
    if qty <= 0:
        return {"skip": "boyut sıfır"}
    if margin < float(balance or 0) * pct / 100 * 0.999:
        notes.append(f"son dilim kalan bakiyeden ({usd_short(margin)}), tam dilim {usd_short(float(balance or 0) * pct / 100)}")
    stop_pct = float(getattr(cfg, "sim_stop_pct", 10) or 0)
    stop = mark * (1 - stop_pct / 100) if up else mark * (1 + stop_pct / 100)
    if tp_src == "liq":
        notes.append("defter yok → hedef liq fiyatı, ters bacak yok")
    if casc and casc.get("exhausted"):
        notes.append("görünen defter zincir sonunda bitti (hedef alt sınır)")
    return {"coin": coin, "leg": 1, "side": side, "entry_px": mark, "entry_src": "ctx",
            "qty": qty, "notional": notional, "margin": margin, "leverage": lev,
            "stop_px": stop, "tp_px": tp, "tp_src": tp_src,
            "fee_usd": fee(notional, getattr(cfg, "sim_fee_taker_pct", 0.045)),
            "whale_addr": whale.get("address"), "whale_side": whale.get("side"),
            "whale_notional": float(whale.get("notional") or 0), "whale_liq_px": liq,
            "casc_end_px": end or None, "casc_total": float((casc or {}).get("total_usd") or 0) or None,
            "casc_note": (f"{int((casc or {}).get('n_pos') or 0)} poz zincirde"
                          + (" · kaba defter" if (casc or {}).get("coarse") else "")) if casc else None,
            "note": " · ".join(notes) or None}


def plan_leg2(cfg, parent: dict, px: float, available: float, balance: float,
              n_open: int = 0) -> dict | None:
    """Ters bacak: ön bacak hedefte dolduğu fiyattan ters yön. Yalnız hedef
    gerçek zincir sonuysa (defter yok / kırpılmış hedefte iğne anlamsız).
    Ön bacak kapanınca dilimi boşalır; ters bacak o dilimi alır."""
    if parent.get("tp_src") != "cascade" or not px or float(px) <= 0:
        return None
    if available <= 0 or available < float(balance or 0) * MIN_AVAIL_FRAC:
        return None
    pct = float(getattr(cfg, "sim_margin_pct", 33) or 100)
    if int(n_open or 0) >= slots_of(pct):
        return None
    px = float(px)
    side = "short" if parent.get("side") == "long" else "long"
    up = side == "long"
    lev = float(getattr(cfg, "sim_leverage", 5) or 1)
    margin, notional, qty = sizing(available, pct, lev, px, balance)
    if qty <= 0:
        return None
    tp_pct = float(getattr(cfg, "sim_post_tp_pct", 0.75) or 0)
    stop_pct = float(getattr(cfg, "sim_post_stop_pct", 10) or 0)
    tp = px * (1 + tp_pct / 100) if up else px * (1 - tp_pct / 100)
    stop = px * (1 - stop_pct / 100) if up else px * (1 + stop_pct / 100)
    return {"coin": parent["coin"], "leg": 2, "side": side, "entry_px": px, "entry_src": "tp",
            "qty": qty, "notional": notional, "margin": margin, "leverage": lev,
            "stop_px": stop, "tp_px": tp, "tp_src": "pct",
            "fee_usd": fee(notional, getattr(cfg, "sim_fee_maker_pct", 0.015)),
            "whale_addr": parent.get("whale_addr"), "whale_side": parent.get("whale_side"),
            "whale_notional": parent.get("whale_notional"), "whale_liq_px": parent.get("whale_liq_px"),
            "casc_end_px": parent.get("casc_end_px"), "casc_total": parent.get("casc_total"),
            "casc_note": None, "parent_id": parent.get("id"), "liq_ts": parent.get("liq_ts"),
            "note": None}


def select_candles(cands: list[dict], last_eval_ts, entry_ts) -> list[dict]:
    """Son değerlendirmeden sonra biten mumlar (oluşan mum yeniden okunur; tamamen
    kapanmış olanlar bir daha değil → çift işlem yok). Giriş dakikası dahil."""
    lo = max(int(last_eval_ts or 0), int(entry_ts or 0))
    return sorted((c for c in cands if int(c["t"]) + CANDLE_SEC > lo), key=lambda c: c["t"])


def step(t: dict, c: dict, live_liq_px=None) -> dict | None:
    """Tek mum: uçlar güncellenir, balina liq kesişimi işaretlenir, stop/hedef
    denetlenir. Sıra: liq kesişimi → stop → hedef; aynı mumda ikisi → stop
    (kötümser, `both`). Dönüş {"reason","px","ts","both"} ya da None."""
    up = t.get("side") == "long"
    hi, lo = float(c["h"]), float(c["l"])
    t["hi_px"] = max(float(t.get("hi_px") or hi), hi)
    t["lo_px"] = min(float(t.get("lo_px") or lo), lo)
    if int(t.get("leg") or 1) == 1 and not t.get("liq_ts"):
        liq = float(live_liq_px or t.get("whale_liq_px") or 0)
        wside = t.get("whale_side")
        if liq > 0 and ((wside == "short" and hi >= liq) or (wside == "long" and lo <= liq)):
            t["liq_ts"] = int(c["t"])
    stop, tp = float(t.get("stop_px") or 0), float(t.get("tp_px") or 0)
    hit_stop = stop > 0 and ((up and lo <= stop) or (not up and hi >= stop))
    hit_tp = tp > 0 and ((up and hi >= tp) or (not up and lo <= tp))
    if hit_stop:
        return {"reason": "stop", "px": stop, "ts": int(c["t"]), "both": bool(hit_tp)}
    if hit_tp:
        return {"reason": "tp", "px": tp, "ts": int(c["t"]), "both": False}
    return None


def expire(t: dict, now_ts: int, cfg) -> dict | None:
    """Süre kuralları: 2. bacak azami ömür; 1. bacakta liq sonrası bekleme
    (iğne gelmedi) ve azami ömür (liq gelmedi). Dönüş {"reason": "timeout", "why"}."""
    entry_ts = int(t.get("entry_ts") or 0)
    if int(t.get("leg") or 1) == 2:
        mx = int(getattr(cfg, "sim_post_max_min", 120) or 0) * 60
        if mx > 0 and now_ts - entry_ts >= mx:
            return {"reason": "timeout", "why": f"ters bacak {mx // 60} dk içinde hedefe gelmedi"}
        return None
    after = int(getattr(cfg, "sim_after_liq_min", 30) or 0) * 60
    if t.get("liq_ts") and after > 0 and now_ts - int(t["liq_ts"]) >= after:
        return {"reason": "timeout", "why": f"liq geldi, iğne {after // 60} dk içinde hedefe uzanmadı"}
    pre = int(getattr(cfg, "sim_pre_max_min", 360) or 0) * 60
    if pre > 0 and now_ts - entry_ts >= pre:
        return {"reason": "timeout", "why": f"balina {pre // 60} dk içinde patlamadı (liq gelmedi)"}
    return None


def whale_event(t: dict, w: dict | None) -> dict | None:
    """Balina izleme satırı (cryptoliq_watch) ne diyor: likide olduysa `liq_ts`
    (kesişim daha önce görülmediyse); liq olmadan kapattıysa tez bozuldu (void).
    Doğrulanamadıysa: liq kesildiyse likide say, kesilmediyse tez bozuldu."""
    if int(t.get("leg") or 1) != 1 or not w or not w.get("closed_ts"):
        return None
    kind = w.get("closed_kind")
    if kind == "liq":
        if not t.get("liq_ts"):
            t["liq_ts"] = int(w["closed_ts"])
        return None
    if kind == "close":
        return {"reason": "void", "why": "balina liq olmadan kapattı (tez bozuldu)"}
    if t.get("liq_ts"):
        return None
    return {"reason": "void", "why": "balina pozisyonu yok oldu, likidasyon doğrulanamadı (tez bozuldu)"}


def stats(closed: list[dict], start_balance: float, balance: float) -> dict:
    n = len(closed)
    wins = [c for c in closed if float(c.get("pnl_usd") or 0) > 0]
    pnl_sum = sum(float(c.get("pnl_usd") or 0) for c in closed)
    fees = sum(float(c.get("fee_usd") or 0) for c in closed)
    start = float(start_balance or 0)
    by_leg = {}
    for leg in (1, 2):
        rows = [c for c in closed if int(c.get("leg") or 1) == leg]
        by_leg[leg] = {"n": len(rows), "wins": sum(1 for c in rows if float(c.get("pnl_usd") or 0) > 0),
                       "pnl": sum(float(c.get("pnl_usd") or 0) for c in rows)}
    reasons: dict[str, int] = {}
    for c in closed:
        reasons[c.get("exit_reason") or "?"] = reasons.get(c.get("exit_reason") or "?", 0) + 1
    return {"n": n, "wins": len(wins), "losses": n - len(wins),
            "hit_rate": (len(wins) / n * 100) if n else None,
            "pnl_sum": pnl_sum, "fees": fees,
            "pnl_pct": (pnl_sum / start * 100) if start > 0 else None,
            "best": max(closed, key=lambda c: float(c.get("pnl_usd") or 0)) if closed else None,
            "worst": min(closed, key=lambda c: float(c.get("pnl_usd") or 0)) if closed else None,
            "by_leg": by_leg, "reasons": reasons}


def curve(closed: list[dict], start_balance: float, start_ts: int) -> list[tuple[int, float, dict | None]]:
    """(ts, bakiye, işlem) noktaları: başlangıç + her kapanıştan sonra."""
    pts = [(int(start_ts or 0), float(start_balance or 0), None)]
    bal = float(start_balance or 0)
    for c in sorted(closed, key=lambda c: (int(c.get("exit_ts") or 0), int(c.get("id") or 0))):
        bal += float(c.get("pnl_usd") or 0)
        pts.append((int(c.get("exit_ts") or 0), bal, c))
    return pts


def svg_curve(points: list, start_balance: float, w: int = 640, h: int = 120) -> str:
    """Tek seri bakiye eğrisi (inline SVG): 2px çizgi, başlangıç çizgisi kesikli,
    her kapanışa ≥8px işaret + <title> (hover). Renk sonuca göre (yeşil/kırmızı),
    metin sayfa tonlarında. Site tema değişkenleriyle boyanır."""
    if len(points) < 2:
        return ""
    pad_l, pad_r, pad_t, pad_b = 46, 12, 10, 18
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    lo, hi = min(ys + [float(start_balance)]), max(ys + [float(start_balance)])
    if hi - lo < 1e-9:
        hi, lo = hi + 1, lo - 1
    span = (hi - lo) * 0.08
    lo, hi = lo - span, hi + span
    x0, x1 = xs[0], xs[-1]
    n = len(points)

    def X(i, t):
        # zaman ekseni yerine sıra: kapanışlar günlere yayılır, eşit adım okunur
        return pad_l + (w - pad_l - pad_r) * (i / (n - 1))

    def Y(v):
        return pad_t + (h - pad_t - pad_b) * (1 - (v - lo) / (hi - lo))

    end_color = "var(--green)" if ys[-1] >= float(start_balance) else "var(--red)"
    poly = " ".join(f"{X(i, p[0]):.1f},{Y(p[1]):.1f}" for i, p in enumerate(points))
    ys0 = Y(float(start_balance))
    out = [f'<svg class="simcurve" viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img"'
           f' aria-label="Bakiye eğrisi: {n - 1} kapanış, son {ys[-1]:,.0f}$" preserveAspectRatio="none">',
           f'<line x1="{pad_l}" y1="{ys0:.1f}" x2="{w - pad_r}" y2="{ys0:.1f}" stroke="var(--dim)"'
           f' stroke-dasharray="3 4" stroke-width="1" opacity="0.7"/>',
           f'<text x="{pad_l - 6}" y="{ys0 + 4:.1f}" text-anchor="end" font-size="10" fill="var(--dim)">'
           f'${start_balance / 1000:.1f}K</text>',
           f'<polyline fill="none" stroke="{end_color}" stroke-width="2" stroke-linejoin="round"'
           f' stroke-linecap="round" vector-effect="non-scaling-stroke" points="{poly}"/>']
    for i, (ts, v, c) in enumerate(points):
        if not c:
            continue
        good = float(c.get("pnl_usd") or 0) >= 0
        col = "var(--green)" if good else "var(--red)"
        title = (f"{c.get('coin')} {'ön' if int(c.get('leg') or 1) == 1 else 'ters'} bacak"
                 f" · {c.get('exit_reason')} · {float(c.get('pnl_usd') or 0):+,.0f}$ → {v:,.0f}$")
        out.append(f'<circle cx="{X(i, ts):.1f}" cy="{Y(v):.1f}" r="4" fill="{col}" stroke="var(--surface-1)"'
                   f' stroke-width="2" vector-effect="non-scaling-stroke"><title>{title}</title></circle>')
    out.append(f'<text x="{pad_l - 6}" y="{pad_t + 8}" text-anchor="end" font-size="10" fill="var(--dim)">'
               f'${max(ys) / 1000:.1f}K</text>')
    out.append(f'<text x="{pad_l - 6}" y="{h - pad_b}" text-anchor="end" font-size="10" fill="var(--dim)">'
               f'${min(ys) / 1000:.1f}K</text>')
    out.append("</svg>")
    return "".join(out)


def gate(cfg, notifier) -> str:
    """Telegram'a gönderim mümkün değilse NEDENİ (boş = gönderilebilir).
    Kanal boşsa hiçbir yere gitmez — ana sohbet kirlenmez; defter yine ilerler."""
    from ..notify import kind_enabled
    if not (getattr(cfg, "sim_chat_id", "") or "").strip():
        return "SIM_CHAT_ID yok"
    if notifier is None or getattr(notifier, "bot", None) is None:
        return "bot yok"
    if not kind_enabled(cfg, "sim"):
        return "bildirim kapalı (notify_sim)"
    return ""


def source_gate(cfg) -> str:
    """Sinyal kaynağı (kripto liq radarı) çalışmıyorsa nedeni: radar `scan()`
    kapı kapalıyken adım 6'ya gelmez, sim de sinyal alamaz."""
    if not getattr(cfg, "crypto_liq_enabled", True):
        return "kripto liq radarı kapalı (crypto_liq_enabled)"
    if not (getattr(cfg, "crypto_chat_id", "") or "").strip():
        return "CRYPTO_CHAT_ID yok — radar tarama yapmıyor"
    if not getattr(cfg, "notify_cryptoliq", True):
        return "kripto liq bildirimi kapalı (notify_cryptoliq) — radar tarama yapmıyor"
    return ""


def available_of(acc: dict, opens: list[dict]) -> float:
    return float(acc.get("balance") or 0) - sum(float(o.get("margin") or 0) for o in opens)


# ---------------- DB / hesap ----------------

_COLS = ("run", "coin", "leg", "side", "status", "entry_px", "entry_ts", "entry_src", "qty", "notional",
         "margin", "leverage", "stop_px", "tp_px", "tp_src", "exit_px", "exit_ts", "exit_reason",
         "pnl_usd", "pnl_pct", "fee_usd", "whale_addr", "whale_side", "whale_notional", "whale_liq_px",
         "casc_end_px", "casc_total", "casc_note", "liq_ts", "last_eval_ts", "hi_px", "lo_px",
         "parent_id", "skip_reason", "note", "created_ts")


async def account(cfg) -> dict:
    acc = await kv_get(ACCOUNT_KV)
    if not acc or "balance" not in acc:
        start = float(getattr(cfg, "sim_start_balance", 10000) or 0)
        acc = {"run": 1, "balance": start, "start_balance": start, "start_ts": now()}
        await kv_set(ACCOUNT_KV, acc)
    return acc


async def open_trades(coin: str | None = None) -> list[dict]:
    async with db() as conn:
        if coin:
            cur = await conn.execute(
                "SELECT * FROM sim_trades WHERE status='open' AND coin=? ORDER BY id", (coin,))
        else:
            cur = await conn.execute("SELECT * FROM sim_trades WHERE status='open' ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def _insert(t: dict) -> int:
    cols = [c for c in _COLS if c in t]
    async with db() as conn:
        cur = await conn.execute(
            f"INSERT INTO sim_trades({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
            tuple(t[c] for c in cols))
        return cur.lastrowid


async def _set(tid: int, **fields) -> None:
    if not fields:
        return
    async with db() as conn:
        await conn.execute(
            f"UPDATE sim_trades SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?",
            (*fields.values(), tid))


async def _close(cfg, t: dict, reason: str, px: float, ts: int, maker: bool,
                 why: str | None = None) -> dict:
    """Kapanış: kâr/zarar + ücret, bakiye bileşik, satır closed. Bir kez ilerler."""
    fee_pct = getattr(cfg, "sim_fee_maker_pct", 0.015) if maker else getattr(cfg, "sim_fee_taker_pct", 0.045)
    net, fee_total, pct = pnl_of(t, px, fee_pct)
    acc = await account(cfg)
    acc["balance"] = float(acc.get("balance") or 0) + net
    await kv_set(ACCOUNT_KV, acc)
    note = " · ".join(x for x in (t.get("note"), why) if x) or None
    fields = dict(status="closed", exit_px=float(px), exit_ts=int(ts), exit_reason=reason,
                  pnl_usd=net, pnl_pct=pct, fee_usd=fee_total, note=note,
                  hi_px=t.get("hi_px"), lo_px=t.get("lo_px"), liq_ts=t.get("liq_ts"),
                  last_eval_ts=int(ts))
    await _set(t["id"], **fields)
    t.update(fields)
    t["balance_after"] = acc["balance"]
    log.info("sim #%s %s %s bacak %s kapandı: %s @ %.6g → %+.0f$ (bakiye %.0f)",
             t["id"], t.get("coin"), t.get("leg"), t.get("side"), reason, float(px), net, acc["balance"])
    return t


async def _skip(coin: str, whale: dict, reason: str, run: int) -> bool:
    """Atlanan sinyal satırı (sayfada 'neden işlem yok' sorusu için). Aynı
    (coin, balina, neden) bir saatte bir yazılır — spam yok."""
    addr = whale.get("address") or ""
    ts = now()
    async with db() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM sim_trades WHERE status='skipped' AND coin=? AND whale_addr=?"
            " AND skip_reason=? AND created_ts>? LIMIT 1", (coin, addr, reason, ts - SKIP_DEDUPE_SEC))
        if await cur.fetchone():
            return False
    await _insert({"run": run, "coin": coin, "leg": 1, "side": "long" if whale.get("side") == "short" else "short",
                   "status": "skipped", "whale_addr": addr, "whale_side": whale.get("side"),
                   "whale_notional": float(whale.get("notional") or 0),
                   "whale_liq_px": float(whale.get("liq_px") or 0) or None,
                   "skip_reason": reason, "created_ts": ts})
    return True


async def _watch_row(coin: str, addr: str) -> dict | None:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT liq_px, closed_ts, closed_kind, closed_px, stage FROM cryptoliq_watch"
            " WHERE coin=? AND address=?", (coin, addr))
        r = await cur.fetchone()
        return dict(r) if r else None


async def _mark(client, coin: str, *, ttl: int, fetch: bool) -> tuple[float | None, int | None]:
    """(fiyat, damga) — ana dex özetinden; alınamazsa (None, None)."""
    try:
        ctx = await main_dex_ctx(client, ttl=ttl, fetch=fetch and client is not None)
    except Exception as e:
        log.debug("sim fiyat alınamadı %s: %s", coin, e)
        return None, None
    m = ((ctx.get("c") or {}).get(coin) or {}).get("m")
    ts = ctx.get("ts")
    try:
        return (float(m) if m else None), (int(ts) if ts else None)
    except (TypeError, ValueError):
        return None, None


async def _notify(cfg, notifier, key: str, text: str, out: dict) -> bool:
    """Kapı doluysa hiç denemez. Düşerse `fail:sim` — defter GERİ ALINMAZ."""
    g = gate(cfg, notifier)
    if g:
        out["gate"] = g
        return False
    try:
        ok = await notifier.send("sim", text, key=key, chat_id=cfg.sim_chat_id)
    except Exception as e:
        log.warning("sim mesajı gönderilemedi (%s): %s", key, e)
        ok = False
    if ok:
        out["sent"] = out.get("sent", 0) + 1
    else:
        out["failed"] = out.get("failed", 0) + 1
        await alert_log("fail:sim", key, text)
    return ok


async def _bump(**counts) -> None:
    """Kanca tick dışında koşar; sayaçları kv istatistiğine ekler."""
    st = await kv_get(STATS_KV) or {}
    for k, v in counts.items():
        st[k] = int(st.get(k) or 0) + int(v)
    await kv_set(STATS_KV, st)


# ---------------- sinyal (cryptoliq adım 6 kancası) ----------------

async def on_signal(cfg, client, notifier, coin: str, mark, fresh: list[dict], casc: dict | None) -> bool:
    """Doğrulanmış SON UYARI → ön bacak. `fresh` mesafeye göre sıralı, `need`
    mesafeden türer: kademe 3 varsa fresh[0] odur ve `casc` onun için hesaplandı.
    Dönüş: işlem açıldı mı. Coinde açık işlem varsa sessiz (tekrar sinyal spam
    yazmaz); doğrulanmamış/açılamayan sinyal 'skipped' satırı olur."""
    if not getattr(cfg, "sim_enabled", True) or not fresh:
        return False
    w = fresh[0]
    if int(w.get("need") or 0) != 3:
        return False
    if float(w.get("notional") or 0) < float(getattr(cfg, "crypto_liq_min_usd", 0) or 0):
        return False
    async with _get_lock():
        acc = await account(cfg)
        opens = await open_trades()
        if any(o.get("coin") == coin for o in opens):
            return False
        run = int(acc.get("run") or 1)
        if not w.get("verified"):
            if await _skip(coin, w, "sonda teyidi yok", run):
                await _bump(skipped_signals=1)
            return False
        m, _ = await _mark(client, coin, ttl=ENTRY_MARK_TTL, fetch=True)
        entry = m or mark
        plan = plan_leg1(cfg, coin, entry, w, casc, available_of(acc, opens), float(acc.get("balance") or 0),
                         n_open=len(opens))
        if plan.get("skip"):
            if await _skip(coin, w, plan["skip"], run):
                await _bump(skipped_signals=1)
            return False
        ts = now()
        plan.update(run=run, status="open", entry_ts=ts, last_eval_ts=ts, created_ts=ts,
                    hi_px=plan["entry_px"], lo_px=plan["entry_px"])
        plan["id"] = await _insert(plan)
        log.info("sim #%s AÇILDI %s %s @ %.6g · hedef %.6g (%s) · stop %.6g · balina %s %s",
                 plan["id"], coin, plan["side"], plan["entry_px"], plan["tp_px"], plan["tp_src"],
                 plan["stop_px"], w.get("side"), (w.get("address") or "")[:10])
        out: dict = {}
        from ..telegram import format as fmt
        await _notify(cfg, notifier, f"sim:open:{plan['id']}", fmt.sim_opened(plan, acc, cfg), out)
        await _bump(opened=1, sent=out.get("sent", 0), failed=out.get("failed", 0))
        return True


# ---------------- değerlendirme döngüsü ----------------

async def _eval_one(cfg, client, notifier, t: dict, ts: int, out: dict) -> None:
    coin = t["coin"]
    cands: list[dict] = []
    start = max(int(t.get("last_eval_ts") or t.get("entry_ts") or ts) - CANDLE_SEC, ts - CANDLE_LOOKBACK)
    try:
        from .pricechart import parse_candles
        raw = await client.candles(coin, INTERVAL, start * 1000, ts * 1000)
        cands = parse_candles(raw)
        out["candles"] += 1
    except Exception as e:
        out["candle_err"] += 1
        log.debug("sim mum alınamadı %s: %s", coin, e)
    if not cands:
        # Mum yok: taze kv fiyatı tek düz mum sayılır; o da bayatsa ERTELE
        # (damga ilerlemez, sonraki tur yeniden bakar — uydurma fiyatla kapanış yok).
        m, mts = await _mark(client, coin, ttl=120, fetch=True)
        if not m or not mts or ts - mts > MARK_MAX_AGE:
            out["deferred"] += 1
            return
        cands = [{"t": int(mts), "o": m, "h": m, "l": m, "c": m}]
        out["mark_used"] += 1
    w = None
    live_liq = None
    if int(t.get("leg") or 1) == 1 and t.get("whale_addr"):
        w = await _watch_row(coin, t["whale_addr"])
        if w and w.get("liq_px"):
            live_liq = float(w["liq_px"])
    sel = select_candles(cands, t.get("last_eval_ts"), t.get("entry_ts"))
    ev = None
    for c in sel:
        ev = step(t, c, live_liq)
        if ev:
            break
    last = sel[-1] if sel else cands[-1]
    maker = False
    if ev is None:
        ev = whale_event(t, w)
        if ev is None:
            ev = expire(t, ts, cfg)
        if ev is not None:
            # piyasadan: ≤2 dk taze son mum kapanışı, yoksa kv fiyatı, o da yoksa son kapanış
            px = float(last["c"])
            if ts - int(last["t"]) > 2 * CANDLE_SEC + CANDLE_SEC:
                m, mts = await _mark(client, coin, ttl=120, fetch=True)
                if m and mts and ts - mts <= MARK_MAX_AGE:
                    px = m
            ev.update(px=px, ts=ts, both=False)
    else:
        maker = ev["reason"] == "tp"
    if ev is None:
        await _set(t["id"], last_eval_ts=ts, hi_px=t.get("hi_px"), lo_px=t.get("lo_px"), liq_ts=t.get("liq_ts"))
        return
    exit_ts = min(ts, max(int(ev["ts"]) + (CANDLE_SEC if ev["reason"] in ("tp", "stop") else 0),
                          int(t.get("entry_ts") or 0)))
    why = ev.get("why")
    if ev.get("both"):
        why = "aynı mumda hedef de vardı, stop sayıldı"
    closed = await _close(cfg, t, ev["reason"], float(ev["px"]), exit_ts, maker, why)
    out["closed"] += 1
    child = None
    if ev["reason"] == "tp" and int(t.get("leg") or 1) == 1:
        acc = await account(cfg)
        opens = await open_trades()
        plan = plan_leg2(cfg, closed, float(ev["px"]), available_of(acc, opens), float(acc.get("balance") or 0),
                         n_open=len(opens))
        if plan:
            # İğne mumu yeniden değerlendirilmez: ters bacak sonraki mumdan izlenir.
            plan.update(run=int(acc.get("run") or 1), status="open", entry_ts=exit_ts,
                        last_eval_ts=int(ev["ts"]) + CANDLE_SEC, created_ts=ts,
                        hi_px=plan["entry_px"], lo_px=plan["entry_px"])
            plan["id"] = await _insert(plan)
            child = plan
            out["opened2"] += 1
            log.info("sim #%s ters bacak AÇILDI %s %s @ %.6g · hedef %.6g · stop %.6g",
                     plan["id"], coin, plan["side"], plan["entry_px"], plan["tp_px"], plan["stop_px"])
    from ..telegram import format as fmt
    acc = await account(cfg)
    st = stats(await closed_trades(int(acc.get("run") or 1), limit=None), acc.get("start_balance"), acc.get("balance"))
    await _notify(cfg, notifier, f"sim:close:{t['id']}", fmt.sim_closed(closed, acc, cfg, child=child, st=st), out)


async def closed_trades(run: int | None, limit: int | None = PAGE_CLOSED) -> list[dict]:
    q = "SELECT * FROM sim_trades WHERE status='closed'"
    args: list = []
    if run is not None:
        q += " AND run=?"
        args.append(int(run))
    q += " ORDER BY exit_ts DESC, id DESC"
    if limit:
        q += f" LIMIT {int(limit)}"
    async with db() as conn:
        cur = await conn.execute(q, tuple(args))
        return [dict(r) for r in await cur.fetchall()]


async def skipped_trades(run: int | None, limit: int = PAGE_SKIPPED) -> list[dict]:
    q = "SELECT * FROM sim_trades WHERE status='skipped'"
    args: list = []
    if run is not None:
        q += " AND run=?"
        args.append(int(run))
    q += f" ORDER BY created_ts DESC, id DESC LIMIT {int(limit)}"
    async with db() as conn:
        cur = await conn.execute(q, tuple(args))
        return [dict(r) for r in await cur.fetchall()]


async def tick(cfg, client, notifier) -> dict:
    """Açık işlemleri 1 dk mumlarla değerlendir. Açık işlem yoksa istek yok."""
    out = {"open": 0, "closed": 0, "opened2": 0, "candles": 0, "candle_err": 0, "mark_used": 0,
           "deferred": 0, "sent": 0, "failed": 0, "errors": 0, "gate": "", "skipped": ""}
    if not getattr(cfg, "sim_enabled", True):
        out["skipped"] = "kapalı"
        return await _stats(out)
    out["gate"] = gate(cfg, notifier)
    out["source_gate"] = source_gate(cfg)
    async with _get_lock():
        acc = await account(cfg)
        opens = await open_trades()
        out["open"] = len(opens)
        ts = now()
        for t in opens:
            try:
                await _eval_one(cfg, client, notifier, t, ts, out)
            except Exception:
                out["errors"] += 1
                log.exception("sim değerlendirme hatası #%s %s", t.get("id"), t.get("coin"))
        acc = await account(cfg)
        out["open"] = len(await open_trades())
        out["balance"] = acc.get("balance")
        out["run"] = acc.get("run")
    return await _stats(out)


async def reset(cfg, notifier=None) -> dict:
    """Yeni tur: açıklar 'reset' ile (kv fiyatından, yoksa girişten) kapanır,
    run+1, bakiye başlangıca döner. Eski satırlar eski turda kalır."""
    async with _get_lock():
        acc = await account(cfg)
        ts = now()
        n = 0
        for t in await open_trades():
            m, mts = await _mark(None, t["coin"], ttl=MARK_MAX_AGE, fetch=False)
            px = m if (m and mts and ts - mts <= MARK_MAX_AGE) else float(t["entry_px"])
            await _close(cfg, t, "reset", px, ts, False,
                         "sıfırlama" + ("" if px != float(t["entry_px"]) or m else " (fiyat yok, giriş fiyatından)"))
            n += 1
        old = dict(acc)
        start = float(getattr(cfg, "sim_start_balance", 10000) or 0)
        acc = {"run": int(acc.get("run") or 1) + 1, "balance": start, "start_balance": start, "start_ts": ts}
        await kv_set(ACCOUNT_KV, acc)
        out: dict = {}
        if notifier is not None:
            from ..telegram import format as fmt
            await _notify(cfg, notifier, f"sim:reset:{acc['run']}", fmt.sim_reset(old, acc, n), out)
        await _bump(resets=1)
        log.info("sim sıfırlandı: tur #%s → #%s, %d açık işlem kapatıldı", old.get("run"), acc["run"], n)
        return {"run": acc["run"], "closed": n, "balance": start, "prev_balance": old.get("balance"),
                "sent": out.get("sent", 0), "gate": out.get("gate", "")}


# ---------------- sayfa / özet ----------------

def live_pnl(t: dict, mark, cfg) -> dict | None:
    """Açık işlem için anlık (çıkış ücreti düşülmüş) kâr/zarar; fiyat yoksa None."""
    if not mark:
        return None
    net, fee_total, pct = pnl_of(t, float(mark), getattr(cfg, "sim_fee_taker_pct", 0.045))
    up = t.get("side") == "long"
    tp, stop = float(t.get("tp_px") or 0), float(t.get("stop_px") or 0)
    m = float(mark)
    return {"usd": net, "pct": pct, "mark": m,
            "to_tp_pct": ((tp - m) / m * 100 * (1 if up else -1)) if tp else None,
            "to_stop_pct": ((m - stop) / m * 100 * (1 if up else -1)) if stop else None}


def status_of(t: dict, now_ts: int) -> str:
    if int(t.get("leg") or 1) == 2:
        return "ters bacak · geri çekilme bekleniyor"
    if t.get("liq_ts"):
        return f"liq geldi {max(0, now_ts - int(t['liq_ts'])) // 60} dk önce · iğne bekleniyor"
    return "balina liq'i bekleniyor"


async def page(cfg, run: str | None = None) -> dict:
    """/sim şablonunun verisi. run: None = güncel tur · 'all' · tur numarası."""
    acc = await account(cfg)
    cur_run = int(acc.get("run") or 1)
    run_f: int | None
    if run == "all":
        run_f = None
    elif run:
        try:
            run_f = int(run)
        except ValueError:
            run_f = cur_run
    else:
        run_f = cur_run
    opens = await open_trades()
    closed_all = await closed_trades(run_f, limit=None)
    closed = closed_all[:PAGE_CLOSED]
    skipped = await skipped_trades(run_f)
    ts = now()
    marks: dict = {}
    marks_age = None
    try:
        ctx = await main_dex_ctx(None, fetch=False)
        marks = {c: v.get("m") for c, v in (ctx.get("c") or {}).items()}
        if ctx.get("ts"):
            marks_age = max(0, ts - int(ctx["ts"]))
    except Exception:
        pass
    equity = float(acc.get("balance") or 0)
    for t in opens:
        t["live"] = live_pnl(t, marks.get(t["coin"]), cfg)
        t["status_txt"] = status_of(t, ts)
        t["age"] = ts - int(t.get("entry_ts") or ts)
        if t["live"]:
            equity += t["live"]["usd"]
    for c in closed:
        c["dur"] = max(0, int(c.get("exit_ts") or 0) - int(c.get("entry_ts") or 0))
    st = stats(closed_all if run_f is not None else closed_all, acc.get("start_balance"), acc.get("balance"))
    if run_f is None:
        # tüm turlar: bakiye yüzdesi anlamsız, toplam kâr/zarar yeter
        st["pnl_pct"] = None
    pts = curve([c for c in closed_all if run_f is None or int(c.get("run") or 1) == run_f],
                acc.get("start_balance"), acc.get("start_ts")) if closed_all else []
    svg = svg_curve(pts, float(acc.get("start_balance") or 0)) if len(pts) > 1 else ""
    kv_stats = await kv_get(STATS_KV) or {}
    return {"acc": acc, "run": run_f, "cur_run": cur_run, "opens": opens, "closed": closed,
            "closed_total": len(closed_all), "skipped": skipped, "stats": st, "equity": equity,
            "open_pnl": equity - float(acc.get("balance") or 0), "svg": svg, "marks_age": marks_age,
            "gate": gate(cfg, object() if getattr(cfg, "telegram_bot_token", "") else None)
            if False else ("SIM_CHAT_ID yok" if not (getattr(cfg, "sim_chat_id", "") or "").strip()
                           else ("bildirim kapalı (notify_sim)" if not getattr(cfg, "notify_sim", True) else "")),
            "source_gate": source_gate(cfg), "enabled": bool(getattr(cfg, "sim_enabled", True)),
            "kv": kv_stats, "ts": ts}


async def summary(cfg) -> dict:
    """Telegram /sim: sayfa verisinin küçük hali."""
    s = await page(cfg)
    s["closed"] = s["closed"][:5]
    return s


async def _stats(out: dict) -> dict:
    """Tur istatistiği kv'ye; kanca sayaçları (opened / skipped_signals / resets)
    tick'te sıfırlanmaz — `_bump` onları tur dışında artırır."""
    prev = await kv_get(STATS_KV) or {}
    st = {k: prev[k] for k in ("opened", "skipped_signals", "resets") if k in prev}
    st.update(out)
    st["ts"] = now()
    await kv_set(STATS_KV, st)
    return out


async def loop(cfg, client, notifier) -> None:
    """Denetimli döngü. Açık işlem yokken istek yapmaz; site buna bağımlı değil."""
    from ..health import beat
    await asyncio.sleep(180)               # açılışta metrik/kv otursun
    while True:
        try:
            await beat("sim")
            await tick(cfg, client, notifier)
            await beat("sim")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("sim turu hatası")
            await _stats({"error": f"{type(e).__name__}: {e}"[:200]})
        await asyncio.sleep(max(30, int(getattr(cfg, "sim_poll_sec", 60))))
