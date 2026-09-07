"""Sayım (census) — tanıdığımız HER hesabın defteri, ana dex + kripto dex'ler, tur tur.

Neden: HL'de "bu marketteki tüm pozisyonlar" API'si yok. Havuz yalnız WS'te
gördüğümüz / hasat ettiğimiz adresler kadardı (PUMP'ta OI'nin ~üçte biri) ve
"bizimki hep eksik" sorusunun kökü buydu. Dış siteler ya düğüm çalıştırır
(16 vCPU / 128 GB — Railway'de olmaz) ya da tüm hesapları sorgular. Bu modül
ikincisini yapar: evren = leaderboard'un TÜM satırları (bakiye ≥ taban, haftalık
hacim şartı YOK — pozisyon tutan hesap haftalarca işlem yapmayabilir) ∪
addresses ∪ fills adresleri; sıra bakiye büyükten küçüğe (OI'nin büyüğü önce →
kapsama $ olarak hızla yükselir); tur bitince leaderboard tazelenir, baştan.

İki mod, açılışta SONDA ile seçilir (`probe_mode`):
  • toplu — `batchClearinghouseStates` (sağlayıcı belgelerinde var, resmi SDK'da
    yok): bir istekte `census_batch_size` hesap. 250 istek/dk × 50 = 12.500
    hesap/dk → 50 bin hesap ≈ 4 dk.
  • tek tek — `clearinghouseState` hesap başına 1 istek: 250 hesap/dk → ≈ 3,5 saat.
  Toplu mod tur içinde üst üste BATCH_FAIL_MAX hata verirse tek moda düşer,
  sonda PROBE_TTL sonra yeniden yapılır. HL 429 dönerse hız yarıya iner (en az
  1/8), SLOW_RECOVER_SEC sessizlikten sonra kademeli geri çıkar (`client.n_429`).

Yazım süpürücüyle AYNI yazıcılardır, otorite YALNIZ sorgulanan dex:
  • ana dex → addr_positions (her boyut, `dexes=[""]`), hl_positions (kademe üstü),
    bakiye; HIP-3 satırlarına dokunulmaz.
  • kripto dex (para) → positions_current (`dexes=[dex]`); xyz/ana satırı yaşar.
  Bozuk/boş yanıt hata sayılır, kayıt SİLMEZ. Pozisyonsuz hesap addresses'a
  sokulmaz (havuz şişmesin); kapanış damgası yalnız açık satırı olanlarda.

Worker'lar (ROLE=census-worker, P3): `census_accounts` satırını kiralar
(`leased_ts`), sonucu /api/census/ingest ile yollar; ana uygulama kiralı satırı
atlar. Dürüst not: Railway servislerinin çıkış IP'si paylaşımlıysa HL 429 döner
ve herkes yavaşlar — kazanım tek IP bütçesiyle sınırlı kalır.

Durum: kv `census_state` (süren tur), `census_stats` (biten son tur), `census_mode`
(sonda), `census_lb` (leaderboard işlenişi). Restart'ta tur kaldığı yerden sürer
(`scanned_ts < pass_ts` olanlar). Özet `/tani` "sayım" satırı ve kapsama kutusu.
"""
import asyncio
import logging
import time

from ..db import db, kv_get, kv_set, now
from ..health import beat
from . import sweeper as _sw

log = logging.getLogger("radar.census")

STATE_KV = "census_state"
STATS_KV = "census_stats"
MODE_KV = "census_mode"
LB_KV = "census_lb"
START_DELAY = 300          # açılışta evren + leaderboard otursun
IDLE_SEC = 600             # kapalıyken bekleme (nabız atar)
PASS_GAP = 60              # iki tur arası nefes
LB_TTL = 6 * 3600          # leaderboard evrene bu sıklıkla işlenir
UNI_TTL = 900              # addresses ∪ fills birleştirmesi bu sıklıkla (toplu modda tur 4 dk)
PROBE_TTL = 6 * 3600       # tek moddayken toplu sorgu sondası bu sıklıkla yenilenir
LEASE_SEC = 600            # worker kirası
STATE_EVERY = 200          # kaç hesapta bir ilerleme kaydı
BATCH_FAIL_MAX = 3         # üst üste bu kadar toplu hata → bu tur tek mod
SLOW_RECOVER_SEC = 300     # KENDİ 429'undan sonra hız bu süre sonra bir kademe geri çıkar
CHUNK_SINGLE = 100         # tek modda bellekten bir seferde alınan adres
LB_BATCH = 2000            # leaderboard satırları bu parçalarla tabloya
WORKERS_KV = "census_workers"   # {ad: {ts, leased, ingested, err}} — worker'lar (P3)
WORKER_ACTIVE_SEC = 900    # bu süre içinde kira almış worker "aktif" sayılır
CHUNK_PASS = 200           # ana turda bellekten bir seferde alınan hesap (toplu: parti parti bölünür)
CHUNK_HOT = 50             # sıcak şerit: bir adımda en çok bu kadar hesap (toplu parçadan ÖNCE)
GAP_STEP = 10              # tur arası bekleme adımı: sıcak bekleyen varsa erken çık
MODE_TR = {"batch": "toplu", "single": "tek tek"}


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "census_enabled", False))


