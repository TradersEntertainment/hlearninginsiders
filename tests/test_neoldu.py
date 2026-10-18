"""🔎 "Ne oldu?" eksik veri — 18.10 denetimi.

Kullanıcı (18.10): "ne oldu? sekmesi neden eksik veri çekiyor ?"

Denetim (canlı ölçüm + kod yolu) kök nedenleri:
  • $5K taban işlemlerin çoğunu siliyor (BTC ~%26, PUMP ~%5, TWAP dilimleri ~%0) — son 15 dk'nın hepsi 📼 bantta.
  • Parti kuralı: tek mesajda bir parça tabanı geçince taban altı kalanlar hiç yazılmıyordu (1×$6K + 20×$2K → $6K).
  • Yeniden bağlanınca HL'nin yeniden yolladığı işlemler ikinci bir sentetik toplam satırı yazıyordu (çift sayım).
  • Coin çözümü: kPEPE hep boş, STX yanlış coin (para:STX), HYPE/USDC bulunamıyor, yazım hatası sessiz.
  • "Alım / Satım" her işlemin iki tarafını sayıyordu (tanım gereği eşit; HYPE'ta yön ters çıktı).
  • 60 adres kesimi gizliydi ve toplamlar kesilmiş listeden hesaplanıyordu.
  • OI: 35 dk'yı aşan her pencere "belirsiz" (yanlış gerekçeyle); spotta da aynı gerekçe.
  • Saklama dışı pencere "kayıt başlamadı" diyordu; ms epoch 500; ileri saat bugün sayılıyordu.
  • para coinleri "hisse perp"; spotta yanlış taban; formdaki gizli anahtar alanı bozuk (name="k").

Pinlenenler: aşağıdaki her test bir maddeyi sabitler.
"""
import asyncio
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-neoldu.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import Config, get_config  # noqa: E402
from app.hl import universe as uni  # noqa: E402
from app.radar import forensics as fx  # noqa: E402
from app.radar import tape  # noqa: E402

A, B, C = "0x" + "a" * 40, "0x" + "b" * 40, "0x" + "c" * 40


def _addr(i: int) -> str:
    return "0x" + f"{i:040x}"


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "neoldu.db"))
    g = get_config()
    g.equity_dexes, g.crypto_dexes = ["xyz"], ["para"]
    assets.set_crypto_dex_symbols(["ANSEM", "STX"])
    async with dbm.db() as c:
        for coin, dex, sym in (("xyz:TSLA", "xyz", "TSLA"), ("para:STX", "para", "STX"), ("para:ANSEM", "para", "ANSEM")):
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
        await c.commit()
    t = dbm.now()
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HYPE": {"m": 40.0}, "kPEPE": {"m": 0.01}, "STX": {"m": 1.0},
                                            "BTC": {"m": 100_000.0}}, "ts": t})
    await dbm.kv_set("ws_universe", {"crypto": ["HYPE", "kPEPE", "STX", "BTC"], "spot": ["@107"], "ts": t})
    assets.set_spot_names({"@107": "HYPE/USDC"})


async def _fill(coin, tid, addr, side, usd, ts, taker):
    async with dbm.db() as c:
        await c.execute("INSERT INTO fills(coin,tid,address,side,px,sz,notional,ts,taker) VALUES(?,?,?,?,?,?,?,?,?)",
                        (coin, tid, addr, side, 1.0, usd, usd, ts, taker))
        await c.commit()


def _trade(coin, tid, usd, buyer, seller, ts, side="B", px=40.0):
    return {"coin": coin, "side": side, "px": str(px), "sz": str(usd / px), "time": ts * 1000,
            "hash": "0x" + "1" * 64, "tid": tid, "users": [buyer, seller]}


