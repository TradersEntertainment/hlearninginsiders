"""🧭 HIP-3 dex keşfi + dürüst "bulunamadı" cevabı + /tani evren satırı.

Pinlenenler:
  • discover_dexes: perpDexs (ilk eleman None) + dex başına meta → kv {ts, dexes[{name, full_name,
    n, assets}]}; TTL içinde istek yok; fetch=False kv-only; hata/boş yanıtta eski kv korunur;
    meta alınamayan dex n=None ile durur
  • find_in_hip3: sembol → dex + izleniyor mu; resolve_coin izlenmeyen dex'i ÇÖZMEZ (veri yok)
  • similar_names: yakın adlar (ana dex + tickers), kendisi hariç
  • coin sayfası: 'abc builder dex'inde listeli ama izlenmiyor' + EQUITY_DEXES yönlendirmesi;
    hariç tutulmuş; keşif varken 'builder dex'lerinde de yok'; yakın ad linkleri
  • bot: çözümlenemeyen sembolde aynı bilgi, hak yenmez
  • /tani 'evren' satırı: ana dex sayısı + kv yaşı, HIP-3 listesi (✓ izlenen), PROPR'da olup
    izlenmeyen dex'te / HL'de bulunamayan
"""
import asyncio
import html as _html
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-hip3.db")
from app import db as dbm
from app import users
from app.config import Config
from app.hl import universe as uni
from app.telegram import public


class Client:
    def __init__(self, fail=False, empty=False):
        self.fail, self.empty, self.calls = fail, empty, []
        self.metas = {"xyz": ["TSLA", "xyz:SNDK"], "abc": ["ANSEM", "abc:MEME", "OLD"]}

    async def perp_dexs(self):
        self.calls.append("perpDexs")
        if self.fail:
            raise RuntimeError("HL 500")
        if self.empty:
            return []
        return [None, {"name": "xyz", "full_name": "Trade.xyz"}, {"name": "abc", "full_name": "ABC Memes"},
                {"name": "bad"}, {"name": ""}, "çöp"]

    async def meta(self, dex=""):
        self.calls.append(f"meta:{dex}")
        if dex == "bad":
            raise RuntimeError("meta 500")
        return {"universe": [{"name": n, "isDelisted": n == "OLD"} for n in self.metas[dex]]}


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "hip3.db"))
    users.reset_memory()
    public.reset_memory()
    async with dbm.db() as c:
        await c.execute("INSERT INTO tickers(coin,symbol,dex) VALUES('xyz:SNDK','SNDK','xyz')")
    await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"HYPE": {"m": 40.0}, "PUMP": {"m": 0.003}}, "ts": dbm.now()})


def test_discover_and_find():
    async def run():
        await _fresh()
        cli = Client()
        rec = await uni.discover_dexes(cli)
        assert cli.calls == ["perpDexs", "meta:xyz", "meta:abc", "meta:bad"], cli.calls
        names = {d["name"]: d for d in rec["dexes"]}
        assert set(names) == {"xyz", "abc", "bad"} and names["xyz"]["assets"] == ["SNDK", "TSLA"]
        assert names["abc"]["assets"] == ["ANSEM", "MEME"] and names["abc"]["n"] == 2, "delist OLD düşer, ön ek kalkar"
        assert names["bad"]["n"] is None and names["bad"]["assets"] == [] and names["xyz"]["full_name"] == "Trade.xyz"
        assert (await dbm.kv_get(uni.HIP3_KV))["dexes"] == rec["dexes"]
        # TTL: istek yok; fetch=False kv-only
        await uni.discover_dexes(cli)
        await uni.discover_dexes(cli, fetch=False)
        assert len(cli.calls) == 4
        # hata → eski kv korunur; boş yanıt → eski kv korunur
        async with dbm.db():
            pass
        await dbm.kv_set(uni.HIP3_KV, {**rec, "ts": dbm.now() - 7 * 3600})
        bad = Client(fail=True)
        assert (await uni.discover_dexes(bad))["dexes"] == rec["dexes"]
        emp = Client(empty=True)
        assert (await uni.discover_dexes(emp))["dexes"] == rec["dexes"]
        assert uni.parse_perp_dexs(None) == [] and uni.parse_perp_dexs("x") == []
        # find_in_hip3 / hip3_known / resolve_coin
        h = await uni.find_in_hip3("ansem", ["xyz"])
        assert h and h["dex"] == "abc" and h["watched"] is False and h["full_name"] == "ABC Memes", h
        assert (await uni.find_in_hip3("tsla", ["xyz"]))["watched"] is True
        assert await uni.find_in_hip3("abc:MEME") and await uni.find_in_hip3("nope") is None
        assert await uni.find_in_hip3("") is None and await uni.hip3_known()
        assert await uni.resolve_coin("ANSEM") is None, "izlenmeyen dex çözümlenmez (veri yok)"
        assert (await uni.resolve_coin("hype"))["kind"] == "crypto"
        # yakın adlar
        assert await uni.similar_names("HYP") == ["HYPE"] and await uni.similar_names("hype") == []
        assert await uni.similar_names("SND") == ["SNDK"] and await uni.similar_names("x") == []
        assert "PUMP" in await uni.similar_names("PUMPFUN")
        print("✅ keşif) perpDexs+meta → kv; TTL; hata/boş yanıtta eski kv; find_in_hip3; resolve değişmedi; yakın adlar")
    asyncio.run(run())


