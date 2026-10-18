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

İnceleme düzeltmeleri (18.10, her biri önce yeniden üretildi) — Pinlenenler:
  • yeniden bağlanınca HL'nin yeniden yolladığı son 30 işlem bantta iki kez sayılmaz (gerçek Collector._handle)
  • pencere asla 15 dk'yı aşmaz (tavan kova); kovası olmayan geç işlem yanlış kovaya yazılmaz
  • kapsama: restart → "son N dk"; kopukluk → "o süre eksik olabilir"; şu an kopuk → bilinmiyor ("işlem yok" denmez)
  • defter sahip sorguları süre sınırlı: yavaş HL'de bile okunmuş defter döner, bitmeyen aday "okunamadı"; düşük
    şerit duraklıyken aday sorulmaz; kurulum düşerse hata sonucu yazılır (sayfa döngüye girmez)
  • adaylar önce en kalın kademede dolumu görülenler; 100'de kesilen emir listesi "emri yok" sayılmaz;
    bulunamayan kısım adetle; sahip tutarı kademeyi aşmaz; "dolan, şu an emri yok" ayrı; süresi geçen önbellek atılır
  • mesaj: gruplu okunamazsa ilk satır tam fiyattan; "%-0.00" / "+$0" / "7.35e-05" yazılmaz; kapsam ve emir yaşı dipnotta
  • /defter UBTC (yalnız spotta olan token) → USDC çifti

2. inceleme (18.10, yeniden üretildi) — Pinlenenler:
  • açılıştan önceki işlem (abonelikte yollanan son 30) sayılmaz; restart etiketi açılış anından, sonraki kopukluk ayrıca
  • bağlı görünen ama SILENT_SEC'tir mesaj almayan akış şu an kopuk (bilinmiyor); kollektör kopukluğu son mesajdan başlatır
  • openOrders tamamlaması tetikli emri bayraksız verir: görünen/büyük emir orderStatus ile doğrulanır, doğrulanamayan
    sayılmaz; tamamlama yalnız tam 100'de; hız sınırında / hepsi okunamayınca "sahip bilinmiyor"; ertelenen sonuç
    kısa önbellekte; tam fiyat düşüp gruplu gelince "tam fiyat defteri okunamadı"; /tani sayaçları şişmez
  • mesaj: küçük kapsam "%0.0" yazmaz; defter yoksa "okundu" denmez; kırpma boş başlık bırakmaz, ≤ BOOK_MSG_MAX
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


class _Clean:
    """Bant testleri: açılış anı geçmişte, canlı kollektör yok (başka testten kalan LIVE kapsamayı bozmasın)."""
    def __enter__(self):
        from app.hl import collector as col
        self.col = col
        self.old = (tape.STARTED, col.LIVE)
        tape.STARTED, col.LIVE = dbm.now() - 100_000, None
        tape.REG.clear()
        tape.HWM.clear()
        return self

    def __exit__(self, *exc):
        tape.STARTED, self.col.LIVE = self.old
        tape.REG.clear()
        tape.HWM.clear()


def test_tape_counts_everything():
    with _Clean():
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
        assert round(b["buy"]) == 20_700 and b["sell"] == 0 and round(b["taker"]) == 8400, b   # 8400 piyasadan + 12300 pasif
        assert s["sellers"][0]["addr"] == M and round(s["sellers"][0]["net"]) == -12_300
        assert tape.makers_near(s, "bid", 0.0041, 0.0041) == [(B, s["makers"][("bid", 0.0041, B)])]
        assert tape.makers_near(s, "ask", 0.0042, 0.0042)[0][0] == S and tape.makers_near(s, "ask", 0.005, 0.006) == []
        assert s["full"] and not s["restart"] and not s["live_down"] and tape.cover(s)["label"] == "son 15 dk"
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
        assert "📼 15 dk bant" in tape.diag_line()
    print("✅ bant) her işlem sayılır; agresör yönü; pasif = defterdeki emri dolan; 15 dk penceresi; adres tavanı")