def test_coin_resolution_route():
    """kPEPE / STX / HYPE/USDC / TSLAA: rota coin sayfasıyla aynı çözümü kullanır, bulunamayan söylenir."""
    async def run():
        await _fresh()
        old = tape.STARTED
        tape.STARTED = dbm.now() + 10_000           # bant yolu kapalı: fills yolu sınansın
        try:
            t = dbm.now() - 3600
            await _fill("kPEPE", "1", A, "buy", 9000, t + 60, 1)
            await _fill("STX", "2", A, "buy", 9000, t + 60, 1)
            await _fill("@107", "3", A, "buy", 9000, t + 60, 1)
            from tests.test_routes_smoke import get
            from app.main import app
            app.state.cfg, app.state.client, app.state.session = get_config(), None, None
            app.state.bot, app.state.notifier = None, None
            q = f"&t0={t}&t1={t + 600}".encode()
            for sym, coin, market in (("kPEPE", "kPEPE", "kripto"), ("kpepe", "kPEPE", "kripto"),
                                      ("STX", "STX", "kripto"), ("HYPE/USDC", "@107", "spot")):
                st, body = await get(app, "/neoldu", b"sym=" + sym.encode().replace(b"/", b"%2F") + q)
                assert st == 200, (sym, st, body[-400:])
                assert f'data-coin="{coin}"' in body and market in body, (sym, body[body.find("<h2>"):][:600])
            st, body = await get(app, "/neoldu", b"sym=TSLAA" + q)
            assert st == 200 and "bulunamadı" in body, body[-400:]
            st, body = await get(app, "/neoldu", b"sym=HYPE" + q)
            assert "spot çiftleri ayrı sorulur" in body and "HYPE/USDC" in body
            st, body = await get(app, "/neoldu", b"sym=ANSEM" + q)
            assert "kripto dex" in body and "$1K" in body, body[body.find("Kaynak"):][:300]
            # gizli anahtar alanı diğer sayfalarla aynı
            assert 'name="k"' not in open(os.path.join(ROOT, "app/web/templates/neoldu.html")).read()
        finally:
            tape.STARTED = old
        print("✅ coin) kPEPE/kpepe → kPEPE, STX → ana dex Stacks, HYPE/USDC → @107 (spot), TSLAA → 'bulunamadı';"
              " HYPE'ta spot çiftleri linkli; para → 'kripto dex' + $1K taban; form anahtarı name=key")
    asyncio.run(run())


def test_aggressor_totals_and_cut():
    """Alım/satım = piyasadan; 300 adreste toplam hepsinden, tabloda 60 ve kesim yazılı."""
    async def run():
        await _fresh()
        old = tape.STARTED
        tape.STARTED = dbm.now() + 10_000
        try:
            t = dbm.now() - 7200
            # HYPE: A piyasadan $90K sattı (B pasif aldı), C piyasadan $20K aldı (A pasif sattı)
            await _fill("HYPE", "1", A, "sell", 90_000, t + 10, 1)
            await _fill("HYPE", "1", B, "buy", 90_000, t + 10, 0)
            await _fill("HYPE", "2", C, "buy", 20_000, t + 20, 1)
            await _fill("HYPE", "2", A, "sell", 20_000, t + 20, 0)
            rep = await fx.report("HYPE", t, t + 600)
            assert rep["source"] == "fills"
            assert rep["buy"] == 20_000 and rep["sell"] == 90_000 and rep["net"] == -70_000, (rep["buy"], rep["sell"])
            # 300 adres → toplam hepsinden
            for i in range(300):
                await _fill("BTC", f"t{i}", _addr(i + 1), "buy", 10_000 + i, t + 30, 1)
                await _fill("BTC", f"t{i}", _addr(10_000 + i), "sell", 10_000 + i, t + 30, 0)
            rep = await fx.report("BTC", t, t + 600)
            assert rep["n_addr"] == 600 and len(rep["rows"]) == fx.MAX_ADDRS and rep["cut"] == 600 - fx.MAX_ADDRS
            assert abs(rep["buy"] - sum(10_000 + i for i in range(300))) < 1e-6 and rep["sell"] == 0
            brief = await fx.window_brief(get_config(), None, "BTC", t, t + 600, probe=False)
            assert brief["n_addr"] == 600 and brief["taker_pct"] is None and brief["net"] == rep["buy"]
            from app.telegram import format as fmt
            line = "\n".join(fmt.what_happened(brief))
            assert "piyasadan alım" in line and "600 adres" in line and "taker %" not in line.split("\n")[1], line
        finally:
            tape.STARTED = old
        print("✅ toplam) piyasadan alım/satım (HYPE: satış $90K > alış $20K, eskiden ters); 600 adres → tabloda 60,"
              " kesim yazılı, toplam hepsinden; alarm özeti 'piyasadan alım', toplam taker % yok")
    asyncio.run(run())


