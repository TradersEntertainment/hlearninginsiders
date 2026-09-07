"""🪙 Kripto builder dex'leri — CRYPTO_DEXES=para (para:ANSEM).

Pinlenenler:
  • refresh_universe: hisse + kripto dex'leri tek geçişte; kripto dex sembolleri
    assets.CRYPTO_DEX_SYMBOLS + kv `crypto_dex_symbols`; para meta hatasında küme ve tickers
    korunur; kripto dex kapatılınca küme boşalır ve para coinleri evrenden düşer; new_out
  • sınıf: kind('para:ANSEM') = kind('ANSEM') = crypto; klass kripto; takvim yok; endeks perp'i
    değil; watched_dexes = hisse + kripto (tekrarsız, csv dizesi de olur); restart'ta kv'den yükleme
  • resolve_coin: para:ANSEM → kind equity (veri hattı HIP-3) + klass kripto; ana dex → klass kripto
  • yönlendirme: twaplive kripto kanalı; bigpos crypto; liqattack normal kapı + _equity_positions
    para'yı atlar; equityvol.route_for → cryptovol/kripto kanalı/kripto tabanı; sweeper dex listesi
  • coin sayfası: 'para dex kripto' rozeti, bilanço satırı yok; bulunamadı dalında izlenen dex →
    'evren yenilemesi' (EQUITY_DEXES yönlendirmesi YOK); bot aynı; /tani evren satırı para ✓
"""
import asyncio
import html as _html
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-cdex.db")
from app import assets  # noqa: E402
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config, get_config  # noqa: E402
from app.hl import universe as uni  # noqa: E402

ROOT = os.path.dirname(HERE)
T = lambda h: _html.unescape(re.sub(r"<[^>]+>", " ", h))  # noqa: E731 — autoescape: ' → &#39;


def rd(*p):
    return open(os.path.join(ROOT, *p), encoding="utf-8").read()


class Client:
    def __init__(self):
        self.metas = {"xyz": ["TSLA", "xyz:SNDK"], "para": ["ANSEM", "para:MEME"]}
        self.fail: set[str] = set()
        self.calls: list[str] = []

    async def meta(self, dex=""):
        self.calls.append(dex)
        if dex in self.fail:
            raise RuntimeError("meta 500")
        return {"universe": [{"name": n, "maxLeverage": 3} for n in self.metas[dex]]}


def _cfg() -> Config:
    cfg = Config()
    cfg.equity_dexes = ["xyz"]
    cfg.crypto_dexes = ["para"]
    cfg.crypto_chat_id = "-100"
    cfg.crypto_stocks_id = "-200"
    cfg.crypto_vol_alert_min_usd = 1_000_000
    cfg.telegram_chat_id = "111"
    return cfg


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cdex.db"))
    assets.set_crypto_dex_symbols([])
    g = get_config()                       # is_crypto_dex(cfg=None) tekil ayarı okur
    g.equity_dexes, g.crypto_dexes = ["xyz"], ["para"]
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HYPE": {"m": 40.0}}, "ts": dbm.now()})


