"""Auto-solve the portal's math captcha via OpenRouter vision models.

Ported from the campusbuddies backend. The captcha is always a `a±b` expression
with both operands < 100 and a positive-integer answer, so we extract the
expression and compute the answer ourselves rather than trusting the model's
arithmetic.
"""
from __future__ import annotations

import base64
import io
import re

import httpx

from . import config
from .applog import log

try:
    from PIL import Image

    _HAS_PIL = True
except Exception:  # pragma: no cover
    _HAS_PIL = False


async def solve_captcha(image_bytes: bytes) -> tuple[str | None, str]:
    """Return (answer_or_None, joined_attempt_log)."""
    if not config.OPENROUTER_API_KEY:
        return None, "OPENROUTER_API_KEY not configured"

    png_bytes = image_bytes
    if _HAS_PIL:
        try:
            img = Image.open(io.BytesIO(image_bytes))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()
        except Exception:
            png_bytes = image_bytes

    data_url = "data:image/png;base64," + base64.b64encode(png_bytes).decode()
    logs: list[str] = []
    models = config.OPENROUTER_MODELS
    prompt = (
        "Identify the math expression in this image, ignoring the blue noise. "
        "Constraints: The final answer is always a positive integer between 1 and 99. "
        "Logic: If your calculation exceeds 99, re-examine the operator; it is likely "
        "a minus (-) instead of a plus (+). Format: EXPR=ANSWER (e.g. 8+39=47)"
    )

    for model in models:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "temperature": 0,
            "max_tokens": 16,
        }
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    config.OPENROUTER_URL,
                    headers={
                        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if resp.status_code != 200:
                logs.append(f"[{model}] HTTP {resp.status_code}: {resp.text[:200]}")
                continue
            text = (
                resp.json()
                .get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            m = re.search(r"(\d+)\s*([+\-])\s*(\d+)", text.replace(" ", ""))
            if m:
                a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
                if a < 100 and b < 100:
                    answer = str(a + b if op == "+" else a - b)
                    logs.append(f"[{model}] '{text}' -> {a}{op}{b}={answer}")
                    log.info("captcha solved by %s: %s%s%s=%s", model, a, op, b, answer)
                    return answer, "\n".join(logs)
            logs.append(f"[{model}] no valid expression in: '{text}'")
            log.debug("captcha: %s returned no valid expression: %r", model, text)
        except Exception as e:  # pragma: no cover
            logs.append(f"[{model}] exception: {e}")
            log.warning("captcha: %s exception: %s", model, e)

    log.warning("captcha: all %d model(s) failed to solve", len(models))
    return None, "\n".join(logs) if logs else "no models tried"