def test_reconnect_replay_not_double_counted():
    """İnceleme 18.10 (canlı ölçüldü): yeni WS bağlantısında HL her aboneliğe son 30 işlemi yeniden yollar. Gerçek
    Collector._handle'a aynı parti iki kez → bant değişmez (fills zaten INSERT OR IGNORE)."""
    import json
    from app.config import Config
    from app.hl import collector as colmod

    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "dup.db"))
        with _Clean():
            col = colmod.Collector(Config(), None)
            colmod.LIVE = None                              # kurucu LIVE'ı kendine bağlar; kapsamayı bozmasın
            col.valid_coins = {"xyz:TSLA"}
            t = int(dbm.now())
            data = [{"coin": "xyz:TSLA", "side": "A", "px": "400", "sz": "5000", "time": (t - 400) * 1000, "tid": 901,
                     "hash": "0x" + "3" * 64, "users": [W, B]},
                    {"coin": "xyz:TSLA", "side": "B", "px": "401", "sz": "100", "time": (t - 100) * 1000, "tid": 902,
                     "hash": "0x" + "0" * 64, "users": [M, B]}]
            await col._handle(json.dumps({"channel": "trades", "data": data}))
            s1 = tape.summary("xyz:TSLA")
            d0 = tape.STATS["dups"]
            await col._handle(json.dumps({"channel": "trades", "data": data}))       # yeniden bağlanma
            s2 = tape.summary("xyz:TSLA")
            assert s1["n"] == s2["n"] == 2 and s1["sell"] == s2["sell"] == 2_000_000 and s2["buy"] == 40_100, s2
            assert s1["makers"] == s2["makers"] and len(s2["trades"]) == 2 and s2["twap"] == 40_100
            assert tape.STATS["dups"] - d0 == 2 and "yeniden yollanan işlem atıldı" in tape.diag_line()
            # aynı saniyede yeni tid sayılır; eski saniye atılır
            tape.observe("xyz:TSLA", "B", M, B, 401, 100, t - 100, tid="903")
            tape.observe("xyz:TSLA", "B", M, B, 401, 100, t - 300, tid="904")
            assert tape.summary("xyz:TSLA")["n"] == 3
    asyncio.run(run())
    print("✅ yeniden bağlanma) HL'nin yeniden yolladığı son işlemler bantta iki kez sayılmaz (gerçek _handle)")


def test_window_and_coverage():
    """İnceleme 18.10: pencere en eski kovayı tümüyle alıyordu (15:59'a kadar); kopukluk / restart yazılmıyordu,
    akış kopukken "işlem görülmedi" deniyordu (doğrusu bilinmiyor)."""
    from app.hl import collector as col
    with _Clean():
        t = 1_800_000_010                                  # kova sınırı + 10 sn
        tape.observe("Z", "B", B, S, 1.0, 100, t - 905)    # pencere dışı ama yarım kalan kovada → sayılmaz
        tape.observe("Z", "B", B, S, 1.0, 200, t - 880)
        s = tape.summary("Z", t)
        assert s["n"] == 1 and s["buy"] == 200 and tape.BUCKET * 29 < s["span"] <= tape.WINDOW, s
        assert tape.cover(s) == {"label": "son 15 dk", "warn": "", "unknown": False}
        # kovası olmayan geç işlem en yeni kovaya yazılmaz
        tape.observe("Z", "B", B, S, 1.0, 50, t - 1)
        late0 = tape.STATS["late"]
        tape.observe("Z", "A", B, S, 1.0, 777, t - 600)
        assert tape.summary("Z", t)["sell"] == 0 and tape.STATS["late"] == late0 + 1
        # restart: bot 5 dk önce açıldı
        tape.STARTED = t - 300
        c = tape.cover(tape.summary("Z", t))
        assert c["label"] == "son 5 dk" and "bot 5 dk önce yeniden başladı" in c["warn"] and not c["unknown"], c
        tape.STARTED = t - 100_000

        class Live:
            down_log = [(t - 400, t - 220)]
            down_since = 0.0
        col.LIVE = Live()
        s = tape.summary("Z", t)
        assert s["gaps"] == [[t - 400, t - 220]] and s["gap_sec"] == 180 and not s["full"], s
        assert "3 dk kopuktu, o süre eksik olabilir" in tape.cover(s)["warn"]
        Live.down_since = float(t - 60)
        s = tape.summary("Z", t)
        assert s["live_down"] and tape.cover(s)["unknown"] and "şu an kopuk" in tape.cover(s)["warn"]
        msg = fmt.book_owners("Z", None, {**s, "n": 0}, listened=True)
        assert "⏱ son 15 dk bilinmiyor: canlı akış şu an kopuk" in msg and "işlem görülmedi" not in msg, msg
        # 2. inceleme: yarı-açık bağlantı — bağlı görünüyor ama SILENT_SEC'tir mesaj yok → şu an kopuk
        Live.down_since, Live.down_log = 0.0, []
        Live.connected, Live.last_msg_at = True, float(t - tape.SILENT_SEC - 5)
        s = tape.summary("Z", t)
        assert s["live_down"] and s["gaps"][-1][0] == t - tape.SILENT_SEC - 5, s["gaps"]
        Live.last_msg_at = float(t - 10)
        assert not tape.summary("Z", t)["live_down"]
        # 2. inceleme: restart + sonradan kopukluk → etiket açılıştan, kopukluk ayrıca yazılır
        tape.STARTED = t - 600
        Live.down_log = [(t - 600, t - 597), (t - 300, t - 60)]            # açılış → abonelik; 4 dk kopukluk
        c = tape.cover(tape.summary("Z", t))
        assert c["label"] == "son 10 dk" and "bot 10 dk önce yeniden başladı" in c["warn"], c
        assert "canlı akış ayrıca 4 dk kopuktu" in c["warn"], c
        Live.down_log = [(t - 600, t - 597)]
        c = tape.cover(tape.summary("Z", t))
        assert c["warn"] == "bot 10 dk önce yeniden başladı, öncesi bantta yok", c
        # 3. inceleme: açılış → abonelik boşluğu uzun sürdüyse (5 dk) gizlenmez
        Live.down_log = [(t - 598, t - 300)]
        c = tape.cover(tape.summary("Z", t))
        assert "canlı akış ayrıca 4 dk kopuktu" in c["warn"], c
        # 2. inceleme: açılıştan önceki işlem (abonelikte yollanan son 30) sayılmaz
        tape.REG.clear()
        tape.HWM.clear()
        pre0 = tape.STATS["pre"]
        for i in range(30):
            tape.observe("Z", "B", B, S, 1.0, 10, t - 900 + i * 10, tid=f"p{i}")   # t-900 … t-610: açılıştan önce
        tape.observe("Z", "B", B, S, 1.0, 10, t - 30, tid="new")
        s = tape.summary("Z", t)
        assert s["n"] == 1 and tape.STATS["pre"] - pre0 == 30 and "açılış öncesi işlem sayılmadı" in tape.diag_line()
    print("✅ pencere/kapsam) asla 15 dk'dan uzun değil; geç işlem yanlış kovaya yazılmaz; restart, kopukluk ve"
          " şu an kopuk (bilinmiyor) yazılır")


