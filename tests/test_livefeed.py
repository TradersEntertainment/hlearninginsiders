"""📟 Coin terminali + canlı akış (livefeed, /t/{coin}/live SSE, static/term.js).

Kullanıcı (18.10): "grafik ve liq falan her şey tam tek bakışta sığacak şekilde tasarım yapman lazım, terminal gibi
akmalı canlı veriler de ben o sayfadaysam, iyice uğraş".

Kararlar: işlemler kollektörün akışından (ek HL isteği yok), yalnız bantta SAYILAN işlem yayılır; halka yalnız izlenen
coinde; 15 dk toplamları / defter coin başına önbellekli; akış ömürlü, istemci `seq` ile kaldığı yerden; eşzamanlı akış
tavanlı; canlı paneller yumuşak yenilemede nakledilir, panel içi kaydırma korunur.

Pinlenenler:
  • livefeed: izlenmeyen coinde yalnız son fiyat; izlenende halka + sıra; since/row; WATCH_TTL sonra budanır
  • tape.observe sayıldıysa True; kollektör yalnız sayılan işlemi yayar (yeniden yollanan gitmez — gerçek _handle)
  • stats coin başına önbellek; depth hemen döner, arka planda tek istek (düşük şerit), duraklı şeritte sormaz
  • owners: bookowners sonucu kısa hâl (✅ / dolan / emri yok, bilinmiyor bayrağı)
  • SSE: hello → t (init) → s → b → x; tavan dolunca 503; seq ile yeniden bağlanınca eski işlem yeniden gelmez
  • sayfa: body.termpage, #term (path/seq/prev/px), 7 panel, canlı paneller data-keep, kayan listeler data-sid, term.js,
    ayrıntı panelleri altta (çapalar), TWAP / pozisyon / liq sıkışık hâlde; dar ekranda ikincil sütun .nw
  • base.html: pxLive (son mum canlı), panel içi kaydırma korunur, termAfterRefresh, grafik dili tr-TR
  • /tani satırı, README bölümü (kör nokta)
"""
import asyncio
import re
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-livefeed.db")
from app import db as dbm  # noqa: E402
from app.radar import livefeed as lf, tape  # noqa: E402

A, B = "0x" + "a" * 40, "0x" + "b" * 40


def _reset():
    lf.WATCH.clear()
    lf.BUF.clear()
    lf.LAST.clear()
    lf._SUMM.clear()
    lf._DEPTH.clear()
    lf._DEPTH_TASK.clear()
    lf.STATS.update(streams=0, rejected=0)


def test_hub_ring_and_prune():
    _reset()
    lf.push("PUMP", 1_000, 0.004, 1000, "B", A, B, False)            # izlenmiyor: yalnız son fiyat
    assert lf.LAST["PUMP"] == (1_000, 0.004) and "PUMP" not in lf.BUF and lf.since("PUMP", 0) == []
    lf.watch("PUMP")
    s0 = lf.SEQ[0]
    lf.push("PUMP", 2_000, 0.0041, 1000, "B", A, B, True)
    lf.push("PUMP", 3_000, 0.0040, 500, "A", A, B, False)
    rows = lf.since("PUMP", 0)
    assert [r[0] for r in rows] == [s0 + 1, s0 + 2] and lf.last_seq("PUMP") == s0 + 2
    assert lf.row(rows[0]) == [s0 + 1, 2_000, 0.0041, 4.1, "B", A, B, 1], lf.row(rows[0])     # alıcı agresör: A piyasadan
    assert lf.row(rows[1])[5:7] == [B, A], "satıcı agresör: piyasadan satan B, defterdeki alışı dolan A"
    assert lf.since("PUMP", s0 + 1) == [rows[1]] and lf.since("PUMP", s0 + 2) == []
    lf.prune(time.time() + lf.WATCH_TTL + 1)
    assert "PUMP" not in lf.BUF and "PUMP" not in lf.WATCH and lf.LAST["PUMP"][1] == 0.0040
    assert "📟 canlı sayfa akışı" in lf.diag_line()
    print("✅ halka) izlenmeyen coinde yalnız son fiyat; izlenende sıra + satır; süresi geçen budanır")