def test_universe_and_classes():
    async def run():
        await _fresh()
        cfg = _cfg()
        cli = Client()
        coins = await uni.refresh_universe(cli, ["xyz"], None, ["para"])
        assert set(coins) == {"xyz:TSLA", "xyz:SNDK", "para:ANSEM", "para:MEME"} and cli.calls == ["xyz", "para"], coins
        assert assets.CRYPTO_DEX_SYMBOLS == {"ANSEM", "MEME"}
        rec = await dbm.kv_get(assets.CRYPTO_DEX_KV)
        assert rec["syms"] == ["ANSEM", "MEME"] and rec["ts"], rec
        async with dbm.db() as c:
            cur = await c.execute("SELECT coin, dex, symbol FROM tickers")
            rows = {(r["coin"], r["dex"], r["symbol"]) for r in await cur.fetchall()}
        assert rows == {("para:ANSEM", "para", "ANSEM"), ("para:MEME", "para", "MEME"),
                        ("xyz:SNDK", "xyz", "SNDK"), ("xyz:TSLA", "xyz", "TSLA")}, rows
        # sınıflandırma — önekli ve öneksiz
        assert assets.kind("para:ANSEM") == "crypto" and assets.kind("ANSEM") == "crypto" and assets.kind("xyz:TSLA") == "equity"
        assert assets.klass("para:ANSEM") == "kripto" and assets.klass("HYPE") == "kripto"
        assert assets.klass("xyz:SP500") == "endeks" and assets.klass("xyz:TSLA") == "hisse"
        assert not assets.has_earnings("ANSEM") and not assets.has_earnings("para:ANSEM") and assets.has_earnings("TSLA")
        assert not assets.is_index_perp("para:ANSEM") and assets.is_index_perp("xyz:SP500")
        assert assets.is_crypto_dex("para:X") and assets.is_crypto_dex("meme") and not assets.is_crypto_dex("xyz:X")
        assert not assets.is_crypto_dex("") and not assets.is_crypto_dex("TSLA")
        assert assets.watched_dexes(cfg) == ["xyz", "para"]
        c2 = Config()
        c2.equity_dexes, c2.crypto_dexes = ["xyz", "para"], "para; abc"
        assert assets.watched_dexes(c2) == ["xyz", "para", "abc"] and assets.crypto_dexes(c2) == ["para", "abc"]
        assert assets.crypto_dexes(type("C", (), {"crypto_dexes": None})()) == []
        # çözümleme: veri hattı HIP-3 (kind equity), sınıf kripto
        a = await uni.resolve_coin("ansem")
        assert a["coin"] == "para:ANSEM" and a["dex"] == "para" and a["kind"] == "equity" and a["klass"] == "kripto", a
        assert (await uni.resolve_coin("para:MEME"))["klass"] == "kripto"
        assert (await uni.resolve_coin("sndk"))["klass"] == "hisse"
        assert (await uni.resolve_coin("hype"))["klass"] == "kripto"
        # restart: evren koşana kadar kv'deki küme
        assets.set_crypto_dex_symbols([])
        assert assets.kind("ANSEM") == "equity"
        assert await assets.load_crypto_dex_symbols() == 2 and assets.kind("ANSEM") == "crypto"
        # yeni listeleme → new_out
        cli.metas["para"].append("NEW")
        fresh: list[dict] = []
        await uni.refresh_universe(cli, ["xyz"], fresh, ["para"])
        assert [f["coin"] for f in fresh] == ["para:NEW"] and "NEW" in assets.CRYPTO_DEX_SYMBOLS, fresh
        # para meta hatası → küme ve tickers korunur (kısmi hatada silme yok)
        cli.fail = {"para"}
        coins = await uni.refresh_universe(cli, ["xyz"], None, ["para"])
        assert set(coins) == {"xyz:TSLA", "xyz:SNDK"} and assets.CRYPTO_DEX_SYMBOLS == {"ANSEM", "MEME", "NEW"}
        async with dbm.db() as c:
            cur = await c.execute("SELECT COUNT(*) AS n FROM tickers")
            assert (await cur.fetchone())["n"] == 5
        assert (await dbm.kv_get(assets.CRYPTO_DEX_KV))["syms"] == ["ANSEM", "MEME", "NEW"]
        # kripto dex kapatıldı → küme boşalır, para coinleri evrenden düşer, çözümlenmez
        cli.fail = set()
        coins = await uni.refresh_universe(cli, ["xyz"], None, [])
        assert set(coins) == {"xyz:TSLA", "xyz:SNDK"} and assets.CRYPTO_DEX_SYMBOLS == set()
        assert (await dbm.kv_get(assets.CRYPTO_DEX_KV))["syms"] == []
        async with dbm.db() as c:
            cur = await c.execute("SELECT coin FROM tickers")
            assert {r["coin"] for r in await cur.fetchall()} == {"xyz:SNDK", "xyz:TSLA"}
        assert await uni.resolve_coin("ansem") is None and assets.kind("ANSEM") == "equity"
        print("✅ evren/sınıf) hisse+kripto dex tek geçiş; kv küme; restart yükleme; hata korur; kapatınca düşer; resolve klass")
    asyncio.run(run())


