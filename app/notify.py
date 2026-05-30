"""Discord webhook notifications."""
from __future__ import annotations

import json

import httpx

from . import config
from .applog import log


async def discord_send(
    content: str,
    image_bytes: bytes | None = None,
    filename: str = "image.png",
) -> bool:
    """Post a message (optionally with an image) to the configured webhook.

    Returns True on success, False if not configured or the request failed.
    """
    if not config.DISCORD_WEBHOOK:
        log.debug("discord_send: no webhook configured; skipping: %s", content[:80])
        return False
    content = content[:1900]  # Discord hard limit is 2000 chars.
    log.info("discord_send: %s%s", content[:120], " (+image)" if image_bytes else "")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            if image_bytes:
                resp = await client.post(
                    config.DISCORD_WEBHOOK,
                    data={"payload_json": json.dumps({"content": content})},
                    files={"file": (filename, image_bytes, "application/octet-stream")},
                )
            else:
                resp = await client.post(
                    config.DISCORD_WEBHOOK, json={"content": content}
                )
        ok = resp.status_code in (200, 204)
        log.log(20 if ok else 30, "discord_send -> HTTP %s (ok=%s)", resp.status_code, ok)
        return ok
    except Exception as e:  # pragma: no cover
        log.exception("discord_send failed: %s", e)
        return False
