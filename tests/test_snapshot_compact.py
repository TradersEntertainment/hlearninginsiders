"""🖼 /pump TEK mesaj — foto + sığdırılmış altyazı (≤ 1024 görünür), ⭐ band altyazısı.

Pinlenenler:
  • PUMP fikstürü (6 band + 3 tek + zincir + bağlam): tam metin 1024'ü aşar, compact sığar;
    başlık, ⭐ ana band, zorunlu bağlam (havuz/kapsama), DISCLAIMER ve altbilgi asla düşmez
  • düşme sırası: zincir → en yakın band → 2. tek → bağlam ekleri → duvar bandı → tekler/etki
  • compact bantlar: ⭐ (en yakın anlamlı) + DUVAR (en büyük) + kalanların en yakını = 3 band;
    gösterim MESAFE sıralı (⭐ ille de ilk satır değil); tekler en çok 2 (/takip ile)
  • foto altyazısı (metin ayrı giderse): ⭐ band; band yoksa en yakın tek
  • bot._cmd_coin_liq: sığıyorsa tek sendPhoto (compact); sığmazsa metin + foto (band altyazısı)
  • public.send_snapshot: caption/fallback parametreleri
"""
import asyncio
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-compact.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.telegram import format as fmt  # noqa: E402
from app.telegram.format import visible_len  # noqa: E402

A = "0x" + "a" * 40


def band(side, lo, hi, dlo, dhi, total, n):
    return {"side": side, "px_lo": lo, "px_hi": hi, "px": hi if side == "long" else lo,
            "dist_lo": dlo, "dist_hi": dhi, "total": float(total), "n": n}


def pump_snapshot():
    t = dbm.now()
    cl = [band("short", 0.0046, 0.0047, 7.5, 9.9, 265_000, 4), band("long", 0.0034, 0.0036, 15.2, 19.9, 3_500_000, 40),
          band("long", 0.0030, 0.0034, 20.1, 29.9, 11_000_000, 78), band("short", 0.0052, 0.0055, 22.1, 29.5, 1_100_000, 8),
          band("long", 0.0022, 0.0030, 30.3, 49.6, 19_000_000, 80), band("short", 0.0056, 0.0062, 31.4, 44.8, 1_200_000, 6)]
    rows = [{"address": "0x" + c * 40, "side": "long", "notional": n, "liq_px": lp, "dist": d, "leverage": lv, "ts": t - 2 * 86400}
            for c, n, lp, d, lv in (("e", 986_000.0, 0.0036, 16.37, 6), ("9", 1_200_000.0, 0.0035, 18.92, 10), ("b", 628_000.0, 0.0034, 21.25, 10))]
    casc = {"direction": "down", "trigger_usd": 19_000_000.0, "start_px": 0.0030, "end_px": 0.0023, "move_pct": -46.2,
            "total_usd": 19_000_000.0, "n_pos": 1, "steps": [{"from": 0.0030, "to": 0.0023, "spent": 19e6, "n_new": 0, "usd_new": 0}],
            "exhausted": True, "pending_usd": 17_300_000.0, "book_usd": 1_600_000.0, "no_book": False, "coarse": True}
    return {"coin": "PUMP", "kind": "crypto", "mark": 0.00428, "age": 3, "rows": rows, "n_all": 635, "n_big": 12,
            "min_usd": 500_000, "png": b"\x89PNG\r\n\x1a\n" + b"0" * 6000, "cascade": casc,
            "coverage": {"pct_long": 37.0, "pct_short": 46.0, "over": False, "census": {"text": "sayım %63 (toplu)"}},
            "all_far": False, "n_far": 328, "far_pct": 50.0, "clusters": cl, "main_band": cl[1], "n_dust": 288, "dust": 1950.0}