def _book(levels_bid, levels_ask):
    return {"levels": [[{"px": str(p), "sz": str(u / p), "n": n} for p, u, n in levels_bid],
                       [{"px": str(p), "sz": str(u / p), "n": n} for p, u, n in levels_ask]]}


class _Cli:
    def __init__(self, orders=None, fail=(), slow=(), paused=0.0, grouped_fails=False, full=None, ostat=None,
                 raw_fails=False, ostat_fail=False):
        self.orders, self.fail, self.slow, self.calls = orders or {}, set(fail), set(slow), []
        self.paused, self.grouped_fails, self.full = paused, grouped_fails, full or {}
        self.ostat, self.raw_fails = ostat or {}, raw_fails      # oid → (açık mı, tetikli mi)
        self.ostat_fail = ostat_fail

    def low_paused(self):
        return self.paused

    async def l2_book(self, coin, n_sig_figs=None, retries=4):
        assert retries == 1, "kullanıcı bekleyen yol: 429 tekrarları süreyi aşmasın"
        if not n_sig_figs and self.raw_fails:
            raise RuntimeError("429")
        if n_sig_figs:
            if self.grouped_fails:
                raise RuntimeError("429")
            return _book([(0.0041, 9_800_000, 197), (0.00398, 1_300_000, 32)], [(0.00417, 1_440_000, 154)])
        return _book([(0.004117, 20_000, 3), (0.0041, 7_780_000, 20), (0.004104, 407_000, 29)],
                     [(0.004119, 15_000, 2), (0.004131, 87_000, 14)])

    async def info(self, p, retries=4, priority=None, stats=None):
        assert p["type"] in ("frontendOpenOrders", "openOrders", "orderStatus") and priority == "low" and retries == 1, p
        if p["type"] == "orderStatus":
            self.calls.append(("status", p["oid"]))
            if self.ostat_fail:
                raise RuntimeError("429")
            if p["oid"] not in self.ostat:
                return {"status": "unknownOid"}
            op, trig = self.ostat[p["oid"]]
            return {"status": "order", "order": {"order": {"oid": p["oid"], "isTrigger": trig,
                                                           "orderType": "Stop Market" if trig else "Limit"},
                                                 "status": "open" if op else "canceled"}}
        if p["type"] == "openOrders":                       # sınırsız liste (frontendOpenOrders 100'de kesince)
            self.calls.append(("full", p["user"]))
            if p["user"] not in self.full:
                raise RuntimeError("429")
            return self.full[p["user"]]
        self.calls.append(p["user"])
        if p["user"] in self.slow:
            await asyncio.sleep(30)
        if p["user"] in self.fail:
            raise RuntimeError("429")
        return self.orders.get(p["user"], [])


def _reset_bo():
    bo.RESULTS.clear()
    bo.ADDR_ORDERS.clear()
    bo.OID_OK.clear()
    bo.INFLIGHT.clear()


