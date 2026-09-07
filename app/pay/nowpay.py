"""NOWPayments — kripto ağ geçidi: fatura (hosted sayfa) → IPN → tahsilat.

`POST /v1/invoice` (x-api-key) → `invoice_url`; kullanıcı sayfada coin/ağ seçer.
NOWPayments her durum değişiminde `ipn_callback_url`'e POST atar; gövde JSON'u
anahtarları (özyinelemeli) sıralı + kompakt serileştirilip IPN gizli anahtarıyla
HMAC-SHA512'lenir, `x-nowpayments-sig` başlığıyla karşılaştırılır — uyuşmazsa
kredi YOK, ilk kötü örnek kv'de (sahip /tani'de görür). `finished`/`confirmed`
→ `core.credit` (payment_id ile idempotent); `failed/expired/refunded` → bekleyen
kayıt kapanır; ara durumlar payment_id'yi saklar ki kayıp IPN'de yoklama yedeği
(`poll_pending`, 10 dk sonra `GET /v1/payment/<id>`) tahsilatı yakalasın.
Anahtar yoksa menüde görünmez."""
import hashlib
import hmac
import json
import logging

import aiohttp

from ..db import db, kv_get, kv_set, now
from . import core

log = logging.getLogger("pay.nowpay")

API = "https://api.nowpayments.io/v1"
KV_STATE = "nowpay_state"
FINAL_OK = ("finished", "confirmed")
FINAL_BAD = ("failed", "expired", "refunded")
POLL_AFTER_SEC = 600


def _sorted(o):
    if isinstance(o, dict):
        return {k: _sorted(o[k]) for k in sorted(o)}
    if isinstance(o, list):
        return [_sorted(x) for x in o]
    return o


def canonical(obj) -> str:
    return json.dumps(_sorted(obj), separators=(",", ":"), ensure_ascii=False)


def signature(secret: str, obj) -> str:
    return hmac.new(secret.encode("utf-8"), canonical(obj).encode("utf-8"), hashlib.sha512).hexdigest()


def verify(secret: str, raw: bytes, sig: str) -> tuple[bool, dict | None]:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        return False, None
    if not isinstance(obj, dict) or not sig or not secret:
        return False, obj if isinstance(obj, dict) else None
    return hmac.compare_digest(signature(secret, obj), str(sig).strip().lower()), obj


def order_pid(order_id) -> int | None:
    s = str(order_id or "")
    try:
        return int(s.split(":")[1]) if s.startswith("pay:") else None
    except (IndexError, ValueError):
        return None


