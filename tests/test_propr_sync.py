"""✅ PROPR listesi — yalnız büyür (propr.xyz/api/universe/config) + 12.10 birleşimi.

Kullanıcı (12.10): propr'ın evren sayfasını yapıştırdı — "bizde olmayan coin varsa takibe alalım, bayadır
eski listeye bakıyoruz" (12 yeni). Aynı gün, propr RSR'yi çıkarıp TSM/RKLB/CRWD'yi ekleyince gelen
mesajları görünce: "canlı takip etmene gerek yok, arada sırada çıkardıklarını geri eklerler; sinyaller
fazla olsun eksik olmasın".

Pinlenenler:
  • gömülü liste = propr'ın bugüne kadar listelediği her şey (07.09 kopyası ∪ 12.10 ∪ aynı gün eklenenler):
    198 (114 ana dex + 83 xyz + para:ANSEM); kara listeye alınanlar (NFLX, WIF, RSR…) İÇERİDE
  • kimlik dex'li: xyz:IREN ✓ / para:IREN ✗, STX (Stacks) ✓ / para:STX ✗; öneksiz sembol herhangi bir
    listeli kimlikle eşleşir; kPEPE büyük/küçük harf; elle ek (propr_symbols) sembol ya da tam kimlik
  • ayrıştırma: şekil tutmazsa hata; hiç listeli yoksa / durumlar tanınmıyorsa hata
  • okuma YALNIZ EKLER: propr'ın çıkardığı kalır, yeni listelenen girer, mesaj yok; ağ/şekil hatasında
    liste değişmez; tek seferde 40'tan fazla yeni → 3 kez aynı gelmeden eklenmez (sayaç hatada korunur);
    açılışta kv ∪ gömülü; okuma kapalıyken gömülü
  • 🧪 evren: liste değişince lab evren önbelleği yeniden kurulur (in_universe arada listeye bakar)
  • radarlar coin kimliğiyle sorar; takvim aynı sembolde hisse dex'ini seçer (IREN takvimi aranır, SNXX
    ETF); find_ticker hisse dex'ini, öneksiz STX ana dex'i seçer
  • kablolama: ayarlar, spawn + açılış yüklemesi, sağlık, TASK_TR, /tani satırı, README, .env; bildirim türü YOK
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
KEPT = ("xyz:TSM", "xyz:NFLX", "xyz:LLY", "WIF", "KAITO", "xyz:KIOXIA", "xyz:SHEIN", "xyz:SOFTBANK", "xyz:JP225",
        "xyz:NATGAS", "xyz:NOK", "para:ANSEM", "RSR", "xyz:RKLB", "xyz:CRWD")
# propr'ın kara listesinde olup bizim eski (07.09) listemizden gelen 55 kimlik — birleşimde içeride
OLD_ONLY = frozenset({
    "2Z", "ACE", "BERA", "CFX", "DYDX", "ENS", "ETC", "HEMI", "KAITO", "KAS", "KSHIB", "LINEA", "PEOPLE", "POPCAT",
    "RUNE", "SAGA", "SKR", "SKY", "STABLE", "STBL", "SUSHI", "W", "WIF", "WLFI", "ZEN", "para:ANSEM", "xyz:ARM",
    "xyz:ASML", "xyz:BOT", "xyz:GIGADEV", "xyz:GME", "xyz:HIMS", "xyz:HYUNDAI", "xyz:IBM", "xyz:JP225",
    "xyz:KIOXIA", "xyz:LLY", "xyz:MINIMAX", "xyz:NATGAS", "xyz:NFLX", "xyz:NOK", "xyz:NOW", "xyz:PALLADIUM",
    "xyz:PLATINUM", "xyz:QCOM", "xyz:QNT", "xyz:SHAZ", "xyz:SHEIN", "xyz:SMH", "xyz:SOFTBANK", "xyz:STRC",
    "xyz:URNM", "xyz:USAR", "xyz:WDC", "xyz:XLE"})
LATE = frozenset({"xyz:TSM", "xyz:RKLB", "xyz:CRWD"})          # 12.10 14:38'de propr'ın eklediği
LIST_1210 = sorted(set(propr.DEFAULT_PROPR) - OLD_ONLY - LATE)  # kullanıcının yapıştırdığı 140
LIVE_1210 = sorted((set(LIST_1210) - {"RSR"}) | LATE)           # propr'ın 12.10 öğleden sonraki listesi (142)


def _payload(ids, black=("NOT", "para:STX", "para:IREN", "xyz:NFLX", "WIF"), extra=None):
    rows = [{"asset": {"KBONK": "kBONK", "KPEPE": "kPEPE"}.get(a, a), "status": "whitelisted",
             "maxOrderNotionalValue": "100000"} for a in ids]
    rows += [{"asset": a, "status": "blacklisted"} for a in black]
    return {"data": rows + list(extra or [])}


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "propr.db"))
    propr.set_live([])
    g = get_config()
    g.propr_symbols, g.propr_live = "", True


def _fetcher(seq):
    """Sırayla (http, payload) döndüren sahte çekici."""
    calls = []

    async def f(session):
        calls.append(1)
        item = seq[min(len(calls), len(seq)) - 1]
        if isinstance(item, Exception):
            raise item
        return item
    return f


# ---------------- liste + eşleşme ----------------

def test_default_list_and_matching():
    async def run():
        await _fresh()
        d = propr.DEFAULT_PROPR
        assert len(d) == 198 and sum(1 for i in d if ":" not in i) == 114, len(d)
        assert sum(1 for i in d if i.startswith("xyz:")) == 83 and {i for i in d if i.startswith("para:")} == {"para:ANSEM"}
        assert len(OLD_ONLY) == 55 and OLD_ONLY <= d and len(LIST_1210) == 140 and len(LIVE_1210) == 142
        assert sum(1 for i in LIST_1210 if ":" not in i) == 89, "kullanıcının yapıştırdığı sayfa: 89 kripto + 51 xyz"
        for x in NEW12 + KEPT:
            assert propr.norm(x) in d and propr.is_listed(x), x
        # dex'li kimlik: propr xyz:IREN'i listeler, para:IREN'i listelemez; ana dex STX listeli, para:STX değil
        assert propr.is_listed("xyz:IREN") and propr.is_listed("IREN") and not propr.is_listed("para:IREN")
        assert propr.is_listed("STX") and not propr.is_listed("para:STX") and not propr.is_listed("km:NVDA")
        assert propr.is_listed("XYZ:nvda"), "dex/sembol harf biçimi normalize"
        assert propr.is_listed("kPEPE") and propr.is_listed("KPEPE") and propr.is_listed("kBONK")
        assert not propr.is_listed("") and not propr.is_listed("@107") and not propr.is_listed(None)
        assert not propr.is_listed("xyz:ZZZZ") and not propr.is_listed("NOTACOIN")
        # elle ek: öneksiz → her dex, ön ekli → yalnız o dex
        g = get_config()
        g.propr_symbols = "zzzz, xyz:QQQQ"
        assert propr.is_listed("ZZZZ") and propr.is_listed("xyz:ZZZZ") and propr.is_listed("para:ZZZZ")
        assert propr.is_listed("xyz:QQQQ") and not propr.is_listed("para:QQQQ") and propr.is_listed("QQQQ"), \
            "ön ekli elle ek: öneksiz (rozet) sorguda sembolle eşleşir, başka dex'te değil"
        assert {"ZZZZ", "QQQQ", "IREN", "S"} <= propr.current_symbols() and "xyz:QQQQ" in propr.current_ids()
        g.propr_symbols = ""
        assert not propr.is_listed("ZZZZ")
        print("✅ liste) gömülü birleşim: 198 (114 ana dex + 83 xyz + para:ANSEM); 12 yeni + propr'ın kara"
              " listedekileri (NFLX, WIF, RSR…) içeride; xyz:IREN ✓ para:IREN ✗, STX ✓ para:STX ✗; elle ek")
    asyncio.run(run())


def test_parse():
    ids, black, other = propr.parse(_payload(["BTC", "KPEPE", "xyz:SNXX", "XYZ:iren"],
                                             extra=[{"asset": "ODD", "status": "paused"}]))
    assert ids == ["BTC", "KPEPE", "xyz:IREN", "xyz:SNXX"] and black == 5 and other == 1
    for bad in (None, [], {}, {"data": []}, {"data": "x"}, {"rows": [{"asset": "BTC"}]},
                {"data": [{"asset": "BTC", "status": "whitelisted"}] + [{"status": "whitelisted"}] * 3},
                {"data": [{"asset": "BTC", "status": "active"}, {"asset": "ETH", "status": "active"}]},
                {"data": [{"asset": "BTC", "status": "blacklisted"}]},
                {"data": [{"asset": "BTC", "status": "whitelisted"}] + [{"asset": f"X{i}", "status": "x"} for i in range(3)]}):
        try:
            propr.parse(bad)
        except ValueError:
            continue
        raise AssertionError(f"şekil hatası yakalanmadı: {bad!r}")
    print("✅ ayrıştırma) whitelisted → kimlik (kPEPE → KPEPE, XYZ:iren → xyz:IREN), kara/diğer sayılır;"
          " şekil tutmazsa / hiç listeli yoksa / durumlar tanınmıyorsa ValueError")


# ---------------- okuma: yalnız ekler ----------------

def test_refresh_only_adds():
    async def run():
        await _fresh()
        v0 = propr.version()
        # propr'ın 12.10 öğleden sonraki listesi: RSR yok, TSM/RKLB/CRWD var → hiçbir şey çıkmaz, eklenen yok
        r = await propr.refresh(None, _fetcher([(200, _payload(LIVE_1210))]))
        assert r["ok"] and r["added"] == [] and r["n"] == 198 and r["n_white"] == 142, r
        assert "removed" not in r and "notify" not in r
        assert propr.is_listed("RSR") and propr.is_listed("xyz:NFLX") and propr.is_listed("WIF"), "kara listedekiler kalır"
        assert propr.source().startswith("birleşik") and propr.version() != v0
        assert len((await dbm.kv_get(propr.LIVE_KV))["ids"]) == 198 and await dbm.kv_get(propr.CHANGES_KV) is None
        # propr yeni şeyler listeler + bazılarını çıkarır → yeniler eklenir, hiçbir şey çıkmaz
        v1 = propr.version()
        new = [x for x in LIVE_1210 if x not in ("S", "xyz:TSLA")] + ["NEWC", "xyz:NEWEQ"]
        r = await propr.refresh(None, _fetcher([(200, _payload(new))]))
        assert r["ok"] and r["added"] == ["NEWC", "xyz:NEWEQ"] and r["n"] == 200, r
        assert propr.is_listed("NEWC") and propr.is_listed("xyz:NEWEQ") and propr.is_listed("S") and propr.is_listed("xyz:TSLA")
        assert propr.version() != v1
        hist = await dbm.kv_get(propr.CHANGES_KV)
        assert hist == [{"ts": hist[0]["ts"], "added": ["NEWC", "xyz:NEWEQ"]}]
        # aynı liste tekrar → değişiklik yok, sürüm sabit
        v2 = propr.version()
        r = await propr.refresh(None, _fetcher([(200, _payload(new))]))
        assert r["ok"] and r["added"] == [] and propr.version() == v2
        # propr listeyi 3 coine indirse bile hiçbir şey çıkmaz
        r = await propr.refresh(None, _fetcher([(200, _payload(["BTC", "ETH", "HYPE"]))]))
        assert r["ok"] and r["n"] == 200 and r["n_white"] == 3 and propr.is_listed("NEWC") and propr.version() == v2
        # HTTP / ağ / şekil hatası → liste değişmez, istatistik yazar
        for item, want in (((403, None), "HTTP 403"), (asyncio.TimeoutError(), "TimeoutError"),
                           ((200, {"oops": 1}), "data listesi yok"),
                           ((200, {"data": [{"asset": "BTC", "status": "active"}]}), "listeli (whitelisted) satır yok")):
            r = await propr.refresh(None, _fetcher([item]))
            assert not r["ok"] and want in r["err"] and propr.is_listed("NEWC") and propr.version() == v2, (item, r)
            st = await dbm.kv_get(propr.STATS_KV)
            assert st["ok"] is False and st["last_ok"] and want in st["err"]
        # tek seferde 40'tan fazla yeni → 3 kez aynı gelmeden eklenmez; ağ hatası sayacı sıfırlamaz
        flood = (200, _payload(LIVE_1210 + [f"JUNK{i}" for i in range(50)]))
        seq = [flood, (503, None), flood, (503, None), flood]
        oks = []
        for x in seq:
            r = await propr.refresh(None, _fetcher([x]))
            oks.append(r["ok"])
            if not r["ok"] and r["http"] == 200:
                assert "tek seferde 50 yeni kimlik" in r["err"] and not propr.is_listed("JUNK1"), r
        assert oks == [False, False, False, False, True] and propr.is_listed("JUNK1") and r["n"] == 250, oks
        other = (200, _payload(LIVE_1210 + [f"OTHER{i}" for i in range(45)]))
        r1 = await propr.refresh(None, _fetcher([other]))
        r2 = await propr.refresh(None, _fetcher([(200, _payload(LIVE_1210 + [f"DIFF{i}" for i in range(45)]))]))
        assert "(1/3)" in r1["err"] and "(1/3)" in r2["err"], "farklı şüpheli ekleme sayacı sıfırlar"
        # açılış: kv ∪ gömülü (kv eski/küçük olsa da hiçbir şey düşmez)
        await dbm.kv_set(propr.LIVE_KV, {"ids": ["BTC", "NEWC"], "ts": 1})
        propr.set_live([])
        assert propr.source().startswith("gömülü") and await propr.load_live() == 199
        assert propr.is_listed("NEWC") and propr.is_listed("WIF") and propr.source().startswith("birleşik")
        # okuma kapalı → gömülü liste (sürüm değişir: lab evreni yeniden kurulur)
        v3 = propr.version()
        get_config().propr_live = False
        assert not propr.is_listed("NEWC") and propr.is_listed("WIF") and propr.version() != v3
        get_config().propr_live = True
        propr.set_live([])
        print("✅ okuma) yalnız ekler: propr'ın çıkardığı (RSR, NFLX…) kalır, yeni listelenen girer, mesaj yok;"
              " hata/şekilde liste değişmez; 40+ yeni 3 kez aynı gelmeden eklenmez (sayaç hatada korunur);"
              " açılış kv ∪ gömülü; okuma kapalı → gömülü")
    asyncio.run(run())


def test_loop_is_silent():
    async def run():
        await _fresh()
        new = LIVE_1210 + ["NEWC"]
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
                await propr.loop(cfg, None)
            except asyncio.CancelledError:
                pass
            assert propr.is_listed("NEWC") and propr.is_listed("RSR")
        finally:
            propr.fetch, propr.START_DELAY, propr._sleep = real_fetch, real_start, real_sleep
            propr.set_live([])
        assert turns[1:] == [600, 600], ("en az 600 sn", turns)
        import inspect
        assert "notifier" not in inspect.signature(propr.loop).parameters, "mesaj yolu yok"
        print("✅ döngü) sessiz: yeni listelenen eklenir, mesaj yolu yok; aralık en az 600 sn")
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
                                   ("para:STX", "para", "STX"), ("xyz:ZZZZ", "xyz", "ZZZZ"),
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
            assert "xyz:IREN" in coins and "xyz:SNXX" in coins and "para:IREN" not in coins and "xyz:ZZZZ" not in coins
            assert "para:ANSEM" in coins, "propr ANSEM'i bir zamanlar listeledi (para:ANSEM) — yalnız büyür"
            nl = await equityvol.not_listed()
            assert "IREN (para)" in nl and "ZZZZ" in nl and "STX" in nl and "IREN" not in nl and "ANSEM" not in nl, nl
            # 🧪 evren: liste değişince önbellek yeniden kurulur; arada in_universe canlı listeye bakar
            labdata._UNIV.update(ts=0, coins={}, pv=None)
            u1 = await labdata.lab_coins(cfg, dbm.now())
            assert "xyz:IREN" in u1 and "para:IREN" not in u1 and "xyz:ZZZZ" not in u1 and labdata.in_universe("xyz:IREN")
            propr.set_live(set(propr.DEFAULT_PROPR) | {"xyz:ZZZZ"})        # propr yeni bir hisse listeledi
            assert labdata.in_universe("xyz:ZZZZ"), "sürüm değişti → doğrudan liste (lab turu beklenmez)"
            assert labdata.in_universe("BTC") and labdata.in_universe("xyz:XYZ100"), "kıyas serileri"
            u2 = await labdata.lab_coins(cfg, dbm.now())
            assert "xyz:ZZZZ" in u2 and "xyz:IREN" in u2, "TTL dolmadan yeniden kuruldu"
            # tam kimlik büyük/küçük harf duyarsız ('/alarm para:STX' büyük harfe çevrilip gelir)
            assert (await uni.find_ticker("PARA:STX"))["coin"] == "para:STX"
            assert (await uni.resolve_coin("PARA:STX"))["coin"] == "para:STX"
            # rozetler coin kimliğiyle: para:STX / para:IREN duvarı ve taraması '✅ PROPR' almaz
            propr.set_live([])                              # gömülü liste (xyz:IREN listeli)
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
                await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('xyz:QQQQ','xyz','QQQQ')")
                await c.execute("INSERT INTO tickers(coin,dex,symbol) VALUES('para:QQQQ','para','QQQQ')")
                await c.commit()
            nl = await equityvol.not_listed()
            assert "QQQQ (xyz)" in nl and "QQQQ (para)" in nl and "QQQQ" not in nl, nl
            # 🧪 evren yedeği (liste değişti, lab turu henüz kurmadı) hariç tutulanı almaz
            old_exc, cfg.exclude_symbols = cfg.exclude_symbols, "HYPE"
            try:
                propr.set_live(set(propr.DEFAULT_PROPR) | {"NEWC"})
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
              " liste büyüyünce yeniden kurulur")
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
        assert '_spawn("propr", lambda: _propr.loop(cfg, session), notifier)' in main
        assert "await _propr.load_live()" in main
        assert "propr" not in notify.KINDS and "notify_propr" not in EDITABLE_FIELDS, "kullanıcı 12.10: mesaj yok"
        assert not hasattr(fmt, "propr_change")
        cfg = Config()
        assert cfg.propr_live is True and cfg.propr_sync_sec == 3600 and not hasattr(cfg, "notify_propr")
        for f in ("propr_live", "propr_sync_sec", "propr_symbols"):
            assert EDITABLE_FIELDS[f]["group"] == "Takvim & semboller" and len(EDITABLE_FIELDS[f]["desc"]) > 30, f
        assert "çıkarılmaz" in EDITABLE_FIELDS["propr_live"]["desc"] and "yalnız büyür" in EDITABLE_FIELDS["propr_symbols"]["desc"]
        assert health.limits(cfg)["propr"] == 3600 * 3 + 600 and health.periods(cfg)["propr"] == 3600
        assert fmt.TASK_TR["propr"] == "PROPR listesi (yalnız ekler)"
        readme, env = src("README.md"), src(".env.example")
        assert "### ✅ PROPR listesi — yalnız büyür" in readme and "PROPR_LIVE=1" in env and "PROPR_SYNC_SEC=3600" in env
        assert "elle tutulur ve eskir" not in readme and "canlı senkron" not in readme
        # /tani satırı: gömülü → okundu → hata → elle ek
        line = await propr.diag_line(cfg)
        assert line.startswith("propr listesi: gömülü (12.10 birleşimi) · 198 enstrüman (114 ana dex kripto · 84 HIP-3)"), line
        assert "henüz çekilmedi" in line
        await propr.refresh(None, _fetcher([(200, _payload(LIVE_1210 + ["NEWC"]))]))
        line = await propr.diag_line(cfg)
        assert line.startswith("propr listesi: birleşik (yalnız büyür) · 199 enstrüman"), line
        assert "propr şu an 143 listeliyor — kara listeye aldıkları da tutulur" in line and "son çekim ✓" in line
        assert "son ekleme 0 sn önce: NEWC" in line, line
        await propr.refresh(None, _fetcher([(503, None)]))
        line = await propr.diag_line(cfg)
        assert "⚠ son çekim başarısız" in line and "HTTP 503" in line and "son başarılı" in line, line
        cfg.propr_symbols = "ZZZZ,BTC"
        line = await propr.diag_line(cfg)
        assert "elle eklenen: ZZZZ" in line and "elle eklenen ama zaten listede: BTC" in line, line
        cfg.propr_symbols = ""
        txt = "\n".join(await diag._subsystems(cfg))
        assert "  propr listesi: birleşik (yalnız büyür) · 199 enstrüman" in txt
        propr.set_live([])
        print("✅ kablolama) bildirim türü yok, ayarlar, spawn + açılış yüklemesi, sağlık, TASK_TR, README,"
              " .env; /tani satırı gömülü/birleşik/hata/elle ek durumlarını yazar")
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
