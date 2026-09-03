"""Tanı dökümü — sistemin kendi durumunu tek metin blokta anlatması.

NEDEN VAR: geliştirme, canlı sistemi göremeyen bir tarafla (ben) görebilen bir
taraf (sen) arasında yürüyor. Ekran görüntüsü eksik bir kanal — yalnız o sayfayı
gösterir, sayılar kırpıktır, hata metinleri kaybolur, Railway log'u hiç gelmez.
ALL_DEXES bugu tam bu yüzden günlerce saklandı.

Burası o kanalı kapatıyor: `/tani` çıktısı kopyalanıp yapıştırılır, karşı taraf
log okur gibi okur. Ekran görüntüsünden üstünlüğü eksiksiz, aranabilir ve
Railway'e gitmeye gerek bırakmaması.

SIR GÜVENLİĞİ: bu metin sohbete yapıştırılacak. Ayar bölümü `cfg.__dict__`
üzerinde DEĞİL, yalnız `EDITABLE_FIELDS` anahtarları üzerinde döner. Ev kuralı
gereği sırlar (bot token, AI anahtarı, pano jetonu, Finnhub anahtarı, yönetici
şifresi) `EDITABLE_FIELDS`'a hiç girmez — yani sızıntı TASARIM GEREĞİ imkânsız,
bir denylist'i güncellemeyi hatırlamaya bağlı değil.
"""
import logging
import os
import time
from collections import deque

from .db import db, kv_get, now

log = logging.getLogger("diag")

RING_MAX = 300          # bellekteki uyarı halkası
DEDUPE_SEC = 300        # aynı mesaj bu pencerede tekrarlarsa yeni satır değil, sayaç
LOG_RETENTION_D = 7
LOG_KEEP_ROWS = 500
SHOW_LOGS = 25          # normal dökümde gösterilen uyarı satırı
SHOW_LOGS_FULL = 120

# Büyük tabloların en eski/yeni kaydı da yazılır: "veri var mı" ile
# "veri TAZE mi" ayrı sorular, ikincisini satır sayısı cevaplamıyor.
SPAN_TABLES = ("fills", "asset_metrics", "positions_current", "hl_positions",
               "alerts_log", "ai_hypotheses")


# --------------------------------------------------------------- uyarı halkası

class RingHandler(logging.Handler):
    """WARNING ve üstünü bellekte tutar. `emit()` I/O YAPMAZ.

    SQLite'a senkron yazmak olay döngüsünü bloklardı ve — daha kötüsü — DB
    kilitliyken log basmak yeni bir hata doğururdu. Halka bellekte dolar,
    kalıcılaştırmayı `flush_logs()` yapar (bekçi turundan çağrılır).
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.ring: deque = deque(maxlen=RING_MAX)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                msg += f" | {type(exc).__name__}: {exc}"
            self.ring.append((int(record.created), record.name,
                              record.levelname, msg[:400]))
        except Exception:
            pass          # log yolunda patlamak, log tutmamaktan kötüdür


_handler: RingHandler | None = None


def install() -> RingHandler:
    """Kök logger'a tak (idempotent — çift takarsa satırlar ikişer olurdu)."""
    global _handler
    if _handler is None:
        _handler = RingHandler()
        logging.getLogger().addHandler(_handler)
    return _handler


async def flush_logs() -> int:
    """Halkayı `log_events`'e boşalt. Kalıcı olması ŞART: asıl merak edilen an
    yeniden başlatmadan HEMEN ÖNCE ne olduğu — yalnız bellekte tutulsa tam o an
    kaybolurdu."""
    if _handler is None or not _handler.ring:
        return 0
    items, n = list(_handler.ring), 0
    _handler.ring.clear()
    async with db() as conn:
        for ts, logger_name, level, msg in items:
            cur = await conn.execute(
                """SELECT id, n FROM log_events
                   WHERE logger=? AND level=? AND msg=? AND ts >= ?
                   ORDER BY ts DESC LIMIT 1""",
                (logger_name, level, msg, ts - DEDUPE_SEC))
            row = await cur.fetchone()
            if row:
                # Bir uyarı fırtınası tabloyu doldurmasın: satır değil sayaç artar.
                await conn.execute(
                    "UPDATE log_events SET n=n+1, ts=? WHERE id=?", (ts, row["id"]))
            else:
                await conn.execute(
                    "INSERT INTO log_events(ts,logger,level,msg,n) VALUES(?,?,?,?,1)",
                    (ts, logger_name, level, msg))
            n += 1
    return n


