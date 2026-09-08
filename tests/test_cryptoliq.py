"""Kripto liq yakını — kapı, kademeler, canlılık sondası, kapanış teyidi, marker
kuralı, bağlantı, mesaj, sembol çözümleme.

Pinlenenler:
  • kapı: ≥ min_usd, ≤ dist_pct, yönü doğru; BTC/ETH ve HIP-3 ('xyz:') girmez
  • coin başına TEK mesaj; bekleme pozisyon başına ve yalnız 1. kademe için
  • kademeler: ≤%2,5 💥 → ≤%1 🔥 (bekleme içinde de) → ≤%0,5 🚨 → yok olunca
    💀 likide (fill'de liquidation) ya da 🏁 kapandı; %3,75'e uzaklaşınca sıfırlanır
  • izlenen pozisyon $500K altına küçülse de takipte kalır (histerezis)
  • sonda: yükselme adayı + kademe ≥2 her tur, kademe 1 on dakikada bir
  • marker/kademe YALNIZ gönderim başarılıysa; chat/bot/tip yoksa sonda da yok
  • metnin peşinden resim (mum + liq çizgisi); resim düşerse metin gitmiştir
  • ana dex özeti kv'den, tazeyse istek yok; resolve_coin hisse > kripto, kPEPE
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db as dbm
from app.config import Config
from app.hl import universe as uni
from app.radar import cryptoliq as cl
from app.radar import sweeper

MARK = {"PUMP": 0.0032, "SOL": 170.0, "BTC": 100_000.0, "HYPE": 40.0}
A, B, C, D = ("0x" + c * 40 for c in "abcd")


def row(coin, addr, side, ntl, dist, lev=10.0, ts=None, entity=None):
    m = MARK[coin]
    liq = m * (1 - dist / 100) if side == "long" else m * (1 + dist / 100)
    return {"coin": coin, "address": addr, "side": side, "notional": float(ntl),
            "liq_px": liq, "leverage": lev, "entry_px": m, "ts": ts or dbm.now(),
            "entity": entity}


# ------------------------------------------------ 1) saf kapı
def test_near_liq():
    rows = [row("PUMP", A, "long", 1_200_000, 1.8),
            row("PUMP", B, "short", 700_000, 2.3),
            row("PUMP", C, "long", 900_000, 3.0),          # uzak
            row("PUMP", D, "long", 400_000, 1.0),          # küçük
            row("BTC", A, "long", 5_000_000, 1.0),         # majör hariç
            {"coin": "xyz:NVDA", "address": A, "side": "long", "notional": 2e6,
             "liq_px": 99.0},                              # HIP-3 hariç
            {"coin": "SOL", "address": B, "side": "short", "notional": 2e6,
             "liq_px": MARK["SOL"] * 0.99},                # short'un liq'i altta: tutarsız
            row("HYPE", C, "long", 600_000, 2.5)]          # tam sınır: dahil
    out = cl.near_liq(rows, MARK, 2.5, 500_000)
    assert set(out) == {"PUMP", "HYPE"}, out.keys()
    assert [p["address"] for p in out["PUMP"]] == [A, B], "yakından uzağa, eşik dışı yok"
    assert abs(out["PUMP"][0]["dist"] - 1.8) < 1e-9 and out["PUMP"][0]["mark"] == MARK["PUMP"]
    assert abs(out["HYPE"][0]["dist"] - 2.5) < 1e-9
    assert cl.near_liq(rows, {}, 2.5, 500_000) == {}, "fiyat yoksa aday yok"
    assert cl.needed_stage(2.6, 2.5, 1.0, 0.5) == 0 and cl.needed_stage(2.5, 2.5, 1.0, 0.5) == 1
    assert cl.needed_stage(1.0, 2.5, 1.0, 0.5) == 2 and cl.needed_stage(0.5, 2.5, 1.0, 0.5) == 3
    c = Config(); c.crypto_liq_dist2_pct = 3.0            # yanlış ayar: d2 > d1 kırpılır
    assert cl.stage_dists(c) == (2.5, 2.5, 0.5)
    print("✅ kapı) eşik/mesafe/yön sağlaması; BTC/ETH, HIP-3 ve tutarsız satır dışarıda; kademe sınırları")


# ------------------------------------------------ 2) ana dex özeti ayrıştırma
def test_parse_ctx():
    meta = {"universe": [{"name": "PUMP"}, {"name": "xyz:NVDA"}, {"name": "OLD", "isDelisted": True},
                         {"name": "SOL"}]}
    ctxs = [{"markPx": "0.0032", "openInterest": "1000", "funding": "0.0001",
             "dayNtlVlm": "5e6", "prevDayPx": "0.0030"},
            {"markPx": "100"}, {"markPx": "1"}, {"markPx": "170", "openInterest": "5"}]
    c = uni.parse_main_dex_ctx(meta, ctxs)
    assert set(c) == {"PUMP", "SOL"}, c
    assert c["PUMP"]["m"] == 0.0032 and c["PUMP"]["p"] == 0.0030 and c["PUMP"]["v"] == 5e6
    assert c["SOL"]["p"] is None, "prevDayPx yoksa None — uydurulmaz"
    print("✅ ctx) HIP-3 ve delist atlanır; prevDayPx yoksa None")


# ------------------------------------------------ 3) tarama fikstürü
class Client:
    """Sahte HL: meta_and_ctxs sayılır (kv tazeyken ÇAĞRILMAMALI); clearinghouse_all
    adres başına yanıt — `closed` kümesindeki adres artık pozisyon tutmuyor;
    `fills` adres → userFillsByTime yanıtı; `candles` 30dk mum listesi."""

    def __init__(self, positions, closed=(), fills=None, candles=None, fills_err=False, book=None):
        self.positions, self.closed = list(positions), set(closed)
        self.fills, self.candles_raw, self.fills_err = fills or {}, candles, fills_err
        self.ctx_calls, self.probes, self.fill_calls, self.candle_calls = 0, [], [], 0
        self.book_calls = []
        m = MARK["PUMP"]
        # PUMP defteri: her seviye ~$1.6M — $1.2M long liq'i (-%1.8) 0.00314'te yenir.
        # `book` bir sözlükse her hane için aynı; {3: …, 2: …} ise haneye göre.
        default = {"levels": [
            [{"px": str(m * 0.995), "sz": "5e8"}, {"px": str(m * 0.98), "sz": "5e8"},
             {"px": str(m * 0.97), "sz": "5e8"}],
            [{"px": str(m * 1.005), "sz": "5e8"}, {"px": str(m * 1.02), "sz": "5e8"},
             {"px": str(m * 1.03), "sz": "5e8"}]]}
        self.books = book if isinstance(book, dict) and set(book) <= {2, 3, None} else None
        self.book = default if book is None or self.books is not None else book

    async def l2_book(self, coin, n_sig_figs=None):
        self.book_calls.append((coin, n_sig_figs))
        if self.books is not None:
            return self.books.get(n_sig_figs)
        return self.book

    async def meta_and_ctxs(self, dex=""):
        self.ctx_calls += 1
        return [{"universe": [{"name": c} for c in MARK]},
                [{"markPx": str(m), "openInterest": "1", "funding": "0", "dayNtlVlm": "1"}
                 for m in MARK.values()]]

    async def clearinghouse_all(self, addr, dexes):
        self.probes.append(addr)
        aps = [] if addr in self.closed else [
            {"position": {"coin": p["coin"], "szi": str(p["notional"] / MARK[p["coin"]]
                                                          * (1 if p["side"] == "long" else -1)),
                          "positionValue": str(p["notional"]), "liquidationPx": str(p["liq_px"]),
                          "entryPx": str(p["entry_px"]), "leverage": {"value": str(p["leverage"])},
                          "unrealizedPnl": "0"}}
            for p in self.positions if p["address"] == addr]
        return {"main": {"assetPositions": aps, "marginSummary": {"accountValue": "1000000"}},
                "xyz": {"assetPositions": []}}

    async def user_fills_by_time(self, addr, start_ms, end_ms=None):
        self.fill_calls.append(addr)
        if self.fills_err:
            raise RuntimeError("HL 500")
        return self.fills.get(addr, [])

    async def candles(self, coin, interval, start_ms, end_ms):
        self.candle_calls += 1
        if self.candles_raw is None:
            raise RuntimeError("mum yok")
        return self.candles_raw


class Bot:
    def __init__(self, ok=True, photo_ok=True):
        self.ok, self.photo_ok, self.sent, self.photos = ok, photo_ok, [], []

    async def send(self, text, chat_id=None):
        if self.ok:
            self.sent.append((chat_id, text))
        return self.ok

    async def send_photo(self, png, caption="", chat_id=None):
        if self.photo_ok:
            self.photos.append((chat_id, caption, len(png)))
        return self.photo_ok


def synth_candles(mark, n=96):
    """HL candleSnapshot biçiminde (ms damgalı, string) sentetik 30dk mumlar."""
    t0 = (dbm.now() - n * 1800) * 1000
    out, px = [], mark * 0.98
    for i in range(n):
        o = px
        px = px * (1 + (0.0004 if i % 3 else -0.0005))
        out.append({"t": t0 + i * 1800 * 1000, "o": str(o), "h": str(max(o, px) * 1.001),
                    "l": str(min(o, px) * 0.999), "c": str(px), "v": "1"})
    return out


async def _seed(rows):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cl.db"))
    sweeper._probe_sem = None                    # önceki döngünün semaforu sızmasın
    async with dbm.db() as c:
        await c.execute("INSERT INTO tickers(coin,symbol) VALUES('xyz:SNDK','SNDK')")
        for p in rows:
            await c.execute(
                "INSERT INTO addr_positions(coin,address,dex,side,szi,entry_px,leverage,liq_px,"
                "upnl,notional,ts,closed_ts) VALUES(?,?,'',?,1,?,?,?,0,?,?,NULL)",
                (p["coin"], p["address"], p["side"], p["entry_px"], p["leverage"],
                 p["liq_px"], p["notional"], p["ts"]))
            if p.get("entity"):
                await c.execute("INSERT OR REPLACE INTO addresses(address,first_seen,entity)"
                                " VALUES(?,?,?)", (p["address"], p["ts"], p["entity"]))
    await set_marks(MARK)


async def set_marks(marks):
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {k: {"m": v, "oi": 1, "f": 0, "v": 1, "p": None}
                                             for k, v in marks.items()}, "ts": dbm.now()})


async def watch_row(coin, addr):
    async with dbm.db() as c:
        cur = await c.execute("SELECT * FROM cryptoliq_watch WHERE coin=? AND address=?", (coin, addr))
        r = await cur.fetchone()
        return dict(r) if r else None


def _cfg(chat="-100"):
    cfg = Config()
    cfg.crypto_chat_id = chat
    cfg.crypto_liq_min_usd = 500_000
    cfg.crypto_liq_dist_pct = 2.5
    cfg.crypto_liq_dist2_pct = 1.0
    cfg.crypto_liq_dist3_pct = 0.5
    cfg.crypto_liq_cooldown = 3600
    return cfg


def test_scan_flow():
    async def run():
        from app.notify import Notifier
        rows = [row("PUMP", A, "long", 1_200_000, 1.8, entity="vault"),
                row("PUMP", B, "short", 700_000, 2.3, lev=20),
                row("PUMP", C, "long", 900_000, 1.0),      # sonda: KAPANMIŞ → düşer
                row("PUMP", D, "long", 300_000, 0.5),      # küçük: aday değil
                row("BTC", A, "long", 9_000_000, 0.8),     # majör: aday değil
                row("SOL", B, "long", 800_000, 9.0)]       # uzak
        await _seed(rows)
        cfg = _cfg()
        cli = Client(rows, closed={C}, candles=synth_candles(MARK["PUMP"]))
        # (a) gönderim BAŞARISIZ (resim de metin de) → marker/kademe yok, failed=1, sonraki tur yeniden dener
        bad = Bot(False, photo_ok=False)
        out = await cl.scan(cfg, cli, Notifier(cfg, bad))
        assert cli.ctx_calls == 0, "kv tazeyken istek atılmamalı"
        assert out["coins"] == 3 and out["candidates"] == 3 and out["fresh"] == 3, out
        assert sorted(cli.probes) == sorted([A, B, C]) and out["probed"] == 3
        assert out["dropped_stale"] == 1 and out["failed"] == 1 and out["alerted"] == 0, out
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind='cryptoliq'")
            assert (await cur.fetchone())["n"] == 0, "başarısız gönderim marker yazdı"
            cur = await c.execute("SELECT closed_ts FROM addr_positions WHERE address=? AND coin='PUMP'", (C,))
            assert (await cur.fetchone())["closed_ts"], "sonda kapanışı damgalamalı"
        assert (await watch_row("PUMP", A) or {}).get("stage", 0) == 0, "başarısız gönderim kademe ilerletti"
        # (b) başarılı → coin başına TEK mesaj = resim + tam metin altyazı (birleşik),
        #     2 pozisyon, doğrulandı, vault etiketi, PROPR YOK
        good = Bot(True)
        cli2 = Client(rows, closed={C}, candles=synth_candles(MARK["PUMP"]))
        out2 = await cl.scan(cfg, cli2, Notifier(cfg, good))
        assert out2["alerted"] == 1 and out2["combined"] == 1 and out2["photos"] == 1, out2
        assert good.sent == [] and len(good.photos) == 1, "birleşik: ayrı metin mesajı olmamalı"
        chat, text, nbytes = good.photos[0]
        assert chat == "-100" and nbytes > 1000 and cli2.candle_calls == 1
        assert text.startswith("💥 <b>PUMP</b>") and "2 pozisyon" in text and "$1.9M" in text
        # zincir: en yakın (A, long -%1.8) patlarsa satış bid'leri yer → hedef + satır
        assert "💣 <b>Zincir</b>" in text and "$1.2M long" in text and "gidebilir" in text, text
        assert cli2.book_calls == [("PUMP", 3)] and out2["cascades"] == 1
        assert "$1.2M" in text and "%1.80 altta" in text and "%2.30 üstte" in text
        assert "🏦VAULT" in text and "doğrulandı" in text and "BTC" not in text and "PROPR" not in text
        assert "SATIŞ</b> ~$1.2M" in text and "ALIŞ</b> ~$700K" in text
        assert (await watch_row("PUMP", A))["stage"] == 1 and (await watch_row("PUMP", B))["stage"] == 1
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind='sent:cryptoliq'")
            assert (await cur.fetchone())["n"] == 1, "birleşik gönderim de sent: kaydı yazmalı"
        # (c) aynı tur tekrar → bekleme: mesaj yok; kademe 1 izlenenler 10 dk sondalanmaz
        cli3 = Client(rows, closed={C})
        out3 = await cl.scan(cfg, cli3, Notifier(cfg, good))
        assert out3["fresh"] == 0 and out3["alerted"] == 0 and cli3.probes == [], out3
        assert out3["tracked"] == 2 and good.sent == [] and len(good.photos) == 1
        # (d) YENİ pozisyon eşiğe girince mesaj gider; eskiler "daha önce bildirildi";
        #     mum çekilemeyince resim yok → düz metin mesajı
        new = row("PUMP", D, "short", 650_000, 1.2)
        async with dbm.db() as c:
            await c.execute("UPDATE addr_positions SET side='short', notional=?, liq_px=? WHERE address=? AND coin='PUMP'",
                            (new["notional"], new["liq_px"], D))
        cli4 = Client([r for r in rows if r["address"] != D] + [new], closed={C})
        out4 = await cl.scan(cfg, cli4, Notifier(cfg, good))
        assert out4["alerted"] == 1 and len(good.sent) == 1 and cli4.probes == [D], out4
        t4 = good.sent[0][1]
        assert "1 pozisyon" in t4 and "$650K" in t4 and "ayrıca 2 pozisyon daha eşikte" in t4, t4
        assert out4["photos"] == 0 and out4["combined"] == 0, "mum çekilemeyince resim yok, metin gitti"
        st = await dbm.kv_get("cryptoliq_stats")
        assert st and st["alerted"] == 1 and st["chat"] is True and st["top"][0]["coin"] == "PUMP"
        print("✅ tarama) başarısız gönderim marker/kademe yazmadı; coin başına tek mesaj + resim;"
              " kapanmış aday sondada düştü; bekleme pozisyon başına, yeni pozisyon yeni mesaj")
    asyncio.run(run())


# ------------------------------------------------ 4) kademeler: 💥 → 🔥 → 🚨 → 💀
def test_stages_and_liquidation():
    async def run():
        from app.notify import Notifier
        a = row("PUMP", A, "long", 1_200_000, 1.8)
        await _seed([a])
        cfg = _cfg(); bot = Bot(); nt = Notifier(cfg, bot)
        cli = Client([a])
        await cl.scan(cfg, cli, nt)
        assert len(bot.sent) == 1 and bot.sent[0][1].startswith("💥"), bot.sent
        # fiyat liq'e yaklaşır: %0,9 → bekleme içinde de 2. uyarı, her tur sondalanır
        m2 = a["liq_px"] / (1 - 0.9 / 100)
        await set_marks({**MARK, "PUMP": m2})
        cli2 = Client([a])
        out2 = await cl.scan(cfg, cli2, nt)
        assert out2["stage2"] == 1 and len(bot.sent) == 2 and cli2.probes == [A], (out2, cli2.probes)
        t2 = bot.sent[1][1]
        assert t2.startswith("🔥 <b>PUMP</b>") and "2. uyarı" in t2 and "≤%1" in t2 and "%0.90 altta" in t2, t2
        assert (await watch_row("PUMP", A))["stage"] == 2
        cli2b = Client([a])
        out2b = await cl.scan(cfg, cli2b, nt)
        assert out2b["alerted"] == 0 and cli2b.probes == [A], "kademe 2 her tur sondalanır, mesaj tekrar etmez"
        # %0,4 → SON UYARI
        m3 = a["liq_px"] / (1 - 0.4 / 100)
        await set_marks({**MARK, "PUMP": m3})
        out3 = await cl.scan(cfg, Client([a]), nt)
        assert out3["stage3"] == 1 and len(bot.sent) == 3 and bot.sent[2][1].startswith("🚨"), out3
        assert "SON UYARI" in bot.sent[2][1] and "≤%0.5" in bot.sent[2][1]
        assert (await watch_row("PUMP", A))["stage"] == 3
        # pozisyon yok oldu, fill'de likidasyon kaydı → 💀 LİKİDE OLDU (gerçekleşen fiyat)
        fills = {A: [{"coin": "PUMP", "px": str(a["liq_px"]), "sz": "1", "side": "A", "time": dbm.now() * 1000,
                      "dir": "Liquidated Cross Long", "liquidation": {"liquidatedUser": A, "markPx": "1", "method": "market"}}]}
        cli4 = Client([a], closed={A}, fills=fills)
        out4 = await cl.scan(cfg, cli4, nt)
        assert out4["closed"] == 1 and out4["closed_liq"] == 1 and out4["close_notes"] == 1, out4
        assert cli4.fill_calls == [A] and len(bot.sent) == 4
        t4 = bot.sent[3][1]
        assert t4.startswith("💀 <b>PUMP</b>") and "LİKİDE OLDU" in t4 and "gerçekleşen" in t4, t4
        assert "SATIŞ</b> piyasaya çarptı" in t4 and "son mesafe %0.40" in t4
        w = await watch_row("PUMP", A)
        assert w["closed_kind"] == "liq" and w["notified_ts"] and w["closed_px"], w
        # bir sonraki tur: tekrar yok, sonda yok
        cli5 = Client([a], closed={A}, fills=fills)
        out5 = await cl.scan(cfg, cli5, nt)
        assert out5["closed"] == 0 and out5["close_notes"] == 0 and len(bot.sent) == 4 and cli5.probes == []
        print("✅ kademe) 💥 → bekleme içinde 🔥 (2. uyarı) → 🚨 (son) → 💀 likide (fill teyidi, gerçekleşen fiyat); tekrar yok")
    asyncio.run(run())


# ------------------------------------------------ 5) sıfırlama + histerezis + kapanış türleri
def test_reset_hysteresis_close_kinds():
    async def run():
        from app.notify import Notifier
        a = row("PUMP", A, "long", 1_200_000, 1.8)
        await _seed([a])
        cfg = _cfg(); bot = Bot(); nt = Notifier(cfg, bot)
        await cl.scan(cfg, Client([a]), nt)
        assert len(bot.sent) == 1
        # %4'e uzaklaştı → kademe sıfır, mesaj yok; %2'ye dönüş bekleme içinde sessiz
        await set_marks({**MARK, "PUMP": a["liq_px"] / (1 - 4.0 / 100)})
        out = await cl.scan(cfg, Client([a]), nt)
        assert out["resets"] == 1 and (await watch_row("PUMP", A))["stage"] == 0 and len(bot.sent) == 1, out
        await set_marks({**MARK, "PUMP": a["liq_px"] / (1 - 2.0 / 100)})
        out = await cl.scan(cfg, Client([a]), nt)
        assert out["alerted"] == 0 and len(bot.sent) == 1, "bekleme içinde 1. kademe tekrar etmemeli"
        assert (await watch_row("PUMP", A))["stage"] == 1, "marker varken sessizce takibe alınır"
        # bekleme bitti (marker silindi) ve sıfırdan geliyorsa → yeniden 💥
        async with dbm.db() as c:
            await c.execute("DELETE FROM alerts_log WHERE kind='cryptoliq'")
            await c.execute("UPDATE cryptoliq_watch SET stage=0")
        out = await cl.scan(cfg, Client([a]), nt)
        assert out["alerted"] == 1 and len(bot.sent) == 2 and bot.sent[1][1].startswith("💥"), out
        # histerezis: kademe 2'ye çıkar, sonra $400K'ya küçülür → izlenmeye devam, %0,4'te SON UYARI
        await set_marks({**MARK, "PUMP": a["liq_px"] / (1 - 0.9 / 100)})
        out = await cl.scan(cfg, Client([a]), nt)
        assert out["stage2"] == 1 and len(bot.sent) == 3
        small = {**a, "notional": 400_000.0}
        async with dbm.db() as c:
            await c.execute("UPDATE addr_positions SET notional=400000 WHERE address=? AND coin='PUMP'", (A,))
        await set_marks({**MARK, "PUMP": a["liq_px"] / (1 - 0.4 / 100)})
        out = await cl.scan(cfg, Client([small]), nt)
        assert out["stage3"] == 1 and len(bot.sent) == 4 and "$400K" in bot.sent[3][1], out
        # kapanış, likidasyon kaydı yok → 🏁 kapatıldı; fill isteği düşerse → doğrulanamadı
        fills = {A: [{"coin": "PUMP", "px": "0.0032", "sz": "1", "side": "A", "time": dbm.now() * 1000, "dir": "Close Long"}]}
        out = await cl.scan(cfg, Client([small], closed={A}, fills=fills), nt)
        assert out["closed"] == 1 and out["closed_liq"] == 0 and len(bot.sent) == 5, out
        assert bot.sent[4][1].startswith("🏁") and "likidasyon kaydı yok" in bot.sent[4][1], bot.sent[4][1]
        # yeniden açılış (aynı adres, yeni pozisyon) → yeni satır, yeni 💥; kapanışta fill hatası → doğrulanamadı
        async with dbm.db() as c:
            await c.execute("DELETE FROM alerts_log WHERE kind='cryptoliq'")
            await c.execute("UPDATE addr_positions SET closed_ts=NULL, notional=900000 WHERE address=? AND coin='PUMP'", (A,))
        await set_marks({**MARK, "PUMP": a["liq_px"] / (1 - 1.5 / 100)})
        out = await cl.scan(cfg, Client([{**a, "notional": 900_000.0}]), nt)
        assert out["alerted"] == 1 and (await watch_row("PUMP", A))["stage"] == 1 and (await watch_row("PUMP", A))["closed_ts"] is None
        # kademe 1 on dakikada bir sondalanır: bu tur sonda yok, kapanış görülmez
        cli = Client([a], closed={A}, fills_err=True)
        out = await cl.scan(cfg, cli, nt)
        assert out["closed"] == 0 and cli.probes == [], out
        # …ama süpürme damgalamışsa sonda gerekmez: satır kapalı → kapanış, fill hatası → doğrulanamadı
        async with dbm.db() as c:
            await c.execute("UPDATE addr_positions SET closed_ts=? WHERE address=? AND coin='PUMP'", (dbm.now(), A))
        cli = Client([a], closed={A}, fills_err=True)
        out = await cl.scan(cfg, cli, nt)
        assert out["closed"] == 1 and cli.probes == [] and "doğrulanamadı" in bot.sent[-1][1], (out, bot.sent[-1][1])
        # kapanış notu kapalıysa sessiz ama notified_ts dolar
        cfg.crypto_liq_notify_close = False
        async with dbm.db() as c:
            await c.execute("UPDATE cryptoliq_watch SET notified_ts=NULL")
        n = len(bot.sent)
        out = await cl.scan(cfg, Client([a], closed={A}), nt)
        assert len(bot.sent) == n and (await watch_row("PUMP", A))["notified_ts"], "kapalıyken sessizce işaretlenir"
        print("✅ sıfırlama/histerezis) %3,75+ uzaklaşınca sıfır, bekleme içinde sessiz; küçülen izlenen"
              " pozisyon SON UYARI alır; kapanış türleri: kaydı yok / doğrulanamadı / kapalı=sessiz")
    asyncio.run(run())


# ------------------------------------------------ 5b) birleşik mesajın yedek yolları
def test_combined_fallbacks():
    """Resim reddedilirse metin gider (kademe ilerler, fail yok); metin altyazıya
    sığmazsa metin + kısa altyazılı resim (iki parça); _caption_fit sınırları."""
    async def run():
        from app.notify import Notifier
        a = row("PUMP", A, "long", 1_200_000, 1.8)
        # (1) sendPhoto düşer → metin yedeği
        await _seed([a])
        cfg = _cfg(); bot = Bot(photo_ok=False)
        out = await cl.scan(cfg, Client([a], candles=synth_candles(MARK["PUMP"])), Notifier(cfg, bot))
        assert out["alerted"] == 1 and out["combined"] == 0 and out["photos"] == 0 and out["failed"] == 0, out
        assert len(bot.sent) == 1 and bot.photos == [] and bot.sent[0][1].startswith("💥")
        assert (await watch_row("PUMP", A))["stage"] == 1
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) n FROM alerts_log WHERE kind LIKE 'fail:%'")
            assert (await cur.fetchone())["n"] == 0
        # (2) metin altyazıya sığmıyor → iki parça: metin + kısa altyazılı resim
        await _seed([a])
        bot2 = Bot()
        real = cl.CAPTION_MAX
        cl.CAPTION_MAX = 50
        try:
            out2 = await cl.scan(cfg, Client([a], candles=synth_candles(MARK["PUMP"])), Notifier(cfg, bot2))
        finally:
            cl.CAPTION_MAX = real
        assert out2["alerted"] == 1 and out2["combined"] == 0 and out2["photos"] == 1, out2
        assert len(bot2.sent) == 1 and len(bot2.photos) == 1
        assert "kaldı" in bot2.photos[0][1] and "PUMP" in bot2.photos[0][1] and len(bot2.photos[0][1]) < 120
        print("✅ birleşik yedek) resim düşünce metin gitti; uzun metin iki parça (kısa altyazı)")
    asyncio.run(run())
    from app.telegram.bot import _caption_fit
    tagged = "<b>" + "a" * 700 + "</b> <a href=\"https://x/" + "y" * 600 + "\">z</a>"   # etiketli 1400+, görünür ~702
    cap, is_html = _caption_fit(tagged)
    assert cap == tagged and is_html is True, "görünür metin sığıyorsa dokunulmaz"
    longv = "<b>başlık</b> &lt;x&gt; " + "ş" * 1500
    cap2, is_html2 = _caption_fit(longv)
    assert is_html2 is False and cap2.endswith("…") and len(cap2) <= 1001 and "<b>" not in cap2
    assert cap2.startswith("başlık <x>"), "düz metinde varlıklar çözülür"
    print("✅ altyazı) etiketler sınıra sayılmaz; taşan metin etiketsiz kesilir")


# ------------------------------------------------ 6) kapı kapalıysa sonda da yok
def test_send_gate():
    async def run():
        from app.notify import Notifier
        rows = [row("PUMP", A, "long", 1_200_000, 1.8)]
        await _seed(rows)
        for cfg, notifier, why in ((_cfg(chat=""), Notifier(_cfg(), Bot()), "CRYPTO_CHAT_ID"),
                                   (_cfg(), None, "bot yok"),
                                   (_cfg(), Notifier(_cfg(), None), "bot yok")):
            cli = Client(rows)
            out = await cl.scan(cfg, cli, notifier)
            assert why in out["skipped"] and out["candidates"] == 1, (why, out)
            assert cli.probes == [] and out["alerted"] == 0 and out["failed"] == 0, (why, out)
        cfg = _cfg(); cfg.notify_cryptoliq = False
        cli = Client(rows)
        out = await cl.scan(cfg, cli, Notifier(cfg, Bot()))
        assert "kapalı" in out["skipped"] and cli.probes == [], out
        cfg = _cfg(); cfg.crypto_liq_enabled = False
        out = await cl.scan(cfg, Client(rows), Notifier(cfg, Bot()))
        assert out["skipped"] == "kapalı"
        # kv bayatsa ve kapı açıksa fiyat İSTENİR (tek istek); grafik kapalıysa resim yok
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": 1}}, "ts": dbm.now() - 3600})
        cfg = _cfg(); cfg.crypto_liq_chart = False
        cli = Client(rows, candles=synth_candles(MARK["PUMP"])); bot = Bot()
        out = await cl.scan(cfg, cli, Notifier(cfg, bot))
        assert cli.ctx_calls == 1 and out["alerted"] == 1 and out["photos"] == 0 and cli.candle_calls == 0, out
        print("✅ kapı) chat/bot/tip yokken hesap var, sonda ve gönderim yok; kv bayatsa tek fiyat isteği; grafik ayarı")
    asyncio.run(run())


# ------------------------------------------------ 7) bağlantı
def test_wiring():
    from app.config import EDITABLE_FIELDS
    c = Config()
    for f in ("crypto_liq_enabled", "crypto_liq_min_usd", "crypto_liq_show_usd",
              "crypto_liq_chart_fit_all", "crypto_liq_dist_pct",
              "crypto_liq_dist2_pct", "crypto_liq_dist3_pct", "crypto_liq_notify_close",
              "crypto_liq_chart", "crypto_liq_cascade", "crypto_liq_poll_sec", "crypto_liq_cooldown",
              "notify_cryptoliq"):
        assert f in EDITABLE_FIELDS and hasattr(c, f), f
        assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
    assert "crypto_chat_id" not in EDITABLE_FIELDS, "chat id env-only kalmalı"
    assert c.crypto_liq_min_usd == 500_000 and c.crypto_liq_dist_pct == 2.5 and c.notify_cryptoliq is True
    assert c.crypto_liq_dist2_pct == 1.0 and c.crypto_liq_dist3_pct == 0.5
    # SAYFA eşiği BİLDİRİM eşiğinden düşük ("sayfa daha çok gösterir, alarm daha sıkı")
    assert c.crypto_liq_show_usd == 200_000 and c.crypto_liq_show_usd <= c.crypto_liq_min_usd
    assert c.crypto_liq_chart_fit_all is True
    for lbl, key in (("SAYFA", "crypto_liq_show_usd"), ("BİLDİRİM", "crypto_liq_min_usd")):
        assert lbl in EDITABLE_FIELDS[key]["label"], key
    env = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".env.example"), encoding="utf-8").read()
    assert "CRYPTO_LIQ_SHOW_USD=200000" in env
    from app.health import limits, periods
    assert "cryptoliq" in limits(c) and "cryptoliq" in periods(c)
    from app.notify import KINDS
    assert KINDS["cryptoliq"][0] == "notify_cryptoliq" and KINDS["cryptoliq"][2] == "high"
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src = open(os.path.join(root, "app", "main.py"), encoding="utf-8").read()
    assert '_spawn("cryptoliq"' in src
    assert "cryptoliq_watch" in open(os.path.join(root, "app", "db.py"), encoding="utf-8").read()
    assert "pillow" in open(os.path.join(root, "requirements.txt"), encoding="utf-8").read()
    for fn in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        assert os.path.exists(os.path.join(root, "app", "web", "static", "fonts", fn)), fn
    print("✅ bağlantı) 10 ayar künyeli, chat id env-only, bekçi/tip/döngü kayıtlı, tablo/font/pillow yerinde")


# ------------------------------------------------ 8) mesaj + grafik
def test_format_and_chart():
    from app.telegram import format as fmt
    fresh = [{**row("PUMP", A, "long", 1_200_000, 1.8, ts=dbm.now() - 47 * 60), "dist": 1.8}]
    t = fmt.crypto_liq_alert("PUMP", 0.0032, fresh, [], 2.5)
    assert "ölçüm 47dk önce" in t and "doğrulandı" not in t, t
    assert "PROPR" not in t, "PROPR satırı kaldırıldı — kanalın coinleri zaten PROPR'da"
    assert "≤%2.5" in t and "1 pozisyon" in t and "yatırım tavsiyesi değildir" in t
    t3 = fmt.crypto_liq_alert("PUMP", 0.0032, [{**fresh[0], "need": 3, "verified": True}], [], 0.5, stage=3)
    assert t3.startswith("🚨") and "SON UYARI" in t3 and "🚨 🟢 LONG" in t3 and "doğrulandı" in t3
    tc = fmt.crypto_liq_closed("PUMP", [{"address": A, "side": "short", "notional": 7e5, "liq_px": 0.0033,
                                         "last_dist": 0.3, "closed_kind": "liq", "closed_px": 0.00331}])
    assert "LİKİDE OLDU" in tc and "ALIŞ</b> piyasaya çarptı" in tc and "gerçekleşen 0.0033" in tc, tc
    from app.radar import liqchart, pricechart
    cs = pricechart.parse_candles(synth_candles(0.0032))
    png = liqchart.render("PUMP", cs, 0.0032, [{"px": 0.0032 * (1 - 0.018), "side": "long", "notional": 1.2e6,
                                               "dist": 1.8, "main": True}])
    assert png and png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000, "PNG üretilmedi"
    assert liqchart.render("PUMP", cs[:2], 0.0032, []) is None, "mum/seviye yoksa None"
    print("✅ mesaj/grafik) kademe başlıkları, PROPR yok, kapanış metni; PNG üretiliyor, yetersiz veride None")


# ------------------------------------------------ 8b) /coin anlık görüntüsü
def test_snapshot():
    async def run():
        rows = [row("PUMP", A, "long", 1_200_000, 1.8), row("PUMP", B, "short", 700_000, 0.9),
                row("PUMP", C, "long", 300_000, 0.3), row("PUMP", D, "short", 4_000_000, 6.0)]
        await _seed(rows)
        cfg = _cfg()
        from app.telegram import format as fmt
        s = await cl.snapshot(cfg, Client(rows, candles=synth_candles(MARK["PUMP"])), "PUMP")
        # SAYFA eşiği $200K: $300K'lık C de listeye girer (alarm eşiği hâlâ $500K)
        assert [p["address"] for p in s["rows"]] == [C, B, A, D], "≥$200K olanlar, yakından uzağa"
        assert s["n_all"] == 4 and s["n_big"] == 4 and s["mark"] == MARK["PUMP"] and s["png"]
        assert s["min_usd"] == 200_000 and s["alert_usd"] == 500_000 and s["n_more"] == 0
        t = fmt.crypto_liq_snapshot(s)
        assert t.startswith("🎯 <b>PUMP</b>") and "%0.90 üstte" in t and "$4.0M" in t and "canlı" in t
        assert s["cascade"] and s["cascade"]["direction"] == "up" and "💣 <b>Zincir</b>" in t, t
        # zincir tetiği ⭐ = en YAKIN anlamlı band: B'nin $700K short'u (%0,9). $300K (%0,3)
        # coin tabanının ($500K) altında olduğu için başlığa çıkmaz, $4.0M ise %6'da uzak.
        assert "$700K short" in t and "zorunlu alış" in t, t
        tl = t.splitlines()
        star = [ln for ln in tl if ln.endswith("⭐")]
        # ⭐ hâlâ $700K short: band seçimi ALARM tabanına bakar, liste tabanı onu kaydırmaz
        assert len(star) == 1 and star[0].startswith("🔴 SHORT <b>$700K</b>") and "👤" in star[0], t
        bands = [ln for ln in tl if ln[:1] in "🟢🔴"]
        assert bands[0].startswith("🟢 LONG <b>$300K</b>") and bands[1] == star[0], "bantlar mesafe sıralı"
        assert "kanala düşme eşiği $500K" in t, "iki eşik farkı mesajda dürüstçe yazılı"
        # ayar kapalı → satır yok, defter isteği yok
        cfg.crypto_liq_cascade = False
        cli_off = Client(rows, candles=synth_candles(MARK["PUMP"]))
        s_off = await cl.snapshot(cfg, cli_off, "PUMP")
        assert s_off["cascade"] is None and cli_off.book_calls == [] and "Zincir" not in fmt.crypto_liq_snapshot(s_off)
        cfg.crypto_liq_cascade = True
        assert "havuzda 4 açık pozisyon, 4'ü ≥ $200K" in t and "PROPR" not in t, t
        # $300K artık SAYFA eşiğini geçiyor (alarm eşiğini değil); pozisyon hiç yoksa nedeni
        small = [row("PUMP", C, "long", 300_000, 0.3)]
        await _seed(small)
        s2 = await cl.snapshot(cfg, Client(small), "PUMP")
        t2 = fmt.crypto_liq_snapshot(s2)
        # tek pozisyon = tek üyeli band: satır pozisyonun kendisi, ayrıca "En büyük tekler" yok
        assert s2["n_big"] == 1 and len(s2["rows"]) == 1 and "liq bantları" in t2 and s2["png"] is None
        assert t2.count("🟢 LONG <b>$300K</b>") == 1 and "👤" in t2 and "tekler" not in t2, t2
        s3 = await cl.snapshot(cfg, Client([]), "HYPE")
        assert s3["rows"] == [] and "açık pozisyon yok" in fmt.crypto_liq_snapshot(s3)
        # fiyat yoksa nedeni (kv'de olmayan coin, istek de boş dönüyor)
        s4 = await cl.snapshot(cfg, Client([]), "ZZZ")
        assert s4["mark"] is None and "fiyat alınamadı" in fmt.crypto_liq_snapshot(s4)
        print("✅ anlık) /coin: ≥$500K en yakın önce, canlı fiyat; eşik altı/boş/fiyatsız durumlar dürüst")
    asyncio.run(run())

# ------------------------------------------------ 8b2) PUMP 08.09: ⭐ en yakın anlamlı band
PM = 0.00424                                       # ekran görüntüsündeki fiyat


def test_near_band_beats_far_wall():
    """Kullanıcının 08.09 ekran görüntüsü: 06:28 alarmı "$2.2M LONG · liq 0.0042" diyor,
    09:07'deki /pump ise ⭐'yı %20-30'daki $17.6M duvara koyup AYNI pozisyonu en alta,
    "Büyük tekler"e gömüyordu — band seçimi yalnız TOPLAMA baktığı için %1,3'teki band
    listeye hiç giremiyordu. Artık yön başına önce "en yakın anlamlı band" alınır
    (≥ en büyük bandın %5'i ve ≥ coin tabanı) ve tek pozisyonluk band, pozisyonun
    kendisi olarak yazılır (adres + /takip aynı satırda, tekler listesinde tekrar yok)."""
    async def run():
        from app.radar import liqchart, liqmap
        from app.telegram import format as fmt
        t, rows = dbm.now(), []

        def add(side, dist, ntl):
            liq = PM * (1 - dist / 100) if side == "long" else PM * (1 + dist / 100)
            rows.append({"coin": "PUMP", "address": "0x%040x" % (len(rows) + 1), "side": side,
                         "notional": float(ntl), "liq_px": liq, "leverage": 10.0,
                         "entry_px": PM, "ts": t, "entity": None})

        add("long", 1.34, 2_200_000)                                    # ⭐ olması gereken (tek)
        for k in range(7):
            add("short", 7.7 + k * 0.3, 258_000 / 7)                    # $258K · %7,7-9,5
        for k in range(20):
            add("long", 3.2 + k * 0.08, 3_100_000 / 20)                 # araya giren şişmanlar:
        for k in range(20):
            add("long", 5.6 + k * 0.09, 2_600_000 / 20)                 # ikisi de $2.2M'den büyük
        for k in range(58):
            add("long", 10.2 + k * 0.08, 6_300_000 / 58)                # $6.3M · %10-15
        for k in range(83):
            add("long", 20.5 + k * 0.1, 17_600_000 / 83)                # $17.6M · %20-30 (duvar)
        # regresyon: eski kural (yalnız toplam) yakın bandı listeden düşürüyordu
        near = [{"side": r["side"], "notional": r["notional"], "liq_px": r["liq_px"],
                 "dist": abs(r["liq_px"] - PM) / PM * 100} for r in rows]
        has22 = lambda bs: any(abs(b["total"] - 2_200_000) < 1 for b in bs)   # noqa: E731
        assert not has22(liqmap.clusters(near, PM, 50.0, per_side=3)), "eski davranış korunuyor"
        assert has22(liqmap.clusters(near, PM, 50.0, per_side=3,
                                     near_share=cl.NEAR_BAND_SIG_SHARE, near_min=500_000))
        await _seed(rows)
        await set_marks({**MARK, "PUMP": PM})
        cfg = _cfg()
        cfg.crypto_liq_cascade = False                 # bu vakada konu defter değil
        s = await cl.snapshot(cfg, Client(rows, candles=synth_candles(PM)), "PUMP")
        mb = s["main_band"]
        assert mb and mb["n"] == 1 and abs(mb["total"] - 2_200_000) < 1 and mb["dist_lo"] < 2, mb
        assert s["clusters"][0] is mb, "en yakın band aynı zamanda ilk satır"
        assert [b["dist_lo"] for b in s["clusters"]] == sorted(b["dist_lo"] for b in s["clusters"])
        assert has22(s["clusters"]) and any(abs(b["total"] - 17_600_000) < 1 for b in s["clusters"]), "duvar da listede"
        # SAYFA eşiği $200K: duvarın 83 üyesi ($212K'lık) de tek tek listelenir; ⭐ değişmez
        assert s["n_big"] == 84 and s["rows"][0]["notional"] == 2_200_000.0 and len(s["rows"]) == 60
        assert s["n_more"] == 24 and s["min_usd"] == 200_000 and s["alert_usd"] == 500_000
        txt = fmt.crypto_liq_snapshot(s, offers=[74])
        line = txt.splitlines()[2]
        assert line.startswith("🟢 LONG <b>$2.2M</b>") and "liq 0.0042" in line, line
        assert "/takip_74" in line and "👤" in line and line.endswith("⭐"), line
        assert "bandı" not in line and txt.count("$2.2M") == 1, txt
        assert "LONG bandı <b>$17.6M</b>" in txt and "SHORT bandı <b>$258K</b>" in txt, txt
        assert "Büyük tekler" in txt and txt.count("👤") == 60, "her ≥$200K pozisyon tek satır"
        assert "24 pozisyon daha ≥ $200K" in txt, "kesilen sayı dürüstçe söylenir"
        # foto altyazısı ve grafik başlığı da tek pozisyonda "band/küme" demez
        capt = fmt.crypto_liq_photo_caption(s)
        assert capt.startswith("📈 <b>PUMP</b> · LONG $2.2M · 0.0042 · %1.3") and "bandı" not in capt, capt
        src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "app", "radar", "liqchart.py"), encoding="utf-8").read()
        assert '(main.get("n") or 0) > 1' in src, "tek pozisyonluk band grafikte 'kümesi' demez"
        assert s["png"] is None or s["png"][:8] == b"\x89PNG\r\n\x1a\n"
        assert liqchart.render is not None
        print("✅ PUMP 08.09) ⭐ = en yakın anlamlı band ($2.2M @%1,3); duvar listede ama başta değil;"
              " tek pozisyonluk band adres + /takip taşır, tekler listesinde tekrar etmez")
    asyncio.run(run())

# ------------------------------------------------ 8c) zincir: ince defter uzanmıyorsa kaba defter
def test_cascade_fallback():
    async def run():
        from app.telegram import format as fmt
        m = MARK["PUMP"]
        rows = [row("PUMP", A, "long", 1_200_000, 1.8)]           # liq 0.982·m
        await _seed(rows)
        cfg = _cfg()
        fine = {"levels": [[{"px": str(m * 0.995), "sz": "5e8"}, {"px": str(m * 0.99), "sz": "5e8"}], []]}
        wide = {"levels": [[{"px": str(m * 0.99), "sz": "5e8"}, {"px": str(m * 0.98), "sz": "5e8"},
                            {"px": str(m * 0.97), "sz": "5e8"}, {"px": str(m * 0.96), "sz": "5e8"}], []]}
        # (1) 3 hane liq'e uzanmıyor (en düşük bid 0.99·m > liq 0.982·m), 2 hane uzanıyor → kaba sonuç
        cli = Client(rows, candles=synth_candles(m), book={3: fine, 2: wide})
        s = await cl.snapshot(cfg, cli, "PUMP")
        assert cli.book_calls == [("PUMP", 3), ("PUMP", 2)], cli.book_calls
        assert s["cascade"] and s["cascade"]["coarse"] is True and not s["cascade"]["no_book"]
        assert abs(s["cascade"]["end_px"] - m * 0.98) < 1e-12, s["cascade"]
        t = fmt.crypto_liq_snapshot(s)
        assert "kaba defter (2 anlamlı hane)" in t and "gidebilir" in t, t
        # (2) ikisi de uzanmıyor → tek satır, en geniş görünüm yazılı
        cli2 = Client(rows, book={3: fine, 2: fine})
        s2 = await cl.snapshot(cfg, cli2, "PUMP")
        assert s2["cascade"]["no_book"] and cli2.book_calls == [("PUMP", 3), ("PUMP", 2)]
        t2 = fmt.crypto_liq_snapshot(s2)
        assert "uzanmıyor" in t2 and "en geniş görünüm fiyattan %-1.0'e kadar" in t2, t2
        # (3) ince defter yetiyorsa tek istek
        cli3 = Client(rows)
        s3 = await cl.snapshot(cfg, cli3, "PUMP")
        assert cli3.book_calls == [("PUMP", 3)] and s3["cascade"]["coarse"] is False
        print("✅ kaba defter) 3 hane uzanmayınca 2 hane; ikisi de uzanmıyorsa en geniş görünüm; yetiyorsa tek istek")
    asyncio.run(run())


# ------------------------------------------------ 9) sembol çözümleme
def test_resolve():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "rs.db"))
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,symbol,dex) VALUES('xyz:SNDK','SNDK','xyz')")
        await dbm.kv_set(uni.MAIN_VOL_KV, {"vols": {"PUMP": 1.0, "kPEPE": 2.0}, "ts": dbm.now()})
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HYPE": {"m": 40.0}}, "ts": dbm.now()})
        eq = await uni.resolve_coin("sndk")
        assert eq and eq["kind"] == "equity" and eq["coin"] == "xyz:SNDK"
        assert (await uni.resolve_coin("xyz:SNDK"))["kind"] == "equity"
        pump = await uni.resolve_coin("pump")
        assert pump == {"coin": "PUMP", "symbol": "PUMP", "dex": "", "kind": "crypto", "klass": "kripto"}, pump
        assert (await uni.resolve_coin("KPEPE"))["coin"] == "kPEPE", "büyük harf → gerçek ad"
        assert (await uni.resolve_coin("hype"))["coin"] == "HYPE", "ctx kv'si de evren"
        assert await uni.resolve_coin("xxx") is None and await uni.resolve_coin("") is None
        names = await uni.crypto_names()
        assert names == {"HYPE": "HYPE", "PUMP": "PUMP", "KPEPE": "kPEPE"}, names
        print("✅ çözümleme) hisse önce, sonra ana dex; kPEPE büyük/küçük harf; bilinmeyen None")
    asyncio.run(run())


test_near_liq()
test_parse_ctx()
test_scan_flow()
test_stages_and_liquidation()
test_reset_hysteresis_close_kinds()
test_combined_fallbacks()
test_send_gate()
test_wiring()
test_format_and_chart()
test_snapshot()
test_near_band_beats_far_wall()
test_cascade_fallback()
test_resolve()
print("\n✅ KRİPTO LIQ TESTLERİ GEÇTİ")
