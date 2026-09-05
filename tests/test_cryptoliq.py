"""Kripto liq yakını — kapı, tarama, canlılık sondası, marker kuralı, bağlantı,
mesaj, sembol çözümleme.

Pinlenenler:
  • kapı: ≥ min_usd, ≤ dist_pct, yönü doğru; BTC/ETH ve HIP-3 ('xyz:') girmez
  • coin başına TEK mesaj; bekleme pozisyon başına (yeni pozisyon → yeni mesaj,
    eskiler "daha önce bildirildi" diye anılır)
  • mesajdan önce sonda: kapanmış pozisyon düşer, açık olan "doğrulandı" yazar
  • marker YALNIZ gönderim başarılıysa; chat/bot/tip yoksa sonda da gönderim de yok
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
    print("✅ kapı) eşik/mesafe/yön sağlaması; BTC/ETH, HIP-3 ve tutarsız satır dışarıda")


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
    adres başına yanıt — `closed` kümesindeki adres artık pozisyon tutmuyor."""

    def __init__(self, positions, closed=()):
        self.positions, self.closed = positions, set(closed)
        self.ctx_calls, self.probes = 0, []

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


class Bot:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    async def send(self, text, chat_id=None):
        if self.ok:
            self.sent.append((chat_id, text))
        return self.ok


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
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {k: {"m": v, "oi": 1, "f": 0, "v": 1, "p": None}
                                             for k, v in MARK.items()}, "ts": dbm.now()})


def _cfg(chat="-100"):
    cfg = Config()
    cfg.crypto_chat_id = chat
    cfg.crypto_liq_min_usd = 500_000
    cfg.crypto_liq_dist_pct = 2.5
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
        cli = Client(rows, closed={C})
        # (a) gönderim BAŞARISIZ → marker yok, failed=1, sonraki tur yeniden dener
        bad = Bot(False)
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
        # (b) başarılı → coin başına TEK mesaj, 2 pozisyon, doğrulandı notu, vault etiketi
        good = Bot(True)
        cli2 = Client(rows, closed={C})
        out2 = await cl.scan(cfg, cli2, Notifier(cfg, good))
        assert out2["alerted"] == 1 and len(good.sent) == 1, out2
        chat, text = good.sent[0]
        assert chat == "-100" and "PUMP" in text and "2 pozisyon" in text and "$1.9M" in text
        assert "$1.2M" in text and "%1.8 altta" in text and "%2.3 üstte" in text
        assert "🏦VAULT" in text and "doğrulandı" in text and "BTC" not in text
        assert "SATIŞ</b> ~$1.2M" in text and "ALIŞ</b> ~$700K" in text
        # (c) aynı tur tekrar → bekleme: mesaj yok, sonda yok
        cli3 = Client(rows, closed={C})
        out3 = await cl.scan(cfg, cli3, Notifier(cfg, good))
        assert out3["fresh"] == 0 and out3["alerted"] == 0 and cli3.probes == [], out3
        assert len(good.sent) == 1
        # (d) YENİ pozisyon eşiğe girince mesaj gider; eskiler "daha önce bildirildi"
        new = row("PUMP", D, "short", 650_000, 1.2)
        async with dbm.db() as c:
            await c.execute("UPDATE addr_positions SET side='short', notional=?, liq_px=? WHERE address=? AND coin='PUMP'",
                            (new["notional"], new["liq_px"], D))
        cli4 = Client([r for r in rows if r["address"] != D] + [new], closed={C})
        out4 = await cl.scan(cfg, cli4, Notifier(cfg, good))
        assert out4["alerted"] == 1 and len(good.sent) == 2 and cli4.probes == [D], out4
        t4 = good.sent[1][1]
        assert "1 pozisyon" in t4 and "$650K" in t4 and "ayrıca 2 pozisyon daha eşikte" in t4, t4
        # stats kv'de, /tani okur
        st = await dbm.kv_get("cryptoliq_stats")
        assert st and st["alerted"] == 1 and st["chat"] is True and st["top"][0]["coin"] == "PUMP"
        print("✅ tarama) başarısız gönderim marker yazmadı; coin başına tek mesaj; kapanmış"
              " aday sondada düştü; bekleme pozisyon başına, yeni pozisyon yeni mesaj")
    asyncio.run(run())


# ------------------------------------------------ 4) kapı kapalıysa sonda da yok
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
        # kv bayatsa ve kapı açıksa fiyat İSTENİR (tek istek)
        await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"PUMP": {"m": 1}}, "ts": dbm.now() - 3600})
        cfg = _cfg(); cli = Client(rows)
        out = await cl.scan(cfg, cli, Notifier(cfg, Bot()))
        assert cli.ctx_calls == 1 and out["alerted"] == 1, out
        print("✅ kapı) chat/bot/tip yokken hesap var, sonda ve gönderim yok; kv bayatsa tek fiyat isteği")
    asyncio.run(run())


# ------------------------------------------------ 5) bağlantı
def test_wiring():
    from app.config import EDITABLE_FIELDS
    c = Config()
    for f in ("crypto_liq_enabled", "crypto_liq_min_usd", "crypto_liq_dist_pct",
              "crypto_liq_poll_sec", "crypto_liq_cooldown", "notify_cryptoliq"):
        assert f in EDITABLE_FIELDS and hasattr(c, f), f
        assert all(EDITABLE_FIELDS[f].get(x) for x in ("type", "label", "group", "desc")), f
    assert "crypto_chat_id" not in EDITABLE_FIELDS, "chat id env-only kalmalı"
    assert c.crypto_liq_min_usd == 500_000 and c.crypto_liq_dist_pct == 2.5 and c.notify_cryptoliq is True
    from app.health import limits, periods
    assert "cryptoliq" in limits(c) and "cryptoliq" in periods(c)
    from app.notify import KINDS
    assert KINDS["cryptoliq"][0] == "notify_cryptoliq" and KINDS["cryptoliq"][2] == "high"
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "main.py"), encoding="utf-8").read()
    assert '_spawn("cryptoliq"' in src
    print("✅ bağlantı) 6 ayar künyeli, chat id env-only, bekçi/tip/döngü kayıtlı")


# ------------------------------------------------ 6) mesaj: doğrulanmamış satır yaşını yazar, PROPR notu
def test_format():
    from app.telegram import format as fmt
    fresh = [{**row("PUMP", A, "long", 1_200_000, 1.8, ts=dbm.now() - 47 * 60), "dist": 1.8}]
    t = fmt.crypto_liq_alert("PUMP", 0.0032, fresh, [], 2.5)
    assert "ölçüm 47dk önce" in t and "doğrulandı" not in t, t
    assert "PROPR'da listeli" in t, "PUMP PROPR'da — not yazılmalı"
    assert "≤%2.5" in t and "1 pozisyon" in t and "yatırım tavsiyesi değildir" in t
    t2 = fmt.crypto_liq_alert("ZZZZ", 1.0, [{**fresh[0], "verified": True, "coin": "ZZZZ"}], [], 3)
    assert "PROPR" not in t2 and "doğrulandı" in t2
    print("✅ mesaj) sondasız satır ölçüm yaşını yazar; PROPR notu yalnız listelide")


# ------------------------------------------------ 7) sembol çözümleme
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
        assert pump == {"coin": "PUMP", "symbol": "PUMP", "dex": "", "kind": "crypto"}, pump
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
test_send_gate()
test_wiring()
test_format()
test_resolve()
print("\n✅ KRİPTO LIQ TESTLERİ GEÇTİ")
