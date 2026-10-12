"""✅ PROPR listesi — canlı senkron (propr.xyz/api/universe/config) + 12.10 listesi.

Kullanıcı (12.10): propr'ın evren sayfasını yapıştırdı — "bizde olmayan coin varsa takibe alalım, bayadır
eski listeye bakıyoruz". propr'ın kendi ucu 140 whitelisted gösteriyordu: 12 yeni (S, IO, USELESS, RSR,
AZTEC, NIL, STX, GRIFFAIN, SAND, MINA, xyz:SNXX, xyz:IREN), elimizdeki 57'si kara listede (TSM, WIF…).

Pinlenenler:
  • gömülü liste = 12.10 anlık görüntüsü (89 ana dex + 51 xyz); 12 yeni içeride, kara listedekiler dışarıda
  • kimlik dex'li: xyz:IREN ✓ / para:IREN ✗, STX (Stacks) ✓ / para:STX ✗; öneksiz sembol herhangi bir
    listeli kimlikle eşleşir; kPEPE büyük/küçük harf; elle ek (propr_symbols) sembol ya da tam kimlik
  • ayrıştırma: şekil tutmazsa hata (liste boş sanılmaz); kara/diğer durum sayılır
  • çekim: ilk çekim sessiz (taban gömülü), sonraki değişiklik bildirilir + günlüğe yazılır; HTTP hatası /
    şekil hatası son iyi listeyi korur; şüpheli küçük liste art arda 3 kez aynı gelmeden uygulanmaz;
    canlı kapalıyken gömülü liste
  • 🧪 evren: liste değişince lab evren önbelleği yeniden kurulur (in_universe arada canlı listeye bakar)
  • radarlar coin kimliğiyle sorar (equityvol not_listed 'IREN (para)'); takvim aynı sembolde hisse dex'ini
    seçer (IREN takvimi aranır, SNXX ETF); find_ticker hisse dex'ini, öneksiz STX ana dex'i seçer
  • kablolama: tür, ayarlar, spawn + açılış yüklemesi, sağlık, TASK_TR, /tani satırı, README, .env
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-propr.db")
from app import db as dbm  # noqa: E402
from app import propr  # noqa: E402
from app.config import EDITABLE_FIELDS, Config, get_config  # noqa: E402

NEW12 = ("S", "IO", "USELESS", "RSR", "AZTEC", "NIL", "STX", "GRIFFAIN", "SAND", "MINA", "xyz:SNXX", "xyz:IREN")
GONE = ("TSM", "NFLX", "LLY", "WIF", "KAITO", "KIOXIA", "SHEIN", "SOFTBANK", "JP225", "NATGAS", "NOK", "ANSEM")


def _payload(ids, black=("NOT", "para:STX", "para:IREN", "xyz:TSM", "WIF"), extra=None):
    rows = [{"asset": a, "status": "whitelisted", "maxOrderNotionalValue": "100000"} for a in ids]
    rows += [{"asset": a, "status": "blacklisted"} for a in black]
    return {"data": rows + list(extra or [])}


def _live_ids():
    """Gömülü listeyi API yazımıyla geri ver (kBONK/kPEPE küçük harfli, HIP-3 ön ekli)."""
    out = []
    for i in sorted(propr.DEFAULT_PROPR):
        out.append({"KBONK": "kBONK", "KPEPE": "kPEPE"}.get(i, i))
    return out


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "propr.db"))
    propr.set_live([])
    g = get_config()
    g.propr_symbols, g.propr_live = "", True


def _fetcher(seq):
    """Sırayla (http, payload) döndüren sahte çekici; her çağrıyı sayar."""
    calls = []

    async def f(session):
        calls.append(1)
        item = seq[min(len(calls), len(seq)) - 1]
        if isinstance(item, Exception):
            raise item
        return item
    f.calls = calls
    return f


# ---------------- liste + eşleşme ----------------

def test_default_list_and_matching():
    async def run():
        await _fresh()
        d = propr.DEFAULT_PROPR
        assert len(d) == 140 and sum(1 for i in d if ":" not in i) == 89 and sum(1 for i in d if i.startswith("xyz:")) == 51
        for x in NEW12:
            assert propr.norm(x) in d and propr.is_listed(x), x
        for x in GONE:
            assert not propr.is_listed(x) and not propr.is_listed(f"xyz:{x}"), x
        # dex'li kimlik: propr xyz:IREN'i listeler, para:IREN'i listelemez; ana dex STX listeli, para:STX değil
        assert propr.is_listed("xyz:IREN") and propr.is_listed("IREN") and not propr.is_listed("para:IREN")
        assert propr.is_listed("STX") and not propr.is_listed("para:STX") and not propr.is_listed("km:NVDA")
        assert propr.is_listed("XYZ:nvda"), "dex/sembol harf biçimi normalize"
        assert propr.is_listed("kPEPE") and propr.is_listed("KPEPE") and propr.is_listed("kBONK")
        assert not propr.is_listed("") and not propr.is_listed("@107") and not propr.is_listed(None)
        # elle ek: öneksiz → her dex, ön ekli → yalnız o dex
        g = get_config()
        g.propr_symbols = "tsm, xyz:NFLX"
        assert propr.is_listed("TSM") and propr.is_listed("xyz:TSM") and propr.is_listed("para:TSM")
        assert propr.is_listed("xyz:NFLX") and not propr.is_listed("para:NFLX") and propr.is_listed("NFLX"), \
            "ön ekli elle ek: öneksiz (rozet) sorguda sembolle eşleşir, başka dex'te değil"
        assert {"TSM", "NFLX", "IREN", "S"} <= propr.current_symbols() and "xyz:NFLX" in propr.current_ids()
        g.propr_symbols = ""
        assert not propr.is_listed("TSM")
        print("✅ liste) gömülü 12.10: 140 (89 ana dex + 51 xyz), 12 yeni içeride, kara listedekiler dışarıda;"
              " xyz:IREN ✓ para:IREN ✗, STX ✓ para:STX ✗; elle ek sembol/kimlik")
    asyncio.run(run())


def test_parse():
    ids, black, other = propr.parse(_payload(["BTC", "kPEPE", "xyz:SNXX", "XYZ:iren"],
                                             extra=[{"asset": "ODD", "status": "paused"}]))
    assert ids == ["BTC", "KPEPE", "xyz:IREN", "xyz:SNXX"] and black == 5 and other == 1
    for bad in (None, [], {}, {"data": []}, {"data": "x"}, {"rows": [{"asset": "BTC"}]}):
        try:
            propr.parse(bad)
        except ValueError:
            continue
        raise AssertionError(f"şekil hatası yakalanmadı: {bad!r}")
    try:
        propr.parse({"data": [{"asset": "BTC", "status": "whitelisted"}] + [{"status": "whitelisted"}] * 3})
        raise AssertionError("adı olmayan satırlar (>%10) kabul edildi")
    except ValueError:
        pass
    # durum adı değişti / herkes kara listede → şekil hatası (inceleme: boş liste 3 turda kabul edilip
    # her 3 saatte '−140, şimdi 0' mesajı atıyordu)
    for bad in ({"data": [{"asset": "BTC", "status": "active"}, {"asset": "ETH", "status": "active"}]},
                {"data": [{"asset": "BTC", "status": "blacklisted"}]},
                {"data": [{"asset": "BTC", "status": "whitelisted"}] + [{"asset": f"X{i}", "status": "x"} for i in range(3)]}):
        try:
            propr.parse(bad)
            raise AssertionError(f"kabul edildi: {bad!r}")
        except ValueError:
            pass
    print("✅ ayrıştırma) whitelisted → kimlik (kPEPE → KPEPE, XYZ:iren → xyz:IREN), kara/diğer sayılır;"
          " şekil tutmazsa ValueError (boş liste sanılmaz)")


# ---------------- çekim ----------------

def test_refresh_change_and_guards():
    async def run():
        await _fresh()
        base = _live_ids()
        v0 = propr.version()
        # ilk çekim: taban gömülü liste → değişiklik yok, bildirim yok; kv'ye yazılır, canlıya geçer
        r = await propr.refresh(None, _fetcher([(200, _payload(base))]))
        assert r["ok"] and not r["added"] and not r["removed"] and not r["notify"] and r["n"] == 140, r
        assert propr.source() == "canlı" and propr.version() != v0
        assert (await dbm.kv_get(propr.LIVE_KV))["ids"] == sorted(propr.DEFAULT_PROPR)
        assert await dbm.kv_get(propr.CHANGES_KV) is None
        # ilk çekimde küçük fark bildirilir (12.10: RSR bir saat sonra kara listeye alındı), büyük fark değil
        for ids, want in (([x for x in base if x != "RSR"], True), (base[:100] + ["Z%d" % i for i in range(30)], False)):
            await dbm.kv_set(propr.LIVE_KV, None)
            propr.set_live([])
            r = await propr.refresh(None, _fetcher([(200, _payload(ids))]))
            assert r["ok"] and r["notify"] is want and (r["removed"] == ["RSR"] if want else len(r["added"]) == 30), r
        await dbm.kv_set(propr.CHANGES_KV, None)
        r = await propr.refresh(None, _fetcher([(200, _payload(base))]))
        assert r["ok"] and r["notify"] and len(r["added"]) == 40 and len(r["removed"]) == 30, "kv tabanından sonra her fark"
        await dbm.kv_set(propr.CHANGES_KV, None)
        # propr yeni coin ekler, birini çıkarır → bildirim + günlük
        v1 = propr.version()
        new = [x for x in base if x != "S"] + ["NEWC", "xyz:NEWEQ"]
        r = await propr.refresh(None, _fetcher([(200, _payload(new))]))
        assert r["ok"] and r["notify"] and r["added"] == ["NEWC", "xyz:NEWEQ"] and r["removed"] == ["S"], r
        assert propr.is_listed("NEWC") and propr.is_listed("xyz:NEWEQ") and not propr.is_listed("S")
        assert propr.version() != v1
        hist = await dbm.kv_get(propr.CHANGES_KV)
        assert len(hist) == 1 and hist[0]["added"] == ["NEWC", "xyz:NEWEQ"] and hist[0]["base"] == "kv"
        # aynı liste tekrar → değişiklik yok, sürüm sabit
        v2 = propr.version()
        r = await propr.refresh(None, _fetcher([(200, _payload(new))]))
        assert r["ok"] and not r["notify"] and propr.version() == v2
        # HTTP hatası / ağ hatası / şekil hatası → son iyi liste kalır, istatistik yazar
        for item, want in (((403, None), "HTTP 403"), (asyncio.TimeoutError(), "TimeoutError"),
                           ((200, {"oops": 1}), "data listesi yok")):
            r = await propr.refresh(None, _fetcher([item]))
            assert not r["ok"] and want in r["err"] and propr.is_listed("NEWC") and propr.version() == v2, (item, r)
            st = await dbm.kv_get(propr.STATS_KV)
            assert st["ok"] is False and st["last_ok"] and want in st["err"]
        # şüpheli küçük liste: art arda 3 kez AYNI gelmeden uygulanmaz
        tiny = (200, _payload(["BTC", "ETH", "HYPE"]))
        for k in (1, 2):
            r = await propr.refresh(None, _fetcher([tiny]))
            assert not r["ok"] and f"({k}/{propr.SUS_ACCEPT})" in r["err"] and propr.is_listed("NEWC"), r
        other = (200, _payload(["BTC", "ETH", "SOL"]))
        r = await propr.refresh(None, _fetcher([other]))
        assert not r["ok"] and f"(1/{propr.SUS_ACCEPT})" in r["err"], "farklı şüpheli liste sayacı sıfırlar"
        # yarıdan fazla küçülme de şüpheli
        half = (200, _payload(base[:60]))
        r = await propr.refresh(None, _fetcher([half]))
        assert not r["ok"] and "yarıdan fazla küçüldü" in r["err"] and propr.is_listed("NEWC")
        for _ in range(propr.SUS_ACCEPT - 1):
            r = await propr.refresh(None, _fetcher([half]))
        assert r["ok"] and r["n"] == 60 and not propr.is_listed("NEWC"), "art arda aynı gelince gerçek sayılır"
        # kabul edilen küçük liste aynen tekrar gelirse artık şüpheli değil (her 3 turda 2 'başarısız' yok)
        small = (200, _payload(base[:35]))
        for _ in range(propr.SUS_ACCEPT):
            r = await propr.refresh(None, _fetcher([small]))
        assert r["ok"] and r["n"] == 35
        for _ in range(3):
            r = await propr.refresh(None, _fetcher([small]))
            assert r["ok"] and not r["err"] and r["sus_n"] == 0, r
        # şüpheli sayaç ağ hatasında ne ilerler ne sıfırlanır: küçülme, 503, küçülme, 503, küçülme → kabul
        shrink = (200, _payload(base[:15]))
        seq = [shrink, (503, None), shrink, (503, None), shrink]
        oks = [(await propr.refresh(None, _fetcher([x])))["ok"] for x in seq]
        assert oks == [False, False, False, False, True] and propr.source() == "canlı" and len(propr._LIVE) == 15, oks
        r = await propr.refresh(None, _fetcher([(200, _payload(base[:60]))]))
        assert r["ok"] and r["n"] == 60, "büyüme şüpheli değil"
        # açılış: kv'den yüklenir
        propr.set_live([])
        assert propr.source().startswith("gömülü") and await propr.load_live() == 60 and propr.source() == "canlı"
        # canlı kapalı → gömülü liste (sürüm değişir: lab evreni yeniden kurulur)
        v3 = propr.version()
        get_config().propr_live = False
        assert propr.is_listed("S") and propr.source().startswith("gömülü") and propr.version() != v3
        get_config().propr_live = True
        propr.set_live([])
        print("✅ çekim) ilk çekim: taban gömülü, küçük fark bildirilir / büyük fark susar; ekleme/çıkarma bildirilir + günlüğe; HTTP/ağ/şekil"
              " hatasında son iyi liste; şüpheli küçük liste 3 kez aynı gelmeden uygulanmaz; kv'den açılış;"
              " canlı kapalı → gömülü")
    asyncio.run(run())


def test_loop_notifies_once():
    async def run():
        await _fresh()
        sent = []

        class N:
            async def send(self, kind, text, **kw):
                sent.append((kind, text, kw))
                return True
        await dbm.kv_set(propr.LIVE_KV, {"ids": sorted(propr.DEFAULT_PROPR), "ts": 1})
        new = [x for x in _live_ids() if x not in ("S", "xyz:TSLA")] + ["NEWC", "xyz:IONQ"]
        real_fetch, real_sleep, real_start = propr.fetch, propr._sleep, propr.START_DELAY
        turns = []

        async def fake_fetch(session):
            return 200, _payload(new)

        async def fake_sleep(sec):
            turns.append(sec)
            if len(turns) >= 3:
                raise asyncio.CancelledError
        propr.fetch, propr.START_DELAY, propr._sleep = fake_fetch, 0, fake_sleep
        try:
            cfg = Config()
            cfg.propr_sync_sec = 60
            try:
                await propr.loop(cfg, None, N())
            except asyncio.CancelledError:
                pass
        finally:
            propr.fetch, propr.START_DELAY, propr._sleep = real_fetch, real_start, real_sleep
            propr.set_live([])
        assert turns[1:] == [600, 600], ("en az 600 sn", turns)
        assert len(sent) == 1 and sent[0][0] == "propr" and sent[0][2].get("public") is False, sent
        txt = sent[0][1]
        assert txt.startswith("✅ <b>PROPR listesi değişti</b> · propr.xyz · şimdi 140 enstrüman"), txt
        assert "➕ <b>eklendi (2)</b> — kripto: NEWC · hisse/endeks: IONQ" in txt
        assert "➖ <b>çıkarıldı (2)</b> — kripto: S · hisse/endeks: TSLA" in txt
        assert "TWAP / balina / likidasyon radarları listeden bağımsız" in txt
        print("✅ döngü) değişiklik tek mesaj (kind propr, fan-out yok), ikinci tur aynı liste → sessiz;"
              " aralık en az 600 sn")
    asyncio.run(run())


# ---------------- tüketiciler ----------------

def test_consumers_use_coin_ids():
    async def run():
        await _fresh()
        from app import assets
        from app.earnings.calendar import symbol_coins
        from app.hl import universe as uni
        from app.lab import data as labdata
        from app.radar import equityvol
        async with dbm.db() as c:
            for coin, dex, sym in (("xyz:IREN", "xyz", "IREN"), ("para:IREN", "para", "IREN"),
                                   ("para:STX", "para", "STX"), ("xyz:TSM", "xyz", "TSM"),
                                   ("xyz:SNXX", "xyz", "SNXX"), ("para:ANSEM", "para", "ANSEM")):
                await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES(?,?,?)", (coin, dex, sym))
            await c.commit()
        # para:IREN önce yazılmış olsa da hisse dex'i
        async with dbm.db() as c:
            await c.execute("DELETE FROM tickers WHERE coin='xyz:IREN'")
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('xyz:IREN','xyz','IREN')")
            await c.commit()
        cfg = get_config()
        old_cd = list(cfg.crypto_dexes) if isinstance(cfg.crypto_dexes, list) else cfg.crypto_dexes
        cfg.crypto_dexes = ["para"]
        assets.set_crypto_dex_symbols({"IREN", "STX", "ANSEM"})
        try:
            assert (await uni.find_ticker("IREN"))["coin"] == "xyz:IREN"
            assert (await uni.find_ticker("para:IREN"))["coin"] == "para:IREN"
            await dbm.kv_set(uni.MAIN_CTX_KV, {"c": {"STX": {"m": 0.38}, "BTC": {"m": 1.0}}, "ts": dbm.now()})
            stx = await uni.resolve_coin("stx")
            assert stx["coin"] == "STX" and stx["kind"] == "crypto", "öneksiz STX → ana dex (Stacks)"
            assert (await uni.resolve_coin("para:STX"))["coin"] == "para:STX"
            assert (await uni.resolve_coin("ansem"))["coin"] == "para:ANSEM", "ana dexte eşi yoksa kripto dex"
            assert (await uni.resolve_coin("iren"))["coin"] == "xyz:IREN"
            # takvim: aynı sembolde hisse dex'i; IREN takvimi aranır, SNXX (kaldıraçlı ETF) ve ANSEM aranmaz
            rows = [{"symbol": "IREN", "coin": "para:IREN"}, {"symbol": "IREN", "coin": "xyz:IREN"},
                    {"symbol": "ANSEM", "coin": "para:ANSEM"}, {"symbol": "SNXX", "coin": "xyz:SNXX"}]
            s2c = symbol_coins(rows, ["xyz"])
            assert s2c == {"IREN": "xyz:IREN", "ANSEM": "para:ANSEM", "SNXX": "xyz:SNXX"}, s2c
            assert assets.has_earnings(s2c["IREN"]) and not assets.has_earnings("IREN"), "öneksiz IREN para yüzünden kripto"
            assert not assets.has_earnings(s2c["ANSEM"]) and not assets.has_earnings(s2c["SNXX"])
            assert assets.klass("xyz:SNXX") == "endeks" and assets.klass("xyz:IREN") == "hisse"
            # hisse hacim radarı: coin kimliğiyle — para:IREN taranmaz, listede-olmayanlar dex'iyle yazılır
            coins, _ = await equityvol.universe(cfg)
            assert "xyz:IREN" in coins and "xyz:SNXX" in coins and "para:IREN" not in coins and "xyz:TSM" not in coins
            nl = await equityvol.not_listed()
            assert "IREN (para)" in nl and "TSM" in nl and "ANSEM" in nl and "STX" in nl and "IREN" not in nl, nl
            # 🧪 evren: liste değişince önbellek yeniden kurulur; arada in_universe canlı listeye bakar
            labdata._UNIV.update(ts=0, coins={}, pv=None)
            u1 = await labdata.lab_coins(cfg, dbm.now())
            assert "xyz:IREN" in u1 and "para:IREN" not in u1 and "xyz:TSM" not in u1 and labdata.in_universe("xyz:IREN")
            propr.set_live([x for x in _live_ids() if x != "xyz:IREN"] + ["xyz:TSM"])
            assert not labdata.in_universe("xyz:IREN") and labdata.in_universe("xyz:TSM"), "sürüm değişti → canlı liste"
            assert labdata.in_universe("BTC") and labdata.in_universe("xyz:XYZ100"), "kıyas serileri"
            u2 = await labdata.lab_coins(cfg, dbm.now())
            assert "xyz:TSM" in u2 and "xyz:IREN" not in u2, "TTL dolmadan yeniden kuruldu"
            # tam kimlik büyük/küçük harf duyarsız ('/alarm para:STX' büyük harfe çevrilip gelir)
            assert (await uni.find_ticker("PARA:STX"))["coin"] == "para:STX"
            assert (await uni.resolve_coin("PARA:STX"))["coin"] == "para:STX"
            # rozetler coin kimliğiyle: para:STX / para:IREN duvarı ve taraması '✅ PROPR' almaz
            propr.set_live([])                              # gömülü 12.10 listesi (xyz:IREN listeli)
            from app.telegram import format as fmt
            from app.web import routes
            wall = {"side": "bid", "px_lo": 1.0, "px_hi": 1.1, "sz": 1, "notional": 2e6, "dist_pct": 0.5,
                    "mark": 1.05, "opp_notional": 0, "address": None}
            assert propr.PROPR_NOTE not in fmt.wall_alert({**wall, "coin": "para:STX", "symbol": "STX"}, 1e7)
            assert propr.PROPR_NOTE in fmt.wall_alert({**wall, "coin": "xyz:IREN", "symbol": "IREN"}, 1e7)
            assert propr.PROPR_NOTE not in fmt.earnings_report({"symbol": "IREN", "coin": "para:IREN"}, "ondemand", {}, [], cfg)
            assert propr.PROPR_NOTE in fmt.earnings_report({"symbol": "IREN", "coin": "xyz:IREN"}, "ondemand", {}, [], cfg)
            # coin sayfası alt adresleri: kripto dex coini tam kimlikle (öneksiz STX artık Stacks'i açar)
            assert routes._page_ref({"coin": "para:STX", "symbol": "STX"}) == "para:STX"
            assert routes._page_ref({"coin": "xyz:IREN", "symbol": "IREN"}) == "IREN"
            assert routes._page_ref({"coin": "STX", "symbol": "STX"}) == "STX"
            # aynı sembol iki dex'te listesizse her perp ayrı satır (sayı = perp sayısı)
            async with dbm.db() as c:
                await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('xyz:SMH','xyz','SMH')")
                await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('para:SMH','para','SMH')")
                await c.commit()
            nl = await equityvol.not_listed()
            assert "SMH (xyz)" in nl and "SMH (para)" in nl and "SMH" not in nl, nl
            # 🧪 evren yedeği (liste değişti, lab turu henüz kurmadı) hariç tutulanı almaz
            old_exc, cfg.exclude_symbols = cfg.exclude_symbols, "HYPE"
            try:
                propr.set_live(_live_ids())
                assert not labdata.in_universe("HYPE") and labdata.in_universe("ETH")
            finally:
                cfg.exclude_symbols = old_exc
        finally:
            cfg.crypto_dexes = old_cd
            assets.set_crypto_dex_symbols(set())
            propr.set_live([])
            labdata._UNIV.update(ts=0, coins={}, pv=None)
        print("✅ tüketiciler) find_ticker hisse dex'i; öneksiz STX → Stacks, para:STX açıkça; takvim IREN'i"
              " xyz'den arar, SNXX ETF; hisse hacim radarı para:IREN'i taramaz, 'IREN (para)' yazar; 🧪 evren"
              " liste değişince yeniden kurulur")
    asyncio.run(run())


def test_openmove_freezes_universe_in_window():
    async def run():
        await _fresh()
        from app.radar import openmove as om
        cfg = Config()
        cfg.equity_dexes = ["xyz"]
        async with dbm.db() as c:
            await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('xyz:MU','xyz','MU')")
            await c.commit()
        O = 1_800_000_000
        saved = {k: getattr(om.REG, k) for k in ("coins", "universe_ts", "open_ts", "obs_since")}
        om.REG.coins, om.REG.universe_ts, om.REG.open_ts = {"xyz:OLD"}, O - 3600, O
        await om.ensure_universe(cfg, O + 600)
        assert om.REG.coins == {"xyz:OLD"}, "açılış penceresinde evren donar"
        await om.ensure_universe(cfg, O + om.WINDOW + 60)
        assert om.REG.coins == {"xyz:MU"}, "pencere bitince tazelenir"
        # açık bir ⏱ pencere (/15dk) sürerken de donar
        T = O + 5 * 3600
        saved_wins = om.REG.wins
        om.REG.coins, om.REG.universe_ts = {"xyz:OLD"}, T - 3600
        om.REG.wins = [{"chat": "1", "t0": T - 300, "t1": T + 600, "mins": 15}]
        try:
            await om.ensure_universe(cfg, T)
            assert om.REG.coins == {"xyz:OLD"}, "pencere sürerken evren donar"
            await om.ensure_universe(cfg, T + 700)
            assert om.REG.coins == {"xyz:MU"}, "pencere bitince tazelenir"
        finally:
            om.REG.wins = saved_wins
        for k, v in saved.items():
            setattr(om.REG, k, v)
        print("✅ açılış) liste pencere içinde değişse de evren donar (çıkan hisse donmuş fiyatla, giren"
              " referanssız rapora girmez); pencere sonra tazelenir")
    asyncio.run(run())


# ---------------- kablolama ----------------

def test_wiring_and_diag():
    async def run():
        await _fresh()
        from app import diag, health, notify
        from app.telegram import format as fmt
        src = lambda p: open(os.path.join(ROOT, p), encoding="utf-8").read()   # noqa: E731
        main = src("app/main.py")
        assert '_spawn("propr", lambda: _propr.loop(cfg, session, notifier), notifier)' in main
        assert "await _propr.load_live()" in main
        assert notify.KINDS["propr"] == ("notify_propr", "✅ PROPR listesi değişti", "normal")
        assert "propr" not in notify.PUBLIC_KINDS
        cfg = Config()
        assert cfg.propr_live is True and cfg.propr_sync_sec == 3600 and cfg.notify_propr is True
        for f in ("propr_live", "propr_sync_sec", "propr_symbols"):
            assert EDITABLE_FIELDS[f]["group"] == "Takvim & semboller" and len(EDITABLE_FIELDS[f]["desc"]) > 30, f
        assert EDITABLE_FIELDS["notify_propr"]["group"] == "Bildirimler"
        assert "CANLI" in EDITABLE_FIELDS["propr_symbols"]["desc"]
        assert health.limits(cfg)["propr"] == 3600 * 3 + 600 and health.periods(cfg)["propr"] == 3600
        assert fmt.TASK_TR["propr"] == "PROPR listesi (canlı senkron)"
        readme, env = src("README.md"), src(".env.example")
        assert "### ✅ PROPR listesi — canlı senkron" in readme and "PROPR_LIVE=1" in env and "PROPR_SYNC_SEC=3600" in env
        assert "elle tutulur ve eskir" not in readme
        # /tani satırı: gömülü → canlı → hata
        line = await propr.diag_line(cfg)
        assert line.startswith("propr listesi: gömülü (12.10) · 140 enstrüman (89 ana dex kripto · 51 HIP-3)"), line
        assert "henüz çekilmedi" in line
        await propr.refresh(None, _fetcher([(200, _payload(_live_ids()))]))
        line = await propr.diag_line(cfg)
        assert line.startswith("propr listesi: canlı · 140 enstrüman") and "son çekim ✓" in line and "kara liste 5" in line, line
        await propr.refresh(None, _fetcher([(503, None)]))
        line = await propr.diag_line(cfg)
        assert "⚠ son çekim başarısız" in line and "HTTP 503" in line and "son başarılı" in line, line
        cfg.propr_symbols = "TSM,BTC"
        line = await propr.diag_line(cfg)
        assert "elle eklenen (propr listesinde yok): TSM" in line and "elle eklenen ama zaten listede: BTC" in line, line
        cfg.propr_symbols = ""
        txt = "\n".join(await diag._subsystems(cfg))
        assert "  propr listesi: canlı · 140 enstrüman" in txt
        propr.set_live([])
        print("✅ kablolama) tür (fan-out yok), ayarlar, spawn + açılış yüklemesi, sağlık, TASK_TR, README,"
              " .env; /tani satırı gömülü/canlı/hata/elle ek durumlarını yazar")
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
