"""Demo/tohum verisi — sayfalar VERİLİ hâlde görülsün diye.

    DB_PATH=/tmp/demo.db python scripts/seed_demo.py
    DB_PATH=/tmp/demo.db python -m uvicorn app.main:app --port 8093

Ne ekler: 4 hisse (SNDK, MU, WDC, NVDA) · SNDK'da 40 likidasyonlu pozisyon
(duvar, uzak, liq'siz) · NVDA'da skorlu şüpheli pozisyonlar · yaklaşan ve
değerlendirilmiş bilançolar · defter duvarı, takip, liq_watch (kripto) ·
liq attack adayları + geçmiş saldırılar · kapalı seans alarm kayıtları ·
saat istatistikleri (sentetik mumlardan gerçek compute_stats ile) · fill'ler ·
adres sicilleri. Gerçek veriye dokunmaz: yalnız verilen DB_PATH'e yazar.
Idempotent değildir — temiz bir DB'ye çalıştırın."""
import asyncio
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB = os.environ.get("DB_PATH") or os.path.join(os.getcwd(), "demo.db")
os.environ["DB_PATH"] = DB
from app import db as dbm                      # noqa: E402
from app.radar import hourstats as hs          # noqa: E402

MARK = {"xyz:SNDK": 149.28, "xyz:MU": 118.40, "xyz:WDC": 92.15, "xyz:NVDA": 176.30}
SYM = {c: c.split(":")[1] for c in MARK}


def addr(r):
    return "0x" + f"{r.getrandbits(160):040x}"


def candles(r, coin, days=70):
    """Sentetik 1 saatlik mumlar: ABD açık saatlerde hafif pozitif, 14-15 ET
    güçlü, 03-04 ET zayıf; kapalı seansta gürültü. compute_stats gerçek."""
    out = []
    t0 = dbm.now() - days * 86400
    px = MARK[coin] * 0.85
    for i in range(days * 24):
        t = t0 + i * 3600
        et = datetime.fromtimestamp(t, hs.ET).hour
        drift = 0.0
        if 9 <= et < 16:
            drift = 0.0006
        if et in (14, 15):
            drift = 0.0028
        if et in (3, 4):
            drift = -0.0022
        if et in (20, 21):
            drift = 0.0015          # kapalıyken de bir şeyler oluyor (perp)
        ret = drift + r.gauss(0, 0.004)
        o = px
        px = px * (1 + ret)
        out.append({"t": t, "o": o, "h": max(o, px) * 1.001, "l": min(o, px) * 0.999,
                    "c": px, "v": r.uniform(1e5, 9e5)})
    return out