def test_routing():
    async def run():
        await _fresh()
        cfg = _cfg()
        from app.radar import bigpos, equityvol, liqattack, sweeper
        from app.radar import twaplive as tl
        # TWAP: sınıf + kanal
        assert tl.klass_of("para:ANSEM") == "kripto" and tl.klass_of("HYPE") == "kripto"
        assert tl.klass_of("xyz:SP500") == "endeks" and tl.klass_of("xyz:TSLA") == "hisse"
        assert tl.chat_for(cfg, "para:ANSEM") == ("-100", True) and tl.chat_for(cfg, "HYPE") == ("-100", True)
        assert tl.chat_for(cfg, "xyz:TSLA") == ("", True)
        c0 = _cfg()
        c0.crypto_chat_id = ""
        assert tl.chat_for(c0, "para:ANSEM") == ("", False), "kripto kanalı boşsa gönderme"
        # büyük pozisyon sınıfı
        assert bigpos.classify("para:ANSEM") == "crypto" and bigpos.classify("HYPE") == "crypto"
        assert bigpos.classify("xyz:TSLA") == "equity"
        # liq saldırısı: normal kapı (endeks kapısı değil) + para pozisyonları tezden dışarı
        g = liqattack.gate_for(cfg, "para:ANSEM")
        assert g == liqattack.gate_for(cfg, "TSLA") and g[2] is False and liqattack.gate_for(cfg, "xyz:SP500")[2] is True
        async with dbm.db() as c:
            for coin, dex, sym in (("xyz:SNDK", "xyz", "SNDK"), ("para:ANSEM", "para", "ANSEM")):
                await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
                await c.execute("INSERT INTO positions_current(coin,address,ts,side,notional,liq_px) VALUES(?,?,?,?,?,?)",
                                (coin, "0x" + "a" * 40, dbm.now(), "long", 2_000_000, 1.0))
        by = await liqattack._equity_positions()
        assert set(by) == {"xyz:SNDK"}, by
        # hacim rekoru: kripto dex → cryptovol türü, kripto kanalı, kripto tabanı
        rt = equityvol.route_for(cfg, "para:ANSEM", 10_000, 1_000_000, "-200")
        assert rt == {"kind": "cryptovol", "chat": "-100", "alert_min": 1_000_000.0, "crypto": True}, rt
        rt = equityvol.route_for(cfg, "xyz:TSLA", 10_000, 1_000_000, "-200")
        assert rt == {"kind": "equityvol", "chat": "-200", "alert_min": 1_000_000, "crypto": False}, rt
        cfg.crypto_vol_alert_min_usd = 5_000
        assert equityvol.route_for(cfg, "para:ANSEM", 10_000, 1_000_000, "-200")["alert_min"] == 10_000, "sayfa tabanının altına inmez"
        # süpürücü / metrik dex listesi
        assert sweeper._dexes(cfg) == ["", "xyz", "para"]
        # kapalı seans süzgeci ve liq saldırısı: para atlanır (kaynak bağlantısı)
        assert "assets.is_crypto_dex(coin)" in rd("app", "radar", "offhours.py")
        assert 'assets.is_crypto_dex(r["coin"])' in rd("app", "radar", "liqattack.py")
        from app.radar import offhours
        res = await offhours.screener(cfg)
        rows = res.get("rows") if isinstance(res, dict) else res
        assert all((r.get("coin") or "") != "para:ANSEM" for r in (rows or [])), rows
        print("✅ yönlendirme) TWAP kripto kanalı; bigpos crypto; liq kapısı normal + para tezden dışarı; hacim → cryptovol; dex listesi")
    asyncio.run(run())