def _raw(pay: dict) -> dict:
    try:
        d = json.loads(pay.get("raw") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


async def _set_raw(pid: int, d: dict) -> None:
    async with db() as conn:
        await conn.execute("UPDATE payments SET raw=? WHERE id=?", (core.dumps(d), int(pid)))


async def _expire(pid: int) -> None:
    async with db() as conn:
        await conn.execute("UPDATE payments SET status='expired' WHERE id=? AND status='pending'", (int(pid),))


async def create_invoice(cfg, session, u: dict, plan: str) -> tuple[dict | None, str]:
    """Bekleyen kayıt + NOWPayments faturası. Dönüş (kayıt, invoice_url) ya da (None, neden)."""
    key = getattr(cfg, "nowpayments_api_key", "") or ""
    if not key or session is None:
        return None, "NOWPayments kapalı"
    pay = await core.create_pending(int(u["id"]), "nowpay", plan, cfg)
    if not pay:
        return None, "paket yok"
    payload = {"price_amount": float(pay["amount_usd"]), "price_currency": "usd",
               "order_id": f"pay:{pay['id']}",
               "order_description": f"HL Insider Radar Pro {int(pay['months'])} ay"}
    base = (getattr(cfg, "public_base_url", "") or "").rstrip("/")
    if base:
        payload["ipn_callback_url"] = f"{base}/pay/ipn"
    status, data = 0, {}
    try:
        async with session.post(f"{API}/invoice", json=payload, headers={"x-api-key": key},
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            status = r.status
            data = await r.json(content_type=None)
    except Exception as e:
        data = {"message": f"{type(e).__name__}: {e}"}
    url = data.get("invoice_url") if isinstance(data, dict) else None
    if status not in (200, 201) or not url:
        await _expire(pay["id"])
        why = str((data or {}).get("message") or status) if isinstance(data, dict) else str(status)
        log.warning("NOWPayments fatura başarısız: %s", why[:200])
        return None, why
    await _set_raw(pay["id"], {"invoice_id": data.get("id"), "invoice_url": url})
    return await core.get(pay["id"]), url


async def handle_ipn(cfg, bot, raw: bytes, sig: str) -> tuple[int, str]:
    """IPN gövdesi → (HTTP durumu, not). 200 dışı dönüşte NOWPayments yeniden dener."""
    secret = getattr(cfg, "nowpayments_ipn_secret", "") or ""
    st = await kv_get(KV_STATE) or {}
    if not secret:
        return 503, "IPN gizli anahtarı yok"
    ok, data = verify(secret, raw, sig)
    if not ok:
        st["bad_sig"] = st.get("bad_sig", 0) + 1
        st["bad_sample"] = raw[:300].decode("utf-8", "replace")
        st["ts"] = now()
        await kv_set(KV_STATE, st)
        return 403, "imza uyuşmadı"
    st["ipn"] = st.get("ipn", 0) + 1
    st.setdefault("sample", core.dumps(data)[:600])
    status = str(data.get("payment_status") or "").lower()
    st["last_status"] = status
    st["ts"] = now()
    pid = order_pid(data.get("order_id"))
    pay = await core.get(pid) if pid else None
    if not pay or pay.get("method") != "nowpay":
        await kv_set(KV_STATE, st)
        return 200, "kayıt yok"
    ext = str(data.get("payment_id") or "") or f"inv:{pid}"
    if status in FINAL_OK:
        res = await core.credit(pid, ext, amount_raw=data.get("actually_paid") or data.get("price_amount"),
                                raw=core.dumps(data), cfg=cfg)
        if res:
            st["paid"] = st.get("paid", 0) + 1
            await core.notify_paid(bot, cfg, res["payment"], res["until"])
        note = "tahsilat" if res else "zaten işlenmiş"
    elif status in FINAL_BAD:
        await _expire(pid)
        note = "kapandı"
    else:
        if pay.get("status") == "pending":
            await _set_raw(pid, {**_raw(pay), "payment_id": data.get("payment_id"), "status": status})
        note = status or "ara durum"
    await kv_set(KV_STATE, st)
    return 200, note


async def poll_pending(cfg, session, bot=None) -> dict:
    """Kayıp IPN yedeği: payment_id'si bilinen, 10 dk'dan eski bekleyenlerin durumu."""
    key = getattr(cfg, "nowpayments_api_key", "") or ""
    out = {"checked": 0, "paid": 0}
    if not key or session is None:
        return out
    for pay in await core.pending("nowpay"):
        pid_np = _raw(pay).get("payment_id")
        if not pid_np or now() - int(pay.get("created_ts") or 0) < POLL_AFTER_SEC:
            continue
        try:
            async with session.get(f"{API}/payment/{pid_np}", headers={"x-api-key": key},
                                   timeout=aiohttp.ClientTimeout(total=20)) as r:
                status_http = r.status
                data = await r.json(content_type=None)
        except Exception:
            log.debug("NOWPayments yoklama", exc_info=True)
            continue
        out["checked"] += 1
        if status_http != 200 or not isinstance(data, dict):
            continue
        status = str(data.get("payment_status") or "").lower()
        if status in FINAL_OK:
            res = await core.credit(pay["id"], str(pid_np), amount_raw=data.get("actually_paid"),
                                    raw=core.dumps(data), cfg=cfg)
            if res:
                out["paid"] += 1
                await core.notify_paid(bot, cfg, res["payment"], res["until"])
        elif status in FINAL_BAD:
            await _expire(pay["id"])
    return out
