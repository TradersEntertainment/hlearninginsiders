"""Derin keşif süpürücüsü — eksik pozisyon verisini kapatır.

HL'de "bu marketteki tüm pozisyonlar" API'si yok; yalnız TANIDIĞIMIZ
adreslerin clearinghouse'u sorgulanabilir. Bot açılmadan önce pozisyon açmış
ve o günden beri işlem yapmamış balinalar WS akışına hiç düşmez — PLTR'de
"en büyük poz $1.26M" yanılgısı buradan çıktı.

İki sürekli akışla kapatılır:
1) Adres süpürmesi: sıcak havuz (watchlist + pozisyon sahipleri + leaderboard)
   dönüşümlü olarak her dex için sorgulanır; TÜM hisse pozisyonları
   positions_current'a işlenir, kapananlar silinir. Sıcak tam tur ~75-80 dk;
   soğuk kuyruk (yalnız fill'de görülen adresler) günler içinde döner.
2) recentTrades hasadı: WS kopukluklarının kaçırdığı işlemler REST'ten
   toplanır (users alanı) → adres havuzu ve zaman çizelgeleri beslenir.
"""
import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from ..config import Config
from ..db import db, kv_get, kv_set, now
from ..hl.client import HLClient
from ..hl.universe import symbol_of
from ..telegram import format as fmt
from .liqwatch import _iter_states
from .scanner import _leaderboard_addrs

log = logging.getLogger("radar.sweeper")

# Bu turda kaç adrese bakiye yazıldı — sweep_stats'a düşüyor. Bakiye kolonu
# boş kalırsa "yazılmıyor mu, adres mi görülmedi" sorusu buradan cevaplanır.
_acct_written = [0]
# Bakiye ölçümü bundan eskiyse arayüzde ⏳ ile işaretlenir. Derin keşif turu
# 75-125 dakika sürdüğü için 3 saat makul bir "hâlâ temsili" sınırı.
ACCOUNT_STALE_SEC = 3 * 3600

TR = ZoneInfo("Europe/Istanbul")

HARVEST_COINS_PER_CYCLE = 6   # tur başına recentTrades çekilecek coin sayısı
HOT_PER_BATCH = 30            # parti başına sıcak havuz adedi
COLD_PER_BATCH = 10           # parti başına soğuk havuz adedi (uzun kuyruk)
SPEC_INTERVAL = 600           # uzman paneli önbelleği tazeleme aralığı (sn)
METRICS_RETENTION_D = 45      # asset_metrics emekliliği (evaluator ≤7 gün bakar)
ALERTS_RETENTION_D = 30       # alerts_log emekliliği (en uzun cooldown 7 gün)
AI_RETENTION_D = 60           # ai_runs/ai_observations emekliliği (hipotezler KALICI)


async def build_pools(cfg: Config, client: HLClient) -> tuple[list[str], list[str]]:
    """(sıcak, soğuk) havuzlar.

    Sıcak: pozisyon güncelliğinin geldiği yer — watchlist + mevcut pozisyon
    sahipleri + leaderboard. Küçük kalır, tur ~1 saatte döner.
    Soğuk: yakın zamanda işlem yapmış ama pozisyonu bilinmeyen adresler —
    uzun kuyruk, günler içinde döner (yenilerini WS/hasat zaten yakalar).
    """
    lb = await _leaderboard_addrs(client, cfg.sweep_leaderboard_top)
    since = now() - cfg.fills_lookback_days * 86400
    async with db() as conn:
        # SIRA ÖNEMLİ: eskiden düz DISTINCT'ti, yani soğuk havuz rastgele
        # sırayla geziliyordu ve imleç her partide yeniden kurulan bir listeyi
        # indeksliyordu — bir adrese hiç sıra gelmeyebiliyordu. Son işlem
        # yapana öncelik veriyoruz: adli raporlarda karşımıza çıkanlar onlar.
        cur = await conn.execute(
            "SELECT address FROM fills WHERE ts >= ? GROUP BY address"
            " ORDER BY MAX(ts) DESC", (since,))
        traded = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT DISTINCT address FROM positions_current")
        holders = [r["address"] for r in await cur.fetchall()]
        cur = await conn.execute("SELECT address FROM addresses WHERE watchlist=1")
        watch = [r["address"] for r in await cur.fetchall()]
    hot, seen = [], set()
    for group in (watch, holders, lb):
        for a in group:
            a = (a or "").lower()
            if a and a not in seen:
                seen.add(a)
                hot.append(a)
    cold = []
    for a in traded:
        a = (a or "").lower()
        if a and a not in seen:
            seen.add(a)
            cold.append(a)
    return hot, cold


def parse_account_value(resp) -> float | None:
    """Tüm dex'lerdeki `marginSummary.accountValue` toplamı — HL perp teminatı.

    BU "NET WORTH" DEĞİL: spot bakiyesi, vault'lar ve zincir dışı varlıklar
    dahil değil. Arayüz de öyle etiketliyor.

    None döner ki 0 YAZILMASIN: "bilmiyoruz" ile "hesap boş" apayrı şeyler ve
    0 yazmak pozisyon/bakiye oranını sonsuza götürürdü.
    """
    total, seen = 0.0, False
    for state in _iter_states(resp):
        ms = state.get("marginSummary")
        if not isinstance(ms, dict):
            continue
        try:
            total += float(ms.get("accountValue") or 0)
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen else None


