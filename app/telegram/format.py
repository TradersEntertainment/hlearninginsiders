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
    if stage == "ondemand":
        # Elle /scan ya da dashboard'dan anlık tarama — earnings zamanı yok
        head = f"🔍 <b>{sym}</b> — anlık tarama"
    else:
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
    hits, misses = record
    if is_watch:
        head = f"🎯 <b>SİCİLLİ BALİNA {sym}'E DÖNDÜ</b>"
        note = f"Bu adres {sym}'i daha önce doğru bildi — şimdi tekrar poz açtı"
    else:
        head = "🐋 <b>BÜYÜK İŞLEM</b>"
        note = None
    lines = [f"{head} — {sym}"]
    if note:
        lines.append(note)
    lines += [f"{act} pozisyon <b>{usd(notional)}</b> @ {px(price)}",
              f"👤 {alink(addr)}"]
    if hits or misses:
        lines.append(f"🎯 Sicil: {hits} doğru / {misses} yanlış")
    if is_listed(sym):
        lines.append(PROPR_NOTE)
    return "\n".join(lines)


def _side_badge(side: str) -> str:
    return "🔴SHORT" if side == "short" else "🟢LONG"


TASK_TR = {
    "universe": "evren keşfi", "calendar": "takvim", "metrics": "metrik toplayıcı",
    "due": "earnings zamanlayıcı", "anomaly": "anomali dedektörü",
    "autoscan": "oto-tarayıcı", "liqwatch": "likidasyon radarı",
    "tracker": "pozisyon takibi", "lowvol": "sessiz su radarı",
    "bookwall": "duvar radarı", "sweeper": "derin keşif",
    "hourstats": "saat istatistiği",
    "digest": "günlük özet", "collector": "canlı işlem akışı (WS)",
    "telegram": "telegram botu", "watchdog": "bekçi",
}


def _task(name: str) -> str:
    return TASK_TR.get(name, name)


def crash_alert(name: str, err, count: int = 1) -> str:
    return (f"⚕️🔥 <b>GÖREV ÇÖKTÜ</b> — {_task(name)}\n"
            f"<code>{str(err)[:200]}</code>\n"
            f"Otomatik yeniden başlatıldı (toplam {count}. çökme).\n"
            "<i>Bu mesajı Claude'a yapıştırırsan kalıcı düzeltme yapılır.</i>")


def health_down(name: str, silent_min: int, restarted: bool) -> str:
    tail = "yeniden başlattım, toparlanmasını izliyorum" if restarted \
        else "yeniden başlatamadım — logları kontrol et"
    return (f"⚕️ <b>SAĞLIK</b> — {_task(name)} <b>{silent_min} dk</b>dır sessizdi"
            f" → {tail}.")


def health_still_down(name: str, mins: int) -> str:
    return (f"⚕️🚨 <b>DÜZELMEDİ</b> — {_task(name)} {mins} dakikadır çalışmıyor."
            " Restart işe yaramadı; Railway loglarına bakmak gerekebilir.")


def health_up(name: str, mins: int) -> str:
    return f"✅ {_task(name)} kendine geldi ({mins} dk kesinti)."


def health_bulk(names: list[str]) -> str:
    lst = ", ".join(_task(n) for n in names)
    return (f"🌐 <b>GENİŞ KESİNTİ</b> — {len(names)} görev aynı anda sessizleşti:"
            f" {lst}.\nBüyük ihtimalle Hyperliquid API'ye erişim sorunu —"
            " görevleri yeniden başlattım, düzelince tek tek haber veririm.")


def health_report(snap: dict) -> str:
    checks, crashes = snap["checks"], snap["crashes"]
    n_ok = sum(1 for c in checks.values() if c["ok"])
    lines = [f"⚕️ <b>Sistem sağlığı</b> — {n_ok}/{len(checks)} görev ✅"]
    for name, c in sorted(checks.items(), key=lambda x: (x[1]["ok"], x[0])):
        mark = "✅" if c["ok"] else "⚠️"
        ago = f"{c['silent'] // 60}dk önce" if c["hb"] else "henüz atmadı"
        lines.append(f"  {mark} {_task(name)} — son atım {ago}")
    if crashes:
        lines.append("\n🔥 <b>Çökme geçmişi:</b>")
        for name, r in sorted(crashes.items(), key=lambda x: -(x[1].get("ts") or 0))[:5]:
            lines.append(f"  {_task(name)} ×{r.get('count', 1)} — son: "
                         f"<code>{(r.get('err') or '')[:90]}</code>")
    lines.append("\n<i>Bekçi 2 dakikada bir kontrol eder; takılan görevi kendisi"
                 " yeniden başlatır ve sana haber verir.</i>")
    return "\n".join(lines)


