"""👤 Kullanıcı katmanı (satılabilir bot) — users.py.

Pinlenenler:
  • kayıt/upsert: yeni satır, son görülme, engel kalkar; katman = pro_until gelecekte
  • grant: şimdi ya da mevcut bitişten (ilerideyse) + gün; not kısaltılır
  • kota: ücretsiz günlük (TSİ günü, gün dönünce sıfır), Pro dakikalık kayan pencere;
    check sayaç ilerletmez, consume ilerletir
  • csv_set/valid_address/quiet_now/flood_ok saf yardımcılar
  • abonelik: subscribers(kind, coin) = Pro + engelsiz + tür + (coin filtresi yok|eşleşir)
  • stats sayaçları
"""
import asyncio
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-users.db")
from app import db as dbm
from app import users
from app.config import Config


async def _fresh():
    await dbm.init_db(os.path.join(tempfile.mkdtemp(), "u.db"))
    users.reset_memory()


def _cfg():
    cfg = Config()
    cfg.free_daily_queries = 3
    cfg.pro_query_per_min = 2
    return cfg


def test_pure_helpers():
    assert users.csv_set("a, b,,c") == {"a", "b", "c"} and users.csv_set(["x", " y "]) == {"x", "y"}
    assert users.csv_set("") == set() and users.csv_set(None) == set()
    a = "0x" + "AB" * 20
    assert users.valid_address(a) == a.lower() and users.valid_address(" " + a + " ") == a.lower()
    assert users.valid_address("0X" + "ab" * 20) is None and users.valid_address("0x123") is None
    now = dbm.now()
    assert users.is_pro({"pro_until": now + 10}) and not users.is_pro({"pro_until": now - 10}) and not users.is_pro({})
    assert users.tier({"pro_until": now + 10}) == "pro" and users.tier(None) == "free"
    t2 = int(datetime(2026, 9, 7, 2, 30, tzinfo=users.TR).timestamp())
    t12 = int(datetime(2026, 9, 7, 12, 30, tzinfo=users.TR).timestamp())
    q = {"quiet_start": 23, "quiet_end": 8}
    assert users.quiet_now(q, t2) and not users.quiet_now(q, t12)
    assert not users.quiet_now({"quiet_start": 8, "quiet_end": 8}, t2) and not users.quiet_now({}, t2)
    users.reset_memory()
    assert all(users.flood_ok(1, now) for _ in range(users.FLOOD_PER_MIN)) and not users.flood_ok(1, now)
    assert users.flood_ok(1, now + 61), "pencere kayınca açılır"
    assert users.tr_day(t2) == "2026-09-07"
    print("✅ saf) csv/adres/katman/sessiz saat/sel yardımcıları")