def _parse_equity_positions(resp, coin_set: set[str],
                            sym_map: dict[str, str],
                            min_ntl: float) -> tuple[dict[str, dict], bool]:
    """Çok-dex yanıtından hisse pozisyonları: (coin -> pozisyon, yanıt geçerli mi).
    Yanıtta hiç state yoksa 'geçersiz' döner — bozuk yanıtla kayıt SİLİNMEZ."""
    out: dict[str, dict] = {}
    exact: set[str] = set()  # doğrudan coin_set eşleşmesiyle yazılanlar
    states = list(_iter_states(resp))
    for state in states:
        for ap in state.get("assetPositions") or []:
            pos = ap.get("position") or {}
            pcoin = pos.get("coin") or ""
            is_exact = pcoin in coin_set
            coin = pcoin if is_exact else sym_map.get(symbol_of(pcoin), "")
            if not coin:
                continue
            try:
                szi = float(pos.get("szi") or 0)
                ntl = float(pos.get("positionValue") or 0)
            except (TypeError, ValueError):
                continue
            if szi == 0 or ntl < min_ntl:
                continue
            # İki dex aynı sembolü tutuyorsa (ör. xyz:TSLA + abc:TSLA) sym_map ikisini
            # de aynı coin'e eşliyor → son gelen üsttekini eziyordu (yön/boyut
            # yanlış). Doğrudan coin eşleşmesi (exact) korunur; sembol eşleşmeleri
            # arasında EN BÜYÜK notional kazanır (deterministik, dict sırasına bağlı değil).
            if coin in out:
                if coin in exact and not is_exact:
                    continue  # exact kaydı sembol-eşleşmesi ezemez
                if is_exact and coin not in exact:
                    pass       # exact, önceki sembol-eşleşmesini ezer
                elif ntl <= out[coin]["notional"]:
                    continue   # aynı sınıf → büyük olan kalır
            liq = pos.get("liquidationPx")
            out[coin] = {
                "szi": szi, "side": "long" if szi > 0 else "short",
                "entry_px": float(pos.get("entryPx") or 0),
                "leverage": float((pos.get("leverage") or {}).get("value") or 0),
                "liq_px": float(liq) if liq else None,
                "upnl": float(pos.get("unrealizedPnl") or 0),
                "notional": ntl,
            }
            if is_exact:
                exact.add(coin)
    return out, bool(states)


def _dexes(cfg: Config) -> list[str]:
    """Sorgulanacak dex'ler: ana dex ("") + tüm HIP-3 hisse dex'leri."""
    return ["", *cfg.equity_dexes]


def _adaptive_batch(cfg: Config, client) -> tuple[int, dict]:
    """Bu partide kaç adres taransın — BOŞTAKİ istek bütçesine göre.

    Sabit parti (varsayılan 40 adres) bütçenin ancak ~%15'ini kullanıyordu;
    havuz 2900 sıcak + 14700 soğuk adresken ilk tam tur saatler sürüyor,
    "veride çok gerideyiz" hissi buradan geliyordu.

    Yetişme modunda süpürücü artan bütçeyi alır: diğer görevler sessizken
    hızlanır, likidasyon radarı gibi ağır bir görev koşarken KENDİLİĞİNDEN
    yavaşlar. İstemcinin küresel bütçesi zaten istekleri sıraya koyuyor, yani
    burada aşırıya kaçmak 429 üretmez — sadece partiyi uzatır; yine de tavan
    koyuyoruz ki bir parti diğer görevleri aç bırakmasın.

    Ölçüm penceresi (60 sn) parti aralığından (90 sn) kısa olduğu için, ölçüm
    anındaki kullanım pratikte DİĞER görevlerin kullanımıdır — kendi önceki
    partimiz pencereden çıkmış olur. Aralık pencereden kısaysa bu varsayım
    bozulur, o yüzden yetişme kapanır.
    """
    base = max(1, int(cfg.sweep_batch_size))
    if not getattr(cfg, "sweep_catchup", True) or client is None:
        return base, {}
    try:
        u = client.usage()
    except Exception:
        return base, {}          # istemci bütçe bildirmiyorsa sabit partiye dön
    if cfg.sweep_interval_sec < u.get("window", 60):
        return base, {}
    headroom = float(getattr(cfg, "sweep_rpm_headroom", 0.85))
    ceiling = u["max"] * max(0.1, min(headroom, 1.0))
    allow_rpm = max(0.0, ceiling - u["rpm"])          # bize kalan istek/dk
    per_addr = max(1, len(_dexes(cfg)))               # adres başına istek
    n = int(allow_rpm * (cfg.sweep_interval_sec / 60.0) / per_addr)
    cap = max(base, int(getattr(cfg, "sweep_batch_max", 250)))
    return max(base, min(n, cap)), {"rpm": u["rpm"], "rpm_max": u["max"]}


def _hl_floor(cfg: Config):
    """coin -> alt sınır çözücüsü (kademeli: HIP-3 / BTC-ETH / diğer kripto)."""
    from .bigpos import threshold
    return lambda coin: threshold(coin, cfg)


def _parse_all_positions(resp, min_ntl) -> dict[str, dict]:
    """Çok-dex yanıtından TÜM pozisyonlar — hisse filtresi YOK.

    `_parse_equity_positions` hisse evreninde olmayan her coin'i eliyor; yani
    BTC/ETH gibi ana dex pozisyonları her taramada elimize gelip ÇÖPE gidiyordu.
    Burada eşiği aşan hepsi tutulur — ek API isteği yok, aynı yanıt.
    Anahtar ham coin (ör. "BTC", "xyz:NVDA"); sembol eşlemesi yapılmaz, çünkü
    burada amaç "hangi hisse" değil "hangi pozisyon".

    `min_ntl` sayı YA DA `coin -> alt sınır` çağrılabiliri olabilir: ana dex'in
    "büyük" tanımı hisse tarafından çok farklı (BTC'de $1M gürültü), o yüzden
    eşik coin başına çözülebiliyor. Kapanma tespitinde 0 geçilir (bkz.
    `_upsert_hl` `held`), yani eşik altına gerileyen "kapandı" sayılmaz.
    """
    floor = min_ntl if callable(min_ntl) else (lambda _c: float(min_ntl))
    out: dict[str, dict] = {}
    for state in _iter_states(resp):
        for ap in state.get("assetPositions") or []:
            pos = ap.get("position") or {}
            coin = pos.get("coin") or ""
            if not coin:
                continue
            try:
                szi = float(pos.get("szi") or 0)
                ntl = float(pos.get("positionValue") or 0)
            except (TypeError, ValueError):
                continue
            if szi == 0 or ntl < floor(coin):
                continue
            if coin in out and ntl <= out[coin]["notional"]:
                continue                      # aynı coin iki state'te → büyük olan
            liq = pos.get("liquidationPx")
            out[coin] = {
                "dex": coin.split(":")[0] if ":" in coin else "",
                "szi": szi, "side": "long" if szi > 0 else "short",
                "entry_px": float(pos.get("entryPx") or 0),
                "leverage": float((pos.get("leverage") or {}).get("value") or 0),
                "liq_px": float(liq) if liq else None,
                "upnl": float(pos.get("unrealizedPnl") or 0),
                "notional": ntl,
            }
    return out


