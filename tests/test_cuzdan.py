"""🌐 Cüzdan sayfası (/cuzdan/<adres>, /takipte) — app/web/cuzdan.py, app/sitelink.py, şablonlar cuzdan.html / takipte.html.

Kullanıcı (20.10): "bu kanala eklediklerim için websitesi yapar mısın railwaye push edeyim bu geçen sana sorduğum
hypepoztakip sitesini kopyalayabilirsin profillere basınca öyle bi websitesi çıksın ve takip bildirimlerinin sonunda takibi
bırak işareti de olsun".

Kararlar: görünüm hyptakip'ten; sayfa ayrı (base.html değil), veri JSON'dan 20 sn'de bir; panel DASHBOARD_TOKEN'ı Telegram'a
yazılmaz — adres başına HMAC imzalı bağlantı (?s=), imzalı ziyarette çerez yazılmaz, Referrer-Policy no-referrer; takip
kanalı mesajlarında 👤 bağlantısı bu sayfaya, sonda "🌐 Cüzdan sayfası · 🛑 Bırakmak: /birak_N".

Pinlenenler:
  • sitelink: jeton yoksa imza yok ve her şey açık; imza adrese bağlı (başka adreste geçmez); taban adres yoksa bağlantı boş
  • rota: jetonla imzasız 401, imzalı 200 (sayfa + JSON + liste), panel anahtarıyla 200, başka adresin imzasıyla 401,
    geçersiz adres 404; dbkey çerezi yazılmaz; Referrer-Policy no-referrer
  • JSON: hesap toplamları bütün dex'lerden; pozisyon alanları (mark, ROE %, liq uzaklığı, takip bilgisi); işlemler son
    24 sa yeniden eskiye; bildirim akışı yalnız bu cüzdanın takip / 🧯 mesajları (etiketsiz); toplu sorgu düşerse dex dex
    (eksik işareti); hiçbiri okunamazsa ok=false
  • liste: yalnız 🧯 takipleri, açık olan cüzdan önce, imzalı profil bağlantısı
  • Telegram: adım mesajında 👤 → imzalı sayfa, sonda 🌐 + 🛑 /birak_N, "key=" yok; kapanışta 🛑 yok
"""
import asyncio
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
from app import db as dbm  # noqa: E402
from app import sitelink  # noqa: E402
from app.config import Config  # noqa: E402
from app.radar import tracker as tr  # noqa: E402
from app.radar import unwindtrack as ut  # noqa: E402
from app.telegram import format as fmt  # noqa: E402
from app.web import cuzdan  # noqa: E402

A = "0x30af" + "0" * 32 + "393e"
B = "0x" + "b" * 40
TRK = "-100777"


def _p(coin, szi, px, liq=None, roe=0.05):
    return {"position": {"coin": coin, "szi": str(szi), "positionValue": str(abs(szi) * px), "entryPx": str(px * 0.99),
                         "liquidationPx": str(liq) if liq else None, "leverage": {"type": "cross", "value": 5},
                         "unrealizedPnl": "1000", "returnOnEquity": str(roe), "marginUsed": str(abs(szi) * px / 5),
                         "cumFunding": {"allTime": "10", "sinceOpen": "-12.5", "sinceChange": "0"}}}


def _states():
    return {"main": {"marginSummary": {"accountValue": "3000000", "totalMarginUsed": "500000", "totalNtlPos": "2000000"},
                     "withdrawable": "1200000", "assetPositions": [_p("BTC", 10, 100_000, liq=80_000), _p("ETH", -100, 4000)]},
            "xyz": {"marginSummary": {"accountValue": "9000000", "totalMarginUsed": "8000000"},
                    "assetPositions": [_p("xyz:GOOGL", 17_000, 2517.6, liq=1500)]}}


class _Cli:
    def __init__(self, states=None):
        self.states, self.calls, self.fail_all, self.fail_dex = states or _states(), 0, False, set()

    async def clearinghouse_all(self, user, dexes):
        self.calls += 1
        if self.fail_all:
            raise RuntimeError("HTTP 429")
        return self.states

    async def clearinghouse(self, user, dex=""):
        if (dex or "main") in self.fail_dex:
            raise RuntimeError("HTTP 500")
        return self.states.get(dex or "main") or {"assetPositions": []}

    async def info(self, p, **kw):
        assert p["type"] == "userFills" and p["user"] == A
        t = dbm.now() * 1000
        return [{"coin": "xyz:GOOGL", "px": "2517.2", "sz": "170", "time": t - 60_000, "dir": "Close Long",
                 "closedPnl": "1200", "fee": "21.4", "crossed": True},
                {"coin": "BTC", "px": "100000", "sz": "0.5", "time": t - 3_600_000, "dir": "Open Long",
                 "closedPnl": "0", "fee": "5", "crossed": False},
                {"coin": "ETH", "px": "4000", "sz": "1", "time": t - 2 * 86400_000, "dir": "Open Short",
                 "closedPnl": "0", "fee": "1", "crossed": True}]


