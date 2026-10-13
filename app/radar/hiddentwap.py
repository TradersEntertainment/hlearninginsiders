"""🕵️ Gizli TWAP — HL TWAP emri OLMADAN kendi botuyla dilim dilim alan / satan adresler (genel radar).

Kullanıcı (13.10): "genel gizli TWAP'ları yakalayabilir miyiz — CBRS balinası TWAP'ı HL'den değil kendi
sisteminden yapıyordu; genel yapamazsak o adamı takip edelim". Hyperliquid'de her işlemin alıcı ve satıcı
adresi açık: canlı TWAP sayacı (twaplive.REG) zaten her adres + coin + yön için dilimleri sayıyor ve
düzenli diziyi HL emir geçmişine soruyor. Emir YOKSA eskiden `no_order` deyip bırakıyordu — bu modül
o dalı devralır. Tek CBRS adresini izleyen 🔂 slicewatch aynen sürer (başladı / bitti, hesap grubu).

Ölçüm (13.10, 25 dk canlı akış, hisseler + ilk 150 kripto): taker emir dizisi ≥ 8 olan 1.017 koşunun
66'sı düzenli (twaplive gevşek eşikleri); $50K üstü 2 tane — biri 0xbf73…5d58'in GOOGL'daki ~$10M long'u
5 dk'da ~30 sn arayla kapatması (maker + taker karışık). CBRS balinası (0x30af…393e) 05–12.10: 4 operasyon,
7 dizi ($7–38M); "başladı" anından dizi sonuna fiyat 7'nin 6'sında onun yönünde (hacim ağırlıklı +%1.7).

Kapı (hepsi ÖLÇÜLEN; tahmin yok — plan, toplam ve süre GÖRÜNMEZ, yalnız "şimdiye kadar"):
  • aday: twaplive'ın düzenli dizisi (≥ 10 dilim, ≥ 5 dk, ≥ lookup tabanı) + HL emir sorgusu BAŞARILI ve
    TWAP emri yok (sorgu düşerse karar yok — veri yok ≠ emir yok); bilinen MM / vault zaten elenmiş
  • taker payı ≥ hidden_twap_taker_min_pct (%50) — pasif kotasyon değil, tahtayı süpüren dilimleme
  • iki yönlü değil: aynı adresin aynı coinde karşı yön dizisi bu dizinin ≥ %25'i ise market yapıcı sayılır
  • büyüklük: görülen ≥ hidden_twap_min_usd ($250K) VE hız ≥ hidden_twap_rate_x × coinin ortalama
    dakika hacmi (0.5×) — ya da görülen ≥ hidden_twap_big_usd ($1M), hacimden bağımsız
  • bekleme: aynı adres + coin + yön 6 sa (twap_alert_cooldown; kendi anahtarı 'htwap')
Bildirim: TWAP ile aynı yer (kripto → CRYPTO_CHAT_ID, hisse/endeks → ana sohbet) + TWAP ayna grubu;
tür 'htwap' (Bildirimler → 🕵️ Gizli TWAP). Yalnız İLK uyarı (kullanıcı 12.10: TWAP'ta sonradan not yok).
🧪 LOG-HTWAP: kapılardan ÖNCE, dizi sabit lab tabanını (LAB_MIN $100K) ilk geçtiğinde bir kez — "önden
girmek" ölçülsün (geçen + kalan; ayar değişse de örneklem değişmez).
"""
from __future__ import annotations

import logging

from ..db import alert_log, alert_recent, kv_get, kv_set, now

log = logging.getLogger("radar.hiddentwap")