async def _upsert_hl(addr: str, positions: dict[str, dict], ts: int,
                     held: set[str] | None = None) -> None:
    """hl_positions'a yaz: zirve YALNIZ büyürse güncellenir, kapanan SİLİNMEZ.

    Rekor arşivi ("gördüğümüz en büyükler") bu yüzden var: positions_current
    kapanınca satırı siliyor, oradan geçmiş geri getirilemiyor.

    `held` = adresin ŞU AN tuttuğu TÜM coin'ler (boyut gözetmeksizin). Kapanma
    kararı buna göre verilir: yalnız eşik üstü sözlüğe bakılırsa $1.05M'lik bir
    pozisyon $0.99M'ye gerileyince listeden düşüp "KAPANDI" damgası yiyordu —
    kripto oynaklığında bu sürekli olur ve arşivi yalan söyletirdi. `held`
    verilmezse eski davranış (yalnız yazılanlar açık sayılır).
    """
    open_coins = held if held is not None else set(positions)
    async with db() as conn:
        for coin, p in positions.items():
            await conn.execute(
                """INSERT INTO hl_positions
                   (coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,notional,
                    ts,first_seen_ts,peak_notional,peak_ts,closed_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(coin,address) DO UPDATE SET
                     ts=excluded.ts, side=excluded.side, szi=excluded.szi,
                     entry_px=excluded.entry_px, leverage=excluded.leverage,
                     liq_px=excluded.liq_px, upnl=excluded.upnl,
                     notional=excluded.notional, dex=excluded.dex,
                     closed_ts=NULL,
                     first_seen_ts=MIN(COALESCE(hl_positions.first_seen_ts,
                                                excluded.first_seen_ts),
                                       excluded.first_seen_ts),
                     peak_ts=CASE WHEN excluded.notional >
                                       COALESCE(hl_positions.peak_notional, 0)
                                  THEN excluded.peak_ts ELSE hl_positions.peak_ts END,
                     peak_notional=MAX(COALESCE(hl_positions.peak_notional, 0),
                                       excluded.notional)""",
                (coin, addr, p["dex"], p["side"], p["szi"], p["entry_px"],
                 p["leverage"], p["liq_px"], p["upnl"], p["notional"],
                 ts, ts, p["notional"], ts))
        # Adresin artık tutmadıkları: SİLME, kapandı damgası vur (rekor kalsın).
        # Ölçüt eşik ÜSTÜ değil, GERÇEKTEN tutulan coin listesi (yukarıdaki not).
        if open_coins:
            q = ",".join("?" * len(open_coins))
            await conn.execute(
                f"UPDATE hl_positions SET closed_ts=? WHERE address=? AND closed_ts IS NULL"
                f" AND coin NOT IN ({q})", (ts, addr, *open_coins))
        else:
            await conn.execute(
                "UPDATE hl_positions SET closed_ts=? WHERE address=? AND closed_ts IS NULL",
                (ts, addr))


async def _upsert_addr_pos(addr: str, positions: dict[str, dict],
                           ts: int) -> None:
    """addr_positions'a yaz: SON BİLİNEN pozisyon, boyut gözetmeksizin.

    `hl_positions` kademe altını hiç yazmadığı için "ne oldu" raporunda
    adreslerin çoğu "bilinmiyor" çıkıyordu — oysa defterlerini çekmiştik,
    sadece $1M'ın altında tuttukları için attık. Burası o boşluğu doldurur.

    `positions` EŞİKSİZ liste (`_parse_all_positions(resp, 0)`) olmalı; zaten
    kapanma tespiti için hesaplanıyor, ek maliyet yok.

    Kapanan SİLİNMEZ, damgalanır: "pozisyonu kapattı" etiketi buna dayanıyor
    ve silinen satırdan geçmiş geri getirilemez.
    """
    rows = [(coin, addr, p["dex"], p["side"], p["szi"], p["entry_px"],
             p["leverage"], p["liq_px"], p["upnl"], p["notional"], ts)
            for coin, p in positions.items()]
    async with db() as conn:
        if rows:
            # executemany: bir adres onlarca pozisyon taşıyabilir ve bu blok
            # süpürme partisinin sıcak yolunda.
            await conn.executemany(
                """INSERT INTO addr_positions
                   (coin,address,dex,side,szi,entry_px,leverage,liq_px,upnl,
                    notional,ts,closed_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)
                   ON CONFLICT(coin,address) DO UPDATE SET
                     dex=excluded.dex, side=excluded.side, szi=excluded.szi,
                     entry_px=excluded.entry_px, leverage=excluded.leverage,
                     liq_px=excluded.liq_px, upnl=excluded.upnl,
                     notional=excluded.notional, ts=excluded.ts,
                     closed_ts=NULL""", rows)
        # Adresin artık tutmadıkları: kapandı damgası. Liste EŞİKSİZ olduğu için
        # burada "eşik altına gerilemiş" diye yanlış kapatma riski YOK.
        if positions:
            q = ",".join("?" * len(positions))
            await conn.execute(
                f"UPDATE addr_positions SET closed_ts=? WHERE address=?"
                f" AND closed_ts IS NULL AND coin NOT IN ({q})",
                (ts, addr, *positions))
        else:
            await conn.execute(
                "UPDATE addr_positions SET closed_ts=? WHERE address=?"
                " AND closed_ts IS NULL", (ts, addr))
    async with db() as conn:
        # Defteri GERÇEKTEN çektik. "hiç uğramadık" ile "uğradık, bu coinde
        # pozisyonu yok"u ayıran işaret bu.
        await conn.execute(
            "INSERT INTO addresses(address, first_seen, probed_ts) VALUES(?,?,?)"
            " ON CONFLICT(address) DO UPDATE SET probed_ts=excluded.probed_ts",
            (addr, ts, ts))


async def prune_addr_positions(days: int) -> int:
    """Tazelenmeyeni at. Saklama `fills_retention_days` ile AYNI: işlem kaydı
    olmayan bir pencere için pozisyon fotoğrafı tutmanın anlamı yok."""
    async with db() as conn:
        cur = await conn.execute(
            "DELETE FROM addr_positions WHERE ts < ?", (now() - int(days) * 86400,))
        return cur.rowcount or 0