def _cfg(token="sekret", base="https://radar.example.app"):
    cfg = Config()
    cfg.dashboard_token, cfg.public_base_url = token, base
    cfg.unwind_track_chat_id = TRK
    cfg.notify_track = True
    cfg.quiet_start_hour = cfg.quiet_end_hour = 0
    return cfg


async def _fresh(cfg, client):
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cz.db"))
    ut._CACHE.clear()
    cuzdan._FILLS.clear()
    from app.main import app
    app.state.cfg, app.state.client, app.state.session = cfg, client, None
    app.state.bot, app.state.notifier = None, None
    return app


async def get(app, path: str, query: bytes = b"", cookie: str = "") -> tuple[int, str, dict]:
    hdrs = [(b"host", b"test")] + ([(b"cookie", cookie.encode())] if cookie else [])
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query, "root_path": "", "headers": hdrs,
             "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    st, body, head = {}, [], {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            st["code"] = msg["status"]
            for k, v in msg.get("headers") or []:
                head.setdefault(k.decode().lower(), []).append(v.decode())
        elif msg["type"] == "http.response.body":
            body.append(msg.get("body", b""))
    await app(scope, receive, send)
    return st.get("code"), b"".join(body).decode("utf-8", "replace"), head


async def _track(cfg, cli, coin="xyz:GOOGL", src="unwind"):
    live = ut.live_of(cli.states, coin)
    return await tr.start_tracker(cfg, A, coin, coin.split(":")[-1], live, chat_id=TRK, src=src)


def test_sitelink():
    cfg = _cfg()
    s = sitelink.sig(cfg, A)
    assert len(s) == 24 and s != sitelink.sig(cfg, B) and s != sitelink.sig(cfg, "liste")
    assert sitelink.check(cfg, A, s) and not sitelink.check(cfg, B, s) and not sitelink.check(cfg, A, "") \
        and not sitelink.check(cfg, A, None)
    assert sitelink.wallet_url(cfg, A) == f"https://radar.example.app/cuzdan/{A}?s={s}"
    assert sitelink.wallet_url(cfg, A.upper().replace("0X", "0x")) == sitelink.wallet_url(cfg, A)
    assert sitelink.wallet_url(cfg, "0xnothex") == "" and sitelink.list_url(cfg).startswith("https://radar.example.app/takipte?s=")
    assert "sekret" not in sitelink.wallet_url(cfg, A) + sitelink.list_url(cfg)
    open_ = _cfg(token="")
    assert sitelink.sig(open_, A) == "" and sitelink.check(open_, A, None)
    assert sitelink.wallet_url(open_, A) == f"https://radar.example.app/cuzdan/{A}"
    assert sitelink.wallet_url(_cfg(base=""), A) == "" and sitelink.list_url(_cfg(base="")) == ""
    print("✅ sitelink) adrese bağlı imza; jeton yoksa açık; taban yoksa bağlantı yok; jeton bağlantıda yok")


def test_routes_guard_and_headers():
    async def run():
        cfg = _cfg()
        cli = _Cli()
        app = await _fresh(cfg, cli)
        s = sitelink.sig(cfg, A).encode()
        for path in (f"/cuzdan/{A}", f"/cuzdan/{A}/veri.json"):
            assert (await get(app, path))[0] == 401
            assert (await get(app, path, b"s=" + sitelink.sig(cfg, B).encode()))[0] == 401
            st, body, head = await get(app, path, b"s=" + s)
            assert st == 200, (path, st, body[:300])
            assert head.get("referrer-policy") == ["no-referrer"] and "set-cookie" not in head
            assert (await get(app, path, b"key=sekret"))[0] == 200
            assert (await get(app, path, cookie="dbkey=sekret"))[0] == 200
        assert (await get(app, "/cuzdan/0xzz/veri.json", b"key=sekret"))[0] == 404
        st, body, _ = await get(app, f"/cuzdan/{A}", b"s=" + s)
        assert A in body and f"/cuzdan/{A}/veri.json?s={s.decode()}" in body and "sekret" not in body
        assert "Gözlem aracıdır, yatırım tavsiyesi değildir" in body
        assert (await get(app, "/takipte"))[0] == 401
        assert (await get(app, "/takipte", b"s=" + sitelink.sig(cfg, "liste").encode()))[0] == 200
        assert (await get(app, "/takipte", b"s=" + s))[0] == 401                  # adres imzası listeyi açmaz
        # jeton yoksa hepsi açık
        app.state.cfg = _cfg(token="")
        assert (await get(app, f"/cuzdan/{A}"))[0] == 200 and (await get(app, "/takipte"))[0] == 200
    asyncio.run(run())
    print("✅ rota) imzasız 401, imzalı / anahtarlı 200, başka adres imzası 401, çerez yok, no-referrer, jeton sızmıyor")