def test_page_bot_diag():
    async def run():
        await _fresh()
        await uni.discover_dexes(Client())
        cfg = Config()
        cfg.telegram_chat_id = "111"
        cfg.public_bot_enabled = True
        cfg.quiet_start_hour = cfg.quiet_end_hour = 0
        cfg.equity_dexes = ["xyz"]
        # coin sayfası bulunamadı dalı
        from app.web.routes import templates
        T = lambda h: _html.unescape(re.sub(r"<[^>]+>", " ", h))  # noqa: E731 — autoescape: ' → &#39;
        req = type("R", (), {"url": type("U", (), {"path": "/t/ANSEM"})()})()
        base = dict(request=req, k="", is_admin=False, has_pw=False, ticker=None, crypto_n=2)
        html = templates.env.get_template("coin.html").render(
            **base, symbol="ANSEM", hip3=await uni.find_in_hip3("ANSEM", ["xyz"]), hip3_known=True, similar=[], excluded=False)
        assert "abc builder dex'inde listeli (ABC Memes) ama bu bot o dex'i izlemiyor" in T(html) and "EQUITY_DEXES" in html, T(html)[-700:]
        html = templates.env.get_template("coin.html").render(
            **base, symbol="BIRD", hip3=None, hip3_known=True, similar=[], excluded=True)
        assert "hariç tutulmuş" in T(html)
        html = templates.env.get_template("coin.html").render(
            **base, symbol="HYP", hip3=None, hip3_known=True, similar=["HYPE"], excluded=False)
        assert "builder dex'lerinde de yok" in T(html) and '<a href="/t/HYPE">HYPE</a>' in html and "2 ana dex coini" in T(html)
        html = templates.env.get_template("coin.html").render(**base, symbol="XXX", hip3=None, hip3_known=False,
                                                              similar=[], excluded=False)
        assert "builder dex'lerinde de yok" not in T(html) and "bulunamadı" in T(html)
        # bot: çözümlenemeyen sembolde dex bilgisi / yakın ad; hak yenmez
        from app.telegram.bot import TelegramBot
        tb = TelegramBot(cfg, None, None, {})
        sent = []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append((chat_id, text))
            return True
        tb.send = fake_send
        dm = lambda t: {"message": {"chat": {"id": 5, "type": "private"}, "from": {"id": 5, "first_name": "A"}, "text": t}}  # noqa: E731
        await tb._handle_update(dm("/start"))
        await tb._handle_update(dm("ansem"))
        assert "<b>abc</b> builder dex'inde" in sent[-1][1] and (await users.get(5))["q_used"] == 0, sent[-1]
        await tb._handle_update(dm("hyp"))
        assert "tanımadım" in sent[-1][1] and "<code>HYPE</code>" in sent[-1][1]
        assert "abc" in await public.unknown_message(cfg, "MEME") and "Yakın" not in await public.unknown_message(cfg, "zzzz")
        # /tani evren satırı
        from app import diag
        txt = await diag.report(cfg, None)
        line = next((ln for ln in txt.splitlines() if ln.strip().startswith("evren:")), "")
        assert "evren: ana dex 2 coin (kv" in line and "HIP-3: xyz 2 ✓, abc 2, bad ? (izlenen: xyz)" in line, line
        assert "PROPR'da olup izlenmeyen dex'te: ANSEM (abc)" in line and "PROPR'da olup HL'de bulunamayan (" in line
        assert "ALGO" in line and "SNDK" not in line.split("bulunamayan")[1] and "HYPE" not in line.split("bulunamayan")[1]
        # keşif yokken ve kv boşken
        await dbm.kv_set(uni.HIP3_KV, {})
        await dbm.kv_set(uni.MAIN_CTX_KV, {})
        txt = await diag.report(cfg, None)
        line = next((ln for ln in txt.splitlines() if ln.strip().startswith("evren:")), "")
        assert "ana dex 0 coin (kv yok" in line and "HIP-3 keşfi henüz koşmadı" in line and "bulunamayan" not in line, line
        # bağlantı
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        rd = lambda *p: open(os.path.join(root, *p), encoding="utf-8").read()  # noqa: E731
        assert "uni.discover_dexes(client)" in rd("app", "main.py") and "hip3_dexes" in rd("README.md")
        from app.config import EDITABLE_FIELDS
        assert "/tani" in EDITABLE_FIELDS["equity_dexes"]["desc"]
        print("✅ sayfa/bot/tanı) izlenmeyen dex mesajı + EQUITY_DEXES yönlendirmesi; hariç; yakın ad; evren satırı PROPR↔HL")
    asyncio.run(run())
