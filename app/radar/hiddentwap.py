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
  • aday: twaplive'ın düzenli dizisi (≥ 10 dilim, ≥ 5 dk, ≥ lookup tabanı) + HL emir sorgusu BAŞARILI ve bu
    coin + yönde TWAP emri yok ya da dizi başlamadan bitmiş eski emir (sorgu düşer / tur tavanına takılırsa
    karar yok — veri yok ≠ emir yok); bilinen MM / vault ve /ignore ile elenen ('manual') adres yok
  • HL TWAP alt emri dolumu (sıfır hash, adres taker) dizide HİÇ görülmemiş olmalı (nat_n = 0)
  • İDDİADAN ÖNCE TAZE KANIT (twaplive.verify_hidden): emir geçmişi dizinin son diliminden SONRA alınmış
    olmalı (adres önbelleği eskiyse zorla sorulur) ve dizinin süresiyle örtüşen HL TWAP emri olmamalı
  • taker payı ≥ hidden_twap_taker_min_pct (%50) — pasif kotasyon değil, tahtayı süpüren dilimleme
  • iki yönlü değil: aynı adresin aynı coinde karşı yön dizisi bu dizinin ≥ %25'i ise market yapıcı sayılır
  • ince spot çifti (24s hacim < spot_twap_min_day_vol) elenir — HL TWAP'ında olduğu gibi
  • büyüklük: görülen ≥ hidden_twap_min_usd ($250K) VE hız ≥ hidden_twap_rate_x × coinin ortalama
    dakika hacmi (0.5×; hacim ölçümü 1 sa'ten eskiyse yok sayılır) — ya da görülen ≥ hidden_twap_big_usd ($1M)
  • tek uyarı / operasyon: aynı adres + coin + yön 30 dk'dan uzun susmadıkça AYNI operasyondur (twaplive
    4 sa'te diziyi yeniden başlatsa da) → ikinci uyarı ve ikinci 🧪 kaydı yok; ayrıca 6 sa bekleme ('htwap')
Bildirim: YALNIZ HIDDEN_TWAP_CHAT_ID (kullanıcının "🕵️ Gizli TWAP" grubu, env-only). TWAP kanalına, kripto
kanalına, ana sohbete ve TWAP aynasına HİÇ gitmez (kullanıcı 13.10: "normal kanala gelmeyecek, sadece gizli TWAP
değişkeni olan chate"); değişken boşsa mesaj yok, ölçüm + 🧪 sürer. Tür 'htwap'. Yalnız İLK uyarı (kullanıcı
12.10: TWAP'ta sonradan not yok). AYAR SAYFASINDA YOK (kullanıcı 13.10: "ayarlara sokma beni, Railway'den
değişken eklerim"): hepsi env — HIDDEN_TWAP_CHAT_ID, kapatmak HIDDEN_TWAP_ENABLED=0, eşikler HIDDEN_TWAP_*.
🧪 LOG-HTWAP: kapı sonucundan bağımsız, operasyon sabit lab tabanını (LAB_MIN $100K) ilk geçtiğinde bir kez
(taze kanıttan sonra) — "önden girmek" ölçülsün (geçen + kalan; ayar değişse de örneklem değişmez).
/tani sayıları DİZİ başınadır (her dizinin son kararı), tur başına değil.
"""
from __future__ import annotations

import logging

from .. import assets
from ..db import alert_log, alert_recent, kv_get, kv_set, now

log = logging.getLogger("radar.hiddentwap")

STATS_KV = "htwap_stats"
LAST_KV = "htwap_last"           # son uyarılar (/tani, sayfa)
OPS_KV = "htwap_ops"             # operasyon hafızası (restart'ta ikinci uyarı / ikinci 🧪 olmasın)
LAST_MAX = 20
LAB_MIN = 100_000.0              # 🧪 sabit kayıt tabanı (ayardan bağımsız)
TWO_SIDED_SHARE = 0.25
OP_GAP = 1800                    # bu kadar sessizlik yoksa aynı operasyon (twaplive.IDLE_SEC)
OPS_MAX_AGE = 86400
RUNS_MAX = 5000
IGNORED = ("mm", "vault", "manual")
REASON_TR = {"ok": "bildirildi", "big": "bildirildi (büyük)", "small": "görülen tabanın altında",
             "slow": "hız coinin dakika hacmine göre küçük", "maker": "taker payı düşük (pasif kotasyon)",
             "two_sided": "iki yönlü (market yapıcı)", "no_vol": "24s hacim yok/eski", "cooldown": "bekleme içinde",
             "off": "kapalı", "no_chat": "HIDDEN_TWAP_CHAT_ID yok (gönderilmedi)",
             "spot_thin": "spot çifti ince", "ignored": "elenmiş adres (mm/vault//ignore)",
             "native": "HL TWAP alt emri dolumu görüldü", "native_order": "taze sorguda HL TWAP emri var",
             "no_data": "taze emir geçmişi alınamadı (sonraki tur)", "same_op": "aynı operasyon zaten bildirildi",
             "ended": "dizi durmuş (iddia yok)",
             "failed": "gönderilemedi (bot grupta mı, HIDDEN_TWAP_CHAT_ID doğru mu?)"}

STATS: dict = {"considered": 0, "alerted": 0, "lab": 0, "reasons": {}, "last_ts": 0}
OPS: dict[tuple, dict] = {}      # (coin, adres, yön) → {start, last, alerted, lab}
_RUNS: dict[tuple, str] = {}     # dizi kimliği → son karar (/tani dizi başına sayar)
_OPS_LOADED = False


def reset() -> None:
    """Bellek durumunu sıfırla (testler)."""
    global _OPS_LOADED
    STATS.update(considered=0, alerted=0, lab=0, reasons={}, last_ts=0)
    OPS.clear()
    _RUNS.clear()
    _OPS_LOADED = False


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
    """'ok' | 'big' geçer; diğerleri nedendir. İkinci değer: dakika hacmine oran. `day_vol` TAZE olmalı
    (bayatsa çağıran None verir → yalnız 'big' yolu)."""
    x = rate_x(m, day_vol)
    tk = m.get("taker_pct")
    if tk is not None and float(tk) < float(getattr(cfg, "hidden_twap_taker_min_pct", 50) or 0):
        return "maker", x
    if two_sided(reg, run):
        return "two_sided", x
    spot_min = float(getattr(cfg, "spot_twap_min_day_vol", 0) or 0)
    if spot_min > 0 and assets.is_spot(run.coin) and float(day_vol or 0) < spot_min:
        return "spot_thin", x                     # HL TWAP'ında olduğu gibi ince spot çifti susar
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


def _lab(run, m: dict, g: str, x, ts: int, two: bool, tk: str, sat: bool = False) -> bool:
    """🧪 LOG-HTWAP: operasyon LAB_MIN'i ilk geçtiğinde (taze kanıttan sonra), kapı sonucu ne olursa olsun.
    `tk` operasyon anahtarı (adres:yön:operasyon başı). Senkron, G/Ç yok, fırlatmaz."""
    try:
        from ..lab.registry import emit
        ok = emit("LOG-HTWAP", run.coin, ts, side=1 if run.side == "buy" else -1, px_ref=run.px_last,
                  trig_key=tk,
                  features={"gor": round(run.total), "n": run.n, "dk": round((run.last_ts - run.first_ts) / 60, 1),
                            "aralik": round(float(m.get("median_gap") or 0), 1), "taker": m.get("taker_pct"),
                            "rate_x": None if x is None else round(x, 3), "iki_yon": int(two), "kapi": g,
                            "sat": int(bool(sat))},
                  gated=g in ("ok", "big"))
        if ok:
            STATS["lab"] += 1
        return ok
    except Exception:                                  # noqa: BLE001
        return False


async def _ops_load() -> bool:
    """Operasyon hafızası kv'den. Okuma düşerse bayrak KALKMAZ (sonraki tur yeniden dener) ve save_stats kv'yi
    ezmez — yoksa tek bir 'database is locked' restart sonrası ikinci uyarı / ikinci 🧪 doğururdu."""
    global _OPS_LOADED
    if _OPS_LOADED:
        return True
    try:
        doc = await kv_get(OPS_KV) or {}
    except Exception:                                  # noqa: BLE001
        log.debug("htwap_ops okunamadı (sonraki tur)", exc_info=True)
        return False
    for k, v in doc.items():
        try:
            coin, addr, side = k.rsplit("|", 2)
            OPS[(coin, addr, side)] = {"start": int(v["start"]), "last": int(v["last"]),
                                       "alerted": int(v.get("alerted") or 0), "lab": int(v.get("lab") or 0)}
        except Exception:                              # noqa: BLE001
            continue                                   # bozuk tek kayıt hafızanın gerisini düşürmesin
    _OPS_LOADED = True
    return True


def touch(reg) -> None:
    """Operasyonun son dilimi DOLUMLARDAN ilerler (consider() çağrılmayan turlarda da: düzensiz / sorgu düştü /
    tavan) — yoksa twaplive'ın 4 sa yeniden başlatmasında susmamış operasyon 'yeni' sanılırdı."""
    for k, op in OPS.items():
        r = reg.runs.get(k)
        if r is not None and not r.nat_n and int(r.first_ts) - int(op["last"]) <= OP_GAP:
            op["last"] = max(int(op["last"]), int(r.last_ts))


def _op(run) -> dict:
    """Operasyon: aynı adres + coin + yön OP_GAP'ten uzun susmadıkça tek (twaplive diziyi 4 sa'te ya da
    sessizlikte yeniden başlatsa da). Tek uyarı ve tek 🧪 kaydı operasyon başınadır."""
    k = (run.coin, run.address, run.side)
    op = OPS.get(k)
    if op is None or int(run.first_ts) - int(op["last"]) > OP_GAP:
        op = OPS[k] = {"start": int(run.first_ts), "last": int(run.last_ts), "alerted": 0, "lab": 0}
    else:
        op["last"] = max(int(op["last"]), int(run.last_ts))
    return op


async def consider(cfg, notifier, client, run, m: dict, day_vol, ents: dict, ts: int, out: dict | None = None,
                   *, vol_ts=None, verify=None) -> str:
    """twaplive'ın emirsiz dalından (sorgu BAŞARILI; emir yok ya da dizi başlamadan bitmiş). Dönüş: karar nedeni.
    `verify()` → True (taze geçmişte örtüşen emir yok) / False (HL emri var) / None (taze veri yok): iddia
    (mesaj) ve 🧪 kaydı YALNIZ True'da. `vol_ts` hacim ölçüm anı (1 sa'ten eskiyse hacim yok sayılır).
    `out` kullanılmaz (imza twaplive çağrısıyla uyumlu)."""
    from . import twaplive
    from ..telegram import format as fmt
    rid = (run.coin, run.address, run.side, int(run.first_ts))
    if not enabled(cfg):
        return _count("off", rid)
    if (ents.get(run.address) or "") in IGNORED:
        return _count("ignored", rid)
    if getattr(run, "nat_n", 0):
        return _count("native", rid)              # dizide HL TWAP alt emri dolumu: emir var demektir
    raw24 = day_vol if (vol_ts and ts - int(vol_ts) <= twaplive.VOL_MAX_AGE) else None   # 🧪 ham 24s ölçüsü
    day_vol, vol_ts, sat = twaplive.eff_vol(cfg, run.coin, day_vol, vol_ts, ts)   # Cumartesi hisse: geçen Cmt
    if not vol_ts or ts - int(vol_ts) > twaplive.VOL_MAX_AGE:
        day_vol = None
    g, x = gate(cfg, run, m, day_vol, twaplive.REG)
    if not await _ops_load():
        return _count("no_data", rid)             # operasyon hafızası okunamadı: ikinci uyarı riski → sonraki tur
    op = _op(run)
    lab_due = run.total >= LAB_MIN and not op["lab"]
    key = f"{run.coin}:{run.address}:{run.side}"
    # YALNIZ kendi grubu (kullanıcı 13.10: "normal kanala gelmeyecek, sadece gizli TWAP değişkeni olan chate"):
    # TWAP kanalına / kripto kanalına / ana sohbete / TWAP aynasına HİÇ gitmez. Değişken boşsa mesaj yok
    # (ölçüm + 🧪 sürer); bekleme yakılmaz → değişken eklenince süren dizi bir sonraki turda bildirilir.
    chat = (getattr(cfg, "hidden_twap_chat_id", "") or "").strip()
    reason, send_due = g, g in ("ok", "big")
    if send_due:
        if op["alerted"]:
            reason, send_due = "same_op", False
        elif not chat:
            reason, send_due = "no_chat", False
        elif await alert_recent("htwap", key, int(getattr(cfg, "twap_alert_cooldown", 21600) or 0)):
            reason, send_due = "cooldown", False
    if not lab_due and not send_due:
        return _count(reason, rid)
    if twaplive.is_ended(run, m, ts):
        # Dizi durmuş (twaplive'ın bitiş kuralı: 3 aralık ve ≥ 5 dk dilim yok): gecikmiş turda (gönderim / sorgu
        # düştü, bekleme ya da hacim tazelendi) "dilimliyor" demeyelim, 🧪 de geç karar örneklemesin.
        return _count("ended", rid)
    v = await verify() if verify is not None else None
    if v is None:
        return _count("no_data", rid)             # veri yok ≠ emir yok: iddia da 🧪 de yok, sonraki tur
    if not v:
        return _count("native_order", rid)
    if lab_due:
        op["lab"] = 1
        _lab(run, m, g, rate_x(m, raw24) if sat else x, ts, g == "two_sided", f"{run.address}:{run.side}:{op['start']}",
             sat)
    if not send_due:
        return _count(reason, rid)
    pos = await twaplive.position_ctx(client, run.coin, run.address)
    ctx = {"day_vol": day_vol, "rate_x": x, "pos": pos, "gate": g, "klass": twaplive.klass_of(run.coin), "sat": sat,
           "entity": ents.get(run.address) or ""}
    text = fmt.hidden_twap(m, ctx)
    op["alerted"] = ts                            # talep ÖNCE: teslimden sonraki DB hatası ikinci mesaj doğurmasın
    if not await notifier.send("htwap", text, priority="high", key=f"htwap:{key}", chat_id=chat,
                               coin=run.coin, public=False):
        op["alerted"] = 0
        return _count("failed", rid)              # işaret yok → sonraki tur yeniden dener
    await alert_log("htwap", key, text)
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
    return _count(g, rid)


def _count(reason: str, rid: tuple | None = None) -> str:
    """Dizi başına SON karar sayılır (tur başına değil): aynı dizi her dakika yeniden değerlendirilir."""
    rs = STATS["reasons"]
    if rid is not None:
        prev = _RUNS.pop(rid, None)
        if prev is None:
            STATS["considered"] += 1
        elif rs.get(prev, 0) > 0:
            rs[prev] -= 1
            if not rs[prev]:
                rs.pop(prev)
        _RUNS[rid] = reason
        while len(_RUNS) > RUNS_MAX:
            _RUNS.pop(next(iter(_RUNS)))           # en eski dizi unutulur (sayısı kalır)
    rs[reason] = rs.get(reason, 0) + 1
    return reason


async def save_stats() -> None:
    try:
        await kv_set(STATS_KV, {**STATS, "ts": now()})
        if _OPS_LOADED:
            from .twaplive import REG
            touch(REG)
            cut = now() - OPS_MAX_AGE
            for k in [k for k, v in OPS.items() if int(v["last"]) < cut]:
                OPS.pop(k, None)
            await kv_set(OPS_KV, {f"{c}|{a}|{sd}": v for (c, a, sd), v in OPS.items()})
    except Exception:                                  # noqa: BLE001
        log.debug("htwap_stats yazılamadı", exc_info=True)


async def diag_line(cfg) -> str:
    from ..telegram.format import short, usd
    if not enabled(cfg):
        return "gizli twap: kapalı (HIDDEN_TWAP_ENABLED=0 ya da canlı TWAP kapalı)"
    st = await kv_get(STATS_KV) or STATS
    rs = st.get("reasons") or {}
    if not (getattr(cfg, "hidden_twap_chat_id", "") or "").strip():
        where = " · ⚠ HIDDEN_TWAP_CHAT_ID yok → mesaj gönderilmiyor (yalnız ölçüm + 🧪)"
    elif rs.get("failed") and not st.get("alerted"):
        where = " · ⚠ HIDDEN_TWAP_CHAT_ID'ye gönderilemiyor (bot grupta mı, id doğru mu?)"
    else:
        where = " · grup: HIDDEN_TWAP_CHAT_ID"
    emirsiz = max(0, int(st.get("considered", 0)) - int(rs.get("native", 0)) - int(rs.get("native_order", 0)))
    head = (f"gizli twap: {emirsiz} dizi (emirsiz, düzenli) · {st.get('alerted', 0)} bildirim"
            f" · 🧪 {st.get('lab', 0)} kayıt" + where)
    if rs:
        head += " · " + ", ".join(f"{REASON_TR.get(k, k)} {v}" for k, v in sorted(rs.items(), key=lambda kv: -kv[1])[:5])
    last = await kv_get(LAST_KV) or []
    if last:
        a = last[0]
        head += (f" · son: {a.get('coin')} {short(a.get('addr') or '')} {a.get('side')} {usd(a.get('total') or 0)}"
                 f" ({max(0, now() - int(a.get('ts') or 0)) // 60} dk önce)")
    return head