# ────────────────────────────────────────────── evren

def lb_rows(data, floor: float):
    """Leaderboard satırlarından (adres, bakiye) üretir: bakiye ≥ taban, küçük harf.
    Tekrar/sıralama tabloya bırakılır (ON CONFLICT) — koca liste bellekte ikinci
    kez kopyalanmaz."""
    rows = (data or {}).get("leaderboardRows") if isinstance(data, dict) else None
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        addr = (r.get("ethAddress") or "").lower()
        if not addr.startswith("0x"):
            continue
        try:
            av = float(r.get("accountValue") or 0)
        except (TypeError, ValueError):
            continue
        if av < floor:
            continue
        yield addr, av


async def _ingest_leaderboard(data, floor: float, ts: int) -> int:
    n = 0
    buf: list[tuple] = []
    q = ("INSERT INTO census_accounts(address, account_value, src, seen_ts) VALUES(?,?,'lb',?)"
         " ON CONFLICT(address) DO UPDATE SET account_value=excluded.account_value,"
         " seen_ts=excluded.seen_ts")
    async with db() as conn:
        for addr, av in lb_rows(data, floor):
            buf.append((addr, av, ts))
            if len(buf) >= LB_BATCH:
                await conn.executemany(q, buf)
                n += len(buf)
                buf = []
        if buf:
            await conn.executemany(q, buf)
            n += len(buf)
    return n


async def refresh_universe(cfg, client, *, force: bool = False) -> dict:
    """Evreni tazele: leaderboard (LB_TTL'de bir) ∪ addresses ∪ fills.
    {lb, addr, fills, total, lb_ts, lb_err?} — lb: bu turda işlenen satır,
    addr/fills: yeni eklenen adres."""
    ts = now()
    out: dict = {"lb": 0, "addr": 0, "fills": 0, "total": 0, "lb_ts": None}
    rec = await kv_get(LB_KV) or {}
    if force or ts - int(rec.get("ts") or 0) >= LB_TTL:
        data = await client.leaderboard()
        if data:
            floor = float(getattr(cfg, "census_min_account_value", 100) or 0)
            n = await _ingest_leaderboard(data, floor, ts)
            rec = {"ts": ts, "n": n, "floor": floor}
            await kv_set(LB_KV, rec)
            out["lb"] = n
            log.info("sayım evreni: leaderboard %d hesap (bakiye ≥ $%.0f)", n, floor)
        else:
            out["lb_err"] = "leaderboard alınamadı"
    out["lb_ts"] = rec.get("ts")
    out["lb_n"] = rec.get("n")
    merge = force or ts - int(rec.get("uni_ts") or 0) >= UNI_TTL
    async with db() as conn:
        if merge:
            cur = await conn.execute(
                "INSERT OR IGNORE INTO census_accounts(address, account_value, src, seen_ts)"
                " SELECT LOWER(address), account_value, 'addr', ? FROM addresses"
                " WHERE address LIKE '0x%'", (ts,))
            out["addr"] = int(cur.rowcount or 0)
            cur = await conn.execute(
                "INSERT OR IGNORE INTO census_accounts(address, account_value, src, seen_ts)"
                " SELECT DISTINCT LOWER(address), NULL, 'fills', ? FROM fills"
                " WHERE address LIKE '0x%'", (ts,))
            out["fills"] = int(cur.rowcount or 0)
        cur = await conn.execute("SELECT COUNT(*) n FROM census_accounts")
        out["total"] = int((await cur.fetchone())["n"])
    if merge:
        rec = {**rec, "uni_ts": ts}
        await kv_set(LB_KV, rec)
    out["merged"] = merge
    return out


# ────────────────────────────────────────────── sıcak şerit

async def mark_hot(conn, addr_ts: dict[str, int]) -> int:
    """Collector'dan: {adres: fill ts} → census_accounts.hot_ts (yoksa satır açılır,
    src='fills'). hot_ts > scanned_ts olan hesap sıradaki adımda ÖNCE sorgulanır."""
    rows = [(a.lower(), int(t), int(t)) for a, t in addr_ts.items() if str(a).lower().startswith("0x")]
    if not rows:
        return 0
    await conn.executemany(
        "INSERT INTO census_accounts(address, src, seen_ts, hot_ts) VALUES(?,'fills',?,?)"
        " ON CONFLICT(address) DO UPDATE SET hot_ts=excluded.hot_ts,"
        " seen_ts=MAX(COALESCE(census_accounts.seen_ts, 0), excluded.seen_ts)", rows)
    return len(rows)