STATS_KV = "htwap_stats"
LAST_KV = "htwap_last"           # son uyarılar (/tani, sayfa)
LAST_MAX = 20
LAB_MIN = 100_000.0              # 🧪 sabit kayıt tabanı (ayardan bağımsız)
TWO_SIDED_SHARE = 0.25
REASON_TR = {"ok": "bildirildi", "big": "bildirildi (büyük)", "small": "görülen tabanın altında",
             "slow": "hız coinin dakika hacmine göre küçük", "maker": "taker payı düşük (pasif kotasyon)",
             "two_sided": "iki yönlü (market yapıcı)", "no_vol": "24s hacim yok", "cooldown": "bekleme içinde",
             "off": "kapalı", "no_chat": "kanal yok"}

STATS: dict = {"considered": 0, "alerted": 0, "lab": 0, "reasons": {}, "last_ts": 0}
_LAB_DONE: set = set()


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "hidden_twap_enabled", True)) and bool(getattr(cfg, "twap_live_enabled", True))


def rate_x(m: dict, day_vol) -> float | None:
    """Dilimleme hızı / coinin ortalama hızı (gün ölçeğinde aynı oran = dakika hacminin katı)."""
    try:
        dv = float(day_vol or 0)
        return float(m.get("rate_day") or 0) / dv if dv > 0 else None
    except (TypeError, ValueError):
        return None


def two_sided(reg, run) -> bool:
    """Aynı adres aynı coinde karşı yönde de (bu dizinin ≥ %25'i) diziyle işlem yapıyorsa: market yapıcı."""
    opp = reg.runs.get((run.coin, run.address, "sell" if run.side == "buy" else "buy"))
    return bool(opp and opp.last_ts >= run.first_ts and opp.total >= TWO_SIDED_SHARE * run.total)


def gate(cfg, run, m: dict, day_vol, reg) -> tuple[str, float | None]:
    """'ok' | 'big' geçer; diğerleri nedendir. İkinci değer: dakika hacmine oran."""
    x = rate_x(m, day_vol)
    tk = m.get("taker_pct")
    if tk is not None and float(tk) < float(getattr(cfg, "hidden_twap_taker_min_pct", 50) or 0):
        return "maker", x
    if two_sided(reg, run):
        return "two_sided", x
    big = float(getattr(cfg, "hidden_twap_big_usd", 1_000_000) or 0)
    if big > 0 and run.total >= big:
        return "big", x
    if run.total < float(getattr(cfg, "hidden_twap_min_usd", 250_000) or 0):
        return "small", x
    if x is None:
        return "no_vol", x
    if x < float(getattr(cfg, "hidden_twap_rate_x", 0.5) or 0):
        return "slow", x
    return "ok", x


def _lab(run, m: dict, g: str, x, ts: int, two: bool) -> bool:
    """🧪 LOG-HTWAP: dizi LAB_MIN'i ilk geçtiğinde, kapı sonucu ne olursa olsun. Senkron, G/Ç yok, fırlatmaz."""
    try:
        if run.total < LAB_MIN:
            return False
        tk = f"{run.address}:{run.side}:{int(run.first_ts)}"
        if (run.coin, tk) in _LAB_DONE:
            return False
        _LAB_DONE.add((run.coin, tk))
        if len(_LAB_DONE) > 20_000:
            _LAB_DONE.clear()
        from ..lab.registry import emit
        ok = emit("LOG-HTWAP", run.coin, ts, side=1 if run.side == "buy" else -1, px_ref=run.px_last,
                  trig_key=tk,
                  features={"gor": round(run.total), "n": run.n, "dk": round((run.last_ts - run.first_ts) / 60, 1),
                            "aralik": round(float(m.get("median_gap") or 0), 1), "taker": m.get("taker_pct"),
                            "rate_x": None if x is None else round(x, 3), "iki_yon": int(two), "kapi": g},
                  gated=g in ("ok", "big"))
        if ok:
            STATS["lab"] += 1
        return ok
    except Exception:                                  # noqa: BLE001
        return False