async def upsert_account_value(addr: str, value: float | None, ts: int,
                               force: bool = True) -> bool:
    """Bakiyeyi yaz. `force=False` ise YALNIZ mevcut ölçüm daha eskiyse yazar.

    Öncelik kuralı: clearinghouse ölçümü tüm dex'leri kapsadığı için otoriter
    (force=True); leaderboard değeri yalnız ana dex'i bildiği için ancak daha
    taze olduğunda ya da hiç ölçüm yokken yazılır (force=False).
    """
    if value is None:
        return False
    q = ("UPDATE addresses SET account_value=?, account_ts=? WHERE address=?"
         + ("" if force else " AND COALESCE(account_ts,0) < ?"))
    args = [float(value), int(ts), addr] + ([] if force else [int(ts)])
    async with db() as conn:
        cur = await conn.execute(q, tuple(args))
        if cur.rowcount:
            return True
        # Adres henüz addresses'ta yoksa (leaderboard'dan yeni geldi) oluştur.
        cur = await conn.execute(
            "INSERT OR IGNORE INTO addresses(address, first_seen, account_value,"
            " account_ts) VALUES(?,?,?,?)", (addr, ts, float(value), int(ts)))
        return (cur.rowcount or 0) > 0


async def _upsert_address(addr: str, positions: dict[str, dict], ts: int) -> None:
    async with db() as conn:
        for coin, p in positions.items():
            await conn.execute(
                """INSERT INTO positions_current
                   (coin,address,ts,side,szi,entry_px,leverage,liq_px,upnl,notional,
                    opened_ts,score,score_reasons,last_add_ts,last_trim_ts,first_seen_ts)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(coin,address) DO UPDATE SET
                     ts=excluded.ts, side=excluded.side, szi=excluded.szi,
                     entry_px=excluded.entry_px, leverage=excluded.leverage,
                     liq_px=excluded.liq_px, upnl=excluded.upnl, notional=excluded.notional,
                     first_seen_ts=MIN(COALESCE(positions_current.first_seen_ts, excluded.first_seen_ts),
                                       excluded.first_seen_ts)""",
                (coin, addr, ts, p["side"], p["szi"], p["entry_px"], p["leverage"],
                 p["liq_px"], p["upnl"], p["notional"], None, None, None, None, None, ts))
        # yanıtın otoritesi: adresin artık tutmadığı pozisyonları sil
        if positions:
            q = ",".join("?" * len(positions))
            await conn.execute(
                f"DELETE FROM positions_current WHERE address=? AND coin NOT IN ({q})",
                (addr, *positions.keys()))
        else:
            await conn.execute(
                "DELETE FROM positions_current WHERE address=?", (addr,))


def _slice(pool: list[str], cursor: int, count: int) -> tuple[list[str], int, bool]:
    """Dönüşümlü dilim: (adresler, yeni imleç, tur tamamlandı mı)."""
    if not pool:
        return [], 0, False
    cursor %= len(pool)
    take = min(count, len(pool))
    batch = [pool[(cursor + i) % len(pool)] for i in range(take)]
    new_cursor = cursor + take
    wrapped = new_cursor >= len(pool)
    return batch, (0 if wrapped else new_cursor), wrapped


PRIME_TTL = 24 * 3600      # leaderboard önbelleğiyle aynı ritim
PRIME_FAIL_TTL = 900       # geçiş tamamen patlarsa bu kadar bekle (fırtına freni)
PRIME_CONC = 6             # eşzamanlı istek (normal süpürme bütçesini boğmasın)
PROBE_CONC = 4             # anlık sonda: aynı anda en fazla bu kadar istek

_probe_sem: asyncio.Semaphore | None = None


def _sem() -> asyncio.Semaphore:
    # Modül seviyesinde kurulursa import anındaki (yanlış) event loop'a bağlanır
    global _probe_sem
    if _probe_sem is None:
        _probe_sem = asyncio.Semaphore(PROBE_CONC)
    return _probe_sem


async def probe_address(cfg: Config, client: HLClient, addr: str) -> int:
    """Tek adresin TÜM defterini HEMEN çek ve yaz (canlı işlem tetiklemesi).

    Süpürücü havuzu sırayla geziyor; büyük bir işlem gördüğümüz adrese sıra
    saatler sonra gelebiliyordu. Oysa o işlem adresi bize zaten bedava verdi:
    aynı yanıt hem hisse pozisyonlarını (positions_current) hem de tüm HL
    pozisyonlarını (hl_positions) anında tazeler.

    Süpürmeyle AYNI yanıt tipini ve aynı yazma fonksiyonlarını kullanır, yani
    'adresin artık tutmadığını sil/kapat' otoritesi de aynı — ayrı bir kod
    yolu değil. Dönen değer: yazılan hisse pozisyonu sayısı.
    """
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        rows = [dict(r) for r in await cur.fetchall()]
    coin_set = {r["coin"] for r in rows}
    if not coin_set:
        return 0                      # evren henüz keşfedilmedi
    sym_map = {(r["symbol"] or "").upper(): r["coin"] for r in rows}
    async with _sem():
        resp = await client.clearinghouse_all(addr, _dexes(cfg))
    positions, valid = _parse_equity_positions(resp, coin_set, sym_map,
                                               cfg.min_position_notional)
    if not valid:
        return 0                      # bozuk yanıt — mevcut kayıtlara dokunma
    ts = now()
    await _upsert_address(addr, positions, ts)
    # TEK ayrıştırma: eskiden aynı yanıt iki kez geziliyordu (biri eşikli, biri
    # eşiksiz). Eşiksiz liste hem kapanma tespitini hem yeni tabloyu besliyor.
    all_pos = _parse_all_positions(resp, 0)
    floor = _hl_floor(cfg)
    await _upsert_hl(addr, {c: p for c, p in all_pos.items()
                            if p["notional"] >= floor(c)}, ts,
                     held=set(all_pos))
    try:
        await _upsert_addr_pos(addr, all_pos, ts)
    except Exception:
        # BONUS yazım: patlarsa turun asıl işi (hl_positions) etkilenmesin.
        log.exception("addr_positions yazılamadı: %s", addr)
    # AYRI TRY: bakiye bir bonus: ayrıştırması patlarsa pozisyon yazımı — turun
    # asıl işi — etkilenmesin.
    try:
        if await upsert_account_value(addr, parse_account_value(resp), ts):
            _acct_written[0] += 1
    except Exception as e:
        log.debug("bakiye yazılamadı (%s): %s", addr, e)
    return len(positions)