async def hot_rows(n: int) -> list[tuple[str, float | None, int]]:
    """Sıcak bekleyenler: fill'i sayımından yeni (hot_ts > scanned_ts), kirasız, en yeni önce.
    Döner: [(adres, bakiye, hot_ts)]."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT address, account_value, hot_ts FROM census_accounts"
            " WHERE hot_ts IS NOT NULL AND hot_ts > COALESCE(scanned_ts, 0)"
            " AND COALESCE(leased_ts, 0) < ? ORDER BY hot_ts DESC LIMIT ?", (now() - LEASE_SEC, int(n)))
        return [(r["address"], r["account_value"], int(r["hot_ts"])) for r in await cur.fetchall()]


async def hot_pending() -> int:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) n FROM census_accounts WHERE hot_ts IS NOT NULL AND hot_ts > COALESCE(scanned_ts, 0)")
        return int((await cur.fetchone())["n"] or 0)


async def clear_hot(rows: list[tuple[str, int]]) -> None:
    """İşlenen sıcaklar (başarılı ya da bozuk) şeritten düşer — bozuk yanıt sonsuz
    döngü kurmasın; o sırada gelen daha yeni fill (hot_ts büyüdüyse) korunur."""
    if rows:
        async with db() as conn:
            await conn.executemany(
                "UPDATE census_accounts SET hot_ts=NULL WHERE address=? AND hot_ts <= ?", rows)


async def pass_order(pass_ts: int) -> list[tuple[str, float | None]]:
    """Bu turda henüz sayılmamış hesaplar, bakiye büyükten küçüğe (NULL en sonda)."""
    async with db() as conn:
        cur = await conn.execute(
            "SELECT address, account_value FROM census_accounts"
            " WHERE COALESCE(scanned_ts, 0) < ? ORDER BY account_value DESC", (pass_ts,))
        return [(r["address"], r["account_value"]) for r in await cur.fetchall()]


async def _skip_set(addrs: list[str], pass_ts: int) -> set[str]:
    """Bu partide atlanacaklar: worker kiralamış (LEASE_SEC içinde) ya da bu turda
    zaten sayılmış (worker sonucu geldi)."""
    if not addrs:
        return set()
    q = ",".join("?" * len(addrs))
    async with db() as conn:
        cur = await conn.execute(
            f"SELECT address FROM census_accounts WHERE address IN ({q})"
            f" AND (COALESCE(leased_ts, 0) > ? OR COALESCE(scanned_ts, 0) >= ?)",
            (*addrs, now() - LEASE_SEC, pass_ts))
        return {r["address"] for r in await cur.fetchall()}


async def _progress_counts(pass_ts: int) -> tuple[int, int]:
    async with db() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) n, SUM(COALESCE(scanned_ts, 0) >= ?) d FROM census_accounts", (pass_ts,))
        r = await cur.fetchone()
        return int(r["n"] or 0), int(r["d"] or 0)


# ────────────────────────────────────────────── mod sondası

def _batch_states(resp, addrs: list[str]) -> list | None:
    """Toplu yanıtı `addrs` sırasında state listesine çevir; şekil tutmuyorsa None.
    Liste (belgelerdeki şekil: `users` sırasında — state'in içinde adres yok, sıra
    doğrulanamaz, belgeye güvenilir) ya da adres→state sözlüğü kabul edilir."""
    if isinstance(resp, list):
        if len(resp) != len(addrs):
            return None
        out = resp
    elif isinstance(resp, dict) and resp and "assetPositions" not in resp:
        low = {str(k).lower(): v for k, v in resp.items()}
        if not all(a in low for a in addrs):
            return None
        out = [low[a] for a in addrs]
    else:
        return None
    for x in out:
        if x is not None and not (isinstance(x, dict) and "assetPositions" in x):
            return None
    return out


async def probe_batch(client, addrs: list[str]) -> tuple[str, str]:
    """Toplu sorgu sondası (kv'siz — worker da kullanır): ('batch'|'single', neden)."""
    fn = getattr(client, "batch_clearinghouse", None)
    if fn is None:
        return "single", "istemci toplu sorgu bilmiyor"
    if not addrs:
        return "single", "sonda için adres yok"
    sample = list(addrs[:2])
    try:
        resp = await fn(sample, priority="low")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        return "single", f"{type(e).__name__}: {str(e)[:120]}"
    states = _batch_states(resp, sample)
    if states is not None and any(isinstance(x, dict) for x in states):
        return "batch", ""
    return "single", f"beklenmeyen yanıt şekli: {type(resp).__name__}"


async def probe_mode(cfg, client, addrs: list[str], *, force: bool = False) -> str:
    """'batch' ya da 'single'. Sonuç MODE_KV'de: toplu bulunduysa kalıcı (tur içi
    hata sayacı düşürür), tek bulunduysa PROBE_TTL sonra yeniden denenir."""
    rec = await kv_get(MODE_KV) or {}
    if not force and rec.get("mode"):
        if rec["mode"] == "batch" or now() - int(rec.get("ts") or 0) < PROBE_TTL:
            return rec["mode"]
    mode, why = await probe_batch(client, addrs)
    await kv_set(MODE_KV, {"mode": mode, "ts": now(), "why": why})
    log.info("sayım modu: %s%s", MODE_TR[mode], f" ({why})" if why else "")
    return mode


# ────────────────────────────────────────────── hız

class Pace:
    """Sayımın kendi hızı: 60/rpm sn gecikme. Sayımın KENDİ isteği 429 yiyince
    (`stats["429"]` — istemci her 429'da artırır) çarpan yarıya (en az 1/8);
    SLOW_RECOVER_SEC sessizlikten sonra bir kademe geri. Eskiden küresel
    `client.n_429`'a bakıyordu: bars/bookwall'un 429'u sayımı 31/dk'ya
    düşürüyordu. İstemcinin düşük şeridi (LOW_SHARE, 429 duraklaması) ayrıca
    uygulanır."""

    def __init__(self, rpm: int, client=None, clock=time.monotonic):
        self.rpm = max(1, int(rpm or 1))
        self.factor = 1.0
        self.client = client
        self.clock = clock
        self.stats: dict = {"429": 0}
        self.seen = 0
        self.n_429 = 0
        self.last = clock()

    def delay(self) -> float:
        return 60.0 / (self.rpm * self.factor)

    def observe(self) -> None:
        cur = int(self.stats.get("429", 0) or 0)
        t = self.clock()
        if cur > self.seen:
            self.n_429 += cur - self.seen
            self.seen = cur
            self.last = t
            if self.factor > 0.125:
                self.factor = max(0.125, self.factor / 2)
                log.warning("sayım: HL 429 — hız %.0f istek/dk'ya indi", self.rpm * self.factor)
        elif self.factor < 1 and t - self.last >= SLOW_RECOVER_SEC:
            self.factor = min(1.0, self.factor * 2)
            self.last = t


class Fetcher:
    """HL'den defter çekme — ana tur (run_pass) ve worker AYNI yolu kullanır.
    Toplu modda `batch_size`'lık partiler; üst üste BATCH_FAIL_MAX toplu hata →
    tek moda düşer (`fell_back`), kalanlar tek tek. `should_stop()` doğruysa
    (sayım kapatıldı) durur (`stopped`). Yanıt alınamayan adres None ile döner."""

    def __init__(self, client, mode: str, batch_size: int, pace: Pace, *,
                 sleep=asyncio.sleep, should_stop=None):
        self.client, self.mode, self.pace, self.sleep = client, mode, pace, sleep
        self.batch_size = max(1, int(batch_size or 1))
        self.should_stop = should_stop or (lambda: False)
        self.batch_fail = self.requests = 0
        self.fell_back = self.stopped = False

    async def fetch(self, addrs: list[str], dex: str) -> list[tuple[str, object]]:
        if self.mode != "batch":
            return await self._single(addrs, dex)
        pairs: list[tuple[str, object]] = []
        i = 0
        while i < len(addrs):
            if self.should_stop():
                self.stopped = True
                break
            sub = addrs[i:i + self.batch_size]
            i += len(sub)
            await self.sleep(self.pace.delay())
            self.requests += 1
            states = None
            try:
                states = _batch_states(await self.client.batch_clearinghouse(
                    sub, dex, priority="low", stats=self.pace.stats), sub)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("sayım toplu (%s, %d adres): %s", dex or "ana", len(sub), e)
            self.pace.observe()
            if states is None:
                self.batch_fail += 1
                pairs.extend((a, None) for a in sub)
                if self.batch_fail >= BATCH_FAIL_MAX:
                    self.mode, self.fell_back = "single", True
                    log.warning("sayım: toplu sorgu üst üste %d kez hata verdi — tek tek moda düşüldü",
                                self.batch_fail)
                    pairs.extend(await self._single(addrs[i:], dex))
                    break
                continue
            self.batch_fail = 0
            pairs.extend(zip(sub, states))
        return pairs

    async def _single(self, addrs: list[str], dex: str) -> list[tuple[str, object]]:
        pairs: list[tuple[str, object]] = []
        for a in addrs:
            if self.should_stop():
                self.stopped = True
                break
            await self.sleep(self.pace.delay())
            self.requests += 1
            resp = None
            try:
                resp = await self.client.clearinghouse(a, dex, priority="low", stats=self.pace.stats)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("sayım %s %s: %s", dex or "ana", a[:10], e)
            self.pace.observe()
            pairs.append((a, resp))
        return pairs


# ────────────────────────────────────────────── yazım

class _Open:
    """Açık satırı olan adresler (tur başında yüklenir): pozisyonsuz hesapta
    kapanış sorgusu ancak bunlarda koşar — on binlerce boş UPDATE yerine."""

    def __init__(self):
        self.addr: set[str] = set()
        self.hl: set[str] = set()
        self.pc: dict[str, set[str]] = {}

    async def load(self, dexes: list[str]) -> None:
        async with db() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT address FROM addr_positions WHERE closed_ts IS NULL AND instr(coin, ':') = 0")
            self.addr = {r["address"] for r in await cur.fetchall()}
            cur = await conn.execute(
                "SELECT DISTINCT address FROM hl_positions WHERE closed_ts IS NULL AND instr(coin, ':') = 0")
            self.hl = {r["address"] for r in await cur.fetchall()}
            for dex in dexes:
                if not dex:
                    continue
                cur = await conn.execute(
                    "SELECT DISTINCT address FROM positions_current WHERE coin LIKE ?", (f"{dex}:%",))
                self.pc[dex] = {r["address"] for r in await cur.fetchall()}


async def write_main(cfg, addr: str, resp, ts: int, opened: "_Open | None") -> tuple[int, float | None]:
    """Ana dex yanıtını yaz — süpürücüyle aynı yazıcılar, otorite yalnız ana dex.
    Döner: (açık pozisyon sayısı, bakiye)."""
    all_pos = _sw._parse_all_positions(resp, 0)
    if all_pos or opened is None or addr in opened.addr:
        await _sw._upsert_addr_pos(addr, all_pos, ts, dexes=[""], touch_addresses=bool(all_pos))
        if opened is not None:
            (opened.addr.add if all_pos else opened.addr.discard)(addr)
    floor = _sw._hl_floor(cfg)
    big = {c: p for c, p in all_pos.items() if p["notional"] >= floor(c)}
    if big or opened is None or addr in opened.hl:
        await _sw._upsert_hl(addr, big, ts, held=set(all_pos), dexes=[""])
        if opened is not None:
            (opened.hl.add if big else opened.hl.discard)(addr)
    av = _sw.parse_account_value(resp)
    if av is not None:
        try:
            await _sw.upsert_account_value(addr, av, ts, force=False, create=False)
        except Exception:
            log.debug("bakiye yazılamadı (%s)", addr, exc_info=True)
    return len(all_pos), av


async def write_hip3(cfg, addr: str, dex: str, resp, ts: int, tick: tuple, opened: "_Open | None") -> int:
    """Kripto dex (para) yanıtı → positions_current, otorite yalnız o dex.
    Bozuk yanıt ValueError (çağıran hata sayar, kayıt silinmez)."""
    coin_set, sym_map = tick
    positions, valid = _sw._parse_equity_positions({dex: resp}, coin_set, sym_map, _sw._pos_floor(cfg))
    if not valid:
        raise ValueError("bozuk yanıt")
    if not coin_set:
        return 0
    held = opened.pc.setdefault(dex, set()) if opened is not None else None
    if positions or held is None or addr in held:
        await _sw._upsert_address(addr, positions, ts, dexes=[dex])
        if held is not None:
            (held.add if positions else held.discard)(addr)
    return len(positions)


async def _tickers_by_dex(dexes: list[str]) -> dict[str, tuple[set[str], dict[str, str]]]:
    out: dict[str, tuple[set[str], dict[str, str]]] = {}
    async with db() as conn:
        for dex in dexes:
            if not dex:
                continue
            cur = await conn.execute("SELECT coin, symbol FROM tickers WHERE dex=?", (dex,))
            rows = await cur.fetchall()
            out[dex] = ({r["coin"] for r in rows}, {(r["symbol"] or "").upper(): r["coin"] for r in rows})
    return out


async def mark_scanned(rows: list[tuple[str, int, int, float | None]]) -> None:
    """[(adres, ts, pozisyon, bakiye)] → scanned_ts/positions/account_value; kira düşer."""
    if not rows:
        return
    async with db() as conn:
        await conn.executemany(
            "UPDATE census_accounts SET scanned_ts=?, positions=?, account_value=COALESCE(?, account_value),"
            " leased_ts=NULL, lease_worker=NULL WHERE address=?",
            [(ts, n, av, a) for a, ts, n, av in rows])


async def write_results(cfg, results: list[tuple[str, str, object]], pass_ts: int,
                        tick: dict, opened: "_Open | None") -> dict:
    """[(adres, dex, state|None)] → yaz (ana tur ve worker ingest AYNI yol).
    Bozuk/boş state hata sayılır, kayıt silmez, hesap sayılmış olmaz. Ana dex
    sonucu hesabı 'sayıldı' yapar (scanned_ts ≥ pass_ts, kira düşer).
    Döner: {ok, err, found, scanned}."""
    out = {"ok": 0, "err": 0, "found": 0, "scanned": 0}
    scanned: list[tuple[str, int, int, float | None]] = []
    for a, dex, resp in results:
        ts = now()
        if not isinstance(resp, dict) or "assetPositions" not in resp:
            out["err"] += 1
            continue
        try:
            if dex == "":
                n, av = await write_main(cfg, a, resp, ts, opened)
                scanned.append((a, max(ts, int(pass_ts or 0)), n, av))
                out["ok"] += 1
            else:
                n = await write_hip3(cfg, a, dex, resp, ts, tick.get(dex, (set(), {})), opened)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sayım yazımı (%s %s)", dex or "ana", a)
            out["err"] += 1
            continue
        out["found"] += n
    await mark_scanned(scanned)
    out["scanned"] = len(scanned)
    return out


# ────────────────────────────────────────────── worker'lar (P3)

async def note_worker(name: str, *, leased: int = 0, ingested: int = 0, err: int = 0) -> None:
    """Worker'ın son kirası/teslimi (kv WORKERS_KV) — /tani ve yerel hız için."""
    rec = await kv_get(WORKERS_KV) or {}
    t = now()
    w = dict(rec.get(name) or {})
    w["ts"] = t
    w["leased"] = int(w.get("leased") or 0) + int(leased)
    w["ingested"] = int(w.get("ingested") or 0) + int(ingested)
    w["err"] = int(w.get("err") or 0) + int(err)
    rec[name] = w
    rec = {k: v for k, v in rec.items() if t - int((v or {}).get("ts") or 0) < 86400}
    await kv_set(WORKERS_KV, rec)


async def workers_active() -> list[str]:
    rec = await kv_get(WORKERS_KV) or {}
    t = now()
    return sorted(k for k, v in rec.items() if t - int((v or {}).get("ts") or 0) < WORKER_ACTIVE_SEC)


async def effective_rpm(cfg) -> int:
    """Ana uygulamanın kendi sayım hızı: worker aktifken `census_rpm_local` (IP
    bütçesini paylaşıyor olabilirler), yoksa `census_rpm`."""
    base = int(getattr(cfg, "census_rpm", 250) or 250)
    if await workers_active():
        return max(1, min(base, int(getattr(cfg, "census_rpm_local", 100) or 100)))
    return max(1, base)


async def lease(cfg, worker: str, n: int) -> dict:
    """Worker'a sıradaki n hesabı kirala (LEASE_SEC). Tur yoksa / sayım kapalıysa boş."""
    st = await kv_get(STATE_KV) or {}
    dexes = _dexes(cfg)
    base = {"pass_ts": None, "accounts": [], "wait": 30, "dexes": dexes, "lease_sec": LEASE_SEC,
            "hip3_floor": float(getattr(cfg, "census_hip3_min_account_value", 1000) or 0)}
    if not enabled(cfg):
        await note_worker(worker)
        return {**base, "why": "sayım kapalı (census_enabled=0)"}
    if not st or st.get("finished") or not st.get("pass_ts"):
        await note_worker(worker)
        return {**base, "why": "süren tur yok"}
    pass_ts, t = int(st["pass_ts"]), now()
    n = max(1, min(int(n or 1), 5000))
    async with db() as conn:
        cur = await conn.execute(
            "SELECT address, account_value FROM census_accounts"
            " WHERE COALESCE(scanned_ts, 0) < ? AND COALESCE(leased_ts, 0) < ?"
            " ORDER BY CASE WHEN hot_ts > COALESCE(scanned_ts, 0) THEN 0 ELSE 1 END,"
            " hot_ts DESC, account_value DESC LIMIT ?", (pass_ts, t - LEASE_SEC, n))
        rows = [(r["address"], r["account_value"]) for r in await cur.fetchall()]
        if rows:
            await conn.executemany(
                "UPDATE census_accounts SET leased_ts=?, lease_worker=? WHERE address=?",
                [(t, worker, a) for a, _ in rows])
    await note_worker(worker, leased=len(rows))
    tick = await _tickers_by_dex(dexes)
    return {**base, "pass_ts": pass_ts, "mode": st.get("mode"),
            "dexes": [d for d in dexes if not d or tick.get(d, (set(),))[0]],
            "accounts": [{"a": a, "v": v} for a, v in rows], "wait": 0 if rows else 30}


async def ingest(cfg, worker: str, body: dict) -> dict:
    """Worker'ın sonuçları: {pass_ts, results: [{a, dex, state|null}]} → aynı yazıcılar.
    Bilinmeyen dex / bozuk satır hata sayılır; hiçbir şey silinmez."""
    results = (body or {}).get("results") or []
    pass_ts = int((body or {}).get("pass_ts") or 0)
    dexes = set(_dexes(cfg))
    parsed: list[tuple[str, str, object]] = []
    bad = 0
    for r in results if isinstance(results, list) else []:
        if not isinstance(r, dict):
            bad += 1
            continue
        a = str(r.get("a") or "").lower()
        dex = str(r.get("dex") or "")
        if not a.startswith("0x") or len(a) != 42 or dex not in dexes:
            bad += 1
            continue
        parsed.append((a, dex, r.get("state")))
    out = await write_results(cfg, parsed, pass_ts, await _tickers_by_dex(sorted(dexes)), None)
    out["err"] += bad
    await note_worker(worker, ingested=out["ok"], err=out["err"])
    return {"ok": True, "ok_n": out["ok"], "err": out["err"], "found": out["found"], "scanned": out["scanned"]}


# ────────────────────────────────────────────── tur

def _dexes(cfg) -> list[str]:
    from .. import assets
    return [""] + [d for d in assets.crypto_dexes(cfg) if d]


async def run_pass(cfg, client, *, sleep=asyncio.sleep, max_accounts: int | None = None) -> dict:
    """Bir tur: evren tazele → mod sondası → her hesap ana dex'te (+ bakiyesi
    tabanın üstündeyse kripto dex'lerde) → yaz. Yarım kalmış tur (restart /
    kapatma) kaldığı yerden sürer; worker'ın kiraladığı/bitirdiği hesaplar atlanır.
    Döner: tur özeti (kv STATS_KV ile aynı)."""
    t0 = now()
    uni = await refresh_universe(cfg, client)
    prev = await kv_get(STATE_KV) or {}
    resumed = bool(prev and not prev.get("finished") and prev.get("pass_ts"))
    # Yeni tur damgası önceki turun bitişinden KESİN büyük olsun: aynı saniyede
    # biten turun sayımları "bu turda sayıldı" görünmesin (scanned_ts < pass_ts).
    pass_ts = int(prev["pass_ts"]) if resumed else max(t0, int(prev.get("ts") or 0) + 1)
    order = await pass_order(pass_ts)
    dexes = _dexes(cfg)
    tick = await _tickers_by_dex(dexes)
    hip3_floor = float(getattr(cfg, "census_hip3_min_account_value", 1000) or 0)
    mode = await probe_mode(cfg, client, [a for a, _ in order[:2]])
    pace = Pace(await effective_rpm(cfg))
    fetcher = Fetcher(client, mode, int(getattr(cfg, "census_batch_size", 50) or 50), pace,
                      sleep=sleep, should_stop=lambda: not enabled(cfg))
    total, done0 = await _progress_counts(pass_ts)
    st: dict = {"pass_ts": pass_ts, "mode": mode, "started": t0, "resumed": resumed,
                "total": total, "done": done0, "ok": 0, "err": 0, "found": 0, "requests": 0,
                "n_429": 0, "hot": 0, "finished": False, "ts": t0, "universe": uni}
    await kv_set(STATE_KV, st)
    opened = _Open()
    await opened.load(dexes)
    since_ckpt = 0
    i = 0

    async def checkpoint(final: bool = False) -> None:
        st["total"], st["done"] = await _progress_counts(pass_ts)
        st["n_429"] = pace.n_429
        st["requests"] = fetcher.requests
        st["ts"] = max(now(), pass_ts)
        el = max(1, st["ts"] - t0)
        st["rpm"] = round(fetcher.requests / (el / 60.0), 1)
        pace.rpm = await effective_rpm(cfg)          # worker geldiyse/gittiyse hız uyarlanır
        st["rpm_cap"] = round(pace.rpm * pace.factor)
        st["workers"] = len(await workers_active())
        st["finished"] = final
        await kv_set(STATE_KV, st)
        await beat("census")

    async def process(part: list[tuple[str, float | None]]) -> None:
        """Bir parça hesap: ana dex herkeste, kripto dex'ler bakiyesi tabanın üstündekilerde."""
        vals = dict(part)
        addrs = [a for a, _ in part]
        for dex in dexes:
            if dex and not tick.get(dex, (set(), {}))[0]:
                continue                                # evren henüz keşfedilmedi: isteği harcama
            sub = addrs if dex == "" else [a for a in addrs if (vals.get(a) or 0) >= hip3_floor]
            if not sub:
                continue
            pairs = await fetcher.fetch(sub, dex)
            w = await write_results(cfg, [(a, dex, r) for a, r in pairs], pass_ts, tick, opened)
            st["ok"] += w["ok"] if dex == "" else 0
            st["err"] += w["err"]
            st["found"] += w["found"]
            if fetcher.fell_back and not st.get("fell_back"):
                st["fell_back"], st["mode"] = True, "single"
                await kv_set(MODE_KV, {"mode": "single", "ts": now(), "why": "tur içinde üst üste toplu hata"})
            if fetcher.stopped:
                st["stopped"] = "kapatıldı"
                break

    while True:
        if not enabled(cfg):
            st["stopped"] = "kapatıldı"
            break
        if max_accounts is not None and st["ok"] + st["err"] >= max_accounts:
            st["stopped"] = "tavan"
            break
        # Sıcak şerit ÖNCE: az önce işlem yapan adresler (fill'i sayımından yeni).
        # Toplu sıra bitse de sıcaklar kuruyana kadar sürer; işlenen sıcak (bozuk
        # yanıt dahil) şeritten düşer.
        hot = await hot_rows(CHUNK_HOT)
        if hot:
            st["hot"] += len(hot)
            await process([(a, v) for a, v, _ in hot])
            await clear_hot([(a, h) for a, _, h in hot])
            if st.get("stopped"):
                break
            since_ckpt += len(hot)
            if since_ckpt >= STATE_EVERY:
                since_ckpt = 0
                await checkpoint()
            continue
        if i >= len(order):
            break
        part = order[i:i + CHUNK_PASS]
        i += CHUNK_PASS
        skip = await _skip_set([a for a, _ in part], pass_ts)
        part = [(a, v) for a, v in part if a not in skip]
        if not part:
            continue
        await process(part)
        if st.get("stopped"):
            break
        since_ckpt += len(part)
        if since_ckpt >= STATE_EVERY:
            since_ckpt = 0
            await checkpoint()
    finished = "stopped" not in st
    await checkpoint(final=finished)
    if finished:
        stats = {"ts": st["ts"], "started": t0, "sec": st["ts"] - t0, "mode": st["mode"],
                 "accounts": st["done"], "ok": st["ok"], "err": st["err"], "found": st["found"],
                 "requests": st["requests"], "rpm": st["rpm"], "n_429": st["n_429"],
                 "fell_back": bool(st.get("fell_back")), "universe": uni, "resumed": resumed,
                 "workers": st.get("workers", 0), "hot": st.get("hot", 0)}
        await kv_set(STATS_KV, stats)
        log.info("sayım turu bitti: %s · %d hesap → %d poz (%d hata) · %d istek · %.1f dk · 429: %d · sıcak %d",
                 MODE_TR.get(st["mode"], st["mode"]), st["done"], st["found"], st["err"],
                 st["requests"], stats["sec"] / 60, st["n_429"], st.get("hot", 0))
        return stats
    return dict(st)


async def gap_wait(total: int = PASS_GAP, *, sleep=asyncio.sleep) -> int:
    """Tur arası nefes: GAP_STEP'lik adımlarla bekler, sıcak bekleyen belirirse erken
    çıkar (fill → defter dakikalar içinde). Döner: beklenen saniye."""
    waited = 0
    while waited < total:
        if await hot_pending():
            break
        step = min(GAP_STEP, total - waited)
        await sleep(step)
        waited += step
    return waited


async def loop(cfg, client) -> None:
    await asyncio.sleep(START_DELAY)
    while True:
        try:
            await beat("census")              # kapalıyken de nabız: bekçi görevi ölü sanmasın
            if enabled(cfg):
                await run_pass(cfg, client)
                await beat("census")
                await gap_wait(PASS_GAP)
                continue
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sayım hatası")
        await asyncio.sleep(IDLE_SEC)


# ────────────────────────────────────────────── okuma (kapsama kutusu, /tani)

def _n(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "?"
    if abs(f) >= 1_000_000:
        return f"{f / 1_000_000:.2f}M"
    if abs(f) >= 10_000:
        return f"{f / 1000:.1f}K"
    return f"{int(f)}"


def _dur(sec) -> str:
    try:
        s = int(sec)
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s} sn"
    if s < 3600:
        return f"{s // 60} dk"
    return f"{s / 3600:.1f} sa"


async def progress() -> dict | None:
    """Kapsama kutusu / mesaj için: {mode, running, pct, done, total, text, ...};
    hiç koşmadıysa None."""
    st = await kv_get(STATE_KV) or {}
    cst = await kv_get(STATS_KV) or {}
    if not st and not cst:
        return None
    running = bool(st) and not st.get("finished")
    mode = (st.get("mode") if st else None) or cst.get("mode")
    mode_tr = MODE_TR.get(mode, mode or "?")
    total, done = int(st.get("total") or 0), int(st.get("done") or 0)
    pct = (done / total * 100) if total else None
    out = {"mode": mode, "mode_tr": mode_tr, "running": running, "pct": pct, "done": done,
           "total": total, "last_sec": cst.get("sec"), "last_ts": cst.get("ts"),
           "found": (st.get("found") if running else cst.get("found")), "n_429": st.get("n_429") or cst.get("n_429")}
    if running and pct is not None:
        out["text"] = f"sayım %{pct:.0f} ({mode_tr})"
    elif cst:
        out["text"] = f"sayım tam ({mode_tr}, tur {_dur(cst.get('sec'))})"
    else:
        out["text"] = f"sayım başladı ({mode_tr})"
    return out


async def diag_line(cfg) -> str:
    """/tani satırı."""
    if not enabled(cfg):
        return "sayım (census): kapalı (census_enabled=0) — kapsama yalnız havuz kadar"
    st = await kv_get(STATE_KV) or {}
    cst = await kv_get(STATS_KV) or {}
    md = await kv_get(MODE_KV) or {}
    lb = await kv_get(LB_KV) or {}
    mode = (st.get("mode") if st else None) or cst.get("mode") or md.get("mode")
    head = "sayım (census): " + (MODE_TR.get(mode, mode) if mode else "açık")
    if mode == "single" and md.get("why"):
        head += f" ({md['why']})"
    parts = [head]
    t = now()
    if st and not st.get("finished"):
        total, done = int(st.get("total") or 0), int(st.get("done") or 0)
        pct = (done / total * 100) if total else 0.0
        parts.append(f"sürüyor %{pct:.0f} ({_n(done)}/{_n(total)} hesap) · {_n(st.get('found', 0))} poz"
                     f" · {st.get('err', 0)} hata · {st.get('rpm', 0)} istek/dk"
                     + (f" (hız {st['rpm_cap']}/dk'ya indi)" if st.get("rpm_cap") and st.get("n_429") else "")
                     + f" · {_dur(t - int(st.get('ts') or t))} önce")
    if cst:
        parts.append(f"son tur {_dur(cst.get('sec'))} · {_n(cst.get('accounts', 0))} hesap →"
                     f" {_n(cst.get('found', 0))} poz ({cst.get('err', 0)} hata)"
                     + (" · toplu→tek düştü" if cst.get("fell_back") else "")
                     + (f" · {_dur(t - int(cst['ts']))} önce" if cst.get("ts") else ""))
    if not (st or cst):
        parts.append("henüz koşmadı (açılıştan 5 dk sonra başlar)")
    n429 = (st.get("n_429") if st and not st.get("finished") else None) or cst.get("n_429")
    if n429:
        parts.append(f"429: {n429}")
    try:
        pend = await hot_pending()
    except Exception:
        pend = None
    hot_now = int(st.get("hot") or 0) if st and not st.get("finished") else int(cst.get("hot") or 0)
    parts.append(f"sıcak şerit: {pend if pend is not None else '?'} bekliyor · "
                 + ("bu tur" if st and not st.get("finished") else "son tur") + f" {hot_now} hesap")
    if lb.get("n") is not None:
        parts.append(f"evren: leaderboard {_n(lb['n'])} hesap (bakiye ≥ ${lb.get('floor', 0):.0f})"
                     + (f" · {_n(st.get('total') or 0)} toplam" if st.get("total") else ""))
    ws = await kv_get(WORKERS_KV) or {}
    if ws:
        act = await workers_active()
        newest = max(int((v or {}).get("ts") or 0) for v in ws.values())
        tot = sum(int((v or {}).get("ingested") or 0) for v in ws.values())
        parts.append(f"worker: {len(act)} aktif/{len(ws)} bilinen · son kira {_dur(t - newest)} önce"
                     f" · teslim {_n(tot)} hesap"
                     + (f" · yerel hız {int(getattr(cfg, 'census_rpm_local', 100))}/dk" if act else ""))
    return " · ".join(parts)
