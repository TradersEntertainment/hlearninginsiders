"""Zincir simülasyonu — saf: defter yürüyüşü, tetiklenen pozisyonlar, tükenme, metin.

Pinlenenler:
  • yürüyüş liq fiyatından başlar, seviye kısmi yenince o fiyatta durur, defter
    YERİNDE eksilir (ikinci adım aynı seviyeyi yeniden yiyemez)
  • yalnız aynı yönlü, ulaşılan fiyata kadar liq'i olan pozisyonlar tetiklenir
  • görünen defter bitince exhausted + yerleşmeyen $; defter liq'e uzanmıyorsa no_book
  • down yönü aynasal; describe satırı hedef, ara liq, toplam ve şimdiden % yazar
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.radar import cascade as cz


def lv(*pairs):
    return [{"px": p, "sz": s} for p, s in pairs]


ASKS = [(89.95, 50_000), (90.20, 40_000), (90.50, 60_000), (91.00, 80_000), (92.00, 100_000)]
TRIG = {"side": "short", "liq_px": 89.93, "notional": 13_300_000, "address": "0xtrig"}
POOL = [{"side": "short", "liq_px": 90.30, "notional": 2_000_000, "address": "0xa"},
        {"side": "short", "liq_px": 90.45, "notional": 2_100_000, "address": "0xb"},
        {"side": "short", "liq_px": 92.50, "notional": 5_000_000, "address": "0xc"},   # dışarıda
        {"side": "long", "liq_px": 88.00, "notional": 9_000_000, "address": "0xd"},    # ters yön
        {"side": "short", "liq_px": 90.10, "notional": 1_000_000, "address": "0xtrig"}]  # kendisi


def test_walk():
    levels = lv(*ASKS)
    w = cz.walk(levels, 89.93, 13_300_000, cz.UP)
    assert abs(w["reached_px"] - 90.50) < 1e-9 and not w["exhausted"] and abs(w["spent"] - 13_300_000) < 1
    assert abs(levels[2]["sz"] * 90.50 - (5_430_000 - (13_300_000 - 4_497_500 - 3_608_000))) < 1, "seviye yerinde eksilir"
    w2 = cz.walk(levels, 90.50, 4_100_000, cz.UP)
    assert abs(w2["reached_px"] - 91.00) < 1e-9, "kalan 90.50 payı yenir, 91.00'a taşar"
    short = lv((89.95, 50_000), (90.20, 40_000))
    w3 = cz.walk(short, 89.93, 13_300_000, cz.UP)
    assert w3["exhausted"] and abs(w3["pending"] - (13_300_000 - 8_105_500)) < 1 and w3["reached_px"] == 90.20
    w4 = cz.walk(lv((87.9, 30_000), (87.5, 40_000), (88.5, 10)), 88.0, 5_000_000, cz.DOWN)
    assert w4["reached_px"] == 87.5 and not w4["exhausted"], "down: bid'ler azalan, start üstü yok sayılır"
    assert cz.walk(lv((91.0, 10)), 89.0, 1, cz.DOWN)["n_levels"] == 0
    print("✅ yürüyüş) liq'ten başlar, kısmi seviyede durur, yerinde eksilir; tükenme; down aynasal")


def test_simulate_chain():
    c = cz.simulate([], lv(*ASKS), TRIG, POOL, 89.13)
    assert c["direction"] == cz.UP and len(c["steps"]) == 2, c["steps"]
    assert abs(c["steps"][0]["to"] - 90.50) < 1e-9 and c["steps"][0]["n_new"] == 2
    assert abs(c["steps"][0]["usd_new"] - 4_100_000) < 1
    assert abs(c["end_px"] - 91.00) < 1e-9 and abs(c["total_usd"] - 17_400_000) < 1 and c["n_pos"] == 3
    assert not c["exhausted"] and c["pending_usd"] == 0 and abs(c["move_pct"] - (91.0 / 89.13 - 1) * 100) < 1e-9
    assert c["book_usd"] > 29_000_000
    # tükenme: kısa defter → pending, exhausted
    c2 = cz.simulate([], lv((89.95, 50_000), (90.20, 40_000)), TRIG, POOL, 89.13)
    assert c2["exhausted"] and abs(c2["pending_usd"] - (13_300_000 - 8_105_500)) < 1 and c2["end_px"] == 90.20
    assert c2["steps"][0]["n_new"] == 0, "90.30 ulaşılan 90.20'nin ötesinde — tetiklenmez"
    # defter liq'e uzanmıyor
    c3 = cz.simulate([], lv((89.50, 10)), TRIG, POOL, 89.13)
    assert c3["no_book"] and c3["steps"] == [] and c3["end_px"] == 89.93
    # down aynası: long patlar → satış bid'leri yer → alttaki long'lar
    trig = {"side": "long", "liq_px": 88.0, "notional": 5_000_000, "address": "0xl"}
    pool = [{"side": "long", "liq_px": 87.7, "notional": 1_000_000, "address": "0xm"},
            {"side": "short", "liq_px": 87.6, "notional": 9e6, "address": "0xs"}]
    c4 = cz.simulate(lv((87.9, 30_000), (87.5, 40_000), (86.0, 50_000)), [], trig, pool, 88.4)
    assert c4["direction"] == cz.DOWN and c4["steps"][0]["to"] == 87.5 and c4["steps"][0]["n_new"] == 1
    assert c4["end_px"] == 87.5 and abs(c4["total_usd"] - 6_000_000) < 1 and c4["move_pct"] < 0
    print("✅ zincir) 13.3M → 90.50, arada 2 short ($4.1M) → 91.00, toplam $17.4M; tükenme; defter yok; down")


def test_describe():
    c = cz.simulate([], lv(*ASKS), TRIG, POOL, 89.13)
    t = "\n".join(cz.describe(c, 89.13))
    assert "💣 <b>Zincir</b>" in t and "$13.3M short 89.93'te patlarsa zorunlu alış → <b>90.50</b>" in t, t
    assert "arada 2 short daha ($4.1M) → <b>91.00</b>" in t and "toplam <b>$17.4M</b>" in t
    assert "~<b>91.00</b>'a gidebilir (şimdiden +2.1%)" in t and "bitiyor" not in t
    c2 = cz.simulate([], lv((89.95, 50_000), (90.20, 40_000)), TRIG, POOL, 89.13)
    t2 = "\n".join(cz.describe(c2, 89.13))
    assert "arada başka liq yok" in t2 and "görünen defter 90.20'da bitiyor" in t2 and "yerleşmedi" in t2, t2
    c3 = cz.simulate([], lv((89.50, 10)), TRIG, POOL, 89.13)
    assert "uzanmıyor" in cz.describe(c3, 89.13)[0]
    assert cz.describe(None) == []
    print("✅ metin) hedef, ara liq, toplam, şimdiden %, tükenme notu; defter yoksa dürüst")


test_walk()
test_simulate_chain()
test_describe()
print("\n✅ ZİNCİR TESTLERİ GEÇTİ")