def wall_alert(w: dict, day_volume: float | None) -> str:
    """Emir defterine konan dev bekleyen emir duvarı (SPCX $202M tarzı)."""
    sym = w.get("symbol") or (w.get("coin") or "").split(":")[-1]
    if w["side"] == "ask":
        where = "fiyatın hemen <b>ÜSTÜNDE</b> SATIŞ duvarı (short/satmak isteyen)"
    else:
        where = "fiyatın hemen <b>ALTINDA</b> ALIŞ duvarı (long/almak isteyen)"
    lines = [f"🧱 <b>DEV EMİR DUVARI</b> — {sym}",
             where,
             f"<b>{usd(w['notional'])}</b> · {px(w['px_lo'])}–{px(w['px_hi'])} aralığı"
             f" (fiyata %{w['dist_pct']:.1f})"]
    facts = []
    opp = w.get("opp_notional") or 0
    if opp > 0 and w["notional"] / opp >= 2:
        r = w["notional"] / opp
        facts.append(f"karşı taraf derinliğinin <b>{'50+' if r > 50 else f'{r:.1f}'} katı</b>")
    if day_volume:
        facts.append(f"24h hacmin %{w['notional'] / day_volume * 100:.0f}'i kadar")
    if facts:
        lines.append("📊 " + " · ".join(facts))
    addr = w.get("address")
    if addr:
        lines.append(f'👤 Sahibi (emirler eşleşti): <a href="https://hypurrscan.io/address/'
                     f'{addr}#orders">{short(addr)}</a>')
    else:
        lines.append("👤 Sahibi bilinmiyor — defter anonim, tanıdığımız balinalarla eşleşmedi")
    lines.append("<i>Duvar çekilirse (spoof) ya da dolarsa ayrıca haber veririm.</i>")
    if is_listed(sym):
        lines.append(PROPR_NOTE)
    return "\n".join(lines)


def wall_gone(w: dict) -> str:
    sym = w.get("symbol") or (w.get("coin") or "").split(":")[-1]
    side_txt = "satış" if w["side"] == "ask" else "alış"
    life = age_str(w.get("first_ts"))
    return (f"🧱❌ <b>Duvar kalktı</b> — {sym}\n"
            f"{life} önce beliren {usd(w.get('peak_notional'))} {side_txt} duvarı artık"
            " defterde yok.\n"
            "<i>Ya çekildi (spoof/fikir değişimi) ya da doldu — fiyat artık o yönde"
            " daha rahat hareket edebilir.</i>")


def lowvol_alert(p: dict) -> str:
    """Sessiz su devi: düşük hacimli hissede absürt boyutlu yeni pozisyon."""
    sym = p.get("symbol") or p["coin"].split(":")[-1]
    lines = [f"🐘 <b>SESSİZ SUDA DEV POZİSYON</b> — {sym}",
             "Düşük hacimli hissede absürt boyutlu YENİ pozisyon:",
             f"{_side_badge(p['side'])} <b>{usd(p['notional'])}</b>"
             f" @ {px(p.get('entry_px'))} · açılış {age_str(p.get('opened_ts'))} önce",
             f"👤 {alink(p['address'])}"]
    facts = []
    if p.get("oi_share") is not None:
        facts.append(f"OI payı <b>%{p['oi_share']:.0f}</b>")
    if p.get("day_volume") is not None:
        facts.append(f"günlük hacim {usd(p['day_volume'])}")
    if p.get("vol_ratio"):
        r = p["vol_ratio"]
        facts.append(f"poz ≈ hacmin <b>{'50+' if r > 50 else f'{r:.1f}'} katı</b>")
    if facts:
        lines.append("📊 " + " · ".join(facts))
    if p.get("score"):
        lines.append(f"🎯 Şüphe skoru: {score_badge(p['score'])}")
    hits, misses = p.get("hits") or 0, p.get("misses") or 0
    if hits or misses:
        lines.append(f"🎯 Sicil: {hits} doğru / {misses} yanlış")
    lines.append("<i>Bu boyut bu hacimde kolay kapanmaz — sahibi uzun süre haklı"
                 " çıkacağından emin görünüyor.</i>")
    if is_listed(sym):
        lines.append(PROPR_NOTE)
    return "\n".join(lines)


