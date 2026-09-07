"""Kripto liq yakını — "ana dexte büyük bir pozisyon patlamak üzere".

Kullanıcı kuralı: BTC/ETH HARİÇ ana dex kriptoda notional ≥ $500K ve
likidasyon fiyatı şimdiye ≤ %2,5 → kripto kanalına (CRYPTO_CHAT_ID) mesaj;
sonra pozisyon TAKİPTE kalır: ≤%1'de ikinci, ≤%0,5'te son uyarı, yok olunca
likidasyon/kapanış notu (`liqwatch`'ın kademe mantığı, kripto kanalı için).

KAYNAK `addr_positions`: derin keşif her adresin TÜM pozisyonlarını her boyutta
yazar (`hl_positions` yalnız ≥$20M kripto tutar, $500K için kördür). Tur 75–125
dk; bu yüzden mesaj gitmeden önce adayın defteri CANLI çekilir
(`sweeper.probe_address`) — bir saat önce kapanmış pozisyon için "patlamak
üzere" demek yalan olur. Takipteki pozisyonlar da sondalanır: kademe ≥2 her
tur (likidasyon anını kaçırmamak için), kademe 1 on dakikada bir.

MESAJ COİN BAŞINA TEK: bir çöküşte 30 pozisyon aynı anda eşiğe girer, 30 ayrı
mesaj spam'dir. Bekleme POZİSYON başına ve yalnız ilk kademe için: kademe 2/3
yeni bilgidir, beklemeye bakmaz. Pozisyon ilk mesafenin 1,5 katına uzaklaşırsa
kademeler sıfırlanır (liqwatch'ın histerezisi); liq'e yaklaşırken değeri
küçülen pozisyon $500K altına indi diye düşürülmez (izleniyorsa kalır).

KAPANIŞ TEYİDİ: izlenen pozisyon yok olunca adresin son fill'leri çekilir; o
coinde `liquidation` alanlı (ya da "Liquidated …" yönlü) fill varsa 💀 likide,
fill var ama likidasyon yoksa 🏁 kapatıldı, istek düşerse "doğrulanamadı".

FİYAT `main_dex_ctx` kv'si: metrik döngüsü 5 dk'da bir (ABD kapalıyken 60 sn)
yazıyor; bayatsa bu modül kendisi çeker — tek istek, ~200 coin.

Kanal boşsa / bot yoksa / tip kapalıysa hesap yine yapılır (kv `cryptoliq_stats`
→ /tani "neden gelmedi"yi söyler) ama sonda atılmaz, gönderim yapılmaz.
Marker ve kademe YALNIZ gönderim başarılıysa ilerler (kapalı seans dersi).
Metnin peşinden resim (mumlar + liq çizgisi + kalan mesafe): bonus, düşerse
metin zaten gitmiştir.
"""
import asyncio
import logging

from ..db import alert_log, alert_recent, db, kv_set, now
from .bigpos import MAJORS

log = logging.getLogger("radar.cryptoliq")

PROBE_MAX = 12                     # tur başına canlılık sondası (adres); kalanlar sonraki tura
LIST_MAX = 6                       # mesajda tek tek yazılan pozisyon; fazlası toplamla
RESET_FACTOR = 1.5                 # dist > dist1×1.5 → kademeler sıfırlanır (liqwatch: 1.0→1.5)
STAGE_PROBE_SEC = {1: 600, 2: 0, 3: 0}   # izlenen pozisyonu yeniden sondalama aralığı
CLOSE_NOTE_MAX_AGE = 24 * 3600     # bundan eski kapanışa not gitmez (kanal yeni açıldıysa yığılmasın)
FILLS_LOOKBACK = 48 * 3600         # kapanış teyidi: fill'lere en çok bu kadar geriye bak
CHART_INTERVAL, CHART_SPAN = "15m", 48 * 3600   # 192 mum; sağda pay renderer'da
CHART_LABEL = ("15dk", "son 48 saat")
NEAR_BAND_PCT = 10.0         # ana başlık: bu mesafe içindeki en büyük band (varsa)
NEAR_BAND_MIN_SHARE = 0.10   # …ama en büyük bandın %10'undan küçükse en büyük band kazanır
CAPTION_MAX = 1000                 # Telegram altyazı sınırı 1024 görünür karakter; pay bırak
WATCH_KEEP_CLOSED = 7 * 86400
WATCH_KEEP_IDLE = 3 * 86400


# ─────────────────────────────────────────────── saf hesap (test edilir)

def stage_dists(cfg) -> tuple[float, float, float]:
    """(d1, d2, d3) — tekdüze azalan; yanlış ayar (d2 > d1) sessizce kırpılır."""
    d1 = float(getattr(cfg, "crypto_liq_dist_pct", 2.5))
    d2 = min(d1, float(getattr(cfg, "crypto_liq_dist2_pct", 1.0)))
    d3 = min(d2, float(getattr(cfg, "crypto_liq_dist3_pct", 0.5)))
    return d1, d2, d3


def needed_stage(dist: float, d1: float, d2: float, d3: float) -> int:
    """Bu mesafede hangi kademe bildirilmiş olmalı (0 = hiçbiri)."""
    if dist <= d3:
        return 3
    if dist <= d2:
        return 2
    if dist <= d1:
        return 1
    return 0