async def prime_hl(cfg: Config, client: HLClient) -> int:
    """En zengin hesapları ÖNCE tara — 'HL en büyükleri' paneli hemen dolsun.

    Sıcak havuz (watch → holders → leaderboard) sırayla geziliyor ve
    leaderboard EN SONDA; yani bu panelin bütün konusu olan dev hesaplara
    ancak turun sonunda (75-125 dk) sıra geliyordu. Bu tek seferlik geçiş
    yalnız hl_positions'ı besler — positions_current'a DOKUNMAZ, hisse
    hattının tur düzeni aynı kalır.
    """
    top = int(getattr(cfg, "hl_prime_top", 0))
    if top <= 0:
        return 0
    if now() - int(await kv_get("hl_prime_ts") or 0) < PRIME_TTL:
        return 0
    # Tamamen başarısız geçişten sonra HEMEN tekrar deneme. API kalıcı hata
    # veriyorsa 120 adres × 5 deneme × 20 sn zaman aşımı süpürme döngüsünü
    # dakikalarca kilitliyor, nöbetçi de "derin keşif ölü" sanıyordu.
    if now() - int(await kv_get("hl_prime_fail_ts") or 0) < PRIME_FAIL_TTL:
        return 0
    addrs = await _leaderboard_addrs(client, top)
    if not addrs:
        return 0                       # leaderboard düştü → damgalama, sonra dene
    ts = now()
    sem = asyncio.Semaphore(PRIME_CONC)
    n_ok = n_pos = 0
    fail_msg: list[str] = []

    async def one(addr: str):
        nonlocal n_ok, n_pos
        from ..health import beat
        await beat("sweeper")            # nabız ÖNCE: uzun geçiş ölü görünmesin
        async with sem:
            try:
                resp = await client.clearinghouse_all(addr, _dexes(cfg))
                all_pos = _parse_all_positions(resp, 0)      # TEK ayrıştırma
                floor = _hl_floor(cfg)
                pos = {c: p for c, p in all_pos.items()
                       if p["notional"] >= floor(c)}
                await _upsert_hl(addr, pos, ts, held=set(all_pos))
                await _upsert_addr_pos(addr, all_pos, ts)
            except Exception as e:
                if not fail_msg:
                    fail_msg.append(f"{type(e).__name__}: {e}"[:200])
                log.debug("prime %s: %s", addr, e)   # tek adres tüm geçişi düşürmesin
                return
            n_ok += 1
            n_pos += len(pos)

    await asyncio.gather(*(one(a) for a in addrs))
    if not n_ok:
        await kv_set("hl_prime_fail_ts", now())
        log.warning("HL ön taraması tamamen başarısız (%d hesap). İlk hata: %s",
                    len(addrs), fail_msg[0] if fail_msg else "?")
        return 0                       # başarı damgası YOK — süre dolunca yeniden dene
    await kv_set("hl_prime_ts", ts)
    await kv_set("hl_prime_fail_ts", 0)
    log.info("HL en büyükler ön taraması: %d/%d hesap, %d dev pozisyon",
             n_ok, len(addrs), n_pos)
    return n_pos