async def consider(cfg, notifier, client, run, m: dict, day_vol, ents: dict, ts: int, out: dict | None = None) -> str:
    """twaplive'ın `no_order` dalından (sorgu BAŞARILI, TWAP emri yok). Dönüş: karar nedeni."""
    from . import twaplive
    from ..telegram import format as fmt
    STATS["considered"] += 1
    if not enabled(cfg):
        return _count("off")
    g, x = gate(cfg, run, m, day_vol, twaplive.REG)
    _lab(run, m, g, x, ts, g == "two_sided")
    if g not in ("ok", "big"):
        return _count(g)
    key = f"{run.coin}:{run.address}:{run.side}"
    if await alert_recent("htwap", key, int(getattr(cfg, "twap_alert_cooldown", 21600) or 0)):
        return _count("cooldown")
    chat, can = twaplive.chat_for(cfg, run.coin)
    mirror_on = bool(twaplive.mirror_chat(cfg))
    if not can and not mirror_on:
        return _count("no_chat")
    pos = await twaplive.position_ctx(client, run.coin, run.address)
    ctx = {"day_vol": day_vol, "rate_x": x, "pos": pos, "gate": g, "klass": twaplive.klass_of(run.coin),
           "entity": ents.get(run.address) or ""}
    text = fmt.hidden_twap(m, ctx)
    mkey = f"htwap:{key}:{int(run.first_ts)}"
    ok = False
    if can:
        ok = bool(await notifier.send("htwap", text, priority="high", key=f"htwap:{key}", chat_id=chat,
                                      coin=run.coin, public=False))
    mres = await twaplive.mirror(cfg, notifier, text, mkey, run.coin, out, primary=chat if can else None)
    if ok or (not can and mres):
        await alert_log("htwap", key, text)       # kanal tanımsız kriptoda ayna tek başına da bekleme yakar
    else:
        return _count("failed")                   # işaret yok → sonraki tur yeniden dener
    STATS["alerted"] += 1
    STATS["last_ts"] = ts
    try:
        last = await kv_get(LAST_KV) or []
        last.insert(0, {"ts": ts, "coin": run.coin, "addr": run.address, "side": run.side, "total": round(run.total),
                        "n": run.n, "rate_x": None if x is None else round(x, 2), "gate": g})
        await kv_set(LAST_KV, last[:LAST_MAX])
    except Exception:                                  # noqa: BLE001
        log.debug("htwap_last yazılamadı", exc_info=True)
    log.info("gizli twap: %s %s %s görülen %.0f$ (%s) bildirildi", run.coin, run.side, run.address[:10], run.total, g)
    return _count(g)


def _count(reason: str) -> str:
    STATS["reasons"][reason] = STATS["reasons"].get(reason, 0) + 1
    return reason


async def save_stats() -> None:
    try:
        await kv_set(STATS_KV, {**STATS, "ts": now()})
    except Exception:                                  # noqa: BLE001
        log.debug("htwap_stats yazılamadı", exc_info=True)


async def diag_line(cfg) -> str:
    from ..telegram.format import short, usd
    if not enabled(cfg):
        return "gizli twap: kapalı (hidden_twap_enabled=0 ya da canlı TWAP kapalı)"
    st = await kv_get(STATS_KV) or STATS
    rs = st.get("reasons") or {}
    head = (f"gizli twap: {st.get('considered', 0)} aday (emirsiz düzenli dizi) · {st.get('alerted', 0)} bildirim"
            f" · 🧪 {st.get('lab', 0)} kayıt")
    if rs:
        head += " · " + ", ".join(f"{REASON_TR.get(k, k)} {v}" for k, v in sorted(rs.items(), key=lambda kv: -kv[1])[:5])
    last = await kv_get(LAST_KV) or []
    if last:
        a = last[0]
        head += (f" · son: {a.get('coin')} {short(a.get('addr') or '')} {a.get('side')} {usd(a.get('total') or 0)}"
                 f" ({max(0, now() - int(a.get('ts') or 0)) // 60} dk önce)")
    return head