def test_page_bot_diag():
    async def run():
        await _fresh()
        cfg = _cfg()
        cfg.public_bot_enabled = True
        # coin sayfası: rozet + bilanço satırı yok
        from test_coin_render import V2, ctx, render
        html = render(**ctx(V2, ticker={"coin": "para:ANSEM"}, symbol="ANSEM", coin="para:ANSEM",
                            kind="equity", klass="kripto"))
        t = T(html)
        assert "para dex kripto" in t and "mumlar geçmiş bilanço günleri" not in t and "ana dex kripto" not in t, t[:600]
        t = T(render(**ctx(V2, klass="hisse")))
        assert "dex kripto" not in t and "mumlar geçmiş bilanço günleri" in t
        # bulunamadı dalı: dex izleniyor ama evren henüz yenilenmedi → dürüst bekleme mesajı,
        # EQUITY_DEXES yönlendirmesi YOK
        await dbm.kv_set(uni.HIP3_KV, {"ts": dbm.now(), "dexes": [
            {"name": "xyz", "full_name": "Trade.xyz", "n": 1, "assets": ["SNDK"]},
            {"name": "para", "full_name": "Para", "n": 2, "assets": ["ANSEM", "MEME"]}]})
        from app.web.routes import templates
        req = type("R", (), {"url": type("U", (), {"path": "/t/ANSEM"})()})()
        base = dict(request=req, k="", is_admin=False, has_pw=False, ticker=None, crypto_n=1)
        h = await uni.find_in_hip3("ANSEM", assets.watched_dexes(cfg))
        assert h and h["watched"] is True and h["dex"] == "para"
        html = templates.env.get_template("coin.html").render(**base, symbol="ANSEM", hip3=h, hip3_known=True,
                                                              similar=[], excluded=False)
        t = T(html)
        assert "para" in t and "izleme listesinde" in t and "evren yenilemesi" in t and "EQUITY_DEXES" not in html, t[-700:]
        # bot: aynı bilgi
        from app.telegram import public
        m = await public.unknown_message(cfg, "ansem")
        assert "<b>para</b>" in m and "izleme listesinde" in m and "evren yenilemesi" in m, m
        cx = _cfg()
        cx.crypto_dexes = []
        m = await public.unknown_message(cx, "ansem")
        assert "izlemiyor" in m and "izleme listesinde" not in m, m
        # /tani evren satırı: para izlenen (✓), ANSEM ne 'izlenmeyen dex'te' ne 'bulunamayan'
        from app import diag
        txt = await diag.report(cfg, None)
        line = next((ln for ln in txt.splitlines() if ln.strip().startswith("evren:")), "")
        assert "para 2 ✓" in line and "xyz 1 ✓" in line and "(izlenen: para, xyz)" in line, line
        assert "izlenmeyen dex'te" not in line and "ANSEM" not in line.split("bulunamayan")[-1], line
        # ayar / bağlantı
        f = EDITABLE_FIELDS["crypto_dexes"]
        assert f["type"] == "csv" and f["group"] == EDITABLE_FIELDS["equity_dexes"]["group"] and "para" in f["desc"]
        if not os.getenv("CRYPTO_DEXES"):
            assert Config().crypto_dexes == ["para"], "varsayılan: para açık"
        m = rd("app", "main.py")
        assert "cfg.crypto_dexes)" in m and "load_crypto_dex_symbols()" in m
        assert "CRYPTO_DEXES=para" in rd(".env.example") and "CRYPTO_DEXES" in rd("README.md")
        print("✅ sayfa/bot/tanı) para dex kripto rozeti, bilanço yok; izlenen dex bekleme mesajı; evren satırı para ✓; ayar+bağlantı")
    asyncio.run(run())
