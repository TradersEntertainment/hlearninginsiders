"""👑 Tek el — tek cüzdan az işlem gören hisse perp'ini kısa sürede oynatınca bildirim.

Kullanıcı (22.10, CRWD): "hacimsiz hisselerde crwd gibi yani googl da falan çalışmaz … o hacimli dakikada ne olduysa
tespit edip şu balina tek başına kaldırdı diye bildirim geçmek" · "büyükler kapatıyor kanalına at bu bildirimleri".
Vaka (canlı HL, yeniden üretildi): xyz:CRWD 16:23 TSİ, 0x30af…393e hacmin %94'ü ($261K / $278K), fiyat 243,57 → 245,16.

Kararlar: 180 sn pencere; hacim ≥ max($250K, 10 × dk ortalaması × 3); tek cüzdan ≥ $200K ve ≥ %60; fiyat o yönde ≥ %0,5;
yalnız hisse; UNWIND_CHAT_ID (ya da ONEHAND_CHAT_ID); 30 dk sonra ölçüm notu.

Pinlenenler:
  • CRWD benzeri dakika tetikler; büyük hisse (yüksek dk hacmi), endeks, düşük pay, küçük hareket, bekleme tetiklemez
  • doğrulama: açıyor / kapatıyor, kendi likidasyonu yazılmaz
  • mesajın ilk satırı coin + yön + hareket + pay; ABD seansı satırı; kanal UNWIND_CHAT_ID; kanal yoksa izleme yok
  • 30 dk ölçüm notu ve /tani sayımı; ölçülü dil
  • kablolama
"""
import asyncio
import datetime as dt
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-onehand.db")
from app import db as dbm  # noqa: E402
from app.config import EDITABLE_FIELDS, Config  # noqa: E402
from app.radar import onehand as oh  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

W = "0x30afce2f6842bf183c7e3fe7162e279ff0b6393e"
O = lambda i: "0x" + f"{i:040x}"  # noqa: E731
C = "xyz:CRWD"
T0 = int(dt.datetime(2026, 10, 22, 13, 23, tzinfo=dt.timezone.utc).timestamp())


def _reset():
    oh.ON[0] = True
    for d in (oh.WIN, oh.TOT, oh.BY, oh.REF, oh.LAST, oh.VOL1M, oh.PENDING, oh.COOL):
        d.clear()
    oh.STATE.clear()
    oh.STATE.update({"follow": [], "log": []})
    for k in list(oh.STATS):
        oh.STATS[k] = {} if k == "rejected" else 0


def _cfg(**kw):
    cfg = Config()
    cfg.unwind_chat_id = "-777"
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _crwd(cfg, coin=C, whale_usd=261_000, other_usd=17_000, p0=243.57, p1=245.16, side="B"):
    """16:22'de tek işlem (çıpa), 16:23'te balina 20 parça + başkaları küçük işlemler."""
    oh.observe(cfg, coin, "B", O(9), O(8), p0, 1, T0 - 400)
    n = 20
    for i in range(n):
        px = p0 + (p1 - p0) * (i + 1) / n
        a, b = (W, O(50 + i)) if side == "B" else (O(50 + i), W)
        oh.observe(cfg, coin, "B" if side == "B" else "A", a, b, px, whale_usd / n / px, T0 + i * 2)
    for i in range(5):
        oh.observe(cfg, coin, "A", O(90), O(91 + i), p1, other_usd / 5 / p1, T0 + 45 + i)


def test_trigger_and_gates():
    cfg = _cfg()
    _reset()
    oh.VOL1M[C] = 6_240_000 / 1440
    _crwd(cfg)
    assert list(oh.PENDING) == [(C, W, "B")], oh.PENDING
    c = oh.PENDING[(C, W, "B")]
    assert c["share"] > 0.9 and c["move"] > 0.6 and abs(c["ref"] - 243.57) < 1e-9, c
    # büyük hisse: dakika hacmi yüksek → taban ulaşılmaz (GOOGL)
    _reset()
    oh.VOL1M[C] = 200_000_000 / 1440
    _crwd(cfg)
    assert not oh.PENDING
    # endeks / emtia tetiklemez
    _reset()
    oh.VOL1M["xyz:GOLD"] = 4000
    _crwd(cfg, coin="xyz:GOLD")
    assert not oh.PENDING
    # pay düşük (başkaları çok)
    _reset()
    oh.VOL1M[C] = 4333
    _crwd(cfg, whale_usd=210_000, other_usd=250_000)
    assert not oh.PENDING
    # hareket küçük
    _reset()
    oh.VOL1M[C] = 4333
    _crwd(cfg, p1=243.9)
    assert not oh.PENDING
    # satış yönü: fiyat aşağı
    _reset()
    oh.VOL1M[C] = 4333
    _crwd(cfg, p1=241.9, side="S")
    assert list(oh.PENDING) == [(C, W, "S")]
    # bekleme
    _reset()
    oh.VOL1M[C] = 4333
    oh.COOL[(C, W, "B")] = 9e12
    _crwd(cfg)
    assert not oh.PENDING
    # kapalı (kanal yok → ON False) hiçbir şey tutmaz
    _reset()
    oh.ON[0] = False
    oh.VOL1M[C] = 4333
    _crwd(cfg)
    assert not oh.PENDING and not oh.WIN
    print("✅ tetik) CRWD dakikası tetikler; büyük hisse / endeks / düşük pay / küçük hareket / bekleme tetiklemez")