async def prune_logs() -> int:
    """Yaş + satır tavanı. İkisi birden: sessiz haftalarda yaş, fırtınada tavan."""
    async with db() as conn:
        cur = await conn.execute("DELETE FROM log_events WHERE ts < ?",
                                 (now() - LOG_RETENTION_D * 86400,))
        n = cur.rowcount or 0
        cur = await conn.execute(
            "DELETE FROM log_events WHERE id NOT IN"
            " (SELECT id FROM log_events ORDER BY ts DESC LIMIT ?)", (LOG_KEEP_ROWS,))
        return n + (cur.rowcount or 0)


# ------------------------------------------------------------------ yardımcılar

def _dur(sec: float | int | None) -> str:
    """Saniyeyi okunur süreye. '22dk önce' bir zaman damgasından hızlı okunur."""
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return "?"
    if s < 0:
        s = 0
    if s < 90:
        return f"{s}sn"
    if s < 5400:
        return f"{s // 60}dk"
    if s < 172800:
        return f"{s // 3600}s{(s % 3600) // 60:02d}dk"
    return f"{s // 86400}g{(s % 86400) // 3600}s"


def _clock(ts) -> str:
    try:
        from .earnings.calendar import TR
        from datetime import datetime
        return datetime.fromtimestamp(int(ts), TR).strftime("%d.%m %H:%M")
    except Exception:
        return "?"


def _num(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    if abs(f) >= 1_000_000:
        return f"{f / 1_000_000:.2f}M"
    if abs(f) >= 1_000:
        return f"{f / 1_000:.1f}K"
    return f"{f:.0f}" if f == int(f) else f"{f:.2f}"


async def _count(conn, table: str) -> int:
    cur = await conn.execute(f"SELECT COUNT(*) n FROM {table}")
    return (await cur.fetchone())["n"]


# --------------------------------------------------------------------- bölümler

async def _head(cfg) -> list[str]:
    from . import db as dbm
    boot = int(await kv_get("boot_ts") or 0)
    path = getattr(dbm, "_DB_PATH", "?")
    size = wal = 0
    try:
        size = os.path.getsize(path)
        wal = os.path.getsize(path + "-wal")
    except OSError:
        pass
    return [
        "HL INSIDER RADAR — TANI DÖKÜMÜ",
        f"üretildi {_clock(now())} TR · çalışma süresi "
        + (_dur(now() - boot) if boot else "?")
        + f" · DB {path} ({_num(size)}B, WAL {_num(wal)}B)",
    ]


async def _tasks(cfg) -> list[str]:
    from .health import periods, snapshot
    snap = await snapshot(cfg)
    per = periods(cfg)
    out = ["[GÖREVLER]  görev | son nabız | normal periyot | tolerans"]
    for name in sorted(snap["checks"]):
        c = snap["checks"][name]
        p = int(per.get(name) or 0)
        out.append(f"  {'✅' if c['ok'] else '❌'} {name:<12} {_dur(c['silent']):>6}"
                   f" | {_dur(p) if p else '—':>6} | {_dur(c['limit']):>6}"
                   + ("" if c["hb"] else "  (hiç atmadı — boot'a göre)"))
    if snap.get("crashes"):
        for name, rec in sorted(snap["crashes"].items()):
            out.append(f"  ⚠️ ÇÖKME {name}: ×{rec.get('n', '?')}"
                       f" son {_clock(rec.get('ts'))} — {str(rec.get('err'))[:150]}")
    if snap.get("silent_coins"):
        out.append("  ⚠️ sessiz coin: "
                   + ", ".join(q["symbol"] for q in snap["silent_coins"][:12]))
    if snap.get("problems"):
        out.append("  SORUNLU: " + ", ".join(snap["problems"]))
    return out


async def _coverage(cfg, state) -> list[str]:
    async with db() as conn:
        n_tick = await _count(conn, "tickers")
        n_addr = await _count(conn, "addresses")
        cur = await conn.execute("SELECT COUNT(*) n FROM addresses WHERE watchlist=1")
        n_watch = (await cur.fetchone())["n"]
    out = ["[KAPSAM]",
           f"  tickers {n_tick} · adres havuzu {n_addr} · watchlist {n_watch}"]
    coll = getattr(state, "collector", None) if state is not None else None
    if coll:
        out.append(f"  WS {'bağlı' if coll.connected else 'KOPUK'}"
                   f" · abone {len(coll.subscribed)} coin"
                   f" ({len(coll.subscribed) - len(coll.crypto_coins)} hisse"
                   f" + {len(coll.crypto_coins)} kripto tetiği)")
        if getattr(coll, "crypto_err", ""):
            out.append(f"  ⚠️ kripto tetiği KAPALI: {coll.crypto_err}")
        out.append(f"  sonda: {coll.probes_ok}✓ / {coll.probes_err}✗"
                   f" / {coll.probes_skipped} atlandı / {len(coll._probing)} uçuşta")
    else:
        out.append("  (collector yok — web tek başına mı çalışıyor?)")
    return out


async def _data() -> list[str]:
    from .db import SCHEMA
    import re
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA)
    out = ["[VERİ]  tablo=satır (en eski→en yeni kayıt)"]
    async with db() as conn:
        line: list[str] = []
        for t in tables:
            try:
                n = await _count(conn, t)
            except Exception:
                continue
            txt = f"{t}={_num(n)}"
            if t in SPAN_TABLES and n:
                try:
                    cur = await conn.execute(f"SELECT MIN(ts) a, MAX(ts) b FROM {t}")
                    r = await cur.fetchone()
                    if r and r["a"]:
                        txt += f" ({_dur(now() - int(r['a']))}→{_dur(now() - int(r['b']))} önce)"
                except Exception:
                    pass
            line.append(txt)
        for i in range(0, len(line), 3):
            out.append("  " + " · ".join(line[i:i + 3]))
        cur = await conn.execute(
            "SELECT COUNT(*) n FROM hl_positions WHERE closed_ts IS NULL")
        out.append(f"  hl_positions açık: {(await cur.fetchone())['n']}")
    return out


