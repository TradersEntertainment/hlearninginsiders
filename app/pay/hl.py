"""USDC ile ödeme — Hyperliquid içi "Send".

Kullanıcı HL hesabından botun adresine (`PAY_HL_ADDRESS`) tam plan tutarında USDC
gönderir; bot kendi adresinin defter hareketlerini (`userNonFundingLedgerUpdates`,
mevcut `HLClient.ledger_updates`) izler ve gelen transferi GÖNDEREN ADRES + tutarla
bekleyen ödemeyle eşler (kullanıcı adresini /adres ile ya da /pro akışında verir).
Tahmin/elle onay yok: eşleşmeyen transfer kv'de listelenir (sahip /tani'de görür,
/pro_ver ile açar). Mesaj şekli canlıda doğrulanmadı (sandbox ağsız): ayrıştırma
savunmacı, ilk ham kayıt örneği kv'ye — şekil tutmazsa eşleşme olmaz, örnek konuşur.
Kadans: bekleyen 'hl' ödeme varken 60 sn, yokken 600 sn (1 istek)."""
import asyncio
import logging

from ..db import kv_get, kv_set, now
from . import core

log = logging.getLogger("pay.hl")

TOL_USD = 0.05
KV_STATE = "paywatch_state"
IDLE_SEC = 600
LOOKBACK_SEC = 2 * 86400
IN_TYPES = ("internalTransfer", "spotTransfer", "send", "transfer")


def parse_transfers(raw, my_addr: str) -> list[dict]:
    """Ledger kayıtları → bize GELEN USDC transferleri: {hash, ts, from, usd, type}.
    Giden transfer (user = biz), USDC dışı token, köprü yatırımı (gönderen yok) atlanır."""
    me = (my_addr or "").lower()
    out: list[dict] = []
    for e in raw or []:
        try:
            d = e.get("delta") or {}
            typ = str(d.get("type") or "")
            if typ not in IN_TYPES:
                continue
            frm = str(d.get("user") or d.get("from") or "").lower()
            dest = str(d.get("destination") or d.get("to") or "").lower()
            if not frm or frm == me:
                continue
            if dest and me and dest != me:
                continue
            if typ == "spotTransfer":
                tok = str(d.get("token") or "USDC").upper().split(":")[0]
                if tok != "USDC":
                    continue
            usd = d.get("usdc")
            if usd is None:
                usd = d.get("amount")
            usd = float(usd or 0)
            if usd <= 0:
                continue
            ts = int(float(e.get("time") or 0))
            if ts > 10 ** 11:
                ts //= 1000
            h = str(e.get("hash") or "")
            out.append({"hash": h or f"{frm}:{ts}:{usd}", "ts": ts, "from": frm, "usd": usd, "type": typ})
        except (TypeError, ValueError, AttributeError):
            continue
    out.sort(key=lambda t: t["ts"])
    return out


async def match(transfers: list[dict], cfg, bot=None) -> dict:
    """Her gelen transfer: aynı gönderen adresli, fiyatı tutara sığan EN ESKİ bekleyen
    'hl' ödeme → tahsilat. Aynı hash ikinci kez → çift kredi yok."""
    out = {"seen": len(transfers), "matched": 0, "dup": 0, "unmatched": []}
    for t in transfers:
        if await core.already_credited("hl", t["hash"]):
            out["dup"] += 1
            continue
        pend = await core.find_pending("hl", from_addr=t["from"], max_usd=t["usd"] + TOL_USD)
        if not pend:
            out["unmatched"].append(t)
            continue
        res = await core.credit(pend["id"], t["hash"], amount_raw=t["usd"], raw=core.dumps(t))
        if not res:
            out["dup"] += 1
            continue
        out["matched"] += 1
        await core.notify_paid(bot, cfg, res["payment"], res["until"])
    return out


async def poll(cfg, client, bot=None) -> dict:
    """Bir tur: ledger sorgusu (son 2 gün) → ayrıştır → eşle → durum kv'ye."""
    addr = (getattr(cfg, "pay_hl_address", "") or "").lower()
    st = await kv_get(KV_STATE) or {}
    if not addr:
        st.update({"skipped": "PAY_HL_ADDRESS yok", "ts": now()})
        await kv_set(KV_STATE, st)
        return st
    ts = now()
    try:
        raw = await client.ledger_updates(addr, (ts - LOOKBACK_SEC) * 1000)
    except Exception as e:
        st.update({"err": st.get("err", 0) + 1, "last_err": f"{type(e).__name__}: {e}"[:200], "ts": ts})
        await kv_set(KV_STATE, st)
        return st
    if not isinstance(raw, list):
        raw = (raw or {}).get("ledgerUpdates") if isinstance(raw, dict) else []
    if st.get("sample") is None and raw:
        st["sample"] = core.dumps(raw[:2])[:600]
    transfers = parse_transfers(raw, addr)
    res = await match(transfers, cfg, bot)
    st.update({"ts": ts, "ok": st.get("ok", 0) + 1, "n_raw": len(raw or []), "n_in": len(transfers),
               "matched_total": st.get("matched_total", 0) + res["matched"],
               "unmatched": [{k: t[k] for k in ("hash", "from", "usd", "ts")} for t in res["unmatched"][-10:]],
               "skipped": ""})
    await kv_set(KV_STATE, st)
    if res["matched"]:
        log.info("USDC ödeme: %d eşleşti, %d eşleşmedi", res["matched"], len(res["unmatched"]))
    return st


async def loop(cfg, client, bot=None) -> None:
    """Denetimli döngü: bekleyen 'hl' ödeme varken her 60 sn, yokken 600 sn'de bir."""
    from ..health import beat
    await asyncio.sleep(120)
    last = 0
    while True:
        try:
            await beat("paywatch")
            await core.expire_stale()
            ts = now()
            pend = await core.pending("hl")
            if pend or ts - last >= IDLE_SEC:
                await poll(cfg, client, bot)
                last = ts
            await beat("paywatch")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("paywatch turu hatası")
        await asyncio.sleep(60)