def test_book_owners_measured():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "bo.db"))
        _reset_bo()
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
        assert r["orders_ts"] is not None and r["depth"]["reach"]["grouped"][0] < 0 < r["depth"]["reach"]["grouped"][1]
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
        # defter okunamazsa: hata, sahip yok, aday sorulmaz, patlamaz
        down = _Cli(orders)

        async def nobook(coin, n_sig_figs=None, retries=4):
            raise RuntimeError("ağ yok")
        down.l2_book = nobook
        rd = await bo.build(down, "PUMP")
        assert rd["err"] == ["defter okunamadı"] and rd["mid"] is None and rd["raw"]["bid"] == [] and not down.calls
        assert "🧱 defter sahipleri" in bo.diag_line()
        return r
    with _Clean():
        r = asyncio.run(run())
    assert bo.group_unit(0.0041) == 1e-05 and bo.group_unit(77.1) == 0.1 and bo.group_unit(0) == 0
    print("✅ defter) tam fiyat + gruplu; sahip = doğrulanmış açık emir / pasif dolum; bulunamayan ayrı; aday sırası;"
          " önbellek; tetikli emir sayılmaz; okunamazsa dürüst")
    return r


def test_book_owners_review_fixes():
    """İnceleme 18.10 (her biri yeniden üretildi): süre aşımında defter atılıyordu; düşük şerit duraklıyken 10 sn
    bekleyip "hazırlanamadı"; kalın kademenin küçük dolumcusu aday olmuyordu; 100'de kesik liste "emri yok" (openOrders ile tamamlanır);
    gruplu satış kovasında hayalet "bulunamayan"; önbellek bayat ve süresi geçenler bellekte; dolan ≠ sahip."""
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "bo2.db"))
        _reset_bo()
        t = dbm.now()
        # (1) yavaş HL: sahip sorguları bütçede kesilir, defter döner
        old = bo.OWNERS_BUDGET
        bo.OWNERS_BUDGET = 0.3
        try:
            tape.observe("PUMP", "A", B, M, 0.0041, 3_000_000, t - 20)
            cli = _Cli(slow=[B, M])
            t0 = asyncio.get_running_loop().time()
            r = await bo.build(cli, "PUMP")
            took = asyncio.get_running_loop().time() - t0
            assert took < 2 and r["raw"]["bid"] and r["grouped"]["bid"] and r["failed"] == 2, (took, r["failed"])
            assert bo.STATS["timeouts"] >= 2 and "süre aştı" in bo.diag_line()
        finally:
            bo.OWNERS_BUDGET = old
        _reset_bo()
        # (2) düşük şerit duraklı: aday sorulmaz, nedeni yazılır, defter yine var
        cli = _Cli(paused=42.0)
        r = await bo.build(cli, "PUMP")
        assert not cli.calls and r["cands"] == 0 and r["raw"]["bid"] and r["err"] == [
            "açık emirler sorulmadı (HL hız sınırı, birazdan yeniden)"] and r["owners_unknown"] and r["deferred"], r
        l2 = fmt.book_owners("PUMP", {**r, "age": 1}, tape.summary("PUMP"), listened=True).split("\n")[1]
        assert "sahip bilinmiyor (açık emirler sorulamadı)" in l2 and "bulunamadı" not in l2, l2
        bo.RESULTS["PUMP"] = (dbm.now() - bo.DEFER_CACHE - 1, r)   # ertelenen sonuç kısa önbellekte: yeniden kurulur
        assert bo.kick(_Cli(), "PUMP")
        await asyncio.sleep(0.05)
        _reset_bo()
        # (3) kurulum düşerse hata sonucu yazılır → sayfa "hazırlanıyor" döngüsünde kalmaz
        _reset_bo()
        old_b = bo.build

        async def bad_build(client, coin):
            raise ValueError("bozuk")
        bo.build = bad_build
        try:
            assert bo.kick(_Cli(), "PUMP")
            await asyncio.sleep(0.05)
        finally:
            bo.build = old_b
        got = bo.cached("PUMP")
        assert got and got["err"] == ["kurulamadı"] and not bo.kick(_Cli(), "PUMP"), got
        # (4) aday sırası: kalın kademedeki küçük (< MAKER_MIN) dolumcu, başka yerdeki büyük dolumculardan önce
        _reset_bo()
        tape.REG.clear()
        tape.HWM.clear()
        big = [f"0x{i:040x}" for i in range(1, 7)]
        for i, a in enumerate(big):                         # 6 büyük dolumcu, kalın kademelerin dışında
            tape.observe("PUMP", "A", a, M, 0.00405, 5_000_000 + i, t - 30)
        tape.observe("PUMP", "A", W, M, 0.0041, 500_000, t - 10)   # $2,050: kalın 0.0041 kademesinde
        cands = await bo._candidates("PUMP", tape.summary("PUMP"), [("bid", 0.0041, 0.0041)])
        assert cands[0] == W and len(cands) <= bo.CAND_MAX and set(big[:4]) <= set(cands), cands
        # (5) 100'de kesik liste: sahip bulunanlar yazılır ama "emri yok" denmez; tam liste + emirsiz dolumcu → gone
        _reset_bo()
        tape.REG.clear()
        tape.HWM.clear()
        tape.observe("PUMP", "A", B, M, 0.0041, 1_000_000, t - 10)   # B dolumcu (tam liste, emri yok)
        tape.observe("PUMP", "A", W, M, 0.0041, 1_000_000, t - 9)    # W dolumcu (liste kesik)
        many = [{"coin": "BTC", "side": "B", "limitPx": "1", "sz": "1", "oid": i} for i in range(100)]
        cli = _Cli({B: [], W: many})
        r = await bo.build(cli, "PUMP")
        raw0 = next(lv for lv in r["raw"]["bid"] if lv["px"] == 0.0041)
        assert r["partial"] == 1 and [a for a, _ in raw0["makers"]] == [W], raw0
        assert [a for a, _ in raw0["makers_gone"]] == [B] and ("full", W) in cli.calls, raw0
        txt = fmt.book_owners("PUMP", {**r, "age": 1}, tape.summary("PUMP"), listened=True)
        assert "dolan, şu an emri yok" in txt and "HL 100'de kesti" in txt
        # 100 gelir ama openOrders tamamlar: emir bulunur, kesik sayılmaz, tetikli emir eklenmez
        _reset_bo()
        front = many[:99] + [{"coin": "PUMP", "side": "B", "limitPx": "0.0041", "sz": "1", "oid": 777,
                              "isTrigger": True}]
        full = [{"coin": "BTC", "side": "B", "limitPx": "1", "sz": "1", "oid": i} for i in range(99)] + [
            {"coin": "PUMP", "side": "B", "limitPx": "0.0041", "sz": str(200_000 / 0.0041), "oid": 555},
            {"coin": "PUMP", "side": "B", "limitPx": "0.0041", "sz": "1", "oid": 777}]
        cli = _Cli({W: front, B: []}, full={W: full})
        r = await bo.build(cli, "PUMP")
        raw0 = next(lv for lv in r["raw"]["bid"] if lv["px"] == 0.0041)
        # 3. inceleme (canlı): HL orderStatus eski ama hâlâ bekleyen emre de "unknownOid" der → kesin "yok" DEĞİL:
        # doğrulanamadı (sayılmaz) ve W için "şu an emri yok" denmez; önbelleğe de yazılmaz
        assert r["partial"] == 0 and r["unverified"] == 1 and raw0["owners"] == [], (r["partial"], r["unverified"], raw0)
        assert W in [a for a, _ in raw0["makers"]] and W not in [a for a, _ in raw0["makers_gone"]]
        assert ("status", 555) in cli.calls and (W, 555) not in bo.OID_OK
        assert all(len(x) == 7 for x in bo.ADDR_ORDERS[(W, "")][1]), "önbellekte ham yanıt değil kısa kayıt"
        # (5b) tamamlamadan gelen emir orderStatus ile doğrulanır: açık limit ✅; tetikli (Stop, bayraksız gelir) sayılmaz
        _reset_bo()
        full2 = full + [{"coin": "PUMP", "side": "B", "limitPx": "0.0041", "sz": str(900_000 / 0.0041), "oid": 556}]
        cli = _Cli({W: front, B: []}, full={W: full2}, ostat={555: (True, False), 556: (True, True)})
        r = await bo.build(cli, "PUMP")
        raw0 = next(lv for lv in r["raw"]["bid"] if lv["px"] == 0.0041)
        assert raw0["owners"] == [(W, 200_000.0)] and r["unverified"] == 0, (raw0["owners"], r["unverified"])
        assert ("status", 555) in cli.calls and ("status", 556) in cli.calls and 556 not in [o["oid"] for o in r["orders"]]
        # kesin "kapandı" cevabı (status=order, canceled) sahip saymaz ve o adreste emri yok der
        _reset_bo()
        v0 = bo.STATS["verified"]
        cli = _Cli({W: front, B: []}, full={W: full}, ostat={555: (False, False)})
        r = await bo.build(cli, "PUMP")
        raw0 = next(lv for lv in r["raw"]["bid"] if lv["px"] == 0.0041)
        assert raw0["owners"] == [] and r["unverified"] == 0 and W in [a for a, _ in raw0["makers_gone"]], raw0
        assert bo.STATS["verified"] - v0 == 1, "sayaç yalnız kesin cevabı sayar"
        # orderStatus okunamazsa: sayılmaz, "doğrulanamadı" yazılır, o adreste "şu an emri yok" denmez
        _reset_bo()
        cli = _Cli({W: front, B: []}, full={W: full}, ostat_fail=True)
        r = await bo.build(cli, "PUMP")
        raw0 = next(lv for lv in r["raw"]["bid"] if lv["px"] == 0.0041)
        assert r["unverified"] == 1 and raw0["owners"] == [] and W in [a for a, _ in raw0["makers"]], (r["unverified"], raw0)
        assert "1 emir doğrulanamadı (sayılmadı)" in fmt.book_owners("PUMP", {**r, "age": 1}, tape.summary("PUMP"))
        # (5c) 100'den çok gelen liste tamdır: openOrders sorulmaz
        _reset_bo()
        cli = _Cli({W: many + many[:50]})
        r = await bo.build(cli, "PUMP")
        assert ("full", W) not in cli.calls and r["partial"] == 0, cli.calls
        # (5d) /tani sayaçları: önbellekten okunan kesik liste yeniden sayılmaz; openOrders "adres" sayılmaz
        _reset_bo()
        st0 = dict(bo.STATS)
        cli = _Cli({W: many}, fail=[])
        await bo.build(cli, "PUMP")
        await bo.build(cli, "PUMP")
        assert bo.STATS["partial"] - st0["partial"] == 1 and bo.STATS["full"] - st0["full"] == 1, bo.STATS
        # (6) gruplu satış kovası: emir fiyatı kova fiyatından düşük → bulunamayan adetle (hayalet yok)
        lv = {"px": 0.00417, "sz": 1000.0, "usd": 4.17, "n": 1}
        bo._attach([lv], "ask", [{"side": "ask", "px": 0.004161, "sz": 1000.0, "usd": 4.161, "addr": S}],
                   {"makers": {}}, True, 1e-05)
        assert lv["unknown_usd"] == 0 and lv["owners"] == [(S, 4.161)], lv
        # sahip tutarı kademeyi aşamaz
        lv = {"px": 1.0, "sz": 10.0, "usd": 10.0, "n": 1}
        bo._attach([lv], "bid", [{"side": "bid", "px": 1.0, "sz": 50.0, "usd": 50.0, "addr": S}], {}, False, 0)
        assert lv["owners"] == [(S, 10.0)] and lv["unknown_usd"] == 0, lv
        # (7) önbellek: 60 sn; süresi geçen kayıt yazımda atılır
        assert bo.ADDR_TTL == 60
        bo.ADDR_ORDERS[("0xold", "")] = (t - 61, [])
        await bo._orders_of(_Cli(), S, "")
        assert ("0xold", "") not in bo.ADDR_ORDERS and (S, "") in bo.ADDR_ORDERS
        # (8b) tam fiyat düştü, gruplu geldi: "defter okunamadı" denmez
        _reset_bo()
        r = await bo.build(_Cli(raw_fails=True), "PUMP")
        assert r["err"] == ["tam fiyat defteri okunamadı"] and r["grouped"]["bid"], r["err"]
        # (8c) hepsi okunamadı → sahip bilinmiyor
        _reset_bo()
        tape.observe("PUMP", "A", B, M, 0.0041, 1_000_000, t - 5, tid="x1")
        r = await bo.build(_Cli(fail=[B, M, W, S]), "PUMP")
        assert r["cands"] and r["failed"] == r["cands"] and r["owners_unknown"], r
        # (8) gruplu okunamadı: hata yazılır, mesajın ilk satırı tam fiyattan
        _reset_bo()
        r = await bo.build(_Cli(grouped_fails=True), "PUMP")
        assert r["err"] == ["gruplu defter okunamadı"] and not r["grouped"]["bid"] and r["raw"]["bid"]
        txt = fmt.book_owners("PUMP", {**r, "age": 2}, tape.summary("PUMP"), listened=True)
        assert txt.startswith("🧱 <b>PUMP defteri</b> (tam fiyat) · 🟢 alış 0.0041") and "okunamadı" not in txt.split("\n")[0]
        assert "⚠️ gruplu defter okunamadı" in txt
    with _Clean():
        asyncio.run(run())
    _reset_bo()
    print("✅ defter düzeltmeleri) süre sınırı defteri atmaz; duraklı şeritte sorulmaz; düşen kurulum hata sonucu;"
          " kalın kademe dolumcusu önce; 100'de kesik → openOrders, olmazsa ≠ emri yok; adetle bulunamayan; sahip ≤ kademe; 60 sn önbellek")