def test_collector_pushes_only_counted():
    """Gerçek Collector._handle: aynı parti iki kez (yeniden bağlanma) → akışa bir kez."""
    from app.config import Config
    from app.hl import collector as colmod

    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "lf.db"))
        _reset()
        old = (tape.STARTED, colmod.LIVE)
        tape.REG.clear()
        tape.HWM.clear()
        try:
            tape.STARTED = dbm.now() - 100_000
            col = colmod.Collector(Config(), None)
            colmod.LIVE = None
            col.valid_coins = {"xyz:TSLA"}
            lf.watch("xyz:TSLA")
            t = dbm.now()
            data = [{"coin": "xyz:TSLA", "side": "A", "px": "400", "sz": "50", "time": (t - 40) * 1000, "tid": 71,
                     "hash": "0x" + "3" * 64, "users": [A, B]},
                    {"coin": "xyz:TSLA", "side": "B", "px": "401", "sz": "10", "time": (t - 10) * 1000, "tid": 72,
                     "hash": "0x" + "0" * 64, "users": [B, A]}]
            await col._handle(json.dumps({"channel": "trades", "data": data}))
            await col._handle(json.dumps({"channel": "trades", "data": data}))
            rows = [lf.row(r) for r in lf.since("xyz:TSLA", 0)]
            assert len(rows) == 2 and rows[0][4] == "A" and rows[1][7] == 1 and rows[1][1] == (t - 10) * 1000, rows
            assert lf.LAST["xyz:TSLA"] == ((t - 10) * 1000, 401.0)
            assert tape.observe("xyz:TSLA", "B", A, B, 401, 1, t - 10, False, "72") is False
            assert tape.observe("xyz:TSLA", "B", A, B, 401, 1, t - 5, False, "73") is True
        finally:
            tape.STARTED, colmod.LIVE = old
            tape.REG.clear()
            tape.HWM.clear()
    asyncio.run(run())
    print("✅ kanca) yalnız bantta sayılan işlem yayılır (yeniden yollanan iki kez gelmez); son fiyat; TWAP bayrağı")


class _Cli:
    def __init__(self, paused=0.0, fail=False):
        self.paused, self.fail, self.calls = paused, fail, []

    def low_paused(self):
        return self.paused

    async def info(self, p, retries=4, priority=None, stats=None):
        assert p == {"type": "l2Book", "coin": "PUMP"} and retries == 1 and priority == "low", p
        self.calls.append(p)
        if self.fail:
            raise RuntimeError("429")
        lv = lambda px, sz, n: {"px": str(px), "sz": str(sz), "n": n}   # noqa: E731
        return {"levels": [[lv(0.0041 - i * 1e-6, 1e6, 3) for i in range(20)],
                           [lv(0.0042 + i * 1e-6, 1e6, 2) for i in range(20)]]}


def test_stats_depth_owners():
    async def run():
        _reset()
        tape.REG.clear()
        tape.HWM.clear()
        old = tape.STARTED
        tape.STARTED = int(time.time()) - 100_000
        try:
            t = int(time.time())
            tape.observe("PUMP", "B", A, B, 0.0042, 2_000_000, t - 30)
            s1 = lf.stats("PUMP")
            assert s1["n"] == 1 and s1["buy"] == 8400 and s1["buyers"][0][:4] == [A, 8400, 0, 8400] and s1["label"] == "son 15 dk"
            tape.observe("PUMP", "A", A, B, 0.0041, 1_000_000, t - 20)
            assert lf.stats("PUMP") is s1, "STATS_TTL içinde önbellek"
            # defter: hemen döner (önbellek yok → None), arka planda tek istek; sonra önbellek
            cli = _Cli()
            assert lf.depth(cli, "PUMP") is None and lf.depth(cli, "PUMP") is None
            await asyncio.sleep(0.02)
            d = lf.depth(cli, "PUMP")
            assert len(cli.calls) == 1 and len(d["bids"]) == lf.DEPTH_LEVELS and d["asks"][0] == [0.0042, 1e6, 2], d
            # duraklı düşük şerit: sormaz
            lf._DEPTH.clear()
            p = _Cli(paused=30)
            assert lf.depth(p, "PUMP") is None and not lf._DEPTH_TASK and not p.calls
            # okunamazsa sayılır, patlamaz
            f = _Cli(fail=True)
            e0 = lf.STATS["depth_err"]
            lf.depth(f, "PUMP")
            await asyncio.sleep(0.02)
            assert lf.STATS["depth_err"] == e0 + 1 and lf.depth(None, "PUMP") is None
        finally:
            tape.STARTED = old
            tape.REG.clear()
            tape.HWM.clear()
    asyncio.run(run())
    o = lf.owners({"ts": 5, "owners_unknown": True, "cands": 3, "failed": 3, "unit_txt": "0.00001", "err": [],
                   "grouped": {"bid": [{"px": 0.0041, "lo": 0.0041, "hi": 0.00410999, "usd": 9.8e6, "n": 197,
                                        "owners": [(A, 1.2e6)], "makers": [(B, 12_000.0)], "makers_gone": [],
                                        "unknown_usd": 8.6e6}], "ask": []},
                   "raw": {"bid": [], "ask": []}})
    assert o["unknown"] and o["lv"][0]["v"] == "g" and o["lv"][0]["own"] == [[A, 1200000]] and o["lv"][0]["unk"] == 8600000
    assert lf.owners(None) is None
    print("✅ önbellekler) 15 dk toplamları coin başına; defter hemen döner + tek arka plan isteği; duraklı şeritte"
          " sormaz; sahipler kısa hâl")


