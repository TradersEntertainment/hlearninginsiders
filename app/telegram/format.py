"""Telegram mesaj şablonları (HTML)."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from ..db import now

TR = ZoneInfo("Europe/Istanbul")
ET = ZoneInfo("America/New_York")

DISCLAIMER = "\n<i>ℹ️ Gözlem aracıdır, yatırım tavsiyesi değildir.</i>"


def short(addr: str) -> str:
    return f"{addr[:6]}..{addr[-4:]}" if len(addr) > 12 else addr


def alink(addr: str) -> str:
    return f'<a href="https://hypurrscan.io/address/{addr}">{short(addr)}</a>'


def usd(n: float | None) -> str:
    if n is None:
        return "-"
    a = abs(n)
    if a >= 1_000_000:
        return f"${n / 1_000_000:.1f}M"
    if a >= 1_000:
        return f"${n / 1_000:.0f}K"
    return f"${n:.0f}"


def px(p: float | None) -> str:
    if not p:
        return "-"
    if p >= 1000:
        return f"{p:.0f}"
    if p >= 10:
        return f"{p:.2f}"
    return f"{p:.4f}"


def pct(p: float | None, signed: bool = True) -> str:
    if p is None:
        return "-"
    s = "+" if (signed and p > 0) else ""
    return f"{s}{p:.1f}%"


def age_str(opened_ts: int | None) -> str:
    if not opened_ts:
        return "?"
    h = (now() - opened_ts) / 3600
    if h < 1:
        return f"{h * 60:.0f}dk"
    if h < 48:
        return f"{h:.0f}h"
    return f"{h / 24:.0f}g"


def tr_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, TR).strftime("%H:%M")


def score_badge(score: int | None) -> str:
    s = score or 0
    if s >= 70:
        return f"🚨<b>{s}</b>"
    if s >= 50:
        return f"⚠️<b>{s}</b>"
    return f"<code>{s:>2}</code>"


def _flags(p: dict) -> str:
    f = []
    if p.get("fresh"):
        f.append("🆕TAZE")
    hits, misses = p.get("watch_record") or (0, 0)
    if hits:
        f.append(f"🎯sicil {hits}✓/{misses}✗")
    return " ".join(f)


def pos_line(i: int, p: dict) -> str:
    side = "🔴SHORT" if p["side"] == "short" else "🟢LONG"
    lev = f"{p['leverage']:.0f}x" if p.get("leverage") else "?"
    liq = f"liq {px(p['liq_px'])}" if p.get("liq_px") else "liq -"
    flags = _flags(p)
    return (f"{i}. {score_badge(p.get('score'))} {alink(p['address'])} {side} "
            f"<b>{usd(p['notional'])}</b> @{px(p['entry_px'])} {lev} {liq} "
            f"│ {age_str(p.get('opened_ts'))}" + (f" │ {flags}" if flags else ""))


def _ls_balance(rows: list[dict]) -> str:
    lo = sum(p["notional"] for p in rows if p["side"] == "long")
    sh = sum(p["notional"] for p in rows if p["side"] == "short")
    tot = lo + sh
    if not tot:
        return "-"
    return f"%{lo / tot * 100:.0f} long / %{sh / tot * 100:.0f} short ({usd(tot)})"


def _summary_block(summ: dict) -> str:
    oi_part = usd(summ.get("oi_ntl"))
    if summ.get("oi_change_pct") is not None:
        warn = " ⚠️" if abs(summ["oi_change_pct"]) >= 50 else ""
        oi_part += f" (24h {pct(summ['oi_change_pct'])}{warn})"
    fund = summ.get("funding")
    fund_part = f"{fund * 100:+.4f}%/h" if fund is not None else "-"
    if fund is not None and fund < 0:
        fund_part += " (shortlar ödüyor)"
    elif fund is not None and fund > 0:
        fund_part += " (longlar ödüyor)"
    line = f"📊 Mark <b>{px(summ.get('mark'))}</b>"
    if summ.get("px_change_pct") is not None:
        line += f" ({pct(summ['px_change_pct'])} 24h)"
    line += f" │ OI {oi_part} │ Funding {fund_part}"
    if summ.get("day_volume"):
        line += f" │ Vol {usd(summ['day_volume'])}"
    return line


def _reasons_block(rows: list[dict], limit: int = 3) -> str:
    lines = []
    for p in rows:
        if (p.get("score") or 0) < 50:
            continue
        try:
            reasons = json.loads(p.get("score_reasons") or "[]")
        except json.JSONDecodeError:
            reasons = []
        if reasons:
            lines.append(f"  └ {short(p['address'])}: {', '.join(reasons)}")
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def earnings_report(event: dict, stage: str, summ: dict, rows: list[dict], cfg) -> str:
    sym = event["symbol"]
    hint = {"amc": "kapanış sonrası", "bmo": "açılış öncesi"}.get(
        event.get("hour_hint") or "", "saat belirsiz")
    stage_txt = "⏰ ~1 saat kaldı" if stage == "t1" else "🕐 erken pencere"
    head = (f"🎯 <b>{sym}</b> earnings — {stage_txt}\n"
            f"📅 {event['date_et']} ({hint})"
            + (f" │ EPS beklentisi {event['eps_est']}" if event.get("eps_est") else ""))
    if event.get("note"):
        head += f"\n⚠️ {event['note']}"
    parts = [head, _summary_block(summ)]
    if rows:
        parts.append(f"⚖️ Taranan havuzda: {_ls_balance(rows)}")
        parts.append("\n🐋 <b>En büyük pozisyonlar:</b>")
        parts.append("\n".join(pos_line(i + 1, p) for i, p in enumerate(rows[:10])))
        reasons = _reasons_block(rows)
        if reasons:
            parts.append("\n🕵️ <b>Şüphe nedenleri:</b>\n" + reasons)
    else:
        parts.append("🐋 Havuzda açık pozisyon bulunamadı (havuz henüz dar olabilir).")
    parts.append(DISCLAIMER)
    return "\n".join(parts)


def whale_fill_alert(coin: str, addr: str, side: str, price: float,
                     notional: float, is_watch: bool, record: tuple) -> str:
    sym = coin.split(":")[-1]
    act = "🟢 ALIŞ" if side == "buy" else "🔴 SATIŞ"
    head = "🚨 <b>SİCİLLİ BALİNA İŞLEMDE</b>" if is_watch else "🐋 <b>BÜYÜK İŞLEM</b>"
    lines = [f"{head} — {sym}",
             f"{act} <b>{usd(notional)}</b> @ {px(price)}",
             f"👤 {alink(addr)}"]
    hits, misses = record
    if hits or misses:
        lines.append(f"🎯 Sicil: {hits} doğru / {misses} yanlış")
    return "\n".join(lines)


def eval_report(event: dict, move_pct: float | None, results: list[dict],
                closed: list[str], promoted: list[str], cfg) -> str:
    sym = event["symbol"]
    if move_pct is None:
        return (f"📊 <b>{sym}</b> earnings sonucu değerlendirilemedi "
                f"(fiyat verisi eksik).{DISCLAIMER}")
    arrow = "📈" if move_pct > 0 else "📉"
    lines = [f"{arrow} <b>{sym}</b> earnings sonucu: fiyat {pct(move_pct)}"]
    if abs(move_pct) < cfg.eval_move_threshold:
        lines.append("Hareket eşiğin altında — sicile işlenmedi.")
    else:
        right = [r for r in results if r["hit"]]
        wrong = [r for r in results if not r["hit"]]
        if right:
            lines.append("\n✅ <b>Doğru bilenler:</b>")
            for r in right[:8]:
                side = "SHORT" if r["side"] == "short" else "LONG"
                lines.append(f"  {alink(r['address'])} {side} {usd(r['notional'])}")
        if wrong:
            lines.append(f"❌ Yanlış: {len(wrong)} adres")
        if promoted:
            lines.append("\n⭐ <b>Watchlist'e eklendi</b> (2+ doğru): "
                         + ", ".join(alink(a) for a in promoted))
    if closed:
        lines.append(f"🚪 Pozisyonunu kapatanlar: {', '.join(short(a) for a in closed[:10])}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def upcoming_list(events: list[dict]) -> str:
    if not events:
        return "📅 Önümüzdeki 14 günde HL'de listeli hisse earnings'i yok (ya da takvim henüz çekilmedi)."
    lines = ["📅 <b>Yaklaşan earnings (HL'de listeli):</b>"]
    for e in events[:25]:
        hint = {"amc": "AMC", "bmo": "BMO"}.get(e.get("hour_hint") or "", "?")
        flags = []
        if e.get("alerted_t1"):
            flags.append("✅raporlandı")
        lines.append(f"  {e['date_et']} <b>{e['symbol']}</b> ({hint})"
                     + (f" {' '.join(flags)}" if flags else ""))
    return "\n".join(lines)


def status_text(state: dict) -> str:
    lines = ["🤖 <b>Durum</b>"]
    for k, v in state.items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


def help_text() -> str:
    return (
        "🕵️ <b>HL Insider Radar</b>\n"
        "Earnings öncesi Hyperliquid hisse perp'lerindeki balina pozisyonlarını izler.\n\n"
        "Komutlar:\n"
        "/scan SNDK — coini şimdi tara, en büyük pozları göster\n"
        "/upcoming — yaklaşan HL-eşleşen earnings'ler\n"
        "/whale 0x… — adres karnesi + açık pozisyonları\n"
        "/watch 0x… — adresi watchlist'e ekle\n"
        "/unwatch 0x… — watchlist'ten çıkar\n"
        "/watchlist — sicilli adresler\n"
        "/status — bot durumu\n"
        "/id — bu sohbetin chat id'si"
    )
