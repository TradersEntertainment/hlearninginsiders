"""📼 15 dk bant + 🧱 en kalın emirler ve sahipleri (coin sayfasının üstü, /defter).

Kullanıcı (18.10): "sitemde pump yazıp girdiğimde son 15dk alımlar satımlar hepsi, orderbookta en kalın emirlerin
sahipleri gelmiş geçmiş büyük twap emirleri en üstte olsun" · "bu kim nasıl bulabilirim bulmak için bir aracımız yok?
hem sitede hem telegramda sorarak olmalı herhalde".

Pinlenenler:
  • bant: her işlem sayılır (taban yok); HL `side` agresörü → alış/satış piyasadan; pasif taraf = defterdeki emri
    dolan (B → satıcı ask'ta, A → alıcı bid'de); adres başına alış/satış/net/piyasadan/TWAP; en büyük işlemler;
    15 dk'dan eski kova düşer; tavanı aşan adres "diğer"e (toplamlar doğru); kapsama restart'tan beri
  • defter: tam fiyat + gruplu (3 basamak); HL gruplaması alış kovasını aşağı, satış kovasını yukarı yuvarlar;
    sahip = adayın açık emri kademeye denk (✅) ya da son 15 dk'da o kademede pasif dolum; bulunamayan kısım ayrı;
    adaylar: pasif dolum sahipleri → pozisyon sahipleri → büyük alıcı/satıcı (≤ CAND_MAX); adres başına önbellek;
    okunamayan aday sayılır; tetikli / TP-SL emir sayılmaz; sayfa beklemez (kick + önbellek), komut ≤ 10 sn
  • /defter: ilk 4 satır en kalın kademe · sahipler · son 15 dk · en çok alan/satan; ölçülü dil; ≤ 4096
  • kablolama: kollektör kancası, /defter /kim /pumpdefter (iki zincir), /tani, README (kör nokta)
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-tape-book.db")
from app import db as dbm  # noqa: E402
from app.radar import bookowners as bo  # noqa: E402
from app.radar import tape  # noqa: E402
from app.telegram import format as fmt  # noqa: E402

B, S, M, W = ("0x" + c * 40 for c in "bcde")


def test_tape_counts_everything():
    tape.REG.clear()
    t = 1_800_000_000
    tape.observe("PUMP", "B", B, S, 0.0042, 2_000_000, t - 30)                 # $8,400 alış: S ask'ta pasif
    tape.observe("PUMP", "A", B, M, 0.0041, 3_000_000, t - 20, twap=False)     # $12,300 satış: B bid'de pasif
    tape.observe("PUMP", "A", B, M, 0.0041, 100, t - 19, twap=True)            # $0.41 TWAP dilimi (taban yok)
    tape.observe("PUMP", "B", W, S, 0.0042, 1_000_000, t - 1000)               # 16+ dk önce: pencere dışı
    s = tape.summary("PUMP", t)
    assert s["n"] == 3 and abs(s["buy"] - 8400) < 1e-6 and abs(s["sell"] - 12_300.41) < 1e-6, s
    assert abs(s["net"] - (8400 - 12_300.41)) < 1e-6 and s["n_addr"] == 3 and abs(s["twap"] - 0.41) < 1e-9
    top = s["trades"][0]
    assert top["side"] == "sell" and top["taker"] == M and top["maker"] == B and round(top["usd"]) == 12_300
    assert any(tr["twap"] for tr in s["trades"])
    b = next(f for f in s["buyers"] if f["addr"] == B)
    assert round(b["buy"]) == 20_700 and b["sell"] == 0 and round(b["taker"]) == 8400, b    # 8400 piyasadan + 12300 pasif
    assert s["sellers"][0]["addr"] == M and round(s["sellers"][0]["net"]) == -12_300
    assert tape.makers_near(s, "bid", 0.0041, 0.0041) == [(B, s["makers"][("bid", 0.0041, B)])]
    assert tape.makers_near(s, "ask", 0.0042, 0.0042)[0][0] == S and tape.makers_near(s, "ask", 0.005, 0.006) == []
    assert tape.summary("YOK", t)["n"] == 0
    # 15 dk sonra her şey düşer
    assert tape.summary("PUMP", t + 1000)["n"] == 0
    # adres tavanı: toplam doğru, fazlası "diğer"
    tape.REG.clear()
    old = tape.ADDR_CAP
    tape.ADDR_CAP = 2
    try:
        for i in range(5):
            tape.observe("X", "B", f"0x{i:040x}", S, 1.0, 100, t)
        s = tape.summary("X", t)
        assert s["n"] == 5 and s["buy"] == 500 and s["other"] == 400 and s["n_addr"] == 2, s   # 2 yer: ilk alıcı + satıcı
    finally:
        tape.ADDR_CAP = old
        tape.REG.clear()
    assert "📼 15 dk bant" in tape.diag_line()
    print("✅ bant) her işlem sayılır; agresör yönü; pasif = defterdeki emri dolan; 15 dk penceresi; adres tavanı")


def _book(levels_bid, levels_ask):
    return {"levels": [[{"px": str(p), "sz": str(u / p), "n": n} for p, u, n in levels_bid],
                       [{"px": str(p), "sz": str(u / p), "n": n} for p, u, n in levels_ask]]}


class _Cli:
    def __init__(self, orders=None, fail=()):
        self.orders, self.fail, self.calls = orders or {}, set(fail), []

    async def l2_book(self, coin, n_sig_figs=None):
        if n_sig_figs:
            return _book([(0.0041, 9_800_000, 197), (0.00398, 1_300_000, 32)], [(0.00417, 1_440_000, 154)])
        return _book([(0.004117, 20_000, 3), (0.0041, 7_780_000, 20), (0.004104, 407_000, 29)],
                     [(0.004119, 15_000, 2), (0.004131, 87_000, 14)])

    async def info(self, p, retries=4, priority=None, stats=None):
        assert p["type"] == "frontendOpenOrders" and priority == "low", p
        self.calls.append(p["user"])
        if p["user"] in self.fail:
            raise RuntimeError("429")
        return self.orders.get(p["user"], [])


def test_book_owners_measured():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "bo.db"))
        bo.RESULTS.clear()
        bo.ADDR_ORDERS.clear()
        tape.REG.clear()
        t = dbm.now()
        tape.observe("PUMP", "A", B, M, 0.0041, 3_000_000, t - 20)          # B'nin 0.0041 alışı doldu (pasif)
        tape.observe("PUMP", "A", W, M, 0.004105, 2_000_000, t - 10)        # W'nin 0.004105 alışı doldu (aynı kova)
        async with dbm.db() as c:                                          # pozisyon sahibi aday
            await c.execute("INSERT INTO addr_positions(address, coin, side, notional, ts) VALUES(?,?,?,?,?)",
                            (S, "PUMP", "long", 5e6, t))
            await c.commit()
        orders = {B: [{"coin": "PUMP", "side": "B", "limitPx": "0.0041", "sz": str(1.2e6 / 0.0041), "oid": 1},
                      {"coin": "PUMP", "side": "B", "limitPx": "0.0039", "sz": "10", "oid": 2, "isTrigger": True},
                      {"coin": "HYPE", "side": "A", "limitPx": "40", "sz": "100000", "oid": 3}],
                  S: [{"coin": "PUMP", "side": "A", "limitPx": "0.00417", "sz": str(300_000 / 0.00417), "oid": 4,
                       "reduceOnly": True}]}
        cli = _Cli(orders, fail=[W])
        r = await bo.build(cli, "PUMP")
        assert r["mid"] == (0.004117 + 0.004119) / 2 and r["unit"] == 1e-05 and r["unit_txt"] == "0.00001", r
        raw0 = r["raw"]["bid"][0]
        assert raw0["px"] == 0.0041 and raw0["n"] == 20 and raw0["owners"] == [(B, 1.2e6)], raw0
        assert round(raw0["unknown_usd"]) == 6_580_000 and raw0["makers"][0][0] == B
        g0 = r["grouped"]["bid"][0]                                          # 0.0041 kovası: [0.0041, 0.00411)
        assert g0["owners"] == [(B, 1.2e6)] and {a for a, _ in g0["makers"]} == {B, W}, g0
        ga = r["grouped"]["ask"][0]                                          # 0.00417 kovası: (0.00416, 0.00417]
        assert ga["owners"] == [(S, 300_000.0)] and not ga["makers"], ga
        assert [o["addr"] for o in r["orders"]] == [B, S] and r["orders"][1]["ro"], r["orders"]
        assert r["cands"] == 4 and r["failed"] == 1 and set(cli.calls) == {B, W, S, M}, (r["cands"], cli.calls)
        assert cli.calls.index(B) < cli.calls.index(S) < cli.calls.index(M), "pasif dolum → pozisyon → büyük satıcı"
        # adres önbelleği: ikinci kurulumda yeniden sorulmaz (düşen hariç)
        cli.calls.clear()
        await bo.build(cli, "PUMP")
        assert cli.calls == [W], cli.calls
        # kick / cached: sayfa beklemez, tek süren kurulum
        bo.RESULTS.clear()
        assert bo.cached("PUMP") is None and bo.kick(cli, "PUMP") and bo.kick(cli, "PUMP")
        assert len(bo.INFLIGHT) == 1
        await asyncio.sleep(0.05)
        got = bo.cached("PUMP")
        assert got and got["age"] >= 0 and not bo.kick(cli, "PUMP"), "taze: yeniden kurulmaz"
        assert await bo.get(cli, "PUMP", wait=1) is not None
        # defter okunamazsa: hata, sahip yok, patlamaz
        class Down(_Cli):
            async def l2_book(self, coin, n_sig_figs=None):
                raise RuntimeError("ağ yok")
        rd = await bo.build(Down(), "PUMP")
        assert rd["err"] == ["defter okunamadı"] and rd["mid"] is None and rd["raw"]["bid"] == []
        assert "🧱 defter sahipleri" in bo.diag_line()
        tape.REG.clear()
        return r
    r = asyncio.run(run())
    assert bo.group_unit(0.0041) == 1e-05 and bo.group_unit(77.1) == 0.1 and bo.group_unit(0) == 0
    print("✅ defter) tam fiyat + gruplu; sahip = doğrulanmış açık emir / pasif dolum; bulunamayan ayrı; aday sırası;"
          " önbellek; tetikli emir sayılmaz; okunamazsa dürüst")
    return r


def test_defter_message():
    sys.path.insert(0, HERE)
    import test_wording as tw
    r = test_book_owners_measured()
    tape.REG.clear()
    t = dbm.now()
    tape.observe("PUMP", "B", B, S, 0.0042, 2_000_000, t - 30)
    tape.observe("PUMP", "A", B, M, 0.0041, 3_000_000, t - 20, twap=True)
    summ = tape.summary("PUMP", t)
    txt = fmt.book_owners("PUMP", {**r, "age": 7}, summ, listened=True, name="PUMP")
    lines = txt.split("\n")
    assert lines[0].startswith("🧱 <b>PUMP defteri</b> · 🟢 alış 0.0041") and "$9.8M</b> (197 emir)" in lines[0], lines[0]
    assert "🔴 satış 0.00417" in lines[0]
    assert lines[1].startswith("👤 alış 0.0041: ✅") and "satış 0.00417: ✅" in lines[1], lines[1]
    assert lines[2].startswith("⏱ son 15 dk: piyasadan alış <b>$8K</b>") and "net <b>-$4K</b>" in lines[2], lines[2]
    assert "🔹 TWAP" in lines[2] and "-$12K" in lines[3] and "+$21K" in lines[3], lines[3]
    assert "0.004117 $20K" in txt or "0.0041 $7.8M" in txt, "tam fiyat 5 anlamlı hane (PUMP 0.0041xx kademeleri ayrışır)"
    assert lines[3].startswith("👥 en çok alan") and "en çok satan" in lines[3], lines[3]
    assert "Bulunan büyük emirler" in txt and "Tam fiyat" in txt and "7 sn önce" in txt and len(txt) <= 4096
    assert not tw.hits(txt), tw.hits(txt)
    # veri yok / dinlenmiyor / defter yok
    t2 = fmt.book_owners("PUMP", None, {"n": 0, "covered_sec": 900, "window": 900}, listened=False)
    assert "hazırlanamadı" in t2 and "canlı akışta dinlenmiyor" in t2 and not tw.hits(t2)
    t3 = fmt.book_owners("PUMP", {"err": ["defter okunamadı"], "grouped": {"bid": [], "ask": []}, "cands": 0, "age": 1},
                         {"n": 0, "covered_sec": 60, "window": 900}, listened=True)
    assert "okunamadı" in t3 and "bot yeni başladı" in t3
    tape.REG.clear()
    print("✅ /defter) ilk 4 satır: en kalın kademe · sahipler · son 15 dk · en çok alan/satan; ölçülü; dürüst boşlar")


def test_wiring():
    src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
    col = src("app/hl/collector.py")
    assert "tape.observe(coin, aggr, buyer, seller, px, sz, ts, zero)" in col
    bot = src("app/telegram/bot.py")
    assert bot.count("await self._cmd_defter(cmd, args, chat_id)") == 2 and 'c in ("defter", "kim")' in bot
    assert 'c.endswith("defter")' in bot
    assert "/defter PUMP" in fmt.commands_text(owner=False) and "/pumpdefter" in fmt.commands_text(owner=True)
    diag = src("app/diag.py")
    assert "tape.diag_line()" in diag and "bookowners.diag_line()" in diag
    routes = src("app/web/routes.py")
    assert "_tape.summary, coin" in routes and "_bo.kick(" in routes and '"book_pending": book_pending' in routes
    readme = src("README.md")
    assert "Coin sayfasının üstü" in readme and "/defter PUMP" in readme and "explorerTxs" in readme
    assert "0.004100–0.004109 aralığındaki 160 emrin" in readme
    print("✅ bağlantı) kollektör kancası; /defter /kim /pumpdefter iki zincirde; /tani; sayfa; README")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