async def _subsystems(cfg) -> list[str]:
    out = ["[ALT SİSTEMLER]"]
    sw = await kv_get("sweep_stats") or {}
    if sw:
        last_full = await kv_get("sweep_last_full")
        out.append(f"  derin keşif: sıcak {sw.get('hot')} / soğuk {sw.get('cold')}"
                   f" · tur ~{sw.get('tour_min', '?')}dk (soğuk ~{sw.get('cold_min', '?')}dk)"
                   f" · parti {sw.get('batch')} → {sw.get('ok', 0)}✓/{sw.get('err', 0)}✗"
                   f" · {sw.get('batch_positions', 0)} poz")
        out.append(f"       son tam tur "
                   + (f"{_dur(now() - int(last_full))} önce" if last_full else "YOK (ilk tur sürüyor)")
                   + (f" · hl yazma hatası {sw['hl_err']}" if sw.get("hl_err") else "")
                   + (f"\n       ⚠️ HATA: {sw['err_msg']}" if sw.get("err_msg") else ""))
    # Bakiye kapsaması: kolon boşsa sebebi "bozuk" mu "henüz uğramadık" mı,
    # ancak bu sayı söyler.
    try:
        from .radar.sweeper import account_coverage
        ac = await account_coverage()
        out.append(f"  cüzdan bakiyesi: {_num(ac['known'])}/{_num(ac['n'])} adreste var"
                   f" · {_num(ac['stale'])} tanesi 3 saatten eski"
                   + (f" · son ölçüm {_dur(now() - int(ac['newest']))} önce"
                      if ac.get("newest") else " · HİÇ ÖLÇÜM YOK")
                   + (f" · bu turda {sw['acct_written']} yazıldı"
                      if sw.get("acct_written") is not None else ""))
    except Exception as e:
        out.append(f"  cüzdan bakiyesi okunamadı ({type(e).__name__}: {e})")
    pf = await kv_get("hl_prime_fail_ts")
    if pf:
        out.append(f"  ⚠️ dev tarama (prime) hatada — {_dur(now() - int(pf))} önce damgalandı")
    wl = await kv_get("wall_stats") or {}
    if wl:
        out.append(f"  duvar radarı: {wl.get('coins', '?')} coin"
                   f" · {wl.get('alerts', 0)} alarm"
                   + (f" · ⚠️ {wl['book_err']} defter hatası: {wl.get('err_msg', '')}"
                      if wl.get("book_err") else " · defter hatası yok"))
    for kv_key, lbl, chat_env in (("cryptovol_stats", "kripto hacim", "CRYPTO_CHAT_ID"),
                                  ("equityvol_stats", "hisse hacim", "CRYPTO_STOCKS_ID")):
        vs = await kv_get(kv_key) or {}
        if not vs:
            continue
        miss = vs.get("best_miss") or {}
        out.append(f"  {lbl}: {vs.get('coins', '?')} sembol"
                   f" · {vs.get('checked', 0)} tarandı"
                   f" · {vs.get('n_bucket', 0)} kova → {vs.get('n_record', 0)} rekor"
                   f" · {vs.get('events', 0)} yazıldı / {vs.get('alerted', 0)} bildirim"
                   # "0 rekor"un sebebini ayırt eden üç sayı; biri olmadan
                   # eşik mi veri mi sorusu cevapsız kalıyordu.
                   + (f" · ⚠️ {vs['n_nodata']} sembolde MUM YOK" if vs.get("n_nodata") else "")
                   + (f" · {vs['below_page']} rekor sayfa eşiği altında"
                      + (f" (en büyüğü {miss.get('sym')} {_num(miss.get('notional'))}$)"
                         if miss else "") if vs.get("below_page") else "")
                   + (f" · {vs['below_alert']} rekor bildirim eşiği altında"
                      if vs.get("below_alert") else "")
                   + (f" · ⚠️ {vs['err']} mum hatası" if vs.get("err") else "")
                   + ("" if vs.get("chat") else f" · ⚠️ {chat_env} TANIMSIZ (gönderilmiyor)")
                   + (f" · ⚠️ birim şüpheli: {', '.join(vs['unit_bad'][:5])}"
                      if vs.get("unit_bad") else "")
                   + (f"\n       ⏸ {vs['skipped']}" if vs.get("skipped") else ""))
        # PROPR listesi elle tutuluyor ve eskiyor — kaç sembolü kaçırdığımız
        # görünmezse "neden bildirim gelmedi" sorusu bir daha cevapsız kalır.
        if vs.get("n_missing"):
            out.append(f"       📋 PROPR listesinde OLMAYAN {vs['n_missing']} perp"
                       f" taranmıyor: {', '.join(vs.get('missing', [])[:15])}")
    bs = await kv_get("bars_stats") or {}
    if bs:
        out.append(f"  mum arşivi: {bs.get('coins', '?')} sembol"
                   f" · {_num(bs.get('bars'))} bar · {bs.get('deep', 0)} seri 500+"
                   + (f" · ⚠️ {bs['n_empty']} sembolde arşiv sığ/yok"
                      if bs.get("n_empty") else "")
                   + (f" · ⚠️ {bs['err']} hata: {bs.get('err_msg', '')}"
                      if bs.get("err") else ""))
    ps = await kv_get("patterns_stats") or {}
    if ps:
        best = ps.get("best") or {}
        out.append(f"  örüntü: {ps.get('checked', 0)} çözümleme"
                   f" · {ps.get('signals', 0)} sinyal / {ps.get('strong', 0)} güçlü"
                   f" / {ps.get('alerted', 0)} bildirim"
                   + (f" · {ps['thin']} yetersiz örnek" if ps.get("thin") else "")
                   # Yapısal yetersizlik ayrı sayılıyor: bu, eşiğin değil
                   # arşiv/havuz seçiminin sonucu ve çaresi başka.
                   + (f" ({ps['structural']}'i YAPISAL — havuz dar)"
                      if ps.get("structural") else "")
                   + ("" if ps.get("chat") else " · ⚠️ PATTERN_CHAT_ID TANIMSIZ")
                   + (f"\n       en güçlü: {best.get('coin', '')}"
                      f" {best.get('tf', '')} {best.get('horizon', '')}bar"
                      f" z={best.get('z', 0):.1f} fark {best.get('edge', 0):+.0f}p"
                      if best else ""))
        try:
            from .radar.patterns import record as prec
            r = await prec()
            out.append(f"       karne: {r['hit']}✓/{r['miss']}✗/{r['open']} açık"
                       + (f" · isabet %{r['rate']}" if r["rate"] is not None
                          else " · henüz kapanmış tahmin yok"))
        except Exception as e:
            out.append(f"       karne okunamadı ({type(e).__name__}: {e})")
    # "Ne oldu" sekmesinin yakıtı: kripto fill kaydı ve ana dex OI örneği.
    # İkisi de yeni açıldı; çalışıp çalışmadığı buradan görünsün.
    try:
        from .db import db as _db
        async with _db() as c:
            cur = await c.execute(
                "SELECT COUNT(*) n, MIN(ts) a, MAX(ts) b FROM fills"
                " WHERE coin NOT LIKE '%:%'")
            f = dict(await cur.fetchone())
            cur = await c.execute(
                "SELECT COUNT(DISTINCT coin) n, MAX(ts) b FROM asset_metrics"
                " WHERE coin NOT LIKE '%:%'")
            mt = dict(await cur.fetchone())
        out.append(f"  ne oldu (adli): kripto fill {_num(f['n'])} satır"
                   + (f" ({_dur(now() - int(f['a']))} önce başladı)" if f["a"]
                      else " — HENÜZ HİÇ YOK")
                   + f" · ana dex OI {mt['n']} coin"
                   + (f", son örnek {_dur(now() - int(mt['b']))} önce" if mt["b"]
                      else " — HENÜZ HİÇ YOK"))
    except Exception as e:
        out.append(f"  ne oldu (adli) okunamadı ({type(e).__name__}: {e})")
    # Dinleme evreni 30 → 120'ye çıktı ve yakalama tabanı $5K: fill büyümesi
    # buradan izlenir. Şişerse çare tabanı yükseltmek ya da saklamayı kısmak.
    try:
        from .db import db as _db
        async with _db() as c:
            cur = await c.execute(
                "SELECT COUNT(*) n FROM fills WHERE ts >= ?", (now() - 3600,))
            per_h = (await cur.fetchone())["n"]
            cur = await c.execute(
                "SELECT COUNT(*) n FROM fills WHERE ts >= ? AND coin NOT LIKE"
                " '%:%'", (now() - 3600,))
            cr_h = (await cur.fetchone())["n"]
        u = await kv_get("ws_universe") or {}
        n_cr = len(u.get("crypto") or [])
        out.append(f"  fill akışı: son 1 saatte {_num(per_h)} satır"
                   f" ({_num(cr_h)} kripto)"
                   + (f" · dinlenen {n_cr} kripto + {u.get('equity_n', 0)} hisse"
                      f", {_dur(now() - int(u['ts']))} önce yazıldı"
                      if u.get("ts") else
                      " · dinleme evreni HENÜZ YAZILMADI — alarmlarda 'kim ne"
                      " aldı' kırılımı 'bilinmiyor' der"))
    except Exception as e:
        out.append(f"  fill akışı okunamadı ({type(e).__name__}: {e})")
    tw = await kv_get("twap_stats") or {}
    if tw:
        best = tw.get("best") or {}
        out.append(f"  twap: {_num(tw.get('groups'))} dizi tarandı"
                   f" · {tw.get('detected', 0)} düzenli"
                   f" · {tw.get('big', 0)} eşik üstü"
                   + (f" · {tw['skipped_mm']} mm/vault elendi"
                      if tw.get("skipped_mm") else "")
                   + (f"\n       en büyüğü: {best.get('coin', '')} {best.get('side', '')}"
                      f" {_num(best.get('total'))}$ / {best.get('n')} dilim"
                      if best else ""))
    hv = await kv_get("harvest_stats") or {}
    if hv.get("total"):
        out.append(f"  işlem hasadı: {_num(hv['total'])} fill REST'ten toplandı")
    cal = await kv_get("calendar_stats") or {}
    if cal:
        out.append(f"  takvim: yahoo={cal.get('yahoo')} tv={cal.get('tradingview')}"
                   f" finnhub={cal.get('finnhub')} nasdaq={cal.get('nasdaq')}"
                   f" → birleşik {cal.get('merged')} (tv http {cal.get('tv_http')})")
    try:
        from .ai import analyst
        rec = await analyst.record()
        bud = await analyst.budget_state(cfg)
        out.append(f"  AI: {rec['hit']}✓/{rec['miss']}✗/{rec['open']} açık"
                   f"/{rec['unresolvable']} ölçülemedi"
                   + (f" · isabet %{rec['rate']}" if rec["rate"] is not None else "")
                   + f" · bütçe {bud['tokens']}/{bud['cap']} ({bud['calls']} çağrı)")
        last = rec.get("last_run")
        if last:
            out.append(f"       son tur {_dur(now() - int(last['ts']))} önce"
                       f" {'✓' if last['ok'] else '✗'}"
                       + (f" — {last['err']}" if last.get("err") else ""))
    except Exception as e:
        out.append(f"  AI: okunamadı ({type(e).__name__}: {e})")
    return out


