"""🌐 Cüzdan sayfası bağlantıları — Telegram'dan açılan /cuzdan/<adres> ve /takipte için imzalı bağlantı.

Kullanıcı (20.10): "bu kanala eklediklerim için websitesi yapar mısın … profillere basınca öyle bi websitesi çıksın".

Kararlar:
  • Panel DASHBOARD_TOKEN ister; jeton Telegram mesajına YAZILMAZ (kanal üyeleri görür, panelin tamamını açar). Bunun yerine
    her adres için HMAC imzası (jetondan türetilir, yalnız o cüzdanın sayfasını açar): /cuzdan/<adres>?s=<imza>.
  • Jeton yoksa panel zaten açık → imza da yok. Taban adres PUBLIC_BASE_URL ya da RAILWAY_PUBLIC_DOMAIN; ikisi de yoksa
    bağlantı boş döner (mesaj hypurrscan'e bağlanır).
"""
from __future__ import annotations

import hashlib
import hmac
import re

ADDR_RE = re.compile(r"^0x[0-9a-f]{40}$")


def _tok(cfg) -> str:
    return (getattr(cfg, "dashboard_token", "") or "").strip()


def sig(cfg, scope: str) -> str:
    """Kapsam ('0x…' adres ya da 'liste') için imza; jeton yoksa boş."""
    tok = _tok(cfg)
    if not tok:
        return ""
    return hmac.new(tok.encode(), f"cuzdan:{scope}".encode(), hashlib.sha256).hexdigest()[:24]


def check(cfg, scope: str, s: str | None) -> bool:
    """İmza doğru mu (jeton yoksa her zaman evet)."""
    want = sig(cfg, scope)
    # bayt karşılaştırması: ASCII dışı ?s= (ör. %C3%BC) str'de TypeError → 500 veriyordu (inceleme 20.10)
    return not want or (bool(s) and hmac.compare_digest(str(s).encode(), want.encode()))


def _base(cfg) -> str:
    return (getattr(cfg, "public_base_url", "") or "").rstrip("/")


def wallet_path(cfg, addr: str) -> str:
    addr = (addr or "").lower()
    s = sig(cfg, addr)
    return f"/cuzdan/{addr}" + (f"?s={s}" if s else "")


def wallet_url(cfg, addr: str) -> str:
    """Telegram'dan tıklanacak tam adres; taban adres yoksa ya da adres geçersizse boş."""
    base = _base(cfg)
    if not base or not ADDR_RE.match((addr or "").lower()):
        return ""
    return base + wallet_path(cfg, addr)


def list_path(cfg) -> str:
    s = sig(cfg, "liste")
    return "/takipte" + (f"?s={s}" if s else "")


def list_url(cfg) -> str:
    base = _base(cfg)
    return base + list_path(cfg) if base else ""
