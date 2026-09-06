"""Zincir simülasyonu — "bu pozisyon patlarsa fiyat nereye gider, arada kim patlar?"

Likidasyon = zorunlu piyasa emri: short patlarsa zorunlu ALIŞ ask'leri yer
(fiyat yukarı), long patlarsa zorunlu SATIŞ bid'leri yer (aşağı). Emir liq
fiyatından İTİBAREN yürür (oraya kadarki defter, fiyatı oraya getiren
hareketle zaten yenmiş sayılır). Vardığı fiyata kadar liq'i olan aynı yönlü
pozisyonlar da patlar → onların emri de deftere iner → yeni fiyat → … Yeni
tetiklenen kalmayınca ya da görünen defter bitince durur.

DÜRÜSTLÜK: defter anlık ve GÖRÜNEN kadar (l2Book 20 seviye; genişlik için
nSigFigs=3 toplulaştırma), havuz HL'nin tamamı değil. Sonuç bir ALT SINIR:
"en az buraya"; defter bitince kalan $ yerleşmemiş kalır ve bu yazılır.
SAF: ağ yok, DB yok.
"""
UP, DOWN = "up", "down"
MAX_STEPS = 8


def walk(levels: list[dict], start_px: float, usd: float, direction: str) -> dict:
    """Zorunlu emri defterde yürüt; `levels` YERİNDE eksilir (zincirin sonraki
    adımı aynı seviyeyi ikinci kez yiyemesin). up: ask'ler (px ≥ start, artan),
    down: bid'ler (px ≤ start, azalan). Kısmi seviyede o seviyenin fiyatında
    durur. Dönüş {reached_px, spent, pending, exhausted, depth, n_levels}."""
    start_px = float(start_px)
    if direction == UP:
        lv = sorted((l for l in levels if l["px"] >= start_px), key=lambda l: l["px"])
    else:
        lv = sorted((l for l in levels if l["px"] <= start_px), key=lambda l: -l["px"])
    depth = sum(l["px"] * l["sz"] for l in lv if l["sz"] > 0)
    left, spent, reached = float(usd), 0.0, start_px
    for l in lv:
        cap = l["px"] * l["sz"]
        if cap <= 0:
            continue
        take = min(cap, left)
        l["sz"] -= take / l["px"]
        spent += take
        left -= take
        reached = l["px"]
        if left <= 1e-9:
            return {"reached_px": reached, "spent": spent, "pending": 0.0,
                    "exhausted": False, "depth": depth, "n_levels": len(lv)}
    return {"reached_px": reached, "spent": spent, "pending": max(0.0, left),
            "exhausted": True, "depth": depth, "n_levels": len(lv)}


def simulate(bids: list[dict], asks: list[dict], trigger: dict, positions: list[dict],
             mark: float | None, max_steps: int = MAX_STEPS) -> dict:
    """Zincir. `trigger`: {side, liq_px, notional, address}; `positions`: coinin
    açık pozisyonları (her boyut; ters yön ve trigger'ın kendisi yok sayılır).
    Dönüş: {direction, trigger_usd, start_px, end_px, move_pct (mark'a göre),
    total_usd, n_pos, steps[{from,to,spent,n_new,usd_new}], exhausted,
    pending_usd, book_usd, no_book}."""
    side = trigger.get("side")
    direction = UP if side == "short" else DOWN
    levels = [{"px": float(l["px"]), "sz": float(l["sz"])}
              for l in (asks if direction == UP else bids)]
    start = float(trigger["liq_px"])
    pool = [p for p in positions
            if p.get("side") == side and p.get("liq_px")
            and (p.get("address") or "") != (trigger.get("address") or "")]
    used: set[int] = set()
    cur, pending = start, float(trigger.get("notional") or 0)
    total, n_pos = pending, 1
    steps: list[dict] = []
    exhausted, left, depth = False, 0.0, 0.0
    for i in range(max_steps):
        w = walk(levels, cur, pending, direction)
        if i == 0:
            depth = w["depth"]
            if w["n_levels"] == 0:
                return {"direction": direction, "trigger_usd": float(trigger.get("notional") or 0),
                        "start_px": start, "end_px": start, "move_pct": None, "total_usd": total,
                        "n_pos": 1, "steps": [], "exhausted": True, "pending_usd": pending,
                        "book_usd": 0.0, "no_book": True}
        reached = w["reached_px"]
        new = []
        for j, p in enumerate(pool):
            if j in used:
                continue
            liq = float(p["liq_px"])
            lo_ok = (liq >= cur) if i == 0 else (liq > cur)       # ilk adımda aynı fiyat da patlar
            hit = (lo_ok and liq <= reached) if direction == UP else \
                  ((liq <= cur if i == 0 else liq < cur) and liq >= reached)
            if hit:
                used.add(j)
                new.append(p)
        usd_new = sum(float(p.get("notional") or 0) for p in new)
        steps.append({"from": cur, "to": reached, "spent": w["spent"],
                      "n_new": len(new), "usd_new": usd_new})
        if w["exhausted"]:
            exhausted, left = True, w["pending"]
            break
        if not new:
            break
        pending, cur = usd_new, reached
        total += usd_new
        n_pos += len(new)
    end = steps[-1]["to"] if steps else start
    return {"direction": direction, "trigger_usd": float(trigger.get("notional") or 0),
            "start_px": start, "end_px": end,
            "move_pct": (end / float(mark) - 1) * 100 if mark else None,
            "total_usd": total, "n_pos": n_pos, "steps": steps, "exhausted": exhausted,
            "pending_usd": left, "book_usd": depth, "no_book": False}


def describe(c: dict | None, mark: float | None = None) -> list[str]:
    """Mesaj satırları (HTML). Boş zincir → []."""
    if not c:
        return []
    from ..telegram.format import px, usd
    up = c["direction"] == UP
    who = "short" if up else "long"
    act = "alış" if up else "satış"
    head = (f"💣 <b>Zincir</b> (defter anlık) · {usd(c['trigger_usd'])} {who} "
            f"{px(c['start_px'])}'te patlarsa zorunlu {act}")
    if c.get("no_book"):
        return [head + " — görünen defter liq fiyatına kadar uzanmıyor, derinlik bilinmiyor"]
    st = c["steps"]
    parts = [head + f" → <b>{px(st[0]['to'])}</b>"]
    for prev, s in zip(st, st[1:]):
        parts.append(f"arada {prev['n_new']} {who} daha ({usd(prev['usd_new'])}) → <b>{px(s['to'])}</b>")
    if len(st) == 1 and not st[0]["n_new"]:
        parts.append("arada başka liq yok")
    elif len(st) == 1 and st[0]["n_new"]:
        parts.append(f"arada {st[0]['n_new']} {who} daha ({usd(st[0]['usd_new'])}) — defter bitti")
    parts.append(f"toplam <b>{usd(c['total_usd'])}</b>")
    move = f" (şimdiden {c['move_pct']:+.1f}%)" if c.get("move_pct") is not None else ""
    parts.append(f"fiyat kaçınılmaz ~<b>{px(c['end_px'])}</b>'a gidebilir{move}")
    out = [" · ".join(parts)]
    if c.get("exhausted"):
        out.append(f"<i>görünen defter {px(c['end_px'])}'da bitiyor ({usd(c['book_usd'])} derinlik,"
                   f" {usd(c['pending_usd'])} yerleşmedi) — ötesi bilinmiyor, gerçek hareket"
                   f" daha büyük olabilir</i>")
    return out