def track_offer(symbol: str, offers: list[dict], already_closed: list[dict], cfg) -> str:
    """Earnings geçti — "çıkışı takip edelim mi?" teklifi (tıklanabilir /takip_N)."""
    lines = [f"👣 <b>{symbol} earnings geçti — balina çıkışını takip edelim mi?</b>",
             f"Komuta bas, takip başlasın. Balina toplam pozunun <b>%{pct_num(cfg.track_step_pct)}</b>"
             " kadarını her kapattığında haber veririm (her işlemi değil — spam yok)."
             " Tam kapanış + yön değişimi ANINDA gelir.", ""]
    for i, o in enumerate(offers, 1):
        live = o.get("live")
        if live:
            size_txt = f"şu an <b>{usd(live['notional'])}</b>"
            if abs(live["notional"] - (o.get("notional") or 0)) > (o.get("notional") or 0) * 0.1:
                size_txt += f" (rapordaki: {usd(o.get('notional'))})"
        else:
            size_txt = f"rapordaki boyut <b>{usd(o.get('notional'))}</b>"
        score = f" · skor {o['score']}" if o.get("score") else ""
        lines.append(f"{i}. {_side_badge(o['side'])} {alink(o['address'])} — {size_txt}{score}")
        lines.append(f"   → /takip_{o['offer_id']}")
    if already_closed:
        lines.append("")
        for c in already_closed:
            lines.append(f"🚪 {alink(c['address'])} zaten kapatmış"
                         f" ({_side_badge(c['side'])} {usd(c.get('notional'))} idi)")
    lines.append(f"\n⏳ Takip {int(cfg.track_expire_days)} gün sürer · /takipler ile yönet")
    return "\n".join(lines)


def pct_num(p: float) -> str:
    """10.0 → '10', 7.5 → '7.5'"""
    f = float(p)
    return str(int(f)) if f == int(f) else f"{f:g}"


def track_started(tid: int, symbol: str, address: str, live: dict, cfg) -> str:
    return (f"👣 <b>TAKİP BAŞLADI</b> — {symbol} (#{tid})\n"
            f"👤 {alink(address)} {_side_badge(live['side'])} <b>{usd(live['notional'])}</b>"
            f" @ {px(live['entry_px'])}\n"
            f"Bu boyut baz alındı — toplamın <b>%{pct_num(cfg.track_step_pct)}</b> kadarı"
            " her kapandığında bildirim gelecek.\n"
            "🚪 Tam kapanış ve 🔁 yön değişimi anında bildirilir.\n"
            f"⏳ Süre: {int(cfg.track_expire_days)} gün · bırakmak için /birak_{tid}")


def track_step(t: dict, live: dict, base: float, last: float, cur: float) -> str:
    sym = t["symbol"]
    step_pct = (cur - last) / base * 100  # negatif = kapattı
    closed_total = (base - cur) / base * 100
    if step_pct < 0:
        head = f"✂️ <b>BALİNA KAPATIYOR</b> — {sym}"
        act = f"Bu adımda toplamın <b>%{abs(step_pct):.0f}</b> kadarını kapattı"
    else:
        head = f"➕ <b>BALİNA EKLİYOR</b> — {sym}"
        act = f"Bu adımda toplamın <b>%{step_pct:.0f}</b> kadarını EKLEDİ"
    if closed_total > 0:
        total_txt = f"başlangıçtan beri <b>%{closed_total:.0f}</b> kapandı"
    else:
        total_txt = f"poz başlangıca göre <b>%{100 - closed_total:.0f}</b> seviyesinde"
    lines = [head,
             f"👤 {alink(t['address'])} {_side_badge(t['side'])}",
             f"{act} · {total_txt}",
             f"Güncel: <b>{usd(live['notional'])}</b>"
             f" (başlangıç {usd(t.get('base_notional'))})"]
    if is_listed(sym):
        lines.append(PROPR_NOTE)
    return "\n".join(lines)