def test_json_data():
    async def run():
        cfg = _cfg()
        cli = _Cli()
        app = await _fresh(cfg, cli)
        tid = await _track(cfg, cli)
        await dbm.alert_log("sent:track", f"step:{tid}:13600.0000", "✂️ <b>BALİNA KAPATIYOR</b> — GOOGL\\n<a href='x'>0x30</a>")
        await dbm.alert_log("sent:track", "step:999:1.0000", "başka takip")
        await dbm.alert_log("sent:unwind", f"unwind:{A}:xyz:GOOGL:1", "🧯 <b>GOOGL</b> LONG'unu piyasaya satıyor")
        await dbm.alert_log("sent:unwind", f"unwind:{B}:BTC:1", "başka cüzdan")
        cli.states["xyz"]["assetPositions"][0]["position"].update(szi="13600", positionValue=str(13600 * 2517.6))
        st, body, _ = await get(app, f"/cuzdan/{A}/veri.json", b"key=sekret")
        d = json.loads(body)
        assert st == 200 and d["ok"] and not d["partial"] and d["address"] == A
        acc = d["account"]
        assert acc["value"] == 12_000_000 and acc["margin_used"] == 8_500_000 and acc["withdrawable"] == 1_200_000
        assert acc["n"] == 3 and abs(acc["ntl"] - (1e6 + 4e5 + 13600 * 2517.6)) < 1 and acc["lev"] is not None
        g = d["positions"][0]
        assert g["coin"] == "xyz:GOOGL" and g["label"] == "GOOGL" and g["dex"] == "xyz" and g["side"] == "long"
        assert abs(g["mark"] - 2517.6) < 1e-6 and g["roe"] == 5.0 and g["liq"] == 1500 and abs(g["liq_dist"] - 40.42) < 0.01
        assert g["funding"] == -12.5 and g["lev"] == 5 and g["lev_type"] == "cross" and g["propr"] in (True, False)
        assert g["track"]["id"] == tid and g["track"]["closed_pct"] == 20.0 and g["track"]["chat"] == "takip kanalı"
        eth = next(p for p in d["positions"] if p["coin"] == "ETH")
        assert eth["side"] == "short" and eth["liq"] is None and eth["liq_dist"] is None and eth["track"] is None
        assert [f["coin"] for f in d["fills"]] == ["xyz:GOOGL", "BTC"]                    # 24 sa, yeniden eskiye
        assert d["fills"][0]["usd"] == 2517.2 * 170 and d["fills"][0]["pnl"] == 1200 and d["fills"][1]["taker"] is False
        kinds = [(a["kind"], a["text"].split("\\n")[0]) for a in d["alerts"]]
        assert ("takip", "✂️ BALİNA KAPATIYOR — GOOGL") in kinds and ("boşaltma", "🧯 GOOGL LONG'unu piyasaya satıyor") in kinds
        assert len(d["alerts"]) == 2 and all("<" not in a["text"] for a in d["alerts"])
        assert d["tracked"] == [{"id": tid, "coin": "xyz:GOOGL", "label": "GOOGL", "side": "long",
                                 "base_ntl": 17000 * 2517.6, "since": d["tracked"][0]["since"], "active": True,
                                 "end_note": "", "closed_pct": 20.0}]
        # toplu sorgu düştü: dex dex, biri de düştü → eksik işareti, okunanlar gösterilir
        ut._CACHE.clear()
        cli.fail_all, cli.fail_dex = True, {"xyz"}
        d = await cuzdan.build(cfg, cli, A)
        assert d["ok"] and d["partial"] and [p["coin"] for p in d["positions"]] == ["BTC", "ETH"]
        cli.fail_dex = {"main", "xyz"}
        from app.radar import unwind as u
        old = u._dexes

        async def two(cfg):
            return ["", "xyz"]
        u._dexes = two
        try:
            d = await cuzdan.build(cfg, cli, A)
        finally:
            u._dexes = old
        assert not d["ok"] and d["err"].startswith("Hyperliquid'e ulaşılamadı") and d["account"] is None
    asyncio.run(run())
    print("✅ JSON) hesap toplamı bütün dex'ler; pozisyon alanları; 24 sa işlemler; yalnız bu cüzdanın bildirimleri; eksik")