async def sweep_batch(cfg: Config, client: HLClient) -> dict:
    hot, cold = await build_pools(cfg, client)
    if not hot and not cold:
        return {"hot": 0, "cold": 0}
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        rows = await cur.fetchall()
    coin_set = {r["coin"] for r in rows}
    sym_map = {(r["symbol"] or "").upper(): r["coin"] for r in rows}
    if not coin_set:
        return {"hot": len(hot), "cold": len(cold)}

    # Parti bütçesi: sıcak öncelikli, soğuk kısa bir kuyruk. Sıcak dilim
    # sweep_batch_size ile ÖLÇEKLENİR (30'da sabitlenmez — batch>30 ölü koldu);
    # soğuğa en fazla COLD_PER_BATCH ve bütçenin yarısı ayrılır (bs≤30'da soğuk
    # havuz artık sessizce ölmez); bir havuz tükenince artan slot diğerine geçer.
    bs, budget = _adaptive_batch(cfg, client)
    # Soğuk kuyruk da partiyle ÖLÇEKLENİR: COLD_PER_BATCH tabanda kalır ama
    # yetişme modunda partinin üçte birine kadar çıkar — 14.7K adreslik uzun
    # kuyruk sabit 10'ar 10'ar gezilirken günler sürüyordu.
    n_cold = min(max(COLD_PER_BATCH, bs // 3), len(cold), bs // 2)
    n_hot = min(len(hot), bs - n_cold)
    if n_hot < bs - n_cold:            # sıcak havuz tükendi → artan soğuğa
        n_cold = min(len(cold), bs - n_hot)
    hb, hcur, hot_done = _slice(hot, int(await kv_get("sweep_cursor_hot") or 0), n_hot)
    cb, ccur, _ = _slice(cold, int(await kv_get("sweep_cursor_cold") or 0), n_cold)
    batch = hb + cb
    ts = now()
    n_pos = 0
    n_ok = 0
    n_err = 0
    n_hlerr = 0
    last_err: list[str] = []      # ilk hata metni — panelde/Telegram'da görünsün

    async def one(addr: str):
        nonlocal n_pos, n_ok, n_err, n_hlerr
        from ..health import beat
        await beat("sweeper")  # ilerleme nabzı
        try:
            resp = await client.clearinghouse_all(addr, _dexes(cfg))
        except Exception as e:
            n_err += 1
            # İlk hatayı GÖRÜNÜR yap: debug seviyesinde kaldığı için canlıda
            # "parti tamamen hata" yazıyor ama NEDENİ hiçbir yerde okunmuyordu.
            if not last_err:
                last_err.append(f"{type(e).__name__}: {e}"[:200])
                log.warning("derin keşif isteği başarısız (%s…): %s", addr[:10], e)
            return
        positions, valid = _parse_equity_positions(resp, coin_set, sym_map,
                                                   cfg.min_position_notional)
        if not valid:
            n_err += 1
            return  # bozuk yanıt — mevcut kayıtlara dokunma
        n_ok += 1
        await _upsert_address(addr, positions, ts)
        n_pos += len(positions)
        # Aynı yanıttan TÜM Hyperliquid pozisyonları (ana dex dahil) — ek
        # istek yok, yalnız şimdiye kadar atılan kısmı saklıyoruz.
        try:
            all_pos = _parse_all_positions(resp, 0)      # TEK ayrıştırma
            floor = _hl_floor(cfg)
            await _upsert_hl(addr, {c: p for c, p in all_pos.items()
                                    if p["notional"] >= floor(c)}, ts,
                             held=set(all_pos))
            await _upsert_addr_pos(addr, all_pos, ts)
        except Exception:
            # Sayaç ŞART: bu blok sessizdi, kalıcı bir hata olsa panel sonsuza
            # dek "birazdan dolar" der, kullanıcı bozuk olduğunu asla göremezdi.
            n_hlerr += 1
            log.exception("hl_positions yazılamadı: %s", addr)

    await asyncio.gather(*(one(a) for a in batch))

    await kv_set("sweep_cursor_hot", hcur)
    await kv_set("sweep_cursor_cold", ccur)
    # Parti tamamen başarısızsa (API reddi vb.) tur muhasebesini damgalama:
    # eskiden %100 hatada bile 'son tam tur şimdi' yazılıp tablo donmuşken
    # 'derin keşif çalışıyor' sanılıyordu (PLTR $1.26M yanılgısının dönüşü).
    total_calls = n_ok + n_err
    all_failed = total_calls > 0 and n_ok == 0
    if all_failed:
        log.warning("derin keşif partisi tamamen başarısız (%d/%d hata) —"
                    " tur damgalanmadı. İlk hata: %s",
                    n_err, total_calls, last_err[0] if last_err else "?")
    elif hot_done:
        await kv_set("sweep_last_full", ts)
        log.info("derin keşif SICAK tur bitti: %d adres (soğuk kuyruk %d)",
                 len(hot), len(cold))
    tour_min = round(len(hot) / max(n_hot, 1) * cfg.sweep_interval_sec / 60)
    cold_min = round(len(cold) / max(n_cold, 1) * cfg.sweep_interval_sec / 60)
    await kv_set("sweep_stats", {"hot": len(hot), "cold": len(cold),
                                 "tour_min": tour_min, "cold_min": cold_min,
                                 "cursor": hcur,
                                 "batch": bs, "batch_hot": n_hot, "batch_cold": n_cold,
                                 "batch_positions": n_pos,
                                 "ok": n_ok, "err": n_err, "hl_err": n_hlerr,
                                 "err_msg": last_err[0] if last_err else "",
                                 "acct_written": _acct_written[0],
                                 "acct_known": await account_coverage(),
                                 **budget, "ts": ts})
    _acct_written[0] = 0
    return {"hot": len(hot), "cold": len(cold), "positions": n_pos,
            "ok": n_ok, "err": n_err}


async def account_coverage() -> dict:
    """Kaç adreste bakiye biliniyor, kaçı bayat. Kolonun yarısı boşsa sebebi
    "bozuk" değil "henüz uğramadık" — bunu göstermeden ayırt edilemez."""
    ts = now()
    async with db() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) n, SUM(account_value IS NOT NULL) known,"
            " SUM(account_ts < ?) stale, MAX(account_ts) newest"
            " FROM addresses", (ts - ACCOUNT_STALE_SEC,))
        r = await cur.fetchone()
    return {"n": r["n"] or 0, "known": r["known"] or 0,
            "stale": r["stale"] or 0, "newest": r["newest"] or 0}


async def harvest_trades(cfg: Config, client: HLClient) -> int:
    """WS'in kaçırdığı işlemleri REST recentTrades'ten topla (evren rotasyonu)."""
    async with db() as conn:
        cur = await conn.execute("SELECT coin FROM tickers ORDER BY coin")
        coins = [r["coin"] for r in await cur.fetchall()]
    if not coins:
        return 0
    start = int(await kv_get("harvest_cursor") or 0) % len(coins)
    todo = [coins[(start + i) % len(coins)] for i in range(min(HARVEST_COINS_PER_CYCLE, len(coins)))]
    added = 0
    for coin in todo:
        try:
            trades = await client.recent_trades(coin)
        except Exception as e:
            log.debug("recentTrades %s: %s", coin, e)
            continue
        rows = []
        for t in trades or []:
            try:
                px = float(t["px"])
                sz = float(t["sz"])
                tts = int(t["time"]) // 1000
                tid = str(t.get("tid") or t.get("hash") or "")
                users = t.get("users") or []
            except (KeyError, TypeError, ValueError):
                continue
            notional = px * sz
            if not tid or len(users) < 2 or notional < cfg.min_fill_notional:
                continue
            # `side` agresörü söyler: "B" = alıcı süpürdü, "A" = satıcı (collector ile aynı)
            aggr = str(t.get("side") or "").upper()
            tk_buy = tk_sell = None
            if aggr in ("A", "B"):
                tk_buy, tk_sell = (1, 0) if aggr == "B" else (0, 1)
            rows.append((coin, tid, (users[0] or "").lower(), "buy", px, sz, notional, tts, tk_buy))
            rows.append((coin, tid, (users[1] or "").lower(), "sell", px, sz, notional, tts, tk_sell))
        if not rows:
            continue
        async with db() as conn:
            for r in rows:
                cur = await conn.execute(
                    "INSERT OR IGNORE INTO fills(coin,tid,address,side,px,sz,notional,ts,taker)"
                    " VALUES(?,?,?,?,?,?,?,?,?)", r)
                added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
                await conn.execute(
                    "INSERT INTO addresses(address, first_seen) VALUES(?,?)"
                    " ON CONFLICT(address) DO NOTHING", (r[2], r[7]))
    await kv_set("harvest_cursor", (start + len(todo)) % len(coins))
    if added:
        stats = await kv_get("harvest_stats") or {"total": 0}
        stats["total"] = int(stats.get("total") or 0) + added
        stats["ts"] = now()
        await kv_set("harvest_stats", stats)
        log.info("işlem hasadı: %d yeni fill (%s)", added, ", ".join(
            fmt.short(c) if c.startswith("0x") else c.split(":")[-1] for c in todo))
    return added