def test_defter_message():
    sys.path.insert(0, HERE)
    import test_wording as tw
    r = test_book_owners_measured()
    with _Clean():
        t = dbm.now()
        tape.observe("PUMP", "B", B, S, 0.0042, 2_000_000, t - 30)
        tape.observe("PUMP", "A", B, M, 0.0041, 3_000_000, t - 20, twap=True)
        summ = tape.summary("PUMP", t)
        txt = fmt.book_owners("PUMP", {**r, "age": 7}, summ, listened=True, name="PUMP")
        lines = txt.split("\n")
        assert lines[0].startswith("🧱 <b>PUMP defteri</b> · 🟢 alış 0.0041") and "$9.8M</b> (197 emir)" in lines[0], lines[0]
        assert "🔴 satış 0.00417" in lines[0] and "(%-0.44)" in lines[0]
        assert lines[1].startswith("👤 alış 0.0041: ✅") and "satış 0.00417: ✅" in lines[1], lines[1]
        assert lines[2].startswith("⏱ son 15 dk: piyasadan alış <b>$8K</b>") and "net <b>-$4K</b>" in lines[2], lines[2]
        assert "🔹 TWAP" in lines[2] and "-$12K" in lines[3] and "+$21K" in lines[3], lines[3]
        assert "0.004104 $407K" in txt, "tam fiyat 5 anlamlı hane (PUMP 0.0041xx kademeleri ayrışır)"
        assert lines[3].startswith("👥 en çok alan") and "en çok satan" in lines[3], lines[3]
        assert "Bulunan büyük emirler" in txt and "Tam fiyat" in txt and "defter 7 sn önce okundu" in txt
        assert "açık emirler en eski" in txt and "her yönde 20 kademe verir: fiyatın %" in txt and len(txt) <= 4096
        assert not tw.hits(txt), tw.hits(txt)
        # restart: 3.-4. satır kısmi bandı "son 15 dk" diye yazmaz
        tape.STARTED = t - 420
        t4 = fmt.book_owners("PUMP", {**r, "age": 7}, tape.summary("PUMP", t), listened=True)
        assert t4.split("\n")[2].startswith("⏱ son 7 dk:") and "bot 7 dk önce yeniden başladı" in t4, t4.split("\n")[2]
    # veri yok / dinlenmiyor / defter yok
    t2 = fmt.book_owners("PUMP", None, {"n": 0, "covered_sec": 900, "window": 900}, listened=False)
    assert "hazırlanamadı" in t2 and "canlı akışta dinlenmiyor" in t2 and not tw.hits(t2)
    t3 = fmt.book_owners("PUMP", {"err": ["defter okunamadı"], "grouped": {"bid": [], "ask": []}, "cands": 0, "age": 1},
                         {"n": 0, "window": 900, "span": 900, "covered_sec": 60, "restart": True}, listened=True)
    assert "okunamadı" in t3 and "son 1 dk'da işlem görülmedi (bot 1 dk önce yeniden başladı" in t3, t3
    # biçim: "%-0.00" / "+$0" / bilimsel gösterim yok
    assert fmt.dist_txt(1.00001, 1.0) == "fiyatta" and fmt.dist_txt(0.99, 1.0) == "%-1.00"
    assert fmt._sgn_usd(0.3) == "$0" and fmt._sgn_usd(-0.3) == "$0" and fmt._sgn_usd(-4000) == "-$4K"
    assert fmt.px5(0.0000735) == "0.0000735" and fmt.px5(0.004117) == "0.004117" and fmt.px5(97123.4) == "97,123"
    assert fmt.px5(0.00012345) == "0.00012345" and "e-" not in fmt.px5(1.2e-7)
    # 2. inceleme: dar kapsam (BTC tam fiyat ±%0,03) "%0.0" yazmaz; defter yoksa "okundu" denmez
    assert fmt._reach_txt({"depth": {"reach": {"raw": (-0.0253, 0.024), "grouped": (None, None)}}}) == \
        "fiyatın %0.03 altı – %0.02 üstü"
    assert "%0.01'den az" in fmt._reach_txt({"depth": {"reach": {"grouped": (-0.004, 2.0)}}})
    nob = fmt.book_owners("PUMP", {"err": ["defter okunamadı"], "grouped": {"bid": [], "ask": []}, "raw": {},
                                   "cands": 0, "age": 3}, {"n": 0, "window": 900, "span": 900}, listened=True)
    assert "3 sn önce denendi" in nob and "okundu" not in nob, nob
    # uzun ayrıntı (BTC gibi, 4973 karakter görüldü 18.10): mesaj tek parça kalır, ilk 4 satır kırpılmaz
    addrs = [f"0x{i:040x}" for i in range(1, 40)]
    lv = lambda p: {"px": p, "sz": 1.0, "usd": 9e6, "n": 300, "owners": [(a, 1e5) for a in addrs[:3]],   # noqa: E731
                    "unknown_usd": 8e6, "makers": [(a, 5e3) for a in addrs[3:6]], "makers_gone": [(a, 1e3) for a in addrs[6:9]]}
    huge = {"mid": 81_100.0, "ts": 1, "age": 1, "cands": 10, "failed": 0, "partial": 0, "err": [], "orders_ts": 1,
            "grouped": {"bid": [lv(81_000.0 - i * 100) for i in range(4)], "ask": [lv(81_200.0 + i * 100) for i in range(4)]},
            "raw": {"bid": [lv(81_000.0)], "ask": [lv(81_200.0)]},
            "orders": [{"addr": a, "side": "bid", "px": 81_000.0, "usd": 1e5} for a in addrs[:15]],
            "depth": {"reach": {"grouped": (-2.5, 2.4)}}}
    big_s = {"n": 500, "buy": 1e6, "sell": 2e6, "net": -1e6, "n_addr": 300, "twap": 0, "window": 900, "span": 900,
             "full": True, "buyers": [{"addr": addrs[20], "net": 1e5}], "sellers": [{"addr": addrs[21], "net": -1e5}],
             "trades": [{"usd": 1e5, "side": "buy", "twap": False, "taker": a} for a in addrs[22:26]]}
    tl = fmt.book_owners("BTC", huge, big_s, listened=True, name="BTC")
    assert len(tl) <= fmt.BOOK_MSG_MAX and "satırı sığmadı: sitede tamamı" in tl and tl.endswith("</i>"), len(tl)
    # kırpma altı boş bölüm başlığı bırakmaz (2. inceleme: 64 birleşimin 7'sinde kalıyordu)
    for nb in range(1, 5):
        for ng in range(0, 4):
            hb = {**huge, "orders": [], "raw": {"bid": [], "ask": []},
                  "grouped": {"bid": [{**lv(81_000.0 - i * 100), "makers_gone": [(a, 1e3) for a in addrs[6:6 + ng]]}
                                      for i in range(nb)], "ask": [lv(81_200.0)]}}
            m = fmt.book_owners("BTC", hb, {**big_s, "trades": []}, listened=True).split("\n")
            body = [x for x in m if x and not x.startswith("<i>")]
            assert not body[-1].startswith(("<b>🟢 Alış kademeleri", "<b>🔴 Satış kademeleri")), (nb, ng, body[-1])
            assert len("\n".join(m)) <= fmt.BOOK_MSG_MAX
    assert tl.split("\n")[3].startswith("👥 en çok alan") and tl.count("<b>") == tl.count("</b>")
    assert len(fmt.book_owners("BTC", huge, big_s, listened=True)) <= 4000
    print("✅ /defter) ilk 4 satır: en kalın kademe · sahipler · son 15 dk (kapsamıyla) · en çok alan/satan; ölçülü;"
          " dürüst boşlar; %-0.00 / +$0 / 7e-05 yok")


