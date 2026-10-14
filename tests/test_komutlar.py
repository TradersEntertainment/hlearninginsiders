"""📖 /komutlar — tüm komutlar ve işlevleri.

Kullanıcı (14.10): "/komutlar yazarsam mümkün olan tüm komutlar ve işlevlerini göreyim".

Pinlenenler:
  • bot.py ve admin.py'deki HER komut adı (eş anlamlılar dahil) listede geçer — yeni komut eklenip listeye yazılmazsa
    bu test düşer
  • sahip her şeyi görür (🔒 = yalnız sahip); botun kanal / gruplarında yalnız orada çalışanlar listelenir
  • /komutlar · /komut · /commands: sahip DM'i, kendi grubunda üye, kanal gönderisi; yabancı grup sessiz
  • Telegram sınırı: parçalar ≤ 4096, her parça kendi içinde HTML'i kapalı (satır sınırından bölünür)
"""
import asyncio
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", "/tmp/hlr-test-komutlar.db")
from app import db as dbm  # noqa: E402
from app.config import Config  # noqa: E402
from app.telegram import format as fmt  # noqa: E402


def _names() -> set[str]:
    names = set()
    for f in ("app/telegram/bot.py", "app/telegram/admin.py"):
        s = open(os.path.join(ROOT, f), encoding="utf-8").read()
        for m in re.finditer(r'cmd in \(([^)]*)\)', s):
            names.update(re.findall(r'"([^"]+)"', m.group(1)))
        names.update(re.findall(r'cmd == "([^"]+)"', s))
        names.update(n + "N" for n in re.findall(r'cmd\.startswith\("([^"]+)"\)', s))
    return names


def test_every_command_listed():
    full = fmt.commands_text(owner=True)
    names = _names()
    assert len(names) > 60, names
    missing = sorted(n for n in names if not re.search(r"/" + re.escape(n) + r"(?![\w_])", full))
    assert not missing, f"/komutlar'da yok: {missing}"
    for extra in ("/5dk", "/15dk", "/hype", "/xpltwap", "/TICKERtwap"):     # komut adı kalıpla çözülenler
        assert extra in full
    print(f"✅ eksiksiz) bot.py + admin.py'deki {len(names)} komut adının hepsi /komutlar'da")


def test_owner_vs_group_view():
    full, grp = fmt.commands_text(owner=True), fmt.commands_text(owner=False)
    assert full.startswith("📖 <b>Tüm komutlar</b>") and "🔒 /tani" in full and "🔒 /pro_ver" in full
    for c in ("/tani", "/scan", "/pro_ver", "/watch", "/alarm", "/twap durum", "/refresh", "/duyuru"):
        assert c not in grp, c
    for c in ("/twap ", "/twapf", "/xpltwap", "/takip_N", "/birak_N", "/hesaplar", "/balina", "/seans", "/strat",
              "/acilis", "/5dk", "/sim", "/id", "/komutlar", "/duvartakipler", "/twaptakipler"):
        assert c in grp, c
    assert "🔒" not in grp
    from app.telegram.bot import TelegramBot
    for text in (full, grp):
        parts = TelegramBot._split(text)
        assert all(len(p) <= 4096 for p in parts)
        for p in parts:
            assert p.count("<b>") == p.count("</b>") and p.count("<i>") == p.count("</i>"), p[:200]
    print("✅ görünüm) sahip her şeyi (🔒 işaretli) görür; grupta yalnız orada çalışanlar; parçalar Telegram sınırında")


def test_routing():
    async def run():
        await dbm.init_db(os.path.join(tempfile.mkdtemp(), "k.db"))
        cfg = Config()
        cfg.telegram_chat_id, cfg.crypto_chat_id = "111", "-100"
        cfg.twap_mirror_chat_id = cfg.hidden_twap_chat_id = ""
        from app.telegram.bot import TelegramBot
        bot = TelegramBot(cfg, None, None, {})
        sent = []

        async def fake_send(text, chat_id=None, reply_markup=None):
            sent.append((str(chat_id), text))
            return True
        bot.send = fake_send

        async def msg(text, chat, uid, ctype="supergroup"):
            await bot._handle_update({"message": {"chat": {"id": chat, "type": ctype}, "from": {"id": uid},
                                                  "text": text}})
        await msg("/komutlar", 111, 111, "private")
        assert sent[-1][0] == "111" and "🔒 /tani" in sent[-1][1]
        await msg("/komut@HLRadarBot", -100, 42)
        assert sent[-1][0] == "-100" and "Bu grupta çalışan komutlar" in sent[-1][1] and "/tani" not in sent[-1][1]
        await msg("/commands", -100, 111)                            # sahip grupta: tam liste
        assert "🔒 /tani" in sent[-1][1]
        await bot._handle_update({"channel_post": {"chat": {"id": -100, "type": "channel"}, "text": "/komutlar"}})
        assert sent[-1][0] == "-100" and "Bu grupta çalışan komutlar" in sent[-1][1]
        n = len(sent)
        await msg("/komutlar", -999, 42)
        assert len(sent) == n, "yabancı grup sessiz"
        await msg("/help", 111, 111, "private")
        assert "/komutlar" in sent[-1][1]
    asyncio.run(run())
    print("✅ yönlendirme) /komutlar · /komut · /commands: DM, kendi grubu (üye / sahip), kanal gönderisi; yabancı grup sessiz")


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
