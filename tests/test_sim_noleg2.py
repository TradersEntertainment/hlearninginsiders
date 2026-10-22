"""🧪 Sim: "ters bacak olmasaydı" karşı olgusal bakiye.

Kullanıcı (22.10): "bizim sim liq yaklaşınca poz açan ters bacak girmese daha mı çok kazanırdı".
Kararlar: ön bacakların getirileri (kapanış öncesi bakiyeye oranla) bileşik uygulanır, ters bacaklar atlanır
(yaklaşık); ters bacak dilim tutarken "bakiye bağlı" atlanan sinyaller ayrıca sayılır, sonuçları bilinmediği için
hesaba girmez. Sayfa /sim ve Telegram /sim aynı veriyi yazar.

Pinlenenler: sayı; dilim engeli sayımı yalnız ters bacak açıkken ve yalnız "bakiye bağlı" nedeniyle; ters bacak yoksa
satır yok; ölçülü dil.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from app.radar import sim  # noqa: E402
from app.telegram import format as fmt  # noqa: E402


def test_without_leg2():
    closed = [
        {"id": 1, "leg": 1, "pnl_usd": 1000, "entry_ts": 100, "exit_ts": 200},     # 10000 → 11000 (+%10)
        {"id": 2, "leg": 2, "pnl_usd": -550, "entry_ts": 200, "exit_ts": 300},     # 11000 → 10450
        {"id": 3, "leg": 1, "pnl_usd": 1045, "entry_ts": 250, "exit_ts": 400},     # açılışta bakiye 11000 → 11495
    ]
    skipped = [{"skip_reason": "bakiye bağlı (3/3 dilim dolu)", "created_ts": 250},   # ters bacak açıkken
               {"skip_reason": "bakiye bağlı (3/3 dilim dolu)", "created_ts": 350},   # ters bacak kapandıktan sonra
               {"skip_reason": "hedef yok", "created_ts": 260}]
    r = sim.without_leg2(closed, 10_000, skipped)
    # #3 açılışında gerçek bakiye 11000, karşı olgusal da 11000 → +1045 aynen: 12045
    assert abs(r["actual"] - 11_495) < 1e-6 and abs(r["without"] - 12_045) < 1e-6, r
    assert r["n"] == 1 and r["wins"] == 0 and r["pnl"] == -550 and abs(r["diff"] + 550) < 1e-6
    # İnceleme (22.10): getiri AÇILIŞTAKİ bakiyeye göre — ön bacak açıkken kapanan ters bacak zararı onu şişirmez
    r2 = sim.without_leg2([{"id": 1, "leg": 1, "pnl_usd": 990, "entry_ts": 100, "exit_ts": 300},
                           {"id": 2, "leg": 2, "pnl_usd": -1680, "entry_ts": 150, "exit_ts": 200}], 10_000)
    assert abs(r2["without"] - 10_990) < 1e-6, r2
    # aynı coinde ters bacak açıkken atlanan sinyal de sayılır; pencere kayda geçiş anından (created_ts)
    r3 = sim.without_leg2([], 10_000, [{"skip_reason": "ters bacak açık", "created_ts": 500},
                                       {"skip_reason": "bakiye bağlı", "created_ts": 450}],
                          opens=[{"leg": 2, "entry_ts": 400, "created_ts": 460}])
    assert r3["blocked"] == 1, r3
    assert r["blocked"] == 1, r
    # açık ters bacak da dilim tutar
    r = sim.without_leg2(closed[:1], 10_000, [{"skip_reason": "bakiye bağlı", "created_ts": 999}],
                         opens=[{"leg": 2, "entry_ts": 900}])
    assert r["blocked"] == 1 and r["n"] == 0
    # özet satırı
    s = {"acc": {"balance": 11495, "start_balance": 10000, "run": 1}, "equity": 11495, "opens": [], "closed": [],
         "stats": {"n": 3, "wins": 2, "losses": 1, "hit_rate": 66.7},
         "noleg2": sim.without_leg2(closed, 10_000, skipped)}
    txt = fmt.sim_summary(s)
    assert "ters bacak: 1 işlem · 0✓" in txt and "olmasaydı bakiye ≈$12,045 (gerçek $11,495" in txt, txt
    assert "katkısı −$550" in txt or "katkısı -$550" in txt, txt
    assert "en az 1 sinyal atlandı, sonucu bilinmiyor" in txt
    s["noleg2"] = {"n": 0}
    assert "ters bacak:" not in fmt.sim_summary(s)
    print("✅ sim) ters bacak olmasaydı: bileşik karşı olgusal bakiye; dilim engeli ayrı ve sonucu 'bilinmiyor'")
