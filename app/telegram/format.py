"""Telegram mesaj şablonları (HTML)."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from ..db import now
from ..propr import PROPR_NOTE, is_listed

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
    if p.get("entity"):
        f.append({"mm": "🤖MM", "vault": "🏦VAULT"}.get(p["entity"], "🚫"))
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
    timeline = f"aç:{age_str(p.get('opened_ts'))}"
    if p.get("last_add_ts"):
        timeline += f" ➕{age_str(p['last_add_ts'])}"
    if p.get("last_trim_ts"):
        timeline += f" ✂{age_str(p['last_trim_ts'])}"
    return (f"{i}. {score_badge(p.get('score'))} {alink(p['address'])} {side} "
            f"<b>{usd(p['notional'])}</b> @{px(p['entry_px'])} {lev} {liq} "
            f"│ {timeline}" + (f" │ {flags}" if flags else ""))


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


def clusters_block(cluster_list: list[dict]) -> str:
    lines = []
    for c in cluster_list[:4]:
        side = {"long": "LONG", "short": "SHORT"}.get(c["side"], "KARIŞIK")
        addrs = "+".join(short(a) for a in c["addrs"][:4])
        lines.append(f"  {addrs} → {side} toplam <b>{usd(c['total'])}</b> (bölünmüş pozisyon olabilir)")
    return "\n".join(lines)


def peers_block(peer_list: list[dict]) -> str:
    lines = []
    for p in peer_list:
        s = p.get("summ") or {}
        line = f"  <b>{p['symbol']}</b>: OI {usd(s.get('oi_ntl'))}"
        if s.get("oi_change_pct") is not None:
            line += f" (24h {pct(s['oi_change_pct'])})"
        if s.get("funding") is not None:
            line += f", fund {s['funding'] * 100:+.4f}%/h"
        top = p.get("top")
        if top:
            side = "SHORT" if top["side"] == "short" else "LONG"
            line += f" │ en büyük: {side} {usd(top['notional'])}"
        lines.append(line)
    return "\n".join(lines)


def new_big_position_alert(coin: str, p: dict, event: dict | None) -> str:
    sym = coin.split(":")[-1]
    side = "🔴SHORT" if p["side"] == "short" else "🟢LONG"
    lev = f"{p['leverage']:.0f}x" if p.get("leverage") else "?"
    lines = [f"🆕🐋 <b>YENİ BÜYÜK POZİSYON — {sym}</b>",
             f"{side} <b>{usd(p['notional'])}</b> @{px(p['entry_px'])} {lev}"
             f" │ {age_str(p.get('opened_ts'))} önce açıldı",
             f"👤 {alink(p['address'])} │ skor {p.get('score') or 0}"]
    try:
        reasons = json.loads(p.get("score_reasons") or "[]")
    except json.JSONDecodeError:
        reasons = []
    if reasons:
        lines.append("  └ " + ", ".join(reasons))
    if event:
        hint = {"amc": "AMC", "bmo": "BMO"}.get(event.get("hour_hint") or "", "?")
        lines.append(f"📅 Dikkat: {event['date_et']} ({hint}) earnings var!")
    else:
        lines.append("ℹ️ Yaklaşan earnings yok — başka bir şey mi biliyor? 🤔")
    if is_listed(sym):
        lines.append(PROPR_NOTE)
    lines.append(DISCLAIMER)
    return "\n".join(lines)


LIQ_STAGE_HEAD = {
    1: "⚠️ <b>LİKİDASYON RADARI</b> — %1 bölgesine girdi",
    2: "🔶 <b>LİKİDASYON YAKLAŞIYOR</b> — %0.5 kaldı",
    3: "🚨 <b>SON UYARI</b> — likidasyona %0.1!",
}


def liq_alert(coin: str, addr: str, p: dict, mark: float, dist: float, stage: int) -> str:
    sym = coin.split(":")[-1]
    side = "🔴SHORT" if p["side"] == "short" else "🟢LONG"
    etki = ("likide olursa ~{} zorunlu <b>ALIŞ</b> → fiyatı yukarı süpürebilir"
            if p["side"] == "short" else
            "likide olursa ~{} zorunlu <b>SATIŞ</b> → fiyatı aşağı süpürebilir").format(usd(p["notional"]))
    lines = [
        f"{LIQ_STAGE_HEAD.get(stage, '')} — <b>{sym}</b>",
        f"{side} <b>{usd(p['notional'])}</b> @ {px(p.get('entry_px'))}"
        f" │ liq <b>{px(p['liq_px'])}</b> │ şimdi {px(mark)} (mesafe %{dist:.2f})",
        f"👤 {alink(addr)}",
        f"💥 {etki}",
    ]
    if is_listed(sym):
        lines.append(PROPR_NOTE)
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def liq_cluster_alert(c: dict) -> str:
    """Likidasyon duvarı: fiyata yakın bölgede birikmiş toplu liq yığını."""
    sym = c["symbol"]
    is_short = c["side"] == "short"
    yon = "🔵 SHORT" if is_short else "🟠 LONG"
    nerede = "ÜSTÜNDE" if is_short else "ALTINDA"
    etki = ("fiyat o bölgeye çekilirse zorunlu <b>ALIŞ</b>lar fiyatı yukarı süpürebilir"
            if is_short else
            "fiyat o bölgeye inerse zorunlu <b>SATIŞ</b>lar fiyatı aşağı süpürebilir")
    lo = min(m["liq_px"] for m in c["members"])
    hi = max(m["liq_px"] for m in c["members"])
    top = c["members"][0]
    lines = [
        f"🧲 <b>LİKİDASYON DUVARI — {sym}</b>",
        f"Fiyatın {nerede}: {yon} liq toplam <b>{usd(c['total'])}</b>"
        f" · {c['count']} pozisyon · ort. mesafe %{c['avg_dist']:.1f}",
        f"Bölge: <b>{px(lo)} – {px(hi)}</b> (şimdi {px(c['mark'])})",
        f"En büyüğü: {usd(top['notional'])} liq {px(top['liq_px'])}"
        f" (%{abs(top['dist']):.1f}) {alink(top['address'])}",
        f"💥 {etki}",
    ]
    if c.get("other_total"):
        lines.append(f"⚖️ Karşı yönde de {usd(c['other_total'])} liq var — iki yönlü sıkışma")
    if is_listed(sym):
        lines.append(PROPR_NOTE)
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def liq_closed(coin: str, addr: str, row: dict) -> str:
    sym = coin.split(":")[-1]
    side = "SHORT" if row.get("side") == "short" else "LONG"
    return "\n".join([
        f"🏁 <b>İzlenen dev pozisyon kapandı</b> — {sym}",
        f"{side} {usd(row.get('notional'))} (son mesafe %{(row.get('last_dist') or 0):.2f})"
        " — likidasyon ya da kapatma",
        f"👤 {alink(addr)}",
    ])


def anomaly_alert(symbol: str, coin: str, triggers: list[str], event: dict | None) -> str:
    lines = [f"📡 <b>ANOMALİ — {symbol}</b>"]
    if event:
        hint = {"amc": "AMC", "bmo": "BMO"}.get(event.get("hour_hint") or "", "?")
        lines.append(f"📅 Earnings yaklaşıyor: <b>{event['date_et']}</b> ({hint}) — birileri biliyor olabilir")
    for t in triggers:
        lines.append(f"  ⚠️ {t}")
    lines.append(f"🔍 Detay için: /scan {symbol}")
    if is_listed(symbol):
        lines.append(PROPR_NOTE)
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def earnings_report(event: dict, stage: str, summ: dict, rows: list[dict], cfg,
                    cluster_list: list[dict] | None = None,
                    peer_list: list[dict] | None = None) -> str:
    from ..earnings.calendar import annotate  # döngüsel importu kır

    sym = event["symbol"]
    ann = annotate([dict(event)])[0]
    stage_txt = "⏰ ~1 saat kaldı" if stage == "t1" else "🕐 erken pencere"
    head = (f"{ann['icon']} <b>{sym}</b> earnings — {stage_txt}\n"
            f"📅 {event['date_et']} · <b>{ann['tsi']} TSİ</b> ({ann['when_txt']}"
            + ("" if ann["exact"] else ", yaklaşık") + ")"
            + (f" │ EPS beklentisi {event['eps_est']}" if event.get("eps_est") else ""))
    if event.get("note"):
        head += f"\n⚠️ {event['note']}"
    if is_listed(sym):
        head += f"\n{PROPR_NOTE}"
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
    if cluster_list:
        parts.append("\n🧩 <b>Bağlantılı cüzdanlar:</b>\n" + clusters_block(cluster_list))
    if peer_list:
        parts.append("\n🧲 <b>Korele hisseler:</b>\n" + peers_block(peer_list))
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
    if is_listed(sym):
        lines.append(PROPR_NOTE)
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
    from ..earnings.calendar import annotate

    if not events:
        return "📅 Önümüzdeki 14 günde HL'de listeli hisse earnings'i yok (ya da takvim henüz çekilmedi)."
    evs = annotate([dict(e) for e in events])
    evs.sort(key=lambda e: (e["passed"], e["report_ts"]))
    lines = ["📅 <b>Yaklaşan earnings (HL'de listeli):</b>",
             "<i>☀️ sabah açılış öncesi · 🌙 akşam kapanış sonrası · saatler TSİ</i>"]
    warned = False
    for e in evs[:25]:
        tail = "❗geçti" if e["passed"] else (
            "✅raporlandı" if e.get("alerted_t1") else f"⏳{e['countdown']}")
        risk = ""
        if e.get("maybe_passed"):
            risk = " ⚠️<b>SABAH AÇIKLANMIŞ OLABİLİR</b>"
            warned = True
        lines.append(f"  {e['icon']} <b>{e['symbol']}</b> {e['tsi']}"
                     + ("" if e["exact"] else "~") + f" · {tail}{risk}"
                     + (" 📝" if e.get("note") else ""))
    if warned:
        lines.append("\n⚠️ <i>Saati zayıf kaynaktan gelen bilançolar sabah açıklanmış olabilir."
                     " İşlem açmadan önce doğrula, doğrusunu"
                     " <code>/settime SEMBOL bmo</code> ile sabitle.</i>")
    return "\n".join(lines)


def history_list(rows: list[dict]) -> str:
    if not rows:
        return "🗂 Arşiv henüz boş — ilk bilanço değerlendirmesinden sonra dolar."
    lines = ["🗂 <b>Geçmiş bilançolar</b> (öncesinde en büyük poz kimdi, haklı mıydı):"]
    for r in rows[:12]:
        icon = {"amc": "🌙", "bmo": "☀️"}.get(r.get("hour_hint") or "", "❓")
        mv = f"{r['move_pct']:+.1f}%" if r.get("move_pct") is not None else "?"
        lines.append(f"\n{icon} <b>{r['symbol']}</b> {r['date_et']} → fiyat <b>{mv}</b>")
        if r.get("result_note"):
            lines.append(f"  └ {r['result_note']}")
    return "\n".join(lines)


def winners_list(rows: list[dict]) -> str:
    if not rows:
        return ("🏆 Henüz sicilli adres yok — bot her earnings sonrası kim doğru bildi diye"
                " işler, ilk sonuçlardan sonra burası dolar.")
    lines = ["🏆 <b>En iyi biliciler</b> (bilanço yönünü doğru tahmin sicili):"]
    for r in rows[:15]:
        tot = (r.get("hits") or 0) + (r.get("misses") or 0)
        rate = (r["hits"] / tot * 100) if tot else 0
        star = " ⭐" if r.get("watchlist") else ""
        lines.append(f"  {alink(r['address'])} — <b>{r.get('hits') or 0}</b>✓ /"
                     f" {r.get('misses') or 0}✗ (%{rate:.0f}){star}")
    lines.append("\n<i>⭐ = watchlist: yeni poz açtığı anda bildirim gelir.</i>")
    return "\n".join(lines)


DIGEST_LABELS = {
    "whale_fill": "🐋 büyük işlem", "new_big": "🆕 yeni büyük pozisyon",
    "anomaly": "📡 anomali", "liq": "💥 likidasyon", "liqmap": "🧲 liq duvarı",
    "earnings": "📊 earnings",
    "eval": "🏁 sonuç",
}


def digest(pending: list[dict], events: list[dict], top: list[dict]) -> str:
    lines = ["🌅 <b>GÜNAYDIN — gece raporu</b>"]

    if pending:
        counts: dict[str, int] = {}
        for p in pending:
            k = (p.get("kind") or "").split(":", 1)[-1]
            counts[k] = counts.get(k, 0) + 1
        lines.append("\n😴 <b>Sessiz saatte biriken bildirimler:</b>")
        for k, n in sorted(counts.items(), key=lambda x: -x[1]):
            lines.append(f"  {DIGEST_LABELS.get(k, k)} × {n}")
        lines.append("\n<i>En önemli 3'ü:</i>")
        for p in pending[-3:]:
            first = (p.get("payload") or "").split("\n")[0]
            lines.append(f"  • {first}")
    else:
        lines.append("\n😴 Gece sessizdi — bekleyen bildirim yok.")

    if top:
        lines.append("\n🚨 <b>Şu an en şüpheli pozisyonlar:</b>")
        for p in top:
            side = "SHORT" if p["side"] == "short" else "LONG"
            lines.append(f"  [{p.get('score') or 0}] <b>{p['symbol']}</b> {side}"
                         f" {usd(p['notional'])} · {age_str(p.get('opened_ts'))}")

    live = [e for e in events if not e.get("passed")]
    if live:
        lines.append("\n📅 <b>Bugün/yarın bilanço:</b>")
        for e in live[:6]:
            lines.append(f"  {e['icon']} <b>{e['symbol']}</b> {e['tsi']} TSİ"
                         + ("" if e.get("exact") else "~")
                         + ("  ⚠️sabah olabilir" if e.get("maybe_passed") else ""))

    lines.append(DISCLAIMER)
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
        "/refresh — takvimi ŞİMDİ tüm kaynaklardan yenile (eksik earnings görürsen bas)\n"
        "/settime SHAZ bmo — bilanço saatini elle düzelt (bmo/amc/16:30/09:30et)\n"
        "/whale 0x… — adres karnesi + açık pozisyonları\n"
        "/watch 0x… — adresi watchlist'e ekle\n"
        "/unwatch 0x… — watchlist'ten çıkar\n"
        "/ignore 0x… — adresi ele (MM/vault gibi davran, alert üretme)\n"
        "/unignore 0x… — elemeyi kaldır\n"
        "/watchlist — sicilli adresler\n"
        "/gecmis — geçmiş bilanço arşivi (kim ne pozisyondaydı, kim haklı çıktı)\n"
        "/winners — en iyi biliciler (doğru tahmin sicili)\n"
        "/bildirimler — bildirim ayarları + son gönderilenler\n"
        "/status — bot durumu\n"
        "/id — bu sohbetin chat id'si"
    )