def test_register_grant_quota():
    async def run():
        await _fresh()
        cfg = _cfg()
        u = await users.upsert_from_update({"id": 555, "username": "ali", "first_name": "Ali", "language_code": "tr"}, "555")
        assert u["id"] == 555 and u["chat_id"] == "555" and u["created_ts"] and u["pro_until"] is None
        assert (await users.stats())["total"] == 1
        # kota: ücretsiz 3/gün; check ilerletmez, consume ilerletir
        assert users.check_query(u, cfg) == (True, 3, "")
        assert users.check_query(u, cfg) == (True, 3, "")
        assert await users.consume_query(u, cfg) == 2 and await users.consume_query(u, cfg) == 1
        assert await users.consume_query(u, cfg) == 0 and users.check_query(u, cfg) == (False, 0, "daily")
        r = await users.get(555)
        assert r["q_used"] == 3 and r["q_total"] == 3 and r["q_day"] == users.tr_day()
        # gün dönünce sıfır
        async with dbm.db() as c:
            await c.execute("UPDATE users SET q_day='2000-01-01' WHERE id=555")
        u = await users.get(555)
        assert users.check_query(u, cfg) == (True, 3, "") and await users.consume_query(u, cfg) == 2
        assert (await users.get(555))["q_used"] == 1 and (await users.get(555))["q_total"] == 4
        # grant: şimdi + 30 gün; ikinci grant bitişten devam eder
        now = dbm.now()
        until = await users.grant(555, 30, "test")
        assert abs(until - (now + 30 * 86400)) <= 2
        until2 = await users.grant(555, 10)
        assert until2 == until + 10 * 86400
        u = await users.get(555)
        assert users.is_pro(u) and "+30g test" in u["note"] and "+10g" in u["note"]
        # süresi dolmuş → şimdi'den başlar
        async with dbm.db() as c:
            await c.execute("UPDATE users SET pro_until=? WHERE id=555", (now - 86400,))
        until3 = await users.grant(555, 5)
        assert abs(until3 - (now + 5 * 86400)) <= 2
        # Pro: dakikada 2
        u = await users.get(555)
        assert users.check_query(u, cfg) == (True, None, "")
        assert await users.consume_query(u, cfg) is None and await users.consume_query(u, cfg) is None
        assert users.check_query(u, cfg) == (False, None, "minute")
        assert users.check_query(u, cfg, dbm.now() + 61)[0], "pencere kayınca açılır"
        # engel + kalkması
        assert await users.mark_blocked("555") == 1 and (await users.get(555))["blocked_ts"]
        assert await users.mark_blocked("555") == 0, "ikinci kez işaretlenmez"
        u = await users.upsert_from_update({"id": 555, "first_name": "Ali"}, "555")
        assert u["blocked_ts"] is None and u["username"] == ""
        # adres / sessiz
        await users.set_address(555, "0x" + "ab" * 20)
        await users.set_quiet(555, 23, 8)
        u = await users.get(555)
        assert u["hl_address"] == "0x" + "ab" * 20 and u["quiet_start"] == 23
        print("✅ kayıt/kota) upsert, günlük kota + gün dönümü, Pro dakika penceresi, grant zinciri, engel")
    asyncio.run(run())


def test_subscriptions_and_stats():
    async def run():
        await _fresh()
        now = dbm.now()
        for uid in (1, 2, 3, 4):
            await users.upsert_from_update({"id": uid, "first_name": f"u{uid}"}, str(uid))
        await users.grant(1, 30)
        await users.grant(2, 30)
        await users.grant(3, 30)
        await users.set_kinds(1, "cryptoliq, liqmap")
        await users.set_kinds(2, ["cryptoliq"])
        await users.set_coins(2, "HYPE,SOL")
        await users.set_kinds(3, {"cryptoliq"})
        await users.mark_blocked("3")
        await users.set_kinds(4, {"cryptoliq"})                    # ücretsiz → hedef değil
        assert await users.get_kinds(1) == {"cryptoliq", "liqmap"} and await users.get_coins(2) == {"HYPE", "SOL"}
        ids = lambda rows: [r["id"] for r in rows]  # noqa: E731
        assert ids(await users.subscribers("cryptoliq", "HYPE")) == [1, 2]
        assert ids(await users.subscribers("cryptoliq", "BTC")) == [1], "coin filtresi tutmayan 2 düşer; 3 engelli; 4 ücretsiz"
        assert ids(await users.subscribers("liqmap", "BTC")) == [1] and await users.subscribers("twap") == []
        assert ids(await users.pro_users()) == [1, 2] and ids(await users.all_active()) == [1, 2, 4]
        await users.set_coins(2, "")
        assert ids(await users.subscribers("cryptoliq", "BTC")) == [1, 2], "filtre boşalınca hepsi"
        st = await users.stats()
        assert st["total"] == 4 and st["pro"] == 3 and st["blocked"] == 1 and st["new_24h"] == 4 and st["active_7d"] == 4
        assert st["expiring_3d"] == 0 and st["q_today"] == 0
        async with dbm.db() as c:
            await c.execute("UPDATE users SET pro_until=? WHERE id=1", (now + 86400,))
        assert (await users.stats())["expiring_3d"] == 1
        print("✅ abonelik) subscribers tür+coin+Pro+engel süzgeci; pro_users/all_active; stats")
    asyncio.run(run())