def test_defter_spot_only_token():
    """İnceleme 18.10: yalnız spotta olan token çıplak adla bulunmuyordu (/defter UBTC → "coin bulamadım")."""
    from app import assets
    from app.hl import universe
    from app.telegram.bot import TelegramBot

    async def run():
        bot = TelegramBot.__new__(TelegramBot)
        sent = []

        async def send(text, chat_id=None, **kw):
            sent.append(text)
        bot.send, bot.client, bot.collector = send, None, None
        old_names, old_res, old_get = dict(assets.SPOT_NAMES), universe.resolve_coin, bo.get
        got = []

        async def no_coin(tick):
            return None

        async def fake_get(client, coin, wait=0.0):
            got.append(coin)
            return None
        try:
            assets.SPOT_NAMES.clear()
            assets.SPOT_NAMES.update({"@140": "UBTC/USDT0", "@142": "UBTC/USDC"})
            universe.resolve_coin, bo.get = no_coin, fake_get
            assert await bot._cmd_defter("defter", ["ubtc"], "1") and got == ["@142"], got
            assert await bot._cmd_defter("ubtcdefter", [], "1") and got[-1] == "@142"
            assert await bot._cmd_defter("defter", ["yokboyle"], "1") and "bulamadım" in sent[-1]
        finally:
            assets.SPOT_NAMES.clear()
            assets.SPOT_NAMES.update(old_names)
            universe.resolve_coin, bo.get = old_res, old_get
    with _Clean():
        asyncio.run(run())
    print("✅ /defter UBTC) yalnız spotta olan token → USDC çifti; bilinmeyene dürüst cevap")


