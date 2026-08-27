"""OpenAI-uyumlu minimal sohbet istemcisi (Groq/Cerebras/OpenRouter/DeepSeek).

Neden SDK değil: bu iş tek bir POST. Bir SDK bağımlılığı, projenin şu anki
sekiz paketlik yalın `requirements.txt`'ine tek bir çağrı için yeni bir
sürüm/uyumluluk yüzeyi eklerdi. Sağlayıcı değiştirmek = `ai_base_url` +
`ai_model` ayarını değiştirmek.

Bütçe disiplini burada değil ÇAĞIRANDA (`analyst.py`): istek atılmadan önce
kontrol edilir, çünkü asıl mesele "harcadıktan sonra saymak" değil
"harcamadan önce durmak".
"""
import asyncio
import json
import logging

import aiohttp

log = logging.getLogger("ai.client")

TIMEOUT = 60          # model yavaş olabilir; tur zaten 2 saatte bir
# AKIL YÜRÜTEN MODELLER (gpt-oss…) muhakemeyi de bu bütçeden harcar; 1200 iken
# bütçe muhakemede bitiyor ve içerik kanalı BOŞ dönüyordu (Groq bunu
# json_validate_failed + failed_generation:"" diye bildiriyor).
MAX_OUT_TOKENS = 4000
# Bu modellerde muhakeme derinliği ayarlanabilir; bizim iş kısa ve yapılandırılmış,
# "low" hem yeter hem token yakmaz. Yalnız destekleyen modele gönderilir.
REASONING_MODELS = ("gpt-oss",)


class JsonModeRejected(Exception):
    """Sağlayıcı kendi JSON kipinde üretimi doğrulayamadı — kipsiz tekrar dene."""


class RateLimited(Exception):
    """429 — turu ATLA. Tekrar denemek bedava katmanı daha da yakar."""

    def __init__(self, retry_after: float):
        super().__init__(f"rate limit, {retry_after:.0f} sn sonra")
        self.retry_after = retry_after


class AIClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str,
                 api_key: str, model: str):
        self.session = session
        self.base_url = base_url
        self.api_key = api_key
        self.model = model

    async def complete(self, system: str, user: str) -> tuple[str, int, int]:
        """(metin, girdi_token, çıktı_token). Hata durumunda exception fırlatır.

        Sağlayıcının JSON kipi bir KOLAYLIKTIR, gereklilik değil: çıktının her
        alanını zaten kendimiz doğruluyoruz (`schema.validate`). gpt-oss gibi
        akıl yürüten modellerde Groq'un doğrulayıcısı boş üretimle patlıyor —
        o durumda kipi bırakıp kendi toleranslı ayrıştırıcımızla devam ediyoruz.
        Tekrar YALNIZ bu hataya özeldir ve turda bir kezdir.
        """
        try:
            return await self._call(system, user, json_mode=True)
        except JsonModeRejected as e:
            log.info("sağlayıcı JSON kipini doğrulayamadı (%s) — kipsiz tekrar", e)
            return await self._call(system, user, json_mode=False)

    async def _call(self, system: str, user: str,
                    json_mode: bool) -> tuple[str, int, int]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": MAX_OUT_TOKENS,
            "temperature": 0.4,          # örüntü önerisi için biraz çeşitlilik
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if any(m in self.model for m in REASONING_MODELS):
            payload["reasoning_effort"] = "low"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        try:
            async with self.session.post(
                    self.base_url, json=payload, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as r:
                body = await r.text()
                if r.status == 429:
                    raise RateLimited(_retry_after(r.headers))
                if (r.status == 400 and json_mode
                        and "json_validate_failed" in body):
                    raise JsonModeRejected(body[:120])
                if r.status != 200:
                    # Model adları sağlayıcıda dönüyor (Groq eski llama'ları
                    # emekliye ayırdı). Çıplak "model_not_found" kullanıcıya
                    # NE YAZACAĞINI söylemiyordu; kullanılabilir listeyi hatanın
                    # içine koyuyoruz ki panelde doğrudan okunsun.
                    hint = ""
                    if r.status in (400, 404) and "model" in body.lower():
                        names = await self.list_models()
                        if names:
                            hint = " — kullanılabilir modeller: " + ", ".join(names[:12])
                    raise RuntimeError(f"HTTP {r.status}: {body[:200]}{hint}")
                data = json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise RuntimeError(f"ağ hatası: {e}") from e
        except json.JSONDecodeError as e:
            raise RuntimeError(f"yanıt JSON değil: {e}") from e

        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise RuntimeError(f"beklenmeyen yanıt şekli: {str(data)[:200]}") from e
        usage = data.get("usage") or {}
        return (text,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0))

    async def list_models(self) -> list[str]:
        """Sağlayıcıdaki model kimlikleri. OpenAI-uyumlu API'lerde sohbet uç
        noktasının kardeşi `/models`'tır. Hata yolunda çağrılır — başarısız
        olursa sessizce boş döner, asıl hatanın üstünü örtmesin."""
        if "/chat/completions" not in self.base_url:
            return []
        url = self.base_url.replace("/chat/completions", "/models")
        try:
            async with self.session.get(
                    url, headers={"Authorization": f"Bearer {self.api_key}"},
                    timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    return []
                data = json.loads(await r.text())
        except Exception:
            return []
        out = [str(m.get("id")) for m in (data.get("data") or []) if m.get("id")]
        return sorted(out)


def _retry_after(headers) -> float:
    for h in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        v = headers.get(h)
        if not v:
            continue
        try:
            return float(str(v).rstrip("s"))
        except ValueError:
            continue
    return 60.0


def parse_output(text: str) -> dict:
    """Model çıktısını sözlüğe çevir. Bozuksa RuntimeError.

    JSON kipi kapalıyken (bkz. `complete`) model, JSON'un önüne/arkasına
    açıklama ya da ``` çiti koyabiliyor. Üç aşamalı deniyoruz: düz ayrıştır →
    ``` çitini soy → metnin içindeki ilk süslü parantez bloğunu çıkar.

    Daha ötesini ZORLAMIYORUZ: ayrıştırılamayan çıktı hata sayılır ve panelde
    görünür. DB'ye tahmin yazmaktansa turu kaybetmek yeğdir.
    """
    t = (text or "").strip()
    for cand in (t, _strip_fence(t), _first_object(t)):
        if not cand:
            continue
        try:
            out = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(out, dict):
            return out
        raise RuntimeError("çıktı sözlük değil")
    raise RuntimeError(f"çıktı JSON değil: {t[:120]!r}")


def _strip_fence(t: str) -> str:
    if not t.startswith("```"):
        return ""
    body = t.split("```")[1] if "```" in t[3:] else t[3:]
    body = body.lstrip()
    if body.lower().startswith("json"):
        body = body[4:].lstrip()
    return body.strip()


def _first_object(t: str) -> str:
    """Metindeki ilk dengeli {...} bloğu. Dizge içindeki süslü parantezleri
    ve kaçışları sayar — yoksa 'text' alanındaki bir { ayrıştırmayı bozardı."""
    start = t.find("{")
    if start < 0:
        return ""
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(t[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return t[start:i + 1]
    return ""
