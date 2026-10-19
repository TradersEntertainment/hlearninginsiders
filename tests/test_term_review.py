"""📟 Coin terminali + 📋 TWAP tabloları — inceleme düzeltmeleri (18.10, çok-ajanlı inceleme, her bulgu yeniden üretildi).

Kullanıcı (18.10): "grafik ve liq falan her şey tam tek bakışta sığacak şekilde tasarım yapman lazım, terminal gibi
akmalı canlı veriler de ben o sayfadaysam, iyice uğraş".

Kararlar ve pinlenenler:
  • Sunucu yeniden başlayınca istemcinin eski `seq`'i sıfırlanır (bant ~4 dk donuyordu).
  • Canlı fiyat yalnız taze son işlemden (`livefeed.fresh_px`); dinlenmeyen coinde yok.
  • close_stale: en uzun süredir sorulmamış satır önce; WS görüntüsü (son 30 kayıt) bitişi kapsamıyorsa REST.
  • fill_view: bitmiş emrin yüzdesi adetle (kalan); HL 'stopped' bitiş sayılır; kaydedilmiş süren emir de
    radarın gördüğü HL dilimlerini gösterir.
  • Terminal 📋 TWAP: HL emirleri önde, emri bilinmeyen diziler en çok TERM_FORMING; "ölç." etiketi.
  • Şablon: akış paneli ölçüm yokken "—", bilanço (~) / sabah uyarısı, fiyat yokken liq metni, sekme başlığı
    işaretleri, nakledilen panellerde data-sid, body.termpage, ızgara tabanı 440 px; istemci betikleri.
"""
import asyncio
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-term-review.db")
from app import db as dbm  # noqa: E402
from app.radar import livefeed as lf  # noqa: E402
from app.radar import tape  # noqa: E402
from app.radar import twap, twaplive as tl  # noqa: E402

A, B = "0x" + "a" * 40, "0x" + "b" * 40


def test_seq_reset_after_restart():
    """İstemci eski process'ten kalma büyük seq ile bağlanır → sunucu sıfırlar, ilk döküm (init) gelir."""
    import test_livefeed as tlf
    import test_routes_smoke as rs

    async def run():
        app = await rs._fresh()
        tlf._reset()

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
                lf.push("HYPE", (t - 5 + i) * 1000, 40.0 + i, 100, "B", A, B, False)
            st, body = await tlf._sse(app, "/t/HYPE/live", b"seq=500000")
            ev = tlf._events(body)
            trs = [d for e, d in ev if e == "t"]
            assert st == 200 and trs and trs[0].get("init") == 1 and len(trs[0]["r"]) == 3, ev[:3]
            assert ev[0][1]["px_ts"] and ev[0][1]["px"] == 42.0
        finally:
            lf.STREAM_LIFE, lf.TICK, tape.STARTED = old
            tlf._reset()
    asyncio.run(run())
    print("✅ seq) yeniden başlamada eski seq=500000 → sunucu sıfırlar, son 3 işlem ilk döküm olarak gelir; px_ts var")


def test_fresh_px():
    lf.LAST["ZZZ"] = (int(time.time() * 1000) - 3 * 3600 * 1000, 35.0)
    assert lf.fresh_px("ZZZ") == (None, None), "3 sa önceki son işlem canlı fiyat değil"
    lf.LAST["ZZZ"] = (int(time.time() * 1000) - 5000, 36.0)
    assert lf.fresh_px("ZZZ")[0] == 36.0 and lf.fresh_px("ZZZ", listened=False) == (None, None)
    lf.LAST.pop("ZZZ", None)
    print("✅ canlı fiyat) 3 sa önceki son işlem yok sayılır; taze olan geçer; dinlenmeyen coinde yok")


