"""Central trace logging.

Everything the app does is written to a rotating file (DATA_DIR/trace.log) AND
stdout (so `docker compose logs` shows it too), at DEBUG by default. This is the
forensic log — separate from the user-facing activity feed (`db.log_event`). Use it
to reconstruct exactly what happened: every HTTP call to the portal, every poller
decision, every login/join/drop, every API request.

Usage:
    from .applog import log, traced
    log.info("something happened: %s", value)

    @traced               # logs entry args, exit timing, and exceptions
    async def do_work(...): ...

Set LOG_LEVEL=INFO (or WARNING) to quiet it down; LOG_LEVEL=DEBUG (default) is full.
"""
from __future__ import annotations

import inspect
import logging
import os
import sys
import time
from functools import wraps
from logging.handlers import RotatingFileHandler

_CONFIGURED = False
_LOGGER_NAME = "autoregister"

# Argument names whose values must never be written to the log verbatim.
_REDACT = ("password", "passwd", "cookie", "cookies", "token", "captcha",
           "answer", "secret", "authorization")


def setup_logging() -> None:
    """Configure the `autoregister` logger with a rotating file + stdout handler.
    Idempotent: safe to call from import-time of several modules."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    level_name = os.environ.get("LOG_LEVEL", "DEBUG").upper()
    level = getattr(logging, level_name, logging.DEBUG)

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-5s %(name)s.%(funcName)s:%(lineno)d | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file in the (mounted) data dir so traces survive restarts and are
    # readable from the host.
    try:
        from . import config
        fh = RotatingFileHandler(
            config.DATA_DIR / "trace.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:  # noqa: BLE001 — never let logging setup crash the app
        pass

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    _CONFIGURED = True
    logger.info("trace logging initialised (level=%s)", level_name)


def get_logger(name: str | None = None) -> logging.Logger:
    setup_logging()
    return logging.getLogger(_LOGGER_NAME if not name else f"{_LOGGER_NAME}.{name}")


def _short(v, n: int = 120) -> str:
    try:
        s = repr(v)
    except Exception:  # noqa: BLE001
        s = "<unreprable>"
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n})"


def _fmt_call(fn, args, kwargs) -> str:
    """Render call args for the trace, skipping `self`/`cls` and redacting secrets."""
    try:
        names = list(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        names = []
    parts: list[str] = []
    pos = list(args)
    # Drop a leading self/cls bound arg from the rendering.
    if names and names[0] in ("self", "cls") and pos:
        pos = pos[1:]
        names = names[1:]
    for i, val in enumerate(pos):
        key = names[i] if i < len(names) else f"a{i}"
        parts.append(f"{key}=<redacted>" if key.lower() in _REDACT else f"{key}={_short(val)}")
    for key, val in kwargs.items():
        parts.append(f"{key}=<redacted>" if key.lower() in _REDACT else f"{key}={_short(val)}")
    return ", ".join(parts)


def traced(fn):
    """Decorator: log entry (with args), exit (with elapsed ms), and any exception.
    Works on both sync and async functions/methods."""
    logger = logging.getLogger(f"{_LOGGER_NAME}.trace")
    qual = getattr(fn, "__qualname__", getattr(fn, "__name__", "fn"))

    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def awrapper(*args, **kwargs):
            logger.debug("→ %s(%s)", qual, _fmt_call(fn, args, kwargs))
            t0 = time.time()
            try:
                result = await fn(*args, **kwargs)
                logger.debug("← %s [%.0fms]", qual, (time.time() - t0) * 1000)
                return result
            except Exception as e:  # noqa: BLE001
                logger.exception("✗ %s [%.0fms]: %s", qual, (time.time() - t0) * 1000, e)
                raise
        return awrapper

    @wraps(fn)
    def wrapper(*args, **kwargs):
        logger.debug("→ %s(%s)", qual, _fmt_call(fn, args, kwargs))
        t0 = time.time()
        try:
            result = fn(*args, **kwargs)
            logger.debug("← %s [%.0fms]", qual, (time.time() - t0) * 1000)
            return result
        except Exception as e:  # noqa: BLE001
            logger.exception("✗ %s [%.0fms]: %s", qual, (time.time() - t0) * 1000, e)
            raise
    return wrapper


# Configure as soon as this module is imported.
setup_logging()
log = get_logger()