def test_tape_window_last_15_min():
    """Son 15 dk penceresi banttan: taban altı işlemler ve TWAP dilimleri dahil, kapsam yazılı."""
    async def run():
        await _fresh()
        old = (tape.STARTED, dict(tape.REG), dict(tape.HWM))
        now = dbm.now()
        tape.STARTED = now - 5000
        tape.REG.pop("PUMP", None)
        tape.HWM.pop("PUMP", None)
        try:
            await dbm.kv_set("ws_universe", {"crypto": ["PUMP"], "spot": [], "ts": now})
            base = now - 600
            for i in range(50):                         # 50 × $300 piyasadan alım (taban altı: fills'te YOK)
                tape.observe("PUMP", "B", _addr(i + 1), C, 0.003, 100_000, base + i * 6, twap=(i % 2 == 0), tid=str(i))
            rep = await fx.report("PUMP", now - 900, now)
            assert rep["source"] == "tape", rep["source"]
            assert rep["n_trades"] == 50 and rep["n_addr"] == 51, (rep["n_trades"], rep["n_addr"])
            assert abs(rep["buy"] - 50 * 300) < 1e-6 and rep["sell"] == 0
            assert rep["tape_lo"] >= now - 900 and rep["tape_hi"] == now
            # pencere bandın dışında (20 dk önce) → fills; bot pencerenin içinde açıldıysa da fills
            rep = await fx.report("PUMP", now - 1500, now - 900)
            assert rep["source"] == "fills"
            tape.STARTED = now - 300
            rep = await fx.report("PUMP", now - 900, now)
            assert rep["source"] == "fills" and rep["restart_in"], rep["source"]
            # dinlenmeyen coin: bant yok, not var
            rep = await fx.report("ZZZ", now - 900, now)
            assert rep["source"] == "fills" and rep["listening"] is False
        finally:
            tape.STARTED = old[0]
            tape.REG.clear(); tape.REG.update(old[1])
            tape.HWM.clear(); tape.HWM.update(old[2])
        print("✅ bant) son 15 dk: 50 × $300 (taban altı) işlemin hepsi, 51 adres, kapsam yazılı; 20 dk önceki ya da"
              " açılışı içeren pencere → işlem kaydı (♻️ notlu); dinlenmeyen coin → listening=False")
    asyncio.run(run())