def test_close_stale_rest_and_order():
    """WS görüntüsü dolu ve bitişten yeni → REST'e bir kez sorulur, bitiş kaydı yazılır; sıra lookup_ts'e göre."""
    import tempfile

    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "cs.db"))
        ts = dbm.now()
        t0 = ts - 6 * 3600
        async with dbm.db() as c:
            for addr, planned, lk in ((A, 3e6, ts - 20_000), (B, 1e5, None)):
                await c.execute(
                    "INSERT INTO twap_runs(coin,address,side,first_ts,last_ts,n_slices,total,src,order_status,"
                    "order_ts,order_min,planned_usd,planned_sz,executed_usd,lookup_ts)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("PUMP", addr, "buy", t0, t0 + 60, 5, 1e4, "live", "activated", t0, 30, planned,
                     planned / 0.004, 0.0, lk))
            await c.commit()
        newer = [{"time": ts - 60 * i, "state": {"coin": "HYPE", "side": "B", "sz": "1", "timestamp": (ts - 60 * i) * 1000,
                                                  "minutes": 5}, "status": {"status": "finished"}} for i in range(30)]
        fin = {"time": t0 + 1800, "state": {"coin": "PUMP", "side": "B", "sz": str(1e5 / 0.004),
                                            "executedSz": str(0.5e5 / 0.004), "executedNtl": "50000",
                                            "timestamp": t0 * 1000, "minutes": 30}, "status": {"status": "stopped"}}

        class Col:
            connected = True

            async def fetch_twap_history(self, addr):
                return newer                              # yalnız son 30 kayıt: bitiş kaydı yok

        calls = []

        class Cl:
            async def info(self, payload, retries=4, priority=None):
                calls.append(payload)
                return newer + [fin]
        tl.CLOSE["last"] = 0
        n = await tl.close_stale(Col(), ts, Cl())
        async with dbm.db() as c:
            cur = await c.execute("SELECT address, order_status, executed_usd, lookup_ts FROM twap_runs")
            got = {r["address"]: dict(r) for r in await cur.fetchall()}
        assert len(calls) == 1 and calls[0]["type"] == "twapHistory", calls
        assert calls[0]["user"] == B, "hiç sorulmamış (küçük) satır önce sorulur"
        assert n == 1 and got[B]["order_status"] == "stopped" and got[B]["executed_usd"] == 50000
        assert got[A]["order_status"] == "activated", "turda tek REST: büyük satır sonraki turda"
    asyncio.run(run())
    print("✅ close_stale) hiç sorulmamış satır önce; WS son 30 kayıt bitişi kapsamıyor → tek REST twapHistory;"
          " 'stopped' bitiş kaydı yazılır")


def test_fill_view_pct_stopped_seen():
    r = twap.fill_view({"planned_usd": 1e6, "planned_sz": 1e5, "executed_usd": 1.06e6, "remaining_usd": 0.0,
                        "order_status": "finished"}, dbm.now())
    assert r["fill_txt"] == "%100" and r["state_txt"] == "🏁 bitti", r["fill_txt"]
    r = twap.fill_view({"planned_usd": 1e6, "executed_usd": 2.3e5, "remaining_usd": 7.7e5, "order_status": "stopped",
                        "order_ts": dbm.now() - 3600, "order_min": 30}, dbm.now())
    assert r["state"] == "halted" and r["state_txt"] == "⏹ durduruldu" and r["fill_txt"] == "%23" and r["left_txt"] == "$770K"
    # kaydedilmiş süren emir: radarın gördüğü HL dilimleri (canlı satırla aynı ölçü)
    tl.REG.runs.clear()
    t = dbm.now()
    for i in range(10):
        tl.REG.observe("PUMP", A, "buy", 0.004, 1e7, 4e4, t - 300 + i * 30, 1, True)
    row = {"coin": "PUMP", "address": A, "side": "buy", "order_status": "activated", "executed_usd": 0.0,
           "order_ts": t - 400, "order_min": 60, "planned_usd": 1e6, "planned_sz": 2.5e8}
    twap._seen_row(row, t)
    assert row.get("fill_hl0") and row.get("fill_seen"), row
    v = twap.fill_view({**row, "running": True}, t)
    assert v["fill_txt"].startswith("≈ ") or v["fill_txt"].startswith("≥ "), v["fill_txt"]
    tl.REG.runs.clear()
    print("✅ fill_view) bitmiş emir adetle %100 (eskiden $ ile %106); 'stopped' → ⏹ durduruldu, %23, kalan $770K;"
          " kaydedilmiş süren emir radarın gördüğünü yazar")


def test_term_twaps_order():
    from app.web import routes
    hl = [{"planned_known": True, "running": i == 0, "size_usd": 1e6 * (i + 1), "address": f"h{i}"} for i in range(12)]
    no = [{"planned_known": False, "running": True, "size_usd": 9e6 + i, "address": f"n{i}"} for i in range(9)]
    out = routes.term_twaps(hl + no)
    assert len(out) == routes.TERM_TWAPS and sum(1 for r in out if not r["planned_known"]) == routes.TERM_FORMING
    assert out[0]["address"] == "h0", "süren HL emri en üstte"
    assert {r["address"] for r in out if r["planned_known"]} >= {"h11", "h10"}, "büyük bitmiş HL emirleri panelde"
    out2 = routes.term_twaps(hl[:2] + no)
    assert len(out2) == 11 and sum(1 for r in out2 if not r["planned_known"]) == 9, "HL azsa boşluk dolar"
    print("✅ terminal TWAP) HL emirleri önde, emri bilinmeyen en çok 4; HL azsa boşluk dolar")