def _dist(p: dict, mark) -> float | None:
    """Liq'e uzaklık (%), yönü doğruysa; değilse None (ters veri atlanır)."""
    try:
        mark = float(mark or 0)
        liq = float(p.get("liq_px") or 0)
    except (TypeError, ValueError):
        return None
    side = p.get("side")
    if mark <= 0 or liq <= 0 or side not in ("long", "short"):
        return None
    if (side == "long" and liq >= mark) or (side == "short" and liq <= mark):
        return None
    return abs(mark - liq) / mark * 100


def near_liq(rows: list[dict], marks: dict, dist_pct: float, min_usd: float,
             exclude=MAJORS) -> dict[str, list[dict]]:
    """coin → likidasyona ≤ dist_pct uzaklıkta, yönü doğru pozisyonlar (yakından uzağa).

    Yön sağlaması `liqattack.liq_within` ile aynı: long'un liq'i markın ALTINDA,
    short'unki ÜSTÜNDE olmalı; tersi veri tutarsızlığıdır ve atlanır (sessizce
    yön değiştirmekten iyi). HIP-3 (':' içeren) ve `exclude` coinleri girmez.
    Dönen satırlar girdinin kopyası + `dist`, `mark`.
    """
    out: dict[str, list[dict]] = {}
    for p in rows:
        coin = p.get("coin") or ""
        if not coin or ":" in coin or coin.upper() in exclude:
            continue
        try:
            ntl = float(p.get("notional") or 0)
        except (TypeError, ValueError):
            continue
        if ntl < min_usd:
            continue
        dist = _dist(p, marks.get(coin))
        if dist is None or dist > dist_pct:
            continue
        out.setdefault(coin, []).append({**p, "notional": ntl, "liq_px": float(p["liq_px"]),
                                         "dist": dist, "mark": float(marks[coin])})
    for lst in out.values():
        lst.sort(key=lambda q: q["dist"])
    return out


# ─────────────────────────────────────────────── veri

_COLS = ("a.coin, a.address, a.side, a.notional, a.liq_px, a.leverage, a.entry_px,"
         " a.ts, a.closed_ts, ad.entity")
_FROM = "addr_positions a LEFT JOIN addresses ad ON ad.address = a.address"


async def _rows(min_usd: float, tracked_addrs: list[str]) -> list[dict]:
    """Açık ana dex satırları: ≥ min_usd olanlar ∪ izlenen adreslerin hepsi
    (histerezis: izlenen pozisyon küçüldü diye düşmez; çağıran anahtara bakar)."""
    q = f"SELECT {_COLS} FROM {_FROM} WHERE a.closed_ts IS NULL AND a.liq_px > 0" \
        f" AND instr(a.coin, ':') = 0 AND (a.notional >= ?"
    args: list = [min_usd]
    if tracked_addrs:
        q += f" OR a.address IN ({','.join('?' * len(tracked_addrs))})"
        args += tracked_addrs
    q += ")"
    async with db() as conn:
        cur = await conn.execute(q, tuple(args))
        return [dict(r) for r in await cur.fetchall()]


async def _reread_addrs(addrs: list[str]) -> dict[tuple[str, str], dict]:
    """Sondadan sonra adreslerin ana dex satırları (kapanmışsa closed_ts dolu)."""
    if not addrs:
        return {}
    q = ",".join("?" * len(addrs))
    async with db() as conn:
        cur = await conn.execute(
            f"SELECT {_COLS} FROM {_FROM} WHERE a.address IN ({q}) AND instr(a.coin, ':') = 0",
            tuple(addrs))
        return {(r["coin"], r["address"]): dict(r) for r in await cur.fetchall()}


async def _watch_open() -> dict[tuple[str, str], dict]:
    async with db() as conn:
        cur = await conn.execute("SELECT * FROM cryptoliq_watch WHERE closed_ts IS NULL")
        return {(r["coin"], r["address"]): dict(r) for r in await cur.fetchall()}


async def _watch_upsert(coin: str, addr: str, p: dict, dist: float | None, mark,
                        stage: int | None = None, probed_ts: int | None = None) -> None:
    """Görülen pozisyonu yaz: son değerler tazelenir, first_ts korunur; kademe ve
    sonda damgası yalnız verilirse değişir (yeni satırda sonda damgası da yazılır
    ki az önce sondalanan pozisyon sonraki tur yeniden sondalanmasın)."""
    ts = now()
    async with db() as conn:
        await conn.execute(
            """INSERT INTO cryptoliq_watch(coin,address,side,notional,liq_px,entry_px,leverage,
                 stage,last_dist,last_mark,first_ts,updated_ts,probed_ts)
               VALUES(?,?,?,?,?,?,?,COALESCE(?,0),?,?,?,?,?)
               ON CONFLICT(coin,address) DO UPDATE SET
                 side=excluded.side, notional=excluded.notional, liq_px=excluded.liq_px,
                 entry_px=excluded.entry_px, leverage=excluded.leverage,
                 last_dist=excluded.last_dist, last_mark=excluded.last_mark,
                 updated_ts=excluded.updated_ts,
                 stage=COALESCE(?, cryptoliq_watch.stage),
                 probed_ts=COALESCE(?, cryptoliq_watch.probed_ts),
                 closed_ts=NULL, closed_kind=NULL, closed_px=NULL, notified_ts=NULL""",
            (coin, addr, p.get("side"), p.get("notional"), p.get("liq_px"), p.get("entry_px"),
             p.get("leverage"), stage, dist, mark, ts, ts, probed_ts, stage, probed_ts))