def test_tape_window_bounds():
    """tape.window: yalnız tamamı pencerede kalan kovalar; geçmiş alt pencere sonu süzülür."""
    old = (tape.STARTED, dict(tape.REG), dict(tape.HWM))
    now = (int(time.time()) // 30) * 30 + 7
    tape.STARTED = now - 5000
    tape.REG.pop("ZEC", None)
    tape.HWM.pop("ZEC", None)
    try:
        for i, ts in enumerate((now - 600, now - 400, now - 200, now - 5)):
            tape.observe("ZEC", "A", A, B, 10.0, 100, ts, tid=f"z{i}")
        w = tape.window("ZEC", now - 610, now - 300, now)          # yalnız -600 ve -400 (−200 dışarıda)
        assert w and abs(w["sell"] - 2000) < 1e-6 and w["n"] == 2, w
        w = tape.window("ZEC", now - 300, now, now)                # canlı: dolmakta olan kova dahil
        assert w and w["n"] == 2 and w["hi"] == now
        assert tape.window("ZEC", now - 1200, now, now) is None    # bandın dışı
        assert tape.window("ZEC", now - 100, now - 90, now) is None    # tek kovadan kısa
        assert tape.seen("ZEC", now - 5, "z3") and tape.seen("ZEC", now - 600, "zz") and not tape.seen("ZEC", now, "n")
    finally:
        tape.STARTED = old[0]
        tape.REG.clear(); tape.REG.update(old[1])
        tape.HWM.clear(); tape.HWM.update(old[2])
    print("✅ bant penceresi) son kova süzülür, canlı pencerede dolan kova sayılır, bandın dışı / tek kovadan kısa → None;"
          " seen(): tekrar ve eski işlem")


def test_collector_remainder_and_replay():
    """1×$6K + 20×$2K tek mesaj → $46K; yeniden yollanan işlem ikinci toplam satırı yazmaz."""
    async def run():
        await _fresh()
        from app.hl import collector as colm
        old = (tape.STARTED, dict(tape.HWM))
        T = dbm.now() - 100
        tape.STARTED = T - 1000
        tape.HWM.pop("HYPE", None)
        try:
            col = colm.Collector(Config(), session=None)
            col.crypto_coins = {"HYPE"}

            async def total(addr):
                async with dbm.db() as c:
                    cur = await c.execute("SELECT COUNT(*) n, COALESCE(SUM(notional),0) s FROM fills"
                                          " WHERE address=? AND side='buy'", (addr,))
                    return dict(await cur.fetchone())
            msg = [_trade("HYPE", "1", 6000, A, C, T)] + [_trade("HYPE", str(100 + i), 2000, A, C, T) for i in range(20)]
            await col._handle(json.dumps({"channel": "trades", "data": msg}))
            got = await total(A)
            assert got["n"] == 2 and abs(got["s"] - 46_000) < 1e-6, got
            # B: 3 × $2K tek mesajda ($6K ≥ taban → sentetik satır), sonra yeniden bağlanma: aynı ikisi + yeni $2K
            await col._handle(json.dumps({"channel": "trades", "data": [
                _trade("HYPE", "201", 2000, B, C, T + 1), _trade("HYPE", "202", 2000, B, C, T + 1),
                _trade("HYPE", "203", 2000, B, C, T + 1)]}))
            await col._handle(json.dumps({"channel": "trades", "data": [
                _trade("HYPE", "202", 2000, B, C, T + 1), _trade("HYPE", "203", 2000, B, C, T + 1),
                _trade("HYPE", "204", 2000, B, C, T + 2)]}))
            got = await total(B)
            assert abs(got["s"] - 6000) < 1e-6 and got["n"] == 1, got      # eskiden ikinci agg satırı: $12K
        finally:
            tape.STARTED = old[0]
            tape.HWM.clear(); tape.HWM.update(old[1])
        print("✅ kollektör) tek mesajda 1×$6K + 20×$2K → $46K (kalan sentetik satırla; eskiden $6K);"
              " yeniden yollanan işlemler ikinci toplam satırı yazmaz (eskiden çift sayım)")
    asyncio.run(run())


def test_oi_reasons_and_retention():
    """60 dk pencere OI okunur; spot ve kaydedilmeyen coin gerekçesi doğru; saklama dışı → silindi."""
    async def run():
        await _fresh()
        old = tape.STARTED
        tape.STARTED = dbm.now() + 10_000
        try:
            t = dbm.now() - 3 * 3600
            async with dbm.db() as c:
                for k in range(0, 13):                  # 300 sn'de bir, OI yükseliyor, fiyat yükseliyor
                    await c.execute("INSERT INTO asset_metrics(coin,ts,mark_px,oi,funding,day_volume)"
                                    " VALUES(?,?,?,?,?,?)", ("xyz:TSLA", t + k * 300, 400 + k, 1000 + 10 * k, 0, 1))
                await c.commit()
            rep = await fx.report("xyz:TSLA", t, t + 3600)
            assert rep["oi"]["verdict"] == "yeni LONG açılmış", rep["oi"]
            rep = await fx.report("@107", t, t + 600)
            assert rep["oi"]["verdict"] == "belirsiz" and "spotta" in rep["oi"]["why"] and rep["market"] == "spot"
            rep = await fx.report("kPEPE", t, t + 600)
            assert "kaydedilmiyor" in rep["oi"]["why"], rep["oi"]
            # 20 gün önce: silindi (kayıt başlamadı değil)
            await _fill("HYPE", "x", A, "buy", 9000, dbm.now() - 3600, 1)
            old_t = dbm.now() - 20 * 86400
            rep = await fx.report("HYPE", old_t, old_t + 600)
            assert rep["purged"] and not rep["before_records"] and rep["keep_days"] == 14
            assert fx.market_of("para:ANSEM") == "kripto dex" and fx.market_of("xyz:SP500") == "HIP-3 perp"
            cfg = Config()
            cfg.spot_fill_min_notional, cfg.crypto_fill_min_notional, cfg.crypto_dex_fill_min_notional = 2000, 5000, 1000
            assert fx.floor_of(cfg, "@107") == 2000 and fx.floor_of(cfg, "para:ANSEM") == 1000
            cfg.crypto_fill_min_notional = 0
            assert fx.floor_of(cfg, "BTC") is None
        finally:
            tape.STARTED = old
        print("✅ OI) 60 dk pencere okunur (eskiden 'belirsiz'); spot → 'spotta OI yok'; kaydedilmeyen coin söylenir;"
              " 20 gün önce → 'silindi'; para = kripto dex; spot tabanı kendi ayarı; kapalı kayıt None")
    asyncio.run(run())


def test_parse_window():
    """ms epoch, ileri saat → dün, gece yarısı, 'dün', gg.aa, okunamayan not, 6 sa sınırı."""
    from datetime import datetime
    from app.web.routes import TR, _parse_window

    class R:
        def __init__(self, **q):
            self.query_params = q
    now = int(datetime(2026, 10, 18, 22, 30, tzinfo=TR).timestamp())
    at = lambda d, h, m: int(datetime(2026, 10, d, h, m, tzinfo=TR).timestamp())   # noqa: E731
    t0, t1, note = _parse_window(R(t0=str(now * 1000 - 600_000), t1=str(now * 1000)), now)
    assert (t0, t1) == (now - 600, now), (t0, t1)
    assert _parse_window(R(t0="23:00", t1="23:20"), now)[:2] == (at(17, 23, 0), at(17, 23, 20))
    assert _parse_window(R(t0="18:25", t1="18:35"), now)[:2] == (at(18, 18, 25), at(18, 18, 35))
    assert _parse_window(R(t0="21:50", t1="00:10"), now)[:2] == (at(18, 21, 50), now)      # şimdiye çekildi
    now2 = at(19, 0, 20)
    assert _parse_window(R(t0="23:50", t1="00:10"), now2)[:2] == (at(18, 23, 50), at(19, 0, 10))
    assert _parse_window(R(t0="dün 18:25", t1="dün 18:40"), now)[:2] == (at(17, 18, 25), at(17, 18, 40))
    assert _parse_window(R(t0="15.10 09:00", t1="15.10 09:30"), now)[:2] == (at(15, 9, 0), at(15, 9, 30))
    t0, t1, note = _parse_window(R(t0="25:00"), now)
    assert "okunamadı" in note and (t0, t1) == (now - 900, now), note
    t0, t1, note = _parse_window(R(t0="08:00", t1="22:00"), now)
    assert t1 - t0 == 6 * 3600 and "en çok" in note
    print("✅ zaman) ms epoch, 22:30'da '23:00→23:20' = dün, gece yarısını aşan pencere, 'dün', 'gg.aa',"
          " okunamayan giriş notlu, 6 sa sınırı")


if __name__ == "__main__":
    test_coin_resolution_route()
    test_aggressor_totals_and_cut()
    test_tape_window_last_15_min()
    test_tape_window_bounds()
    test_collector_remainder_and_replay()
    test_oi_reasons_and_retention()
    test_parse_window()