async def main():
    await dbm.init_db(DB)
    now = dbm.now()
    r = random.Random(7)
    async with dbm.db() as c:
        for coin, sym in SYM.items():
            await c.execute("INSERT OR IGNORE INTO tickers(coin,symbol) VALUES(?,?)", (coin, sym))
            await c.execute(
                "INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume)"
                " VALUES(?,?,?,?,?,?)",
                (coin, now, MARK[coin], r.uniform(8e5, 3e6) / MARK[coin],
                 r.choice([0.0001, -0.00004, 0.00032, 0.00008]), r.uniform(3e6, 6e7)))
            await c.execute(
                "INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume)"
                " VALUES(?,?,?,?,?,?)",
                (coin, now - 86400, MARK[coin] * r.uniform(0.96, 1.03), 1000, 0.0001, 1e7))
        await c.execute("DELETE FROM positions_current")

        # ---- SNDK: 40 likidasyonlu pozisyon ----
        rows = []
        for i in range(24):
            dist = r.choice([0.3, 0.7, 1.4, 2.2, 2.6, 2.8, 4.1, 6.5, 8.8, 13.0, 27.0])
            ntl = r.randint(60_000, 2_600_000)
            if i == 0:
                dist, ntl = 2.4, 20_260_000
            rows.append(("short", ntl, MARK["xyz:SNDK"] * (1 + dist / 100), r.choice([2, 3, 5, 10])))
        for i in range(16):
            dist = r.choice([0.4, 1.1, 1.8, 3.4, 4.6, 7.0, 9.5, 17.0, 33.0])
            ntl = r.randint(80_000, 4_000_000)
            if i == 0:
                dist, ntl = 1.6, 12_720_000
            if i == 1:
                dist, ntl = 9.5, 10_110_000
            rows.append(("long", ntl, MARK["xyz:SNDK"] * (1 - dist / 100), r.choice([2, 3, 5, 8])))
        rows.append(("long", 8_160_000, MARK["xyz:SNDK"] * (1 - 0.62), 3))
        rows.append(("short", 1_500_000, None, 4))
        sndk_addrs = []
        for i, (side, ntl, liq, lev) in enumerate(rows):
            a = addr(r); sndk_addrs.append(a)
            entry = MARK["xyz:SNDK"] * r.uniform(0.82, 1.12)
            await c.execute(
                "INSERT INTO positions_current(coin,address,ts,side,szi,entry_px,leverage,"
                "liq_px,upnl,notional,opened_ts,first_seen_ts,score,score_reasons)"
                " VALUES('xyz:SNDK',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a, now - r.randint(60, 7200), side, ntl / MARK["xyz:SNDK"] * (1 if side == "long" else -1),
                 entry, lev, liq, (MARK["xyz:SNDK"] - entry) * ntl / entry * (1 if side == "long" else -1),
                 ntl, now - r.randint(3600, 20 * 86400), now - 86400,
                 r.choice([0, 0, 10, 25]), "[]"))

        # ---- NVDA: skorlu şüpheliler (bilançoya 2 gün) + bir MM ----
        big = [("long", 4_800_000, 72, ["taze cüzdan", "bilançoya 2 gün", "büyük poz"]),
               ("long", 1_900_000, 55, ["bilançoya 2 gün", "tek hisse"]),
               ("short", 1_200_000, 44, ["bilançoya 2 gün"]),
               ("long", 9_400_000, 5, ["mm"])]
        nvda_addrs = []
        for i, (side, ntl, score, reasons) in enumerate(big):
            a = addr(r); nvda_addrs.append(a)
            entry = MARK["xyz:NVDA"] * r.uniform(0.95, 1.02)
            await c.execute(
                "INSERT INTO positions_current(coin,address,ts,side,szi,entry_px,leverage,"
                "liq_px,upnl,notional,opened_ts,first_seen_ts,score,score_reasons)"
                " VALUES('xyz:NVDA',?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (a, now - 600, side, ntl / MARK["xyz:NVDA"] * (1 if side == "long" else -1), entry,
                 5, MARK["xyz:NVDA"] * (0.86 if side == "long" else 1.15), 12_000, ntl,
                 now - r.randint(3600, 60 * 3600), now - 3600, score, json.dumps(reasons)))
            if i == 3:
                await c.execute("INSERT OR REPLACE INTO addresses(address,first_seen,entity,label)"
                                " VALUES(?,?,'mm','wintermute?')", (a, now - 90 * 86400))
        # ---- MU / WDC: liq attack hedefleri ----
        tg = [("xyz:SNDK", sndk_addrs[0], "long", 1_900_000, 147.60),
              ("xyz:MU", addr(r), "short", 2_400_000, 120.40),
              ("xyz:MU", addr(r), "short", 700_000, 121.00),
              ("xyz:WDC", addr(r), "long", 2_050_000, 89.10)]
        for coin, a, side, ntl, liq in tg:
            await c.execute(
                "INSERT OR REPLACE INTO positions_current(coin,address,ts,side,szi,entry_px,leverage,"
                "liq_px,upnl,notional,opened_ts,first_seen_ts) VALUES(?,?,?,?,?,?,5,?,0,?,?,?)",
                (coin, a, now, side, ntl / MARK[coin], MARK[coin], liq, ntl, now - 5 * 86400, now - 86400))

        # ---- adres sicilleri, bakiye, watchlist ----
        for i, a in enumerate(sndk_addrs[:6] + nvda_addrs[:2]):
            await c.execute(
                "INSERT OR REPLACE INTO addresses(address,first_seen,first_deposit_ts,last_deposit_ts,"
                "hits,misses,watchlist,account_value,account_ts) VALUES(?,?,?,?,?,?,?,?,?)",
                (a, now - 200 * 86400, now - 180 * 86400, now - r.randint(1, 30) * 86400,
                 r.choice([0, 2, 3, 5]), r.choice([0, 1, 1, 2]), 1 if i < 3 else 0,
                 r.choice([None, 2.4e6, 180_000, 51e6]), now - r.choice([600, 4 * 3600, 30 * 3600])))

        # ---- bilançolar: 2 yaklaşan, 1 değerlendirilmiş ----
        d_et = datetime.now(hs.ET)
        for sym, days, hint, note in (("NVDA", 2, "amc", "nasdaq"), ("MU", 6, "bmo", "yahoo · onaylanmadı")):
            d = (d_et + timedelta(days=days)).strftime("%Y-%m-%d")
            await c.execute(
                "INSERT OR IGNORE INTO earnings_events(symbol,coin,date_et,hour_hint,source,note,created_ts)"
                " VALUES(?,?,?,?,?,?,?)", (sym, f"xyz:{sym}", d, hint, "nasdaq", note, now))
        d = (d_et - timedelta(days=9)).strftime("%Y-%m-%d")
        await c.execute(
            "INSERT OR IGNORE INTO earnings_events(symbol,coin,date_et,hour_hint,source,evaluated,"
            "move_pct,result_note,created_ts) VALUES('SNDK','xyz:SNDK',?,'amc','nasdaq',1,?,?,?)",
            (d, 8.4, "En büyük poz LONG $12.7M — haklı çıktı (+8.4%)", now - 9 * 86400))

        # ---- defter duvarı, takip, liq_watch (ana dex), fill'ler ----
        await c.execute(
            "INSERT INTO book_walls(coin,side,px_lo,px_hi,sz,notional,dist_pct,mark_px,address,"
            "first_ts,last_ts,peak_notional,active) VALUES('xyz:SNDK','ask',151.0,151.4,60000,9060000,"
            "1.3,?,NULL,?,?,9060000,1)", (MARK["xyz:SNDK"], now - 3 * 3600, now - 120))
        await c.execute(
            "INSERT INTO book_walls(coin,side,px_lo,px_hi,sz,notional,dist_pct,mark_px,address,"
            "first_ts,last_ts,peak_notional,active) VALUES('xyz:MU','bid',115.8,116.2,38000,4410000,"
            "2.0,?,?,?,?,7100000,1)", (MARK["xyz:MU"], tg[1][1], now - 40 * 60, now - 60))
        await c.execute(
            "INSERT INTO trackers(address,coin,symbol,side,base_szi,last_szi,base_notional,created_ts,"
            "expires_ts,active,last_check_ts,entry_px) VALUES(?,'xyz:SNDK','SNDK','long',12800,12100,"
            "1900000,?,?,1,?,147.9)", (sndk_addrs[0], now - 2 * 86400, now + 5 * 86400, now - 900))
        await c.execute(
            "INSERT OR REPLACE INTO liq_watch(address,coin,side,notional,liq_px,stage,last_dist,updated_ts)"
            " VALUES(?,'BTC','long',3200000,108400,1,2.1,?)", (addr(r), now - 300))
        for i in range(8):
            a = sndk_addrs[i % 5]
            await c.execute(
                "INSERT OR IGNORE INTO fills(coin,tid,address,side,px,sz,notional,ts,taker)"
                " VALUES('xyz:SNDK',?,?,?,?,?,?,?,?)",
                (f"t{i}", a, r.choice(["buy", "sell"]), MARK["xyz:SNDK"] * r.uniform(0.99, 1.01),
                 r.uniform(400, 9000), r.uniform(60_000, 1_300_000), now - r.randint(300, 40 * 3600),
                 r.choice([0, 1])))

        # ---- liq attack: adaylar + geçmiş saldırılar ----
        wk = hs.weekend_window(now, 0)
        anchor = wk[0] if wk else now - 86400
        await c.execute("DELETE FROM liq_attack_candidates"); await c.execute("DELETE FROM liq_attacks")
        for row in (("xyz:SNDK", "down", 149.28, 1.50, 2_640_000, 184_000, 14.3, 1, 3, 147.04, 0.82, 1),
                    ("xyz:MU", "up", 118.40, 2.25, 3_100_000, 1_260_000, 2.5, 0, 5, 121.06, -0.35, 1),
                    ("xyz:WDC", "down", 92.15, 3.75, 2_050_000, 2_900_000, 0.7, 0, 4, 88.69, 1.10, 0)):
            await c.execute(
                "INSERT INTO liq_attack_candidates(coin,direction,mark,dist_pct,liq_usd,cost_usd,score,"
                "book_thin,n_pos,target_px,dev_close,hot,ts,weekend_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*row, now - 240, anchor))
        past = anchor - 7 * 86400
        await c.execute(
            "INSERT INTO liq_attacks(coin,ts_start,ts_peak,ts_end,direction,ref_px,extreme_px,move_pct,"
            "liq_usd,n_liq,predicted_score,weekend_ts,found_ts) VALUES('xyz:SNDK',?,?,?,'down',151.10,"
            "147.62,2.30,3120000,4,6.8,?,?)", (past + 9 * 3600, past + 9 * 3600 + 420, past + 9 * 3600 + 1500, past, now))
        await c.execute(
            "INSERT INTO liq_attacks(coin,ts_start,ts_peak,ts_end,direction,ref_px,extreme_px,move_pct,"
            "liq_usd,n_liq,predicted_score,weekend_ts,found_ts) VALUES('xyz:WDC',?,?,?,'up',90.20,92.05,"
            "2.05,1450000,2,NULL,?,?)", (past + 30 * 3600, past + 30 * 3600 + 300, past + 30 * 3600 + 2100, past, now))

        # ---- kapalı seans: çıpa fiyatları + alarm kayıtları ----
        anc = hs.last_close_ts(now, 0)
        for coin, dev in (("xyz:SNDK", 2.05), ("xyz:MU", 1.98), ("xyz:NVDA", 0.21)):
            await c.execute(
                "INSERT OR REPLACE INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume)"
                " VALUES(?,?,?,1000,0,5000000)", (coin, anc - 60, MARK[coin] / (1 + dev / 100)))
        for kind, key in (("offhours", f"dev:xyz:SNDK:{anc}:+4"), ("sent:offhours", f"dev:xyz:SNDK:{anc}:+4"),
                          ("fail:offhours", f"dev:xyz:MU:{anc}:+3")):
            await c.execute("INSERT INTO alerts_log(kind,key,ts,payload) VALUES(?,?,?,'')", (kind, key, now - 600))

    # ---- kv: saat istatistikleri (gerçek compute_stats), künyeler ----
    for coin in ("xyz:SNDK", "xyz:NVDA", "xyz:MU"):
        rec = hs.compute_stats(candles(r, coin))
        assert rec, "compute_stats None döndü"
        rec["ts"] = now
        await dbm.kv_set(f"hstats:{coin}", rec)
    await dbm.kv_set("fills_count", 8)
    await dbm.kv_set("specialists_cache", [
        {"address": sndk_addrs[1], "coin": "xyz:SNDK", "symbol": "SNDK", "n": 14, "vol": 6.2e6,
         "hits": 3, "misses": 1, "watchlist": 1, "open": {"side": "long", "notional": 520_000}}])
    await dbm.kv_set("offhours_stats", {"dev": 1, "spike": 0, "failed": 1, "looked": 3, "skipped": "", "ts": now})
    await dbm.kv_set("liqattack_stats", {"window": bool(wk), "coins": 3, "books": 3, "book_err": 0,
                                         "candidates": 2, "alerted": 2, "failed": 0,
                                         "skipped": "" if wk else "hafta sonu değil — pencere kapalı",
                                         "ts": now - 240})
    print(f"tohum hazır → {DB}")


if __name__ == "__main__":
    asyncio.run(main())