def test_compact_fit_and_drop_order():
    s = pump_snapshot()
    full = fmt.crypto_liq_snapshot(s, offers=[74, 75, 76])
    assert visible_len(full) > 1024 and full.count("bandı") == 6 and "/takip_76" in full and "görünen defter" in full
    cap = fmt.crypto_liq_snapshot(s, offers=[74, 75, 76], compact=True)
    assert visible_len(cap) <= 1024, visible_len(cap)
    lines = cap.split("\n")
    # ⭐ (en yakın anlamlı $3.5M) + duvar (en büyük $19.0M) + kalanların en yakını ($265K),
    # gösterim mesafe sıralı; tekler en çok 2; zincir tek satır; bağlam; disclaimer
    assert lines[0].startswith("🎯 <b>PUMP</b>"), lines[0]
    assert "SHORT bandı <b>$265K</b>" in lines[1] and "⭐" not in lines[1], lines[1]
    assert "LONG bandı <b>$3.5M</b>" in lines[2] and "⭐" in lines[2], lines[2]
    assert "LONG bandı <b>$19.0M</b>" in lines[3] and "⭐" not in lines[3], lines[3]
    assert cap.count("bandı") == 3 and "/takip_74" in cap and "/takip_75" in cap and "/takip_76" not in cap
    assert "💣 <b>Zincir</b>: $19.0M long 0.0030'te patlarsa → <b>0.0023</b> · toplam <b>$19.0M</b> · -46.2% · kaba defter · defter bitti" in cap, cap
    assert "havuzda 635 açık pozisyon, 12'ü ≥ $500K · HL'nin tamamı değil · kapsama long %37 · short %46 (havuz / HL OI) · sayım %63 (toplu)" in cap
    assert "2g önce" in cap and "288 toz" in cap and cap.rstrip().endswith("yatırım tavsiyesi değildir.</i>")
    assert "görünen defter" not in cap and "<" not in fmt.strip_tags(cap)
    # düşme sırası: limit küçüldükçe zincir → 3. band → 2. tek → bağlam ekleri → 2. band
    seen = []
    prev = None
    for lim in range(visible_len(cap), 250, -5):
        c = fmt.crypto_liq_snapshot(s, offers=[74, 75, 76], compact=True, limit=lim)
        assert visible_len(c) <= lim or "bandı" not in c.split("\n")[2:], (lim, c)
        key = (("💣" in c), ("$265K" in c), ("/takip_75" in c), ("288 toz" in c), ("LONG bandı <b>$19.0M</b>" in c),
               ("/takip_74" in c), ("SATIŞ" in c))
        if key != prev:
            seen.append(key)
            prev = key
    order = [next(i for i, (a, b) in enumerate(zip(seen[k], seen[k + 1])) if a != b) for k in range(len(seen) - 1)]
    assert order == [0, 1, 2, 3, 4, 6, 5], (order, seen)     # eşit öncelikte (tek #1, etki) sonraki önce düşer
    last = fmt.crypto_liq_snapshot(s, offers=[74, 75, 76], compact=True, limit=250)
    assert "⭐" in last and "havuzda 635" in last and "yatırım tavsiyesi" in last and lines[0] in last, "asla düşmeyenler"
    # altbilgi (herkese açık bot) hesaba katılır ve düşmez
    capf = fmt.crypto_liq_snapshot(s, compact=True, extra="<i>bugün <b>2/3</b> sorgu kaldı · /pro</i>")
    assert visible_len(capf) <= 1024 and capf.endswith("/pro</i>")
    # foto altyazısı: ⭐ band; band yoksa tek; hiçbir şey yoksa genel
    assert fmt.crypto_liq_photo_caption(s) == "📈 <b>PUMP</b> · LONG bandı $3.5M · 0.0034–0.0036 · %15.2 altta"
    s2 = dict(s, clusters=[], main_band=None)
    assert fmt.crypto_liq_photo_caption(s2) == "📈 <b>PUMP</b> · liq 0.0036 · %16.37 kaldı"
    assert fmt.crypto_liq_photo_caption({"coin": "X", "rows": []}) == "📈 <b>X</b> · likidasyon grafiği"
    # boş/fiyatsız durumlar compact'ta da dürüst
    assert "fiyat alınamadı" in fmt.crypto_liq_snapshot({"coin": "PUMP", "mark": None}, compact=True)
    assert "açık pozisyon yok" in fmt.crypto_liq_snapshot({"coin": "PUMP", "mark": 1.0, "rows": []}, compact=True, extra="x")
    print("✅ compact) PUMP: tam metin > 1024, altyazı ≤ 1024; ⭐ + duvar + en yakın band (mesafe sıralı) + 2 tek + kısa zincir; düşme sırası; foto altyazısı ⭐ band")


def test_bot_single_message():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "compact.db"))
        from app.radar import cryptoliq
        from app.telegram import bot as botmod
        from app.telegram.bot import TelegramBot
        from app.hl import universe
        cfg = Config()
        cfg.telegram_chat_id = "111"
        bot = TelegramBot(cfg, None, None, {})
        sent, photos = [], []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append((chat_id, text))
            return True

        async def fake_photo(png, caption="", chat_id=None, reply_markup=None):
            photos.append((chat_id, caption))
            return True
        bot.send, bot.send_photo = fake_send, fake_photo
        s = pump_snapshot()

        async def fake_snapshot(cfg_, client, coin, kind="crypto", **kw):
            return s

        async def fake_resolve(cmd):
            return {"coin": "PUMP", "symbol": "PUMP", "kind": "crypto"} if cmd == "pump" else None
        orig_snap, orig_res = cryptoliq.snapshot, universe.resolve_coin
        cryptoliq.snapshot, universe.resolve_coin = fake_snapshot, fake_resolve
        try:
            assert await bot._cmd_coin_liq("pump", "111") is True
            assert sent == [] and len(photos) == 1 and photos[0][0] == "111", (sent, photos)
            cap = photos[0][1]
            assert visible_len(cap) <= 1024 and "⭐" in cap and "yatırım tavsiyesi" in cap, cap
            # sığmazsa: tam metin + foto (⭐ band altyazısı)
            orig_fit = botmod._caption_fit
            botmod._caption_fit = lambda c, **kw: (c, False)
            try:
                await bot._cmd_coin_liq("pump", "111")
            finally:
                botmod._caption_fit = orig_fit
            assert len(sent) == 1 and visible_len(sent[0][1]) > 1024 and len(photos) == 2
            assert photos[1][1] == "📈 <b>PUMP</b> · LONG bandı $3.5M · 0.0034–0.0036 · %15.2 altta"
            assert await bot._cmd_coin_liq("tani", "111") is False
        finally:
            cryptoliq.snapshot, universe.resolve_coin = orig_snap, orig_res
        # herkese açık gönderici: caption sığarsa tek foto; fallback altyazı
        from app.telegram import public
        photos.clear()
        sent.clear()
        await public.send_snapshot(bot, "555", "tam metin", b"png", "PUMP", caption="kısa", fallback="⭐ band")
        assert photos == [("555", "kısa")] and sent == []
        botmod._caption_fit = lambda c, **kw: (c, False)
        try:
            await public.send_snapshot(bot, "555", "tam metin", b"png", "PUMP", caption="kısa", fallback="⭐ band")
        finally:
            botmod._caption_fit = orig_fit
        assert sent == [("555", "tam metin")] and photos[-1] == ("555", "⭐ band")
        print("✅ bot) /pump tek foto + compact altyazı; sığmazsa metin + ⭐ band altyazılı foto; public caption/fallback")
    asyncio.run(run())