async def _watch_set(coin: str, addr: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    async with db() as conn:
        await conn.execute(f"UPDATE cryptoliq_watch SET {cols} WHERE coin=? AND address=?",
                           (*fields.values(), coin, addr))


async def _watch_prune() -> None:
    ts = now()
    async with db() as conn:
        await conn.execute("DELETE FROM cryptoliq_watch WHERE closed_ts IS NOT NULL AND closed_ts < ?",
                           (ts - WATCH_KEEP_CLOSED,))
        await conn.execute("DELETE FROM cryptoliq_watch WHERE closed_ts IS NULL AND stage = 0"
                           " AND COALESCE(updated_ts, 0) < ?", (ts - WATCH_KEEP_IDLE,))


def send_gate(cfg, notifier) -> str:
    """Gönderim mümkün değilse NEDENİ (boş = gönderilebilir)."""
    from ..notify import kind_enabled
    if not (getattr(cfg, "crypto_chat_id", "") or "").strip():
        return "CRYPTO_CHAT_ID yok"
    if notifier is None or getattr(notifier, "bot", None) is None:
        return "bot yok"
    if not kind_enabled(cfg, "cryptoliq"):
        return "bildirim kapalı (notify_cryptoliq)"
    return ""


async def _closure_kind(client, coin: str, addr: str, w: dict) -> tuple[str, float | None]:
    """'liq' | 'close' | 'unknown', (+ likidasyon fiyatı). Tek istek: adresin
    son fill'leri; o coinde `liquidation` alanlı ya da 'Liquidated …' yönlü fill
    varsa likidasyon. Fill yok / istek düştü → doğrulanamadı (uydurulmaz)."""
    fn = getattr(client, "user_fills_by_time", None)
    if fn is None:
        return "unknown", None
    since = max(int(w.get("first_ts") or 0), now() - FILLS_LOOKBACK)
    try:
        fills = await fn(addr, since * 1000)
    except Exception as e:
        log.debug("fill sorgusu %s: %s", addr, e)
        return "unknown", None
    seen = False
    for f in fills or []:
        if not isinstance(f, dict) or (f.get("coin") or "") != coin:
            continue
        seen = True
        if f.get("liquidation") or "liquidat" in str(f.get("dir") or "").lower():
            try:
                return "liq", float(f.get("px") or 0) or None
            except (TypeError, ValueError):
                return "liq", None
    return ("close" if seen else "unknown"), None


async def _coin_rows(coin: str) -> list[dict]:
    """Coinin TÜM açık ana dex pozisyonları (her boyut) — zincir havuzu."""
    async with db() as conn:
        cur = await conn.execute(
            f"SELECT {_COLS} FROM {_FROM} WHERE a.coin=? AND a.closed_ts IS NULL"
            " AND a.liq_px > 0 AND a.notional > 0", (coin,))
        return [dict(r) for r in await cur.fetchall()]


async def _cascade(cfg, client, coin: str, mark, trigger: dict, rows: list[dict]) -> dict | None:
    """Zincir: en yakın pozisyon patlarsa defter nereye kadar süpürülür, arada
    kim patlar. Tek l2Book isteği (nSigFigs=3, geniş; düşerse varsayılan).
    Ayar kapalı / fiyat yok / defter alınamadı → None (mesaj satırsız gider)."""
    if not getattr(cfg, "crypto_liq_cascade", True) or not mark or client is None or not trigger:
        return None
    fn = getattr(client, "l2_book", None)
    if fn is None:
        return None
    from . import cascade
    from .bookwall import _parse_book
    # l2Book en çok 20 seviye: 3 anlamlı hane HYPE'ta 0,1'lik kovalar (≈ +%2,3),
    # liq daha uzaksa defter "uzanmıyor" görünür. O zaman 2 hane (1,0'lık kova,
    # ≈ +%23) ile yeniden bak — kaba ama geniş; metinde söylenir. En çok 2 istek.
    best = None
    for n_sig in (3, 2):
        try:
            book = await fn(coin, n_sig)
        except TypeError:                     # eski/sahte client n_sig_figs bilmiyor
            try:
                book = await fn(coin)
            except Exception as e:
                log.debug("l2Book alınamadı (%s): %s", coin, e)
                break
            n_sig = None
        except Exception as e:
            log.debug("l2Book(%s, %s) alınamadı: %s", coin, n_sig, e)
            continue
        if not book:
            continue
        try:
            bids, asks = _parse_book(book)
            c = cascade.simulate(bids, asks, trigger, rows, mark, coarse=(n_sig == 2))
        except Exception:
            log.debug("zincir hesaplanamadı (%s)", coin, exc_info=True)
            continue
        if not c.get("exhausted"):
            return c                          # ince defter yetti
        # tükendi: en uzağa varanı sakla, bir sonraki (daha kaba) haneyi dene
        if best is None or (not c.get("no_book") and (best.get("no_book") or
                                                       abs(c["end_px"] - c["start_px"]) > abs(best["end_px"] - best["start_px"]))):
            best = c
        if n_sig is None:
            break
    return best


def _target(casc: dict | None) -> tuple | None:
    """Grafik için zincir hedefi (px, etiket)."""
    if not casc or not casc.get("steps") or casc.get("no_book"):
        return None
    from ..telegram.format import px, usd
    # kısa: sağ etiket alanına sığsın ("zincir hedefi …" kesiliyordu)
    return (casc["end_px"], f"zincir → {px(casc['end_px'])} · {usd(casc['total_usd'])}")


def _in_band(r: dict, band: dict) -> bool:
    """Pozisyon bandın üyesi mi (aynı yön, liq fiyatı bandın kenarları arasında)."""
    try:
        lp = float(r.get("liq_px") or 0)
    except (TypeError, ValueError):
        return False
    return (r.get("side") == band.get("side") and lp > 0
            and float(band["px_lo"]) - 1e-12 <= lp <= float(band["px_hi"]) + 1e-12)


def _far_pct(cfg) -> float:
    """Grafik/anlık mesafe sınırı — liq tablosuyla aynı ayar (max_liq_distance_pct, %50)."""
    return float(getattr(cfg, "max_liq_distance_pct", 50) or 50)


async def _chart(client, coin: str, mark, fresh: list[dict], target: tuple | None = None,
                 coverage_txt: str | None = None, far_pct: float = 50.0) -> bytes | None:
    """Mesajın resmi: son 48 saatin 15 dk mumları + liq çizgileri + kalan mesafe
    (+ zincir hedefi)."""
    fn = getattr(client, "candles", None)
    if fn is None or not fresh:
        return None
    from . import liqchart, pricechart
    ts = now()
    try:
        raw = await fn(coin, CHART_INTERVAL, (ts - CHART_SPAN) * 1000, ts * 1000)
    except Exception as e:
        log.debug("grafik mumu alınamadı (%s): %s", coin, e)
        return None
    cands = pricechart.parse_candles(raw)
    # `main` işaretli satır varsa o ana seviye (band bazlı görünüm), yoksa en yakın
    ordered = sorted(fresh, key=lambda q: (0 if q.get("main") else 1, q.get("dist") or 0))[:4]
    levels = [{"px": p["liq_px"], "side": p.get("side"), "notional": p.get("notional"),
               "dist": p.get("dist"), "main": i == 0,
               **{k: p[k] for k in ("px_lo", "px_hi", "cluster", "n") if k in p}}   # küme bandı
              for i, p in enumerate(ordered)]
    return liqchart.render(coin, cands, mark, levels, interval=CHART_LABEL[0],
                           span_txt=CHART_LABEL[1], target=target, coverage_txt=coverage_txt,
                           far_pct=far_pct)


async def snapshot(cfg, client, coin: str, kind: str = "crypto", limit: int = 5) -> dict:
    """/hype, /pump… komutu: coinin liq'e EN YAKIN büyük pozisyonları + grafik,
    CANLI fiyat ve güncel kalan mesafeyle (alarmı beklemeden bakmak için).

    Kripto: fiyat ana dex özeti (≤60 sn, gerekirse tek istekle tazelenir),
    satırlar addr_positions (süpürmede görülen her boyut). Hisse: asset_metrics
    fiyatı, positions_current satırları. ≥ min_usd olanlar öne; hiç yoksa en
    yakın küçükler gösterilir ve söylenir. Dönüş: {coin, kind, mark, age, rows,
    n_all, n_big, min_usd, png}."""
    min_usd = float(getattr(cfg, "crypto_liq_min_usd", 500_000))
    age = None
    ctx, summ = None, None
    if kind == "crypto":
        from ..hl.universe import main_dex_ctx
        ctx = await main_dex_ctx(client, ttl=60, fetch=True)
        mark = ((ctx.get("c") or {}).get(coin) or {}).get("m")
        if ctx.get("ts"):
            age = max(0, now() - int(ctx["ts"]))
        async with db() as conn:
            cur = await conn.execute(
                f"SELECT {_COLS} FROM {_FROM} WHERE a.coin=? AND a.closed_ts IS NULL"
                " AND a.liq_px > 0 AND a.notional > 0", (coin,))
            rows = [dict(r) for r in await cur.fetchall()]
    else:
        from .metrics import summary
        summ = await summary(coin)
        mark = summ.get("mark")
        async with db() as conn:
            cur = await conn.execute(
                """SELECT p.coin, p.address, p.side, p.notional, p.liq_px, p.leverage,
                          p.entry_px, p.ts, ad.entity
                   FROM positions_current p LEFT JOIN addresses ad ON ad.address = p.address
                   WHERE p.coin=? AND p.liq_px > 0 AND p.notional > 0""", (coin,))
            rows = [dict(r) for r in await cur.fetchall()]
    cands = []
    for r in rows:
        d = _dist(r, mark)
        if d is None:
            continue
        cands.append({**r, "notional": float(r.get("notional") or 0), "liq_px": float(r["liq_px"]),
                      "dist": d, "mark": float(mark)})
    cands.sort(key=lambda q: q["dist"])
    # "liq'e en yakın büyük": önce mesafe sınırı (max_liq_distance_pct, %50) içindekiler —
    # +%2297'lik 3× short "büyük" diye başa geçip grafiği bozuyordu. İçeride hiç yoksa
    # eski davranış (en yakın uzaklar) ve mesaj bunu söyler; grafik çizilmez.
    far_pct = _far_pct(cfg)
    near = [c for c in cands if c["dist"] <= far_pct]
    # Toz: OI'nin %0,1'i ya da $1K altı pozisyon başlık/küme için sayılmaz (HEMI: "LONG $136")
    oi_ntl = 0.0
    try:
        if summ:
            oi_ntl = float(summ.get("oi_ntl") or 0)
        elif ctx:
            c0 = (ctx.get("c") or {}).get(coin) or {}
            oi_ntl = float(c0.get("oi") or 0) * float(mark or 0)
    except (TypeError, ValueError):
        oi_ntl = 0.0
    dust = max(1_000.0, oi_ntl * 0.001)
    solid = [c for c in near if c["notional"] >= dust]
    n_dust = len(near) - len(solid)
    big = [c for c in solid if c["notional"] >= min_usd]
    all_far = bool(cands) and not near
    # HER ZAMAN band bazlı (PUMP vakası): kovalı liq haritasının bantları başlık olur —
    # tek büyük pozisyon %19'da diye 0.0042'deki onlarca küçük pozisyonun toplamı
    # gizlenmez. Bantlar TÜM yakın pozisyonları toplar (toz dahil — dış sitelerin
    # bandı da öyle; PUMP'ta $36K'lık 30 pozisyon "toz" sayılıp eksik çıkıyordu);
    # toz yalnız tek listesinden düşer. Ana başlık = %10 içindeki en büyük band (en
    # büyük bandın en az %10'u ise), yoksa en büyük band. Mesajda bantlar mesafeye
    # göre; büyük tekler altta.
    clusters: list[dict] = []
    main_band: dict | None = None
    chart_rows: list[dict]
    casc_rows = rows
    if near:
        from . import liqmap
        bands = liqmap.clusters(near, mark, far_pct, per_side=3)
        if bands:
            biggest = bands[0]["total"]
            near_b = [b for b in bands if b["dist_lo"] <= NEAR_BAND_PCT
                      and b["total"] >= biggest * NEAR_BAND_MIN_SHARE]
            main_band = max(near_b, key=lambda b: b["total"]) if near_b else bands[0]
            clusters = sorted(bands, key=lambda b: b["dist_lo"])
        show = (big or sorted(solid or near, key=lambda c: -c["notional"]))[:min(limit, 3)]
        order = sorted(bands, key=lambda b: (0 if b is main_band else 1, b["dist_lo"]))[:4]
        chart_rows = [{"liq_px": b["px"], "side": b["side"], "notional": b["total"], "dist": b["dist_lo"],
                       "px_lo": b["px_lo"], "px_hi": b["px_hi"], "cluster": True, "n": b["n"],
                       "main": b is main_band, "mark": float(mark)} for b in order] or show
        if main_band:
            # zincir tetiği = ana band (toplamı, fiyata yakın kenarından); üyeleri
            # "arada patlayan" diye ikinci kez sayılmasın
            casc_rows = [r for r in rows if not _in_band(r, main_band)]
    else:
        show = cands[:limit]
        chart_rows = show
    trigger = chart_rows[0] if chart_rows else None
    casc = await _cascade(cfg, client, coin, mark, trigger, casc_rows) if trigger else None
    # Kapsama (havuz / HL OI): mesaj ve PNG'de — süs, hesaplanamazsa komut düşmez
    from . import coverage as _coverage
    try:
        cov = await _coverage.coverage(coin, kind, cfg, summ=summ, ctx=ctx)
    except Exception:
        log.debug("kapsama hesaplanamadı (%s)", coin, exc_info=True)
        cov = None
    png = None
    if chart_rows and getattr(cfg, "crypto_liq_chart", True):
        try:
            png = await _chart(client, coin, mark, chart_rows, target=_target(casc),
                               coverage_txt=_coverage.txt(cov), far_pct=far_pct)
        except Exception:
            log.debug("anlık grafik üretilemedi (%s)", coin, exc_info=True)
    return {"coin": coin, "kind": kind, "mark": mark, "age": age, "rows": show,
            "n_all": len(cands), "n_big": len(big), "min_usd": min_usd, "png": png,
            "cascade": casc, "coverage": cov, "all_far": all_far,
            "n_far": len(cands) - len(near), "far_pct": far_pct,
            "clusters": clusters, "main_band": main_band, "n_dust": n_dust, "dust": dust}


# ─────────────────────────────────────────────── tarama

async def scan(cfg, client, notifier=None) -> dict:
    out = {"coins": 0, "positions": 0, "candidates": 0, "fresh": 0, "probed": 0,
           "probe_deferred": 0, "probe_err": 0, "dropped_stale": 0, "alerted": 0,
           "failed": 0, "skipped": "", "chat": False, "top": [], "ctx_age": None,
           "tracked": 0, "stage2": 0, "stage3": 0, "resets": 0, "closed": 0,
           "closed_liq": 0, "close_notes": 0, "photos": 0, "combined": 0, "cascades": 0,
           "sim": 0}
    if not getattr(cfg, "crypto_liq_enabled", True):
        out["skipped"] = "kapalı"
        return await _stats(out)
    d1, d2, d3 = stage_dists(cfg)
    thr = {1: d1, 2: d2, 3: d3}
    min_usd = float(getattr(cfg, "crypto_liq_min_usd", 500_000))
    cool = max(60, int(getattr(cfg, "crypto_liq_cooldown", 4 * 3600)))
    chat = (getattr(cfg, "crypto_chat_id", "") or "").strip()
    out["chat"] = bool(chat)
    gate = send_gate(cfg, notifier)
    out["skipped"] = gate
    ts = now()

    watch = await _watch_open()
    tracked = {k: w for k, w in watch.items() if int(w.get("stage") or 0) >= 1}
    out["tracked"] = len(tracked)
    rows = await _rows(min_usd, sorted({a for _c, a in tracked}))
    out["positions"] = sum(1 for r in rows if float(r.get("notional") or 0) >= min_usd)
    out["coins"] = len({r["coin"] for r in rows})
    if not rows and not tracked:
        # Aday yok; bekleyen kapanış notu (süpürme damgaladı, not gitmedi) olabilir.
        if not gate:
            await _closure_notes(cfg, notifier, chat, ts, out)
        await _watch_prune()
        return await _stats(out)

    from ..hl.universe import main_dex_ctx
    try:
        # Kapı kapalıysa istek de atma: yalnız kv (metrik döngüsü zaten yazıyor).
        ctx = await main_dex_ctx(client, fetch=not gate)
    except Exception as e:
        out["skipped"] = f"fiyat alınamadı: {type(e).__name__}: {e}"
        log.warning("kripto liq: ana dex fiyatları alınamadı: %s", e)
        return await _stats(out)
    marks = {c: v.get("m") for c, v in (ctx.get("c") or {}).items()}
    if ctx.get("ts"):
        out["ctx_age"] = max(0, ts - int(ctx["ts"]))

    # 1) Adaylar: izlenmeyenler eşikle (near_liq); izlenenler HER boyutta —
    #    liq'e yaklaşırken değeri küçülen pozisyon düşmez (histerezis). İzlenen
    #    pozisyon ilk mesafenin 1,5 katına uzaklaşmışsa kademe sıfırlanır.
    open_rows = {(r["coin"], r["address"]): r for r in rows}
    by = near_liq([r for k, r in open_rows.items() if k not in tracked], marks, d1, min_usd)
    for key, w in tracked.items():
        r = open_rows.get(key)
        if not r:
            continue                                   # kapanış adayı (aşağıda)
        dist = _dist(r, marks.get(key[0]))
        if dist is None:
            continue                                   # fiyat yok: dokunma
        if dist > d1 * RESET_FACTOR:
            await _watch_set(key[0], key[1], stage=0, last_dist=dist, updated_ts=ts)
            w["stage"] = 0
            out["resets"] += 1
            continue
        await _watch_upsert(key[0], key[1], r, dist, marks.get(key[0]))
        if dist <= d1:
            by.setdefault(key[0], []).append({**r, "notional": float(r.get("notional") or 0),
                                              "liq_px": float(r["liq_px"]), "dist": dist,
                                              "mark": float(marks[key[0]])})
    for lst in by.values():
        lst.sort(key=lambda q: q["dist"])
    out["candidates"] = sum(len(v) for v in by.values())
    out["top"] = sorted(({"coin": c, "n": len(v), "total": sum(p["notional"] for p in v)}
                         for c, v in by.items()), key=lambda x: -x["total"])[:3]

    # 2) Kademe: need > sent → yükselme (kademe 1 beklemeye bakar); need ≤ sent → eski.
    esc: dict[str, list[dict]] = {}
    old: dict[str, list[dict]] = {}
    for coin, cands in by.items():
        for p in cands:
            key = (coin, p["address"])
            w = watch.get(key) or {}
            sent = int(w.get("stage") or 0)
            need = needed_stage(p["dist"], d1, d2, d3)
            p["need"], p["sent"] = need, sent
            if need > sent:
                if need == 1 and await alert_recent("cryptoliq", f"{coin}:{p['address']}", cool):
                    # Daha önce bildirilmiş (marker var) ama izlenmiyor — deploy
                    # öncesi bildirim ya da sıfırlanıp dönen pozisyon: sessizce
                    # takibe al, bekleme bitince yeniden söylenir.
                    await _watch_upsert(coin, p["address"], p, p["dist"], p["mark"], stage=1)
                    old.setdefault(coin, []).append(p)
                    continue
                esc.setdefault(coin, []).append(p)
            else:
                await _watch_upsert(coin, p["address"], p, p["dist"], p["mark"])
                old.setdefault(coin, []).append(p)
    out["fresh"] = sum(len(v) for v in esc.values())
    if gate:
        return await _stats(out)                        # hesap yapıldı; sonda ve gönderim yok

    # 3) Sondalar (bütçe, sırayla): (a) yükselme adayları, (b) izlenen kademe ≥2
    #    her tur, (c) kademe 1 on dakikada bir. Kalan sonraki tura.
    from .sweeper import probe_address
    order: list[str] = []
    for lst in esc.values():
        order += [p["address"] for p in lst]
    due = []
    for (coin, addr), w in tracked.items():
        st = int(w.get("stage") or 0)
        if st < 1:
            continue
        if ts - int(w.get("probed_ts") or 0) >= STAGE_PROBE_SEC.get(st, 600):
            due.append((0 if st >= 2 else 1, int(w.get("probed_ts") or 0), addr))
    order += [a for _p, _t, a in sorted(due)]
    todo, seen = [], set()
    for a in order:
        if a not in seen:
            seen.add(a)
            todo.append(a)
    out["probe_deferred"] = max(0, len(todo) - PROBE_MAX)
    probed_ok: set[str] = set()
    for addr in todo[:PROBE_MAX]:
        try:
            await probe_address(cfg, client, addr)
            probed_ok.add(addr)
            out["probed"] += 1
        except Exception as e:
            out["probe_err"] += 1
            log.debug("sonda %s: %s", addr, e)
    if probed_ok:
        async with db() as conn:
            await conn.execute(
                f"UPDATE cryptoliq_watch SET probed_ts=? WHERE address IN ({','.join('?' * len(probed_ok))})",
                (ts, *probed_ok))
    live = await _reread_addrs(list(probed_ok))

    # 4) Yükselmeleri canlı satırla doğrula: kapanmış → düşer (izleniyorsa kapanış
    #    yoluna), uzaklaşmış → düşer; yakınlaşmış → kademe yeniden hesaplanır.
    closures: dict[tuple[str, str], dict] = {}
    for coin, lst in list(esc.items()):
        keep = []
        for p in lst:
            key = (coin, p["address"])
            if p["address"] not in probed_ok:
                keep.append(p)                     # sonda düştü/ertelendi: eldeki satır, yaşı yazılır
                continue
            q = live.get(key)
            if not q or q.get("closed_ts"):
                out["dropped_stale"] += 1
                if key in tracked:
                    closures[key] = tracked[key]
                continue
            dist = _dist(q, marks.get(coin))
            floor = 0.0 if key in tracked else min_usd
            if dist is None or float(q.get("notional") or 0) < floor:
                out["dropped_stale"] += 1
                continue
            need = needed_stage(dist, d1, d2, d3)
            if need <= p["sent"]:
                out["dropped_stale"] += 1          # arada uzaklaşmış
                continue
            keep.append({**p, **q, "notional": float(q.get("notional") or 0),
                         "liq_px": float(q["liq_px"]), "dist": dist, "need": need,
                         "verified": True})
        esc[coin] = keep

    # 5) İzlenen pozisyon yok olduysa kapanış: sondalanıp kapanmış ya da satırı
    #    hiç görünmeyen (süpürme damgalamış) her izlenen anahtar.
    for key, w in tracked.items():
        if key in closures or int(w.get("stage") or 0) < 1:
            continue
        r = open_rows.get(key)
        if r is None:
            q = live.get(key) if key[1] in probed_ok else None
            if q is not None and not q.get("closed_ts"):
                continue                           # sonda tekrar açık gördü
            closures[key] = w
        elif key[1] in probed_ok and (live.get(key) or {}).get("closed_ts"):
            closures[key] = w
    for (coin, addr), w in closures.items():
        kind, cpx = await _closure_kind(client, coin, addr, w)
        await _watch_set(coin, addr, closed_ts=ts, closed_kind=kind, closed_px=cpx,
                         updated_ts=ts)
        out["closed"] += 1
        if kind == "liq":
            out["closed_liq"] += 1
        log.info("kripto liq: izlenen pozisyon kapandı %s %s (%s)", coin, addr[:10], kind)

    # 6) Mesajlar: coin başına tek; başlık en yüksek kademe. Marker ve kademe
    #    YALNIZ gönderim başarılıysa ilerler.
    from ..telegram import format as fmt
    for coin, fresh in esc.items():
        if not fresh:
            continue
        fresh.sort(key=lambda q: q["dist"])
        stage = max(int(p["need"]) for p in fresh)
        # Zincir: en yakın pozisyon patlarsa defter nereye kadar süpürülür, arada
        # kim patlar (coinin tüm havuzu, her boyut). Defter alınamazsa satır yok.
        casc = await _cascade(cfg, client, coin, marks.get(coin), fresh[0], await _coin_rows(coin))
        if casc:
            out["cascades"] += 1
        # 🧪 Simülasyon: doğrulanmış SON UYARI → kâğıt üstünde işlem. Alarmdan
        # bağımsız: gönderim düşse de defter ilerler; sim patlarsa alarm etkilenmez.
        if getattr(cfg, "sim_enabled", True):
            try:
                from . import sim
                if await sim.on_signal(cfg, client, notifier, coin, marks.get(coin), fresh, casc):
                    out["sim"] += 1
            except Exception:
                log.exception("sim sinyali işlenemedi (%s)", coin)
        # Her satırın yanına /takip_N: kullanıcı basınca o pozisyon takibe alınır
        # (boyut %10 adımı, liq fiyatı %1 kayması, kapanış/likidasyon). Teklif
        # yazılamazsa mesaj yine gider, sadece komut olmaz.
        offers: list[int] = []
        try:
            from .tracker import offer_positions
            offers = await offer_positions(coin, coin, fresh[:LIST_MAX])
        except Exception:
            log.debug("takip teklifi yazılamadı (%s)", coin, exc_info=True)
        # Kapsama satırı: havuz HL OI'sinin yüzde kaçı (dürüstlük; süs, alarmı düşürmez)
        try:
            from . import coverage as _coverage
            cov = await _coverage.coverage(coin, "crypto", cfg, ctx=ctx)
            cov_txt = _coverage.txt(cov)
        except Exception:
            log.debug("kapsama hesaplanamadı (%s)", coin, exc_info=True)
            cov, cov_txt = None, ""
        text = fmt.crypto_liq_alert(coin, marks.get(coin), fresh, old.get(coin) or [],
                                    thr[stage], stage, cascade=casc, offers=offers, coverage=cov)
        key = f"cryptoliq:{coin}:{stage}:{ts}"
        # Grafik ÖNCE üretilir: sığıyorsa tam metin resmin altyazısı olur → tek
        # mesaj (kullanıcı isteği). Resim yoksa / metin uzunsa / resim
        # reddedildiyse metin ayrı gider — alarm asla resme bağlı değil.
        png = None
        if getattr(cfg, "crypto_liq_chart", True):
            try:
                png = await _chart(client, coin, marks.get(coin), fresh, target=_target(casc),
                                   coverage_txt=cov_txt, far_pct=_far_pct(cfg))
            except Exception:
                log.debug("grafik üretilemedi (%s)", coin, exc_info=True)
        cap = (f"📈 <b>{fmt.esc(coin)}</b> · liq {fmt.px(fresh[0]['liq_px'])}"
               f" · %{fresh[0]['dist']:.2f} kaldı")
        sent, mode = await notifier.send_rich("cryptoliq", text, png, key=key, chat_id=chat, coin=coin,
                                              short_caption=cap, limit=CAPTION_MAX)
        if sent:
            for p in fresh:
                await _watch_upsert(coin, p["address"], p, p["dist"], p["mark"],
                                    stage=int(p["need"]),
                                    probed_ts=ts if p["address"] in probed_ok else None)
                if int(p["need"]) == 1:
                    await alert_log("cryptoliq", f"{coin}:{p['address']}", text)
                if int(p["need"]) >= 2:
                    out["stage2" if int(p["need"]) == 2 else "stage3"] += 1
            out["alerted"] += 1
            if mode in ("combined", "split"):
                out["photos"] += 1
            if mode == "combined":
                out["combined"] += 1
        else:
            await alert_log("fail:cryptoliq", key, text[:200])
            out["failed"] += 1

    # 7) Kapanış notları: bildirilmemiş, taze kapanışlar (coin başına tek mesaj).
    await _closure_notes(cfg, notifier, chat, ts, out)
    await _watch_prune()
    if out["alerted"] or out["dropped_stale"] or out["closed"]:
        log.info("kripto liq: %d aday, %d bildirim, %d kapanış, %d bayat düştü, %d gönderilemedi",
                 out["candidates"], out["alerted"], out["closed"], out["dropped_stale"], out["failed"])
    return await _stats(out)


async def _closure_notes(cfg, notifier, chat: str, ts: int, out: dict) -> None:
    """Bildirilmemiş taze kapanışlar → coin başına tek mesaj; ayar kapalıysa
    sessizce işaretlenir. Marker (`notified_ts`) yalnız gönderim başarılıysa."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT * FROM cryptoliq_watch WHERE closed_ts IS NOT NULL AND notified_ts IS NULL"
            " AND closed_ts >= ? AND stage >= 1 ORDER BY coin, closed_ts", (ts - CLOSE_NOTE_MAX_AGE,))
        pend = [dict(r) for r in await cur.fetchall()]
    if not pend:
        return
    if not getattr(cfg, "crypto_liq_notify_close", True):
        for r in pend:
            await _watch_set(r["coin"], r["address"], notified_ts=ts)
        return
    from ..telegram import format as fmt
    groups: dict[str, list[dict]] = {}
    for r in pend:
        groups.setdefault(r["coin"], []).append(r)
    for coin, rs in groups.items():
        text = fmt.crypto_liq_closed(coin, rs)
        key = f"cryptoliq_close:{coin}:{ts}"
        if await notifier.send("cryptoliq", text, priority="high", key=key, chat_id=chat, coin=coin):
            for r in rs:
                await _watch_set(coin, r["address"], notified_ts=ts)
            out["close_notes"] += 1
        else:
            await alert_log("fail:cryptoliq", key, text[:200])
            out["failed"] += 1


async def _stats(out: dict) -> dict:
    try:
        await kv_set("cryptoliq_stats", {**out, "ts": now()})
    except Exception:
        log.debug("cryptoliq_stats yazılamadı", exc_info=True)
    return out


async def loop(cfg, client, notifier) -> None:
    """Denetimli döngü. Site buna bağımlı değil: patlarsa yalnız bildirim durur
    ve /tani sebebini yazar."""
    from ..health import beat
    await asyncio.sleep(150)               # açılışta süpürme/metrik otursun
    while True:
        try:
            await beat("cryptoliq")
            await scan(cfg, client, notifier)
            await beat("cryptoliq")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("kripto liq turu hatası")
            await _stats({"error": f"{type(e).__name__}: {e}"[:200]})
        await asyncio.sleep(max(60, int(getattr(cfg, "crypto_liq_poll_sec", 120))))