async def _sse(app, path, query=b"", stop_after=3.0):
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query, "root_path": "",
             "headers": [(b"host", b"test")], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80)}
    status, body = {}, []
    done = asyncio.Event()

    async def receive():
        await done.wait()
        return {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status["code"] = msg["status"]
        elif msg["type"] == "http.response.body":
            body.append(msg.get("body", b""))
            if not msg.get("more_body"):
                done.set()
    try:
        await asyncio.wait_for(app(scope, receive, send), stop_after)
    except asyncio.TimeoutError:
        pass
    done.set()
    return status.get("code"), b"".join(body).decode()


def _events(txt):
    out = []
    for blk in txt.split("\n\n"):
        ev = [ln[7:] for ln in blk.split("\n") if ln.startswith("event: ")]
        da = [ln[6:] for ln in blk.split("\n") if ln.startswith("data: ")]
        if ev:
            out.append((ev[0], json.loads(da[0]) if da else None))
    return out


def test_sse_route():
    sys.path.insert(0, HERE)
    import test_routes_smoke as rs

    async def run():
        app = await rs._fresh()
        _reset()

        class Col:
            valid_coins = {"HYPE"}
            connected = True
            down_since, down_log, last_msg_at = 0.0, [], time.time()
        app.state.collector = Col()
        old = (lf.STREAM_LIFE, lf.TICK, tape.STARTED)
        lf.STREAM_LIFE, lf.TICK = 0.6, 0.05
        tape.STARTED = int(time.time()) - 100_000
        try:
            lf.watch("HYPE")
            t = int(time.time())
            for i in range(3):
                tape.observe("HYPE", "B", A, B, 40.0 + i, 100, t - 5 + i, False, f"h{i}")
                lf.push("HYPE", (t - 5 + i) * 1000, 40.0 + i, 100, "B", A, B, False)
            st, body = await _sse(app, "/t/HYPE/live")
            ev = _events(body)
            names = [e for e, _ in ev]
            assert st == 200 and names[0] == "hello" and names[-1] == "x", names
            assert ev[0][1]["listened"] is True and ev[0][1]["live"] is True and ev[0][1]["px"] == 42.0
            tr = next(d for e, d in ev if e == "t")
            assert tr["init"] == 1 and [r[2] for r in tr["r"]] == [40.0, 41.0, 42.0]
            s = next(d for e, d in ev if e == "s")
            assert s["n"] == 3 and s["buy"] == 12300 and s["px"] == 42.0 and s["live"] is True
            seq = ev[-1][1]["seq"]
            # kaldığı yerden: eski işlemler yeniden gelmez, yenisi gelir
            lf.push("HYPE", (t + 1) * 1000, 43.0, 100, "A", A, B, False)
            st, body = await _sse(app, "/t/HYPE/live", f"seq={seq}".encode())
            trs = [d for e, d in _events(body) if e == "t"]
            assert len(trs) == 1 and [r[2] for r in trs[0]["r"]] == [43.0] and "init" not in trs[0], trs
            # tavan
            lf.STATS["streams"] = lf.MAX_STREAMS
            st, _ = await _sse(app, "/t/HYPE/live")
            assert st == 503 and lf.STATS["rejected"] >= 1
            lf.STATS["streams"] = 0
            # bilinmeyen coin
            st, _ = await _sse(app, "/t/YOKBOYLE/live")
            assert st == 404
        finally:
            lf.STREAM_LIFE, lf.TICK, tape.STARTED = old
            tape.REG.clear()
            tape.HWM.clear()
            _reset()
    asyncio.run(run())
    print("✅ SSE) hello → t (ilk boyama) → s → x; seq ile yeniden bağlanınca eski işlem gelmez; tavan 503; yok 404")


def test_terminal_page_and_wiring():
    sys.path.insert(0, HERE)
    import test_routes_smoke as rs

    async def run():
        app = await rs._fresh()
        st, body = await rs.get(app, "/t/HYPE")
        return st, body
    st, html = asyncio.run(run())
    assert st == 200 and '<body class="termpage"' in html and 'id="term"' in html and 'data-path="HYPE"' in html
    # mum önbelleği yok (taze DB): grafik beklenir → 10 sn (inceleme 18.10)
    assert '<script src="/static/term.js" defer>' in html and 'data-refresh="10"' in html
    for sid in ("ttape", "tflow", "tbook"):                 # nakledilen panellerin iç kaydırması da korunur
        assert f'data-sid="{sid}"' in html, sid
    for cls, keep in (("t-chart", "pxchart"), ("t-book", "tbook"), ("t-tape", "ttape"), ("t-flow", "tflow")):
        assert f'class="tp {cls}" data-keep="{keep}"' in html, cls
    for cls in ("t-liq", "t-twap", "t-pos"):
        assert f'class="tp {cls}"' in html, cls
    for sid in ("tliq", "ttwap", "tpos"):
        assert f'data-sid="{sid}"' in html, sid
    assert html.count('id="pxchart"') == 1 and 'id="tpx"' in html and 'id="tlive"' in html and 'id="tladder"' in html
    assert 'data-liqpx="30' in html, "liq / pozisyon satırı canlı mesafe için liq fiyatını taşır"
    for anchor in ('id="twaps"', 'id="liqmap"', 'id="positions"', 'id="tape15"'):
        assert anchor in html, anchor
    assert html.index('id="term"') < html.index("<h2>⏱ Son 15 dakika") < html.index("<h2>📋 TWAP emirleri")
    st2, h2 = asyncio.run(_get_missing())
    assert st2 == 200 and 'class="termpage"' not in h2 and 'id="term"' not in h2, "coin yoksa terminal yok"
    base = open(os.path.join(ROOT, "app/web/templates/base.html"), encoding="utf-8").read()
    assert "function pxLive(p, ts, lite)" in base and "PXCHART.series.update(L)" in base
    assert "termAfterRefresh" in base and "[data-sid]" in base and "localization: {locale: 'tr-TR'}" in base
    assert "section.term .nw{display:none}" in base and "--term-top" in base and not re.search(r"body\.term(?!page)", base)
    js = open(os.path.join(ROOT, "app/web/static/term.js"), encoding="utf-8").read()
    for frag in ("new EventSource(", "/live", "'seq=' + (T.seq || 0)", "if(d.seq < T.seq) T.seq = 0",
                 "pxLive(p,", "document.title", "addEventListener('x'", "document.hidden", "data-liqpx"):
        assert frag in js, frag
    col = open(os.path.join(ROOT, "app/hl/collector.py"), encoding="utf-8").read()
    assert "livefeed.push(coin, int(t[\"time\"]), px, sz, aggr, buyer, seller, zero)" in col
    assert "livefeed.diag_line()" in open(os.path.join(ROOT, "app/diag.py"), encoding="utf-8").read()
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    assert "## 📟 Coin terminali" in readme and "GET /t/{coin}/live" in readme and "**Kör nokta, açıkça:** canlı akış" in readme
    print("✅ sayfa) body.termpage, #term, 7 panel (canlı olanlar data-keep), kayan listeler data-sid, term.js, ayrıntı"
          " altta; base.html pxLive / kaydırma / tr-TR; kanca; /tani; README")


async def _get_missing():
    sys.path.insert(0, HERE)
    import test_routes_smoke as rs
    app = await rs._fresh()
    return await rs.get(app, "/t/YOKBOYLE")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
