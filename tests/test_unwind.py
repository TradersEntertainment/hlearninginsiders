"""🧯 Sert satış / pozisyon boşaltma (app/radar/unwind.py).

Kullanıcı (19.10, 0x0871…e927): "bir coinin fiyatını etkileyebilecek kadar büyük pozu varsa ve tüm pozları kapamaya
başladıysa bildirim istiyorum yeni kanal" · "o yarım saat içinde yakalayıp kapayacağı diğer coinleri olası olarak
yakalayacağımız" · "dünü ayrı bugünü ayrı yakalamak lazımdı … mantık sert satışçının diğer pozları da kapatabileceği".

Kararlar: tetik WS taker akışı (adres+coin 5 dk ≥ max($100K, 10 × dakika hacmi)); doğrulama REST (azaltma, likidasyon
değil, pozisyon fiyatı etkileyecek büyüklükte, ≤15 pozisyon, ≥1 öteki açık ≥$100K); yalnız UNWIND_CHAT_ID; aynı cüzdan
2 sa içinde başka coin → devam; defter boşalınca tek özet; ölçülü dil ("hâlâ açık", tahmin yok).

Pinlenenler:
  • observe: yalnız taker, $1K altı yok, coin hacmi bilinmiyorsa tetik yok, eşik max(min, kat × dakika hacmi), pencere
    dışı düşer, aday adres başına en büyüğü, bekleme süresinde yeniden aday yok, kapalıyken (ON) hiçbir şey tutulmaz
  • judge: açma/ekleme → değil; likidasyon → değil; küçük pozisyon → değil; sepet (>15) → değil; öteki yok → değil;
    short kapatma (piyasadan alım) da çalışır
  • probe/poll: ilk mesaj, aynı bölümde yeni coin → devam (aynı coin ikinci kez yok), defter boşalınca özet; chat yoksa
    gönderim yok
  • metin: ilk satır ne/kim/ne kadar/fiyat; PROPR; pozisyon önce→şimdi + hacme oranı; "Hâlâ açık"; yasak dil yok
  • kablolama: env-only, KINDS (public değil), spawn, kollektör kancası (yeniden yollanan hariç), health, TASK_TR, /tani,
    /bosaltma, _own_chats, README + .env
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import unwind as u  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = "0x0871deb34bfd2052b1c10dc4f6c0912a2a47e927"
T0 = 1_792_430_000


def _reset():
    u.FLOW.clear(); u.PENDING.clear(); u.COOL.clear(); u.VOL1M.clear()       # noqa: E702
    u.STATE.clear(); u.STATE["episodes"] = {}                                  # noqa: E702
    u.ON[0] = True
    for k in ("sent", "updates", "done", "candidates", "probes", "sent_day"):
        u.STATS[k] = 0
    u.STATS["rejected"] = {}


def _fill(coin, px, sz, start, t, d="Close Long", crossed=True, oid=1, **kw):
    return {"coin": coin, "px": str(px), "sz": str(sz), "startPosition": str(start), "time": t * 1000, "dir": d,
            "crossed": crossed, "oid": oid, **kw}


def _pos(coin, szi, usd):
    return {"position": {"coin": coin, "szi": str(szi), "positionValue": str(usd), "unrealizedPnl": "-1"}}


def test_observe_trigger():
    _reset()
    cfg = Config()
    u.VOL1M["xyz:CBRS"] = 19.4e6 / 1440                       # ~$13.5K/dk → eşik max($100K, $135K)
    u.observe(cfg, "xyz:CBRS", "A", "0xb", A, 122.0, 1000, T0)        # $122K: altında
    assert not u.PENDING
    u.observe(cfg, "xyz:CBRS", "B", A, "0xs", 122.0, 1000, T0 + 1)    # alım (başka yön): sayılmaz
    u.observe(cfg, "xyz:CBRS", "A", "0xb", A, 121.5, 200, T0 + 2)     # satış toplamı $146K → aday
    c = u.PENDING[A]
    assert c["coin"] == "xyz:CBRS" and c["side"] == "S" and 140_000 < c["usd"] < 150_000 and c["px0"] == 122.0
    # hacmi bilinmeyen coin tetiklemez; $1K altı akışa yazılmaz
    u.observe(cfg, "ABC", "A", "0xb", "0xz", 1, 10_000_000, T0)
    u.observe(cfg, "xyz:CBRS", "A", "0xb", "0xq", 1, 500, T0)
    assert "0xz" not in u.PENDING and ("0xq", "xyz:CBRS", "S") not in u.FLOW
    # pencere dışına düşen akış sayılmaz
    u.PENDING.clear()
    u.observe(cfg, "xyz:CBRS", "A", "0xb", A, 122.0, 1000, T0 + 400)
    assert not u.PENDING
    # bekleme süresinde aday yok; kapalıyken hiçbir şey tutulmaz
    u.COOL[(A, "xyz:CBRS")] = __import__("time").time()
    u.observe(cfg, "xyz:CBRS", "A", "0xb", A, 122.0, 2000, T0 + 401)
    assert not u.PENDING
    u.ON[0] = False
    n = len(u.FLOW)
    u.observe(cfg, "TAO", "A", "0xb", "0xnew", 280, 5000, T0)
    assert len(u.FLOW) == n
    print("✅ tetik) taker akışı, max($100K, 10× dk hacmi), pencere, bekleme, bilinmeyen hacim / küçük işlem / kapalı")


def test_judge():
    _reset()
    cfg = Config()
    u.VOL1M.update({"xyz:CBRS": 13_500, "TAO": 5_900})
    cand = {"addr": A, "coin": "xyz:CBRS", "side": "S", "usd": 146_000, "first_ts": T0, "last_ts": T0 + 60,
            "px0": 122.0, "px1": 121.5}
    pos = u._positions({"main": {"assetPositions": [_pos("ZEC", 2500, 2.8e6), _pos("TAO", 5000, 1.33e6)]},
                        "xyz": {"assetPositions": [_pos("xyz:CBRS", 23750, 2.9e6)]}})
    fills = [_fill("xyz:CBRS", 122.0, 1250, 25000, T0, oid=7)]
    v = u.judge(cfg, cand, pos, fills)
    assert v["ok"] and v["before"] == 25000 * 122.0 and v["pos_side"] == "LONG" and v["orders"] == 1
    assert [p["coin"] for p in v["others"]] == ["ZEC", "TAO"] and v["now"] == 2.9e6
    assert not u.judge(cfg, cand, pos, [_fill("xyz:CBRS", 122, 1250, 12500, T0, d="Open Long")])["ok"]
    assert u.judge(cfg, cand, pos, [_fill("xyz:CBRS", 122, 1250, 25000, T0, liquidation={"x": 1})])["why"] == "likidasyon"
    assert "küçük" in u.judge(cfg, cand, pos, [_fill("xyz:CBRS", 122, 1250, 1500, T0)])["why"]
    many = pos + [{"coin": f"C{i}", "szi": 1, "usd": 1e5, "side": "LONG", "upnl": 0} for i in range(20)]
    assert "sepet" in u.judge(cfg, cand, many, fills)["why"]
    alone = [p for p in pos if p["coin"] == "xyz:CBRS"]
    assert u.judge(cfg, cand, alone, fills)["why"] == "başka açık pozisyon yok"
    # short kapatma: piyasadan alım
    sc = {**cand, "coin": "TAO", "side": "B"}
    sp = [{"coin": "TAO", "szi": -4000, "usd": 1.1e6, "side": "SHORT", "upnl": 0},
          {"coin": "ZEC", "szi": -100, "usd": 1.2e5, "side": "SHORT", "upnl": 0}]
    sv = u.judge(cfg, sc, sp, [_fill("TAO", 280, 500, -5000, T0, d="Close Short")])
    assert sv["ok"] and sv["pos_side"] == "SHORT"
    txt = fmt.unwind_alert(A, "TAO", sc, sv, {"TAO": 8.5e6, "ZEC": 3.5e8})
    assert "SHORT'unu piyasadan alarak kapatıyor" in txt
    print("✅ karar) azaltma / likidasyon / küçük / sepet / öteki yok; short kapatma")


class _Cli:
    def __init__(self, states, fills):
        self.states, self.fills, self.calls = states, fills, 0

    async def clearinghouse_all(self, user, dexes):
        self.calls += 1
        return self.states

    async def info(self, p, **kw):
        assert p["type"] == "userFillsByTime" and p["aggregateByTime"] is True
        return self.fills


class _N:
    def __init__(self):
        self.sent = []

    async def send(self, kind, text, **kw):
        self.sent.append((kind, text, kw))
        return True


def test_probe_episode_done():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "uw.db"))
        _reset()
        from app.radar import twaplive
        twaplive.VOL_CACHE.update(ts=dbm.now(), v={"xyz:CBRS": (19.4e6, 0), "TAO": (8.5e6, 0), "ZEC": (3.48e8, 0)})
        await u._refresh_vol()
        cfg = Config()
        cfg.unwind_chat_id = "-100555"
        n = _N()
        states = {"main": {"assetPositions": [_pos("ZEC", 2500, 2.8e6), _pos("TAO", 5000, 1.33e6)]},
                  "xyz": {"assetPositions": [_pos("xyz:CBRS", 23750, 2.9e6)]}}
        cli = _Cli(states, [_fill("xyz:CBRS", 122.0, 1250, 25000, T0)])
        cand = {"addr": A, "coin": "xyz:CBRS", "side": "S", "usd": 146_000, "first_ts": T0, "last_ts": T0 + 30,
                "px0": 122.0, "px1": 121.5}
        txt = await u.probe(cfg, cli, n, cand)
        lines = txt.split("\n")
        assert lines[0].startswith("🧯 <b>CBRS</b> LONG'unu piyasaya satıyor · <a href=") and "fiyat -0.41%" in lines[0]
        assert lines[1] == "✅ <b>PROPR'da listeli</b>" or "PROPR" not in lines[1]
        assert any(x.startswith("Pozisyon $3.0M → $2.9M") for x in lines)
        assert any(x.startswith("Hâlâ açık (2): <b>ZEC</b> L $2.8M") and "<b>TAO</b> L $1.3M" in x for x in lines)
        assert n.sent[-1][2]["chat_id"] == "-100555" and n.sent[-1][2]["public"] is False and u.STATS["sent"] == 1
        # aynı coin ikinci kez → mesaj yok; yeni coin → devam
        u.COOL.clear()
        assert await u.probe(cfg, cli, n, {**cand, "first_ts": T0 + 120}) is None and len(n.sent) == 1
        cli.fills = [_fill("TAO", 266.0, 500, 5000, T0 + 300)]
        t2 = await u.probe(cfg, cli, n, {**cand, "coin": "TAO", "first_ts": T0 + 300, "last_ts": T0 + 320,
                                         "px0": 268, "px1": 266})
        assert t2.startswith("🧯➕ boşaltmaya devam: <b>TAO</b>") and "Bu bölümde piyasaya sattıkları: CBRS" in t2
        assert len(n.sent) == 2 and u.STATS["updates"] == 1
        # defter boşaldı → tek özet
        cli.states = {"main": {"assetPositions": []}, "xyz": {"assetPositions": []}}
        ep = u.STATE["episodes"][A]
        ep["polled"] = 0
        out = await u.poll_episodes(cfg, cli, n, t=int(ep["last"]) + 600)
        assert len(out) == 1 and out[0].startswith("🧯✅") and "3 pozisyonun hepsini kapattı" in out[0]
        assert A not in u.STATE["episodes"] and u.STATS["done"] == 1
        from tests.test_wording import hits
        for m in (txt, t2, out[0], fmt.unwind_list(u.active_rows(), True)):
            assert not hits(m), hits(m)
        _reset()
    asyncio.run(run())
    print("✅ bölüm) ilk mesaj (hâlâ açık listesi), aynı coin tekrar yok, yeni coin devam, defter boşalınca özet")


def test_wiring():
    async def run():
        from app import diag, health, notify
        src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
        assert '_spawn("unwind", lambda: unwind.loop(cfg, client, notifier), notifier)' in src("app/main.py")
        col = src("app/hl/collector.py")
        assert "if not replay:" in col and "unwind.observe(self.cfg, coin, aggr, buyer, seller, px, sz, ts)" in col
        assert notify.KINDS["unwind"][0] == "" and "unwind" not in notify.PUBLIC_KINDS
        cfg = Config()
        assert cfg.unwind_enabled and cfg.unwind_chat_id == "" and cfg.unwind_min_usd == 100_000
        assert cfg.unwind_rate_x == 10 and cfg.unwind_big_usd == 250_000 and cfg.unwind_max_positions == 15
        assert not any(f.startswith("unwind_") for f in EDITABLE_FIELDS), "env-only"
        assert health.limits(cfg)["unwind"] == 600 and health.periods(cfg)["unwind"] == 2
        assert fmt.TASK_TR["unwind"] and "/bosaltma" in fmt.help_text()
        assert any(c[0] == "/bosaltma" for _, cmds in fmt.COMMANDS for c in cmds)
        bot = src("app/telegram/bot.py")
        assert 'getattr(self.cfg, "unwind_chat_id", "")' in bot and bot.count('elif cmd in ("bosaltma"') == 2
        readme, env = src("README.md"), src(".env.example")
        assert "## 🧯 Sert satış / pozisyon boşaltma (`UNWIND_CHAT_ID`)" in readme and "| `/bosaltma` |" in readme
        assert "**Kör nokta, açıkça:**" in readme.split("## 🧯 Sert satış")[1].split("\n## ")[0]
        for v in ("UNWIND_CHAT_ID=", "UNWIND_ENABLED=1", "UNWIND_MIN_USD=100000", "UNWIND_RATE_X=10",
                  "UNWIND_BIG_USD=250000", "UNWIND_MAX_POSITIONS=15"):
            assert v in env, v
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "uw2.db"))
        txt = "\n".join(await diag._subsystems(cfg))
        assert "🧯 sert satış: UNWIND_CHAT_ID TANIMSIZ" in txt
        cfg.unwind_chat_id = "-1"
        await dbm.kv_set(u.STATS_KV, {"coins_vol": 300, "candidates": 4, "probes": 4, "sent_day": 1, "updates": 2,
                                      "done": 1, "active": 0, "rejected": {"pozisyon coine göre küçük": 3}})
        line = [x for x in "\n".join(await diag._subsystems(cfg)).split("\n") if "sert satış:" in x][0]
        assert "aday 4" in line and "elenen: pozisyon coine göre küçük 3" in line, line
    asyncio.run(run())
    print("✅ kablolama) env-only, KINDS, spawn, kanca (replay hariç), sağlık, /bosaltma, _own_chats, README, .env, /tani")


if __name__ == "__main__":
    test_observe_trigger()
    test_judge()
    test_probe_episode_done()
    test_wiring()