def _settings(cfg, full: bool) -> list[str]:
    """Panelden değiştirilen ayarlar.

    `cfg.__dict__` GEZİLMEZ — sırlar oradadır. `cfg.overrides` zaten yalnız
    `EDITABLE_FIELDS`'tan geçmiş adları tutar (`Config.apply_overrides`), ev
    kuralı gereği orada sır yoktur. Yani sızıntı TASARIMI GEREĞİ imkânsız, bir
    denylist'i güncellemeyi hatırlamaya bağlı değil.

    Varsayılanla karşılaştırma için `Config.env_default()` — bu iş için zaten
    yazılmış yardımcı; ikinci bir "varsayılanları hesapla" yolu açmıyoruz.
    """
    from .config import EDITABLE_FIELDS
    ov = dict(getattr(cfg, "overrides", {}) or {})
    out = ["[DEĞİŞTİRİLEN AYARLAR] (panelden kaydedilenler)"]
    shown = 0
    for name in sorted(ov):
        if name not in EDITABLE_FIELDS:
            continue                      # panelde olmayan ad asla yazılmaz
        try:
            d = cfg.env_default(name)
        except Exception:
            d = "?"
        out.append(f"  {name} = {getattr(cfg, name, '?')}   (varsayılan {d})")
        shown += 1
    if not shown:
        out.append("  (yok — hepsi env/kod varsayılanı)")
    if full:
        out.append("  --- tüm ayarlar ---")
        for name in EDITABLE_FIELDS:
            out.append(f"  {name} = {getattr(cfg, name, '?')}")
    return out