def track_closed(t: dict, base: float, last: float) -> str:
    sym = t["symbol"]
    lines = [f"🚨🚪 <b>BALİNA TAMAMEN KAPATTI</b> — {sym}",
             f"👤 {alink(t['address'])} {_side_badge(t['side'])}"
             f" <b>{usd(t.get('base_notional'))}</b> pozisyonunu kapattı.",
             f"Takip #{t['id']} sona erdi."]
    if is_listed(sym):
        lines.append(PROPR_NOTE)
    return "\n".join(lines)


def track_flip(t: dict, live: dict) -> str:
    sym = t["symbol"]
    lines = [f"🚨🔁 <b>BALİNA YÖN DEĞİŞTİRDİ</b> — {sym}",
             f"👤 {alink(t['address'])} {_side_badge(t['side'])} →"
             f" {_side_badge(live['side'])} <b>{usd(live['notional'])}</b>"
             f" @ {px(live['entry_px'])}",
             f"Takip yeni yönle devam ediyor (#{t['id']})."]
    if is_listed(sym):
        lines.append(PROPR_NOTE)
    return "\n".join(lines)


def track_expired(t: dict, live: dict, base: float, offer_id: int) -> str:
    closed_total = (base - abs(live["szi"])) / base * 100 if base > 0 else 0
    prog = (f" · başlangıçtan beri %{closed_total:.0f} kapanmış"
            if closed_total > 1 else "")
    return (f"⏰ <b>Takip süresi doldu</b> — {t['symbol']} (#{t['id']})\n"
            f"👤 {alink(t['address'])} pozu hâlâ açık:"
            f" {_side_badge(live['side'])} <b>{usd(live['notional'])}</b>{prog}\n"
            f"Devam etmek istersen: /takip_{offer_id}")


def track_list(rows: list[dict]) -> str:
    if not rows:
        return ("👣 Aktif takip yok.\n"
                "Earnings geçince gelen teklif mesajındaki /takip_N komutuyla"
                " balina çıkışı takibi başlatabilirsin.")
    lines = ["👣 <b>Aktif takipler:</b>"]
    ts = now()
    for r in rows:
        base = float(r.get("base_szi") or 0)
        last = float(r.get("last_szi") if r.get("last_szi") is not None else base)
        closed_total = (base - last) / base * 100 if base > 0 else 0
        left_d = max(0, (int(r.get("expires_ts") or 0) - ts) // 86400)
        prog = (f"%{closed_total:.0f} kapandı" if closed_total > 1
                else ("büyüttü" if closed_total < -1 else "değişim yok"))
        chk = f" · son kontrol {tr_time(r['last_check_ts'])}" if r.get("last_check_ts") else ""
        lines.append(f"  #{r['id']} <b>{r['symbol']}</b> {_side_badge(r['side'])}"
                     f" {alink(r['address'])} — {prog}"
                     f" (başlangıç {usd(r.get('base_notional'))})"
                     f" · ⏳{left_d}g{chk} · /birak_{r['id']}")
    lines.append("\n<i>Her %X adımında bildirim gelir; tam kapanış anında bildirilir.</i>")
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
    "eval": "🏁 sonuç", "track": "👣 pozisyon takibi", "lowvol": "🐘 sessiz su devi",
    "wall": "🧱 emir duvarı", "health": "⚕️ sağlık",
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
        "/forget 0x… — adresin sicilini sıfırla + watchlist'ten çıkar\n"
        "/watchlist — sicilli adresler\n"
        "/takipler — aktif pozisyon takipleri (earnings sonrası balina çıkışı)\n"
        "/gecmis — geçmiş bilanço arşivi (kim ne pozisyondaydı, kim haklı çıktı)\n"
        "/winners — en iyi biliciler (doğru tahmin sicili)\n"
        "/bildirimler — bildirim ayarları + son gönderilenler\n"
        "/saglik — sistem sağlığı (bekçi raporu: hangi görev canlı)\n"
        "/status — bot durumu\n"
        "/id — bu sohbetin chat id'si"
    )