class Client:
    def __init__(self, fills, pos=None, bars=None, later=None):
        self.fills, self.pos, self.bars, self.later, self.calls = fills, pos, bars or [], later or [], []

    async def info(self, p):
        self.calls.append(p["type"])
        if p["type"] == "userFillsByTime":
            return self.later if p.get("endTime") else self.fills
        raise AssertionError(p)

    async def clearinghouse(self, user, dex="", **kw):
        self.calls.append(("ch", dex))
        return {"assetPositions": [{"position": self.pos}] if self.pos else []}

    async def candles(self, coin, interval, s, e):
        self.calls.append("candles")
        return self.bars


class Notifier:
    def __init__(self):
        self.sent = []

    async def send(self, kind, text, **kw):
        self.sent.append((kind, kw.get("chat_id"), text))
        return True


def _fill(side="B", d="Open Long", usd=130_000, t=T0 + 10, liq=None):
    f = {"coin": C, "side": side, "dir": d, "px": "245", "sz": str(usd / 245), "time": t * 1000, "crossed": True,
         "oid": t}
    if liq:
        f["liquidation"] = {"liquidatedUser": liq}
    return f


def test_probe_message_follow():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "oh.db"))
        cfg = _cfg()
        _reset()
        oh.VOL1M[C] = 4333
        _crwd(cfg)
        cand = oh.PENDING.pop((C, W, "B"))
        # kendi likidasyonu: yazılmaz
        n = Notifier()
        assert await oh.probe(cfg, Client([_fill(liq=W)]), n, cand) is None and not n.sent
        assert oh.STATS["rejected"] == {"likidasyon": 1}
        pos = {"coin": C, "szi": "4416", "positionValue": "1077922", "entryPx": "245.0", "liquidationPx": "231.6"}
        cli = Client([_fill(), _fill(t=T0 + 30)], pos=pos)
        n = Notifier()
        txt = await oh.probe(cfg, cli, n, cand)
        assert n.sent and n.sent[0][0] == "onehand" and n.sent[0][1] == "-777", n.sent
        first = txt.split("\n")[0]
        assert "👑 <b>CRWD</b> 🟢 +" in first and "tek cüzdan hacmin %9" in first and "piyasadan alım" in first, first
        lines = txt.split("\n")
        assert lines[1] == "✅ <b>PROPR'da listeli</b>", lines[1]          # CRWD PROPR'da: kim yine 4. satırda
        assert "ortalama dakika hacminin" in lines[2] and "24s hacim" in lines[2] and "ABD" in lines[2], lines[2]
        assert "LONG açıyor" in lines[3] and "2 piyasa emri" in lines[3] and "liq 231.6" in lines[3], lines[3]
        assert ("ch", "xyz") in cli.calls
        assert len(oh.STATE["follow"]) == 1
        # 30 dk ölçüm notu
        f = oh.STATE["follow"][0]
        bars = [{"h": "246.4", "l": "241.1", "c": "243.9"}]
        later = [_fill(usd=1_900_000, t=f["t"] + 60), _fill(side="A", d="Close Long", usd=100_000, t=f["t"] + 90)]
        n = Notifier()
        out = await oh.follow_up(cfg, Client([], bars=bars, later=later), n, t=f["due"] + 5)
        assert out and "30 dk sonra: fiyat 243.9" in out[0] and "tepe 246.4" in out[0] and "dip 241.1" in out[0], out
        assert "$1.90M daha aldı" in out[0] or "$1,9M daha aldı" in out[0] or "daha aldı" in out[0], out[0]
        assert "sattı" in out[0] and not oh.STATE["follow"] and len(oh.STATE["log"]) == 1
        oc = oh.outcome_counts(oh.STATE["log"])
        assert oc["n"] == 1 and oc["below"] == 1 and oc["back"] == 0, oc
        # ölçülü dil: tahmin yok
        for bad in ("düşecek", "manipül", "muhtemelen", "sinyal"):
            assert bad not in txt.lower() and bad not in out[0].lower(), bad
    asyncio.run(run())
    print("✅ doğrulama) likidasyon yazılmaz; ilk satır coin+yön+hareket+pay; UNWIND_CHAT_ID; 30 dk ölçüm notu + sayım")