async def compute_specialists(cfg: Config) -> list[dict]:
    """"Tek hisse uzmanları" panelini arka planda hesaplayıp kv'ye yaz.

    fills GROUP BY milyonlarca satırda saniyeler sürer — istek başına
    çalıştırılamaz (ana sayfadaki 30-40 sn'lik beyaz ekranın kaynağıydı).
    Ana sayfa artık yalnız specialists_cache kv'sini okur.
    """
    ts_now = now()
    async with db() as conn:
        cur = await conn.execute("SELECT coin, symbol FROM tickers")
        sym_map = {r["coin"]: r["symbol"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT address, coin, COUNT(*) c, SUM(notional) v FROM fills"
            " WHERE ts >= ? GROUP BY address, coin",
            (ts_now - cfg.fills_lookback_days * 86400,))
        fillagg = [dict(r) for r in await cur.fetchall()]
    by_addr: dict[str, list[dict]] = {}
    for r in fillagg:
        by_addr.setdefault(r["address"], []).append(r)
    specialists = []
    for addr, lst in by_addr.items():
        total = sum(r["c"] for r in lst)
        if total < 5:
            continue
        top = max(lst, key=lambda r: r["c"])
        if top["c"] / total < 0.9:
            continue
        specialists.append({"address": addr, "coin": top["coin"],
                            "symbol": sym_map.get(top["coin"], top["coin"]),
                            "n": top["c"], "vol": top["v"]})
    specialists.sort(key=lambda s: -s["vol"])
    specialists = specialists[:10]
    if specialists:
        addrs = [s["address"] for s in specialists]
        qm = ",".join("?" * len(addrs))
        async with db() as conn:
            cur = await conn.execute(
                f"SELECT address, hits, misses, watchlist, entity FROM addresses"
                f" WHERE address IN ({qm})", addrs)
            rec = {r["address"]: dict(r) for r in await cur.fetchall()}
            cur = await conn.execute(
                f"SELECT address, coin, side, notional FROM positions_current"
                f" WHERE address IN ({qm})", addrs)
            posmap: dict[str, list[dict]] = {}
            for r in await cur.fetchall():
                posmap.setdefault(r["address"], []).append(dict(r))
        for s in specialists:
            s.update(rec.get(s["address"], {}))
            open_pos = [p for p in posmap.get(s["address"], []) if p["coin"] == s["coin"]]
            s["open"] = open_pos[0] if open_pos else None
        specialists = [s for s in specialists if not s.get("entity")]  # MM/vault hariç
    await kv_set("specialists_cache", specialists)
    return specialists


async def refresh_fills_count() -> int:
    """Şerit sayacı için COUNT(*) — istekte değil, arka planda."""
    async with db() as conn:
        cur = await conn.execute("SELECT COUNT(*) c FROM fills")
        n = (await cur.fetchone())["c"]
    await kv_set("fills_count", n)
    return n


async def _chunked_delete(table: str, cutoff: int, chunk: int = 50000) -> int:
    """Eski satırları PARÇA PARÇA sil — her parça ayrı transaction + arada nefes.
    Tek dev DELETE (4M satırda ~37 sn) WAL yazma kilidini onlarca saniye tutup
    busy_timeout(5s) yüzünden collector'ı 'database is locked' ile düşürüyor,
    WS reconnect fırtınası + bekçi-cancel-rollback döngüsü yaratıyordu."""
    total = 0
    while True:
        async with db() as conn:
            cur = await conn.execute(
                f"DELETE FROM {table} WHERE rowid IN "
                f"(SELECT rowid FROM {table} WHERE ts < ? LIMIT ?)", (cutoff, chunk))
            n = cur.rowcount or 0
        total += n
        if n < chunk:
            break
        from ..health import beat
        await beat("sweeper")           # uzun bakımda bekçi sahte alarm üretmesin
        await asyncio.sleep(0.2)        # diğer yazarlara (collector) yol ver
    return total


async def prune_coin_cache() -> int:
    """Evrenden düşmüş coin'lerin önbellek anahtarlarını sil.

    Mum önbelleği (`pxc:`) coin başına ~52 KB tutuyor ve YALNIZ yazılıyordu —
    bakım kv'ye hiç dokunmadığı için delist/kapatılmış bir coin'in kaydı
    sonsuza dek kalıyordu. Sadece bu iki önek budanır; kalan kv anahtarları
    (hb:, crash:, health_state, spec_*, boot_ts…) durum verisidir, elle
    girilenler gibi korunur.
    """
    async with db() as conn:
        cur = await conn.execute("SELECT coin FROM tickers")
        live = {r["coin"] for r in await cur.fetchall()}
        if not live:
            return 0                     # evren henüz keşfedilmedi → hiçbir şeyi silme
        cur = await conn.execute(
            "SELECT k FROM kv WHERE k LIKE 'pxc:%' OR k LIKE 'hstats:%'")
        keys = [r["k"] for r in await cur.fetchall()]
        dead = [k for k in keys if k.split(":", 1)[1] not in live]
        for i in range(0, len(dead), 200):
            part = dead[i:i + 200]
            await conn.execute(
                f"DELETE FROM kv WHERE k IN ({','.join('?' * len(part))})", part)
    if dead:
        log.info("önbellek budandı: %d evrende olmayan coin anahtarı silindi (%s)",
                 len(dead), ", ".join(k.split(":")[-1] for k in dead[:6]))
    return len(dead)


async def prune_hl_records(keep: int) -> int:
    """Kapanmış HL pozisyonlarından rekor listesine girmeyenleri sil.

    Açık pozisyonlara ve zirveye göre ilk `keep` kayda DOKUNULMAZ — arşivin
    tamamı o. Kalanı (kapanmış ve rekor olmayan) tablo şişmesin diye gider.
    """
    keep = max(50, int(keep))
    async with db() as conn:
        cur = await conn.execute(
            """DELETE FROM hl_positions
               WHERE closed_ts IS NOT NULL
                 AND rowid NOT IN (SELECT rowid FROM hl_positions
                                   ORDER BY peak_notional DESC LIMIT ?)""",
            (keep,))
        n = cur.rowcount or 0
    if n:
        log.info("HL rekor arşivi budandı: %d kapanmış kayıt silindi (ilk %d korundu)",
                 n, keep)
    return n


async def prune_hl_below_tier(cfg: Config) -> int:
    """Kademe altında kalan KRİPTO satırlarını sil.

    Eşikler ayardan yükseltilebiliyor (ya da kripto tarafı sonradan açıldı):
    eski $5M'lik bir BTC satırı artık "dev" sayılmıyor, arşivde durmasının
    anlamı yok. HIP-3 (hisse) satırlarına ve kademe ÜSTÜ kriptoya dokunulmaz.

    Ölçü ZİRVE'dir (peak_notional), anlık değer değil — bir zamanlar $60M'a
    çıkmış, şimdi $10M'a inmiş pozisyon rekor arşivinde kalmayı hak eder.
    Eşik kuralının tek sahibi `bigpos.threshold`; burada kopyalanmaz.
    """
    from .bigpos import threshold
    async with db() as conn:
        cur = await conn.execute(
            "SELECT rowid AS rid, coin, notional, peak_notional FROM hl_positions"
            " WHERE instr(coin, ':') = 0")     # ":" yok = ana dex = kripto
        rows = [dict(r) for r in await cur.fetchall()]
    dead = [r["rid"] for r in rows
            if max(float(r["peak_notional"] or 0), float(r["notional"] or 0))
            < threshold(r["coin"], cfg)]
    if not dead:
        return 0
    async with db() as conn:
        for i in range(0, len(dead), 400):     # SQLite değişken sınırı
            chunk = dead[i:i + 400]
            q = ",".join("?" * len(chunk))
            await conn.execute(
                f"DELETE FROM hl_positions WHERE rowid IN ({q})", tuple(chunk))
    log.info("kademe altı kripto satırı silindi: %d", len(dead))
    return len(dead)


async def maintenance(cfg: Config) -> None:
    """Günlük veri emekliliği — /data volume'u sınırsız büyümesin.

    fills ~700K satır/gün üretir; emeklilik olmadan aylar içinde GB'lara
    çıkar. Zaman çizelgesi/uzman paneli en fazla saklama penceresi kadar
    geriyi görür (taban 7 gün — skorlamanın yakın geçmişi korunur).
    """
    ts_now = now()
    keep = max(int(cfg.fills_retention_days), 7)
    n_fills = await _chunked_delete("fills", ts_now - keep * 86400)
    n_met = await _chunked_delete("asset_metrics", ts_now - METRICS_RETENTION_D * 86400)
    n_al = await _chunked_delete("alerts_log", ts_now - ALERTS_RETENTION_D * 86400)
    # AI turu/gözlemi emekli olur; HİPOTEZLER KALIR — sicil onlar, silinirse
    # modelin karnesi kendiliğinden temizlenmiş olurdu.
    n_ai = await _chunked_delete("ai_runs", ts_now - AI_RETENTION_D * 86400)
    n_ai += await _chunked_delete("ai_observations", ts_now - AI_RETENTION_D * 86400)
    n_left = await refresh_fills_count()
    try:
        # ayrı try: kv budaması patlarsa satır emekliliği yine de yapılmış olsun
        n_kv = await prune_coin_cache()
    except Exception:
        log.exception("önbellek budaması başarısız")
        n_kv = 0
    try:
        n_kv += await prune_hl_records(cfg.hl_records_keep)
    except Exception:
        log.exception("HL rekor budaması başarısız")
    try:
        # Saklama fills ile AYNI ayardan: işlem kaydı olmayan bir pencerede
        # pozisyon fotoğrafı tutmanın faydası yok, iki sabit de tutulmaz.
        n_ap = await prune_addr_positions(keep)
        if n_ap:
            log.info("addr_positions budandı: %d tazelenmemiş satır", n_ap)
    except Exception:
        log.exception("addr_positions budaması başarısız")
    try:
        n_tier = await prune_hl_below_tier(cfg)
    except Exception:
        log.exception("kademe budaması başarısız")
        n_tier = 0
    try:
        from ..diag import prune_logs
        n_log = await prune_logs()
    except Exception:
        log.exception("uyarı kaydı budaması başarısız")
        n_log = 0
    try:
        from .cryptovol import prune as prune_vol
        n_log += await prune_vol()
    except Exception:
        log.exception("hacim olayı budaması başarısız")
    if n_fills or n_met or n_al or n_kv or n_tier or n_ai or n_log:
        log.info("günlük bakım: %d fill, %d metrik, %d alarm kaydı, %d önbellek"
                 " anahtarı, %d kademe altı kripto satırı, %d AI turu/gözlemi,"
                 " %d uyarı kaydı emekli (fills kalan %d)",
                 n_fills, n_met, n_al, n_kv, n_tier, n_ai, n_log, n_left)


async def housekeeping(cfg: Config) -> None:
    """Süpürme döngüsünün istek dışı işleri: uzman önbelleği + günlük bakım.
    İki iş BAĞIMSIZ try bloğunda — uzman hesabı (fills GROUP BY) kalıcı
    patlarsa günlük emeklilik yine de koşsun (yoksa /data sonsuz büyürdü)."""
    if now() - int(await kv_get("spec_last") or 0) >= SPEC_INTERVAL:
        try:
            await compute_specialists(cfg)
            await refresh_fills_count()
            await kv_set("spec_last", now())
        except Exception:
            log.exception("uzman önbelleği hesaplanamadı")
    today = datetime.now(TR).date().isoformat()
    if await kv_get("maint_last_day") != today:
        try:
            await maintenance(cfg)
            await kv_set("maint_last_day", today)
        except Exception:
            log.exception("günlük bakım başarısız")


async def loop(cfg: Config, client: HLClient) -> None:
    await asyncio.sleep(45)  # evren keşfini bekle
    log.info("derin keşif başladı: leaderboard ilk %d + görülen tüm adresler,"
             " %d adres/%ds", cfg.sweep_leaderboard_top, cfg.sweep_batch_size,
             cfg.sweep_interval_sec)
    while True:
        try:
            from ..health import beat
            await beat("sweeper")   # tur başı nabzı ön taramadan ÖNCE
            # Ayrı try: ön tarama patlarsa normal süpürme yine koşsun
            await prime_hl(cfg, client)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("HL ön taraması başarısız")
        try:
            from ..health import beat
            await beat("sweeper")  # turbaşı: uzun parti sahte alarm üretmesin
            await sweep_batch(cfg, client)
            await beat("sweeper")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("süpürme hatası")
        try:
            await harvest_trades(cfg, client)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hasat hatası")
        try:
            await housekeeping(cfg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("bakım hatası")
        await asyncio.sleep(cfg.sweep_interval_sec)
