"""🧪 Teslim — YALNIZ sahibe, YALNIZ mesaj (strateji asla aramaz: bu paket wake'i içe aktarmaz).

  • Kapıyı geçen kural (gecti): sahibe ölçülü özet + ✅ Onayla / 🛑 Reddet tuşları (lab: öneki,
    yalnız sahip basar). Onaysız kural mesaj ATMAZ.
  • Canlı (onaylı) kuralın yeni olayı: ölçülü tetik mesajı — "işlem aç" yok, yön + kayıttan sonraki
    karne + kâğıt üstü durum.
  • Canlıda düşüş (durdu): tek mesaj.
Hedef: STRAT_CHAT_ID, yoksa ana sohbet — her zaman AÇIK chat_id ile (sessiz saatte sabah özetine
düşüp sayıya dönmesin). Tür: 'strat' (herkese açık akışta YOK, public=False).
"""
from __future__ import annotations

import logging

log = logging.getLogger("lab.deliver")

LAB_CB = "lab"


def target(cfg) -> str:
    return str(getattr(cfg, "strat_chat_id", "") or getattr(cfg, "telegram_chat_id", "") or "").strip()


def approve_kb(rule_id: str, ver: int) -> dict:
    return {"inline_keyboard": [[{"text": "✅ Onayla — canlı (mesaj atar)", "callback_data": f"{LAB_CB}:ok:{rule_id}:{ver}"},
                                 {"text": "🛑 Reddet", "callback_data": f"{LAB_CB}:no:{rule_id}:{ver}"}]]}


async def _send(notifier, cfg, text: str, key: str, kb: dict | None = None) -> bool:
    chat = target(cfg)
    if notifier is None or not chat:
        return False
    try:
        return bool(await notifier.send("strat", text, chat_id=chat, key=key, public=False, reply_markup=kb))
    except Exception:                                  # noqa: BLE001 — teslim düşerse lab sürer
        log.warning("strat mesajı gönderilemedi", exc_info=True)
        return False


PROMPT_KV = "lab_prompt:{}:{}"


async def on_pass(notifier, cfg, rec: dict) -> bool:
    """Onay sorusu. Başarılı teslim kv'ye işlenir; düşerse resend_pending saatlik yeniden dener (kural
    'gecti'de sonsuza dek takılı kalmasın)."""
    from ..db import kv_set
    from ..telegram import format as fmt
    from .registry import ACTIVE
    spec = (ACTIVE.get(rec["rule_id"]) or {}).get("spec") or {}
    ok = await _send(notifier, cfg, fmt.strat_pass(rec, spec), f"lab:gecti:{rec['rule_id']}:{rec['ver']}",
                     approve_kb(rec["rule_id"], rec["ver"]))
    if ok:
        await kv_set(PROMPT_KV.format(rec["rule_id"], rec["ver"]), {"ts": now_ts()})
    return ok


def now_ts() -> int:
    from ..db import now
    return int(now())


async def resend_pending(notifier, cfg, ts: int | None = None) -> int:
    """'gecti' durumunda olup onay sorusu henüz TESLİM EDİLMEMİŞ kurallar: son geçen bakışla yeniden sor."""
    from ..db import db, kv_get
    from .registry import ACTIVE
    n = 0
    for rid, a in list(ACTIVE.items()):
        if a["status"] != "gecti" or await kv_get(PROMPT_KV.format(rid, a["ver"])):
            continue
        async with db() as conn:
            cur = await conn.execute(
                "SELECT * FROM lab_tests WHERE rule_id=? AND ver=? AND kind='look' AND decision='gecti'"
                " ORDER BY look_no DESC LIMIT 1", (rid, a["ver"]))
            r = await cur.fetchone()
        if not r:
            continue
        rec = {"rule_id": rid, "ver": a["ver"], "look": r["look_no"], "decision": "gecti", "p": r["p"],
               "alpha_k": r["alpha_k"], "n_c": r["n_clusters"], "n_ev": r["n_events"], "mean": r["est"]}
        n += await on_pass(notifier, cfg, rec)
    return n


async def on_stop(notifier, cfg, rec: dict) -> bool:
    from ..telegram import format as fmt
    return await _send(notifier, cfg, fmt.strat_stop(rec), f"lab:durdu:{rec['rule_id']}:{rec['ver']}")


async def send_live(notifier, cfg, events: list[dict]) -> int:
    """Canlı kuralların yeni olayları (flush'ta yazılanlar). Kural başına karne bir kez okunur."""
    if not events:
        return 0
    from ..telegram import format as fmt
    from . import ui
    from .registry import ACTIVE
    cards: dict = {}
    n = 0
    for e in events:
        k = (e["rule_id"], int(e["ver"]))
        if k not in cards:                              # kural başına tek hedefli sorgu (tüm tablo değil)
            cards[k] = await ui.rule_card(*k)
        r = cards[k]
        spec = (ACTIVE.get(e["rule_id"]) or {}).get("spec") or {}
        if r is None or r["status"] != "canli":
            continue
        n += await _send(notifier, cfg, fmt.strat_signal(e, r, spec),
                         f"lab:olay:{e['rule_id']}:{e['coin']}:{e['trig_key']}")
    return n