def test_no_channel_and_session():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "oh2.db"))
        cfg = Config()
        cfg.unwind_chat_id = ""
        assert oh.chat(cfg) == ""
        assert "TANIMSIZ" in await oh.diag_line(cfg)
        cfg.onehand_chat_id = "-5"
        assert oh.chat(cfg) == "-5"
        cfg.onehand_chat_id, cfg.unwind_chat_id = "", "-9"
        assert oh.chat(cfg) == "-9"
        s = lambda h, m, d=22: oh.us_session(int(dt.datetime(2026, 10, d, h, m, tzinfo=dt.timezone.utc).timestamp()))  # noqa: E731
        assert s(13, 25) == "ABD açılış öncesi (açılışa 5 dk)" and s(15, 0) == "ABD seansı açık"
        assert "kapanış sonrası" in s(21, 0) and "hafta sonu" in s(13, 0, 24)
    asyncio.run(run())
    print("✅ kanal) ONEHAND_CHAT_ID > UNWIND_CHAT_ID; ikisi de boşsa izleme yok; ABD seansı etiketi")


def test_wiring():
    async def run():
        from app import diag, health, notify
        src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
        assert '_spawn("onehand", lambda: _onehand.loop(cfg, client, notifier), notifier)' in src("app/main.py")
        assert "onehand.observe(self.cfg, coin, aggr, buyer, seller, px, sz, ts)" in src("app/hl/collector.py")
        assert notify.KINDS["onehand"][0] == "" and "onehand" not in notify.PUBLIC_KINDS
        cfg = Config()
        assert cfg.onehand_enabled and cfg.onehand_chat_id == "" and cfg.onehand_min_usd == 250_000
        assert cfg.onehand_share_pct == 60 and cfg.onehand_move_pct == 0.5 and cfg.onehand_window_sec == 180
        assert not any(f.startswith("onehand_") for f in EDITABLE_FIELDS), "env-only"
        assert health.limits(cfg)["onehand"] == 600 and health.periods(cfg)["onehand"] == 2
        assert fmt.TASK_TR["onehand"] and "/tekel" in fmt.help_text()
        assert any(c[0] == "/tekel" for _, cmds in fmt.COMMANDS for c in cmds)
        bot = src("app/telegram/bot.py")
        assert 'getattr(self.cfg, "onehand_chat_id", "")' in bot and bot.count('elif cmd in ("tekel"') == 2
        readme, env = src("README.md"), src(".env.example")
        assert "## 👑 Tek el (`ONEHAND_CHAT_ID`, yoksa `UNWIND_CHAT_ID`)" in readme and "| `/tekel` |" in readme
        assert "**Kör nokta, açıkça:**" in readme.split("## 👑 Tek el")[1].split("\n## ")[0]
        for v in ("ONEHAND_ENABLED=1", "ONEHAND_CHAT_ID=", "ONEHAND_MIN_USD=250000", "ONEHAND_SHARE_PCT=60",
                  "ONEHAND_MOVE_PCT=0.5", "ONEHAND_FOLLOW_MIN=30"):
            assert v in env, v
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "oh3.db"))
        cfg.unwind_chat_id = "-1"
        await dbm.kv_set(oh.STATS_KV, {"candidates": 3, "probes": 2, "sent": 1, "followed": 1, "follow": 0,
                                       "outcome": {"n": 1, "back": 1, "below": 0, "ahead": 0}})
        line = [x for x in "\n".join(await diag._subsystems(cfg)).split("\n") if "👑 tek el:" in x][0]
        assert "aday 3" in line and "1 olayın 1'inde fiyat tetik öncesine döndü" in line, line
        txt = fmt.onehand_list({"follow": [], "log": []}, line, True)
        assert "Henüz olay yok" in txt
    asyncio.run(run())
    print("✅ kablolama) env-only, KINDS, spawn, kanca, sağlık, /tekel, _own_chats, README, .env, /tani")