def test_templates_and_client():
    coin = open(os.path.join(ROOT, "app/web/templates/coin.html"), encoding="utf-8").read()
    for frag in ('class="termpage"', 'data-tpre=', 'data-tsuf=', '(~)', 'sabah açıklanmış olabilir',
                 'data-sid="ttape"', 'data-sid="tflow"', 'data-sid="tbook"', "ölç.", "Güncel fiyat yok",
                 "Defter okunamadı ({{ book.err|join(', ') }})", "dinlenmiyor — bilinmiyor"):
        assert frag in coin, frag
    base = open(os.path.join(ROOT, "app/web/templates/base.html"), encoding="utf-8").read()
    for frag in ("max(440px,", "function pxReload(gap)", "box._loading", "TERM_LIVE_KEEP", "window.termLive",
                 ".t-flow .fk{grid-template-columns:repeat(2,1fr)}", ".t-tape .tb{overflow-anchor:none}",
                 "body.termpage .topnav{display:flex;flex:1 0 100%;order:9;flex-wrap:wrap"):
        assert frag in base, frag
    js = open(os.path.join(ROOT, "app/web/static/term.js"), encoding="utf-8").read()
    for frag in ("$('.t-tape .tb')", "window.termLive", "Math.pow(2, T.fail - 1)", "T.tpre", "d.px_ts",
                 "rows.forEach(function(r){ pxLive(", "pxReload(false)", "unk ? '—'", "Defter okunamadı ("):
        assert frag in js, frag
    assert "$('#ttape .tb')" not in js
    routes = open(os.path.join(ROOT, "app/web/routes.py"), encoding="utf-8").read()
    assert "if seq > lf.last_seq(coin):" in routes and "BOOK_KICK_AGE" in routes and "fresh_px(coin, listened)" in routes
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    assert "--timeout-graceful-shutdown 5" in readme and "1366×657" in readme and "REST `twapHistory`" in readme
    print("✅ şablon + istemci) termpage, başlık işaretleri, bilanço (~), data-sid, ölç., boş durum metinleri; base:"
          " 440 px, yeniden çekme, tek kurulum, canlı nakil; term.js: seçici, geri çekilme, px_ts; README")


if __name__ == "__main__":
    test_seq_reset_after_restart()
    test_fresh_px()
    test_close_stale_rest_and_order()
    test_fill_view_pct_stopped_seen()
    test_term_twaps_order()
    test_templates_and_client()
    test_round2_fixes()


def test_round2_fixes():
    """2. inceleme turu (18.10/19.10, yeniden üretildi): terminal TWAP'ta süren/oluşan emirsiz dizi arşivin arkasında
    kalmaz; erken iptal edilen emrin bitişi WS'te yoksa REST'e sorulur (yalnız WS'te bulunamayan satır için);
    istemci: grafik yeniden çekilince katman üst üste binmez, işlem olmayan fiyat mumu oynatmaz, boşluk dolmazsa
    yeniden çekme durur, işlem satırı çift eklenmez, kırpma okuma yerini oynatmaz, paket içi işlem tek çizim."""
    from app.web import routes
    hl = [{"planned_known": True, "running": False, "size_usd": 3e6, "address": f"h{i}"} for i in range(12)]
    arch = [{"planned_known": False, "running": False, "size_usd": 6e6, "address": f"a{i}"} for i in range(5)]
    live = [{"planned_known": False, "running": True, "forming": True, "size_usd": 4e5, "address": "live"}]
    out = routes.term_twaps(hl + arch + live)
    assert out[0]["address"] == "live" and len(out) == routes.TERM_TWAPS, [r["address"] for r in out]
    # _ws_cut: erken iptal — WS'in en eskisi planlanan bitişten ESKİ ama emir başlangıcından YENİ → REST
    now = dbm.now()
    hist = [{"time": now - 3 * 3600 + i} for i in range(30)]
    row = {"order_ts": now - 25 * 3600, "order_min": 1440}
    assert tl._ws_cut(hist, [row]), "erken iptal: bitiş kaydı görüntünün dışında olabilir"
    assert not tl._ws_cut(hist[:10], [row]), "dolu olmayan görüntüde her şey var"
    base = open(os.path.join(ROOT, "app/web/templates/base.html"), encoding="utf-8").read()
    for frag in ("box.querySelectorAll('.lqov,.pxbadge')", "'sub pxnote'", "PXCHART.ac.abort()", "box._gapT",
                 "function pxLive(p, ts, lite)", "function pxReload(gap)"):
        assert frag in base, frag
    js = open(os.path.join(ROOT, "app/web/static/term.js"), encoding="utf-8").read()
    for frag in ("function price(p, ms, quiet, noCandle)", "f !== T.feed", "pxLive(r[2], Math.floor(r[1] / 1000), true)",
                 "if(!window.termLive() && +root.dataset.px > 0) T.px = +root.dataset.px", "pxReload(false)"):
        assert frag in js, frag
    i_comp, i_trim = js.index("box.scrollTop += list.scrollHeight - h0"), js.index("while(list.children.length > MAX_ROWS)")
    assert i_comp < i_trim, "önce telafi, sonra kırpma"
    print("✅ 2. tur) emirsiz süren dizi en üstte; erken iptalde REST; grafik katmanı tek, sahte mum yok, çift satır yok,"
          " okuma yeri sabit, paket başına tek çizim")
