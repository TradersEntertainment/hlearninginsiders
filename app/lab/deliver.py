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


async def on_pass(notifier, cfg, rec: dict) -> bool:
    from ..telegram import format as fmt
    from .registry import ACTIVE
    spec = (ACTIVE.get(rec["rule_id"]) or {}).get("spec") or {}
    return await _send(notifier, cfg, fmt.strat_pass(rec, spec), f"lab:gecti:{rec['rule_id']}:{rec['ver']}",
                       approve_kb(rec["rule_id"], rec["ver"]))


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
    v = await ui.overview()
    by = {(r["rule_id"], int(r["ver"])): r for r in v["rules"]}
    n = 0
    for e in events:
        r = by.get((e["rule_id"], int(e["ver"])))
        spec = (ACTIVE.get(e["rule_id"]) or {}).get("spec") or {}
        if r is None or r["status"] != "canli":
            continue
        n += await _send(notifier, cfg, fmt.strat_signal(e, r, spec),
                         f"lab:olay:{e['rule_id']}:{e['coin']}:{e['trig_key']}")
    return n