async def _logs(full: bool) -> list[str]:
    limit = SHOW_LOGS_FULL if full else SHOW_LOGS
    out = [f"[SON UYARI/HATALAR] (son {limit}, tekrarlar ×N)"]
    try:
        async with db() as conn:
            cur = await conn.execute(
                "SELECT ts,logger,level,msg,n FROM log_events ORDER BY ts DESC LIMIT ?",
                (limit,))
            rows = [dict(r) for r in await cur.fetchall()]
    except Exception as e:
        return out + [f"  (okunamadı: {type(e).__name__}: {e})"]
    if not rows:
        return out + ["  (temiz — kayıtlı uyarı yok)"]
    for r in rows:
        mark = f" ×{r['n']}" if (r["n"] or 1) > 1 else ""
        out.append(f"  {_clock(r['ts'])}{mark} {r['level'][:4]} {r['logger']}: {r['msg']}")
    return out


# ------------------------------------------------------------------------ rapor

async def report(cfg, state=None, full: bool = False) -> str:
    """Tam döküm. Hiçbir bölüm diğerini düşüremez: biri patlarsa yerine hata
    satırı yazılır — yarısı gelen bir tanı, hiç gelmeyenden iyidir."""
    # Önce halkayı boşalt: bekçi 120 sn'de bir yazıyor, açılışta 240 sn bekliyor.
    # O aralıkta üretilen uyarılar — yani açılış çöküşünü AÇIKLAYACAK olanlar —
    # henüz diskte değil. Döküm istendiği an onları da kalıcılaştır.
    try:
        await flush_logs()
    except Exception:
        pass                      # tanı üretmek, tanıyı kaydetmekten önceliklidir
    parts: list[list[str]] = []
    sections = (("başlık", _head(cfg)), ("görevler", _tasks(cfg)),
                ("kapsam", _coverage(cfg, state)), ("veri", _data()),
                ("alt sistemler", _subsystems(cfg)))
    for name, coro in sections:
        try:
            parts.append(await coro)
        except Exception as e:
            parts.append([f"[{name.upper()}] okunamadı: {type(e).__name__}: {e}"])
    try:
        parts.append(_settings(cfg, full))
    except Exception as e:
        parts.append([f"[AYARLAR] okunamadı: {type(e).__name__}: {e}"])
    try:
        parts.append(await _logs(full))
    except Exception as e:
        parts.append([f"[LOG] okunamadı: {type(e).__name__}: {e}"])
    return "\n\n".join("\n".join(p) for p in parts if p) + "\n"