def test_list_page():
    async def run():
        cfg = _cfg()
        cli = _Cli()
        await _fresh(cfg, cli)
        await _track(cfg, cli, "xyz:GOOGL")
        t2 = await _track(cfg, cli, "BTC")
        await _track(cfg, cli, "ETH", src="")                      # sıradan takip: listede yok
        async with dbm.db() as c:
            await c.execute("UPDATE trackers SET active=0, end_note='kapandı' WHERE id=?", (t2,))
        d = await cuzdan.list_rows(cfg)
        assert d["n_active"] == 1 and len(d["wallets"]) == 1
        w = d["wallets"][0]
        assert w["url"] == f"/cuzdan/{A}?s={sitelink.sig(cfg, A)}" and w["active"] == 1 and w["ended"] == 1
        assert [c["label"] for c in w["coins"]] == ["GOOGL", "BTC"] and w["coins"][1]["closed_pct"] == 100.0
        from app.web.routes import templates
        html_ = templates.env.get_template("takipte.html").render(**d)
        assert w["url"].replace("&", "&amp;") in html_ or w["url"] in html_
        empty = templates.env.get_template("takipte.html").render(wallets=[], n_active=0, ts=dbm.now())
        assert "Gözlem aracıdır" in empty
    asyncio.run(run())
    print("✅ liste) yalnız 🧯 takipleri, açık önce, imzalı profil bağlantısı")


def test_telegram_links():
    async def run():
        cfg = _cfg()
        cli = _Cli()
        await _fresh(cfg, cli)
        tid = await _track(cfg, cli)
        n = []

        class N:
            async def send(self, kind, text, **kw):
                n.append((text, kw))
                return True
        cli.states["xyz"]["assetPositions"][0]["position"].update(szi="13600", positionValue=str(13600 * 2517.6))
        ut._CACHE.clear()
        async with dbm.db() as c:
            row = dict(await (await c.execute("SELECT * FROM trackers WHERE id=?", (tid,))).fetchone())
        await tr._check_one(cfg, cli, N(), row, dbm.now())
        txt = n[0][0]
        url = sitelink.wallet_url(cfg, A).replace("&", "&amp;")
        lines = txt.split("\n")
        assert lines[1].startswith(f'👤 <a href="{url}">0x30af..393e</a>'), lines[1]
        assert lines[-1] == f'🌐 <a href="{url}">Cüzdan sayfası</a> · 🛑 Bırakmak: /birak_{tid}', lines[-1]
        assert "key=" not in txt and "sekret" not in txt
        from tests.test_tg_html import tg_html_errors
        assert not tg_html_errors(txt)
        # kapanış: 🌐 var, 🛑 yok
        n.clear()
        cli.states["xyz"]["assetPositions"] = []
        ut._CACHE.clear()
        async with dbm.db() as c:
            row = dict(await (await c.execute("SELECT * FROM trackers WHERE id=?", (tid,))).fetchone())
        await tr._check_one(cfg, cli, N(), row, dbm.now())
        assert n[0][0].split("\n")[-1] == f'🌐 <a href="{url}">Cüzdan sayfası</a>' and "/birak_" not in n[0][0]
        # taban adres yoksa: hypurrscan + yalnız 🛑
        assert fmt.unwind_track_footer([5], "") == "🛑 Bırakmak: /birak_5"
        assert fmt.alink(A).startswith('<a href="https://hypurrscan.io/address/')
    asyncio.run(run())
    print("✅ Telegram) 👤 → imzalı sayfa; sonda 🌐 + 🛑 /birak_N; kapanışta 🛑 yok; jeton yok; HTML geçerli")


def test_wiring():
    src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
    routes = src("app/web/routes.py")
    for r in ('@router.get("/cuzdan/{address}")', '@router.get("/cuzdan/{address}/veri.json")', '@router.get("/takipte")'):
        assert r in routes, r
    readme = src("README.md")
    assert "### 🌐 Cüzdan sayfası (`/cuzdan`, `/takipte`)" in readme and "PUBLIC_BASE_URL" in src(".env.example")
    assert "app/sitelink.py" in src("tests/test_wording.py")
    for t in ("cuzdan.html", "takipte.html"):
        body = src(f"app/web/templates/{t}")
        assert "<!doctype html>" in body.lower() and 'name="referrer" content="no-referrer"' in body
        assert "cdn.tailwindcss" not in body and "extends" not in body.split("\n")[0]
    print("✅ kablolama) rotalar, README, .env, şablonlar ayrı ve CDN'siz")


if __name__ == "__main__":
    test_sitelink()
    test_routes_guard_and_headers()
    test_json_data()
    test_list_page()
    test_telegram_links()
    test_wiring()