def test_wiring():
    src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
    col = src("app/hl/collector.py")
    assert "tape.observe(coin, aggr, buyer, seller, px, sz, ts, zero, tid)" in col
    assert "self.last_msg_at = time.time()" in col and "min(time.time(), self.last_msg_at or time.time())" in col
    bot = src("app/telegram/bot.py")
    assert bot.count("await self._cmd_defter(cmd, args, chat_id)") == 2 and 'c in ("defter", "kim")' in bot
    assert 'c.endswith("defter")' in bot
    assert "/defter PUMP" in fmt.commands_text(owner=False) and "/pumpdefter" in fmt.commands_text(owner=True)
    diag = src("app/diag.py")
    assert "tape.diag_line()" in diag and "bookowners.diag_line()" in diag
    routes = src("app/web/routes.py")
    assert "_tape.summary, coin" in routes and "_bo.kick(" in routes and '"book_pending": book_pending' in routes
    assert '"tape_cover": tape_cover' in routes
    readme = src("README.md")
    assert "Coin sayfasının üstü" in readme and "/defter PUMP" in readme and "explorerTxs" in readme
    assert "0.004100–0.004109 aralığındaki 160 emrin" in readme and "her yönde en yakın 20 kademeyi verir" in readme
    assert "işlem kimliğiyle" in readme and "**bilinmiyor**" in readme
    assert "minmax(min(320px,100%),1fr)" in src("app/web/templates/base.html")
    print("✅ bağlantı) kollektör kancası (tid); /defter /kim /pumpdefter iki zincirde; /tani; sayfa; README")


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
