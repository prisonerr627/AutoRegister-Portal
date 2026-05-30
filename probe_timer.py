"""Probe the exact Select2 -> GetPreReg2 server-side timer.

Method (as requested): each round re-enter Select2, wait N seconds, then call
GetPreReg2. If GetPreReg2 returns the "you tried to manipulate your session"
error, the timer hasn't elapsed -> bump N by STEP and retry with a fresh Select2.
The first N that SUCCEEDS brackets the real timer to (N-STEP, N]. A second pass
then refines inside that bracket with a finer step.

Read-only: only GET Start/Select2/GetPreReg2 are issued (no register/confirm).

Usage:
    PROBE_PASSWORD='...' python probe_timer.py
Env knobs: PROBE_USER, PROBE_START (default 10), PROBE_STEP (default 10),
           PROBE_MAX (default 180), PROBE_REFINE_STEP (default 2).
"""
import asyncio
import os
import time
from datetime import datetime

from app import portal as portal_mod
from app.portal import PortalSession

USER = os.environ.get("PROBE_USER", "25-62595-2").strip()
PASSWORD = os.environ.get("PROBE_PASSWORD", "").strip()
START = int(os.environ.get("PROBE_START", "10"))
STEP = int(os.environ.get("PROBE_STEP", "10"))
MAXW = int(os.environ.get("PROBE_MAX", "180"))
REFINE_STEP = int(os.environ.get("PROBE_REFINE_STEP", "2"))


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{ts()}] {msg}", flush=True)


def _manipulated(j: dict) -> bool:
    """True if GetPreReg2 rejected us because the timer hadn't elapsed."""
    msg = ((j.get("Message") or "") + " " + str(j.get("Error") or "")).lower()
    return bool(j.get("HasError")) and "manipulate" in msg


def _classify(j: dict) -> str:
    if not isinstance(j, dict):
        return f"non-dict response: {str(j)[:120]}"
    if _manipulated(j):
        return f"MANIPULATE error -> too early. Message={j.get('Message')!r}"
    if j.get("HasError"):
        return f"OTHER error: Message={j.get('Message')!r}"
    sem = (j.get("Semester") or {}).get("Title")
    n = len(j.get("RegisterableCourses", []) or [])
    return f"SUCCESS  semester={sem!r} registerable_courses={n}"


async def one_round(s: PortalSession, wait: int) -> tuple[bool, str]:
    """Fresh Select2, wait `wait`s, then GetPreReg2. Returns (success, detail)."""
    t0 = time.monotonic()
    sel = await s.select2()
    log(f"  Select2 ok={sel.get('ok')} q={'yes' if sel.get('q') else 'no'} "
        f"(+{time.monotonic()-t0:.1f}s)")
    log(f"  waiting {wait}s ...")
    await asyncio.sleep(wait)
    elapsed = time.monotonic() - t0
    j = await s.get_prereg2()
    detail = _classify(j)
    ok = isinstance(j, dict) and not j.get("HasError")
    log(f"  GetPreReg2 @ ~{elapsed:.1f}s since Select2 -> {detail}")
    return ok, detail


async def main() -> None:
    if not PASSWORD:
        raise SystemExit("set PROBE_PASSWORD")
    s = PortalSession(USER)
    s.set_credentials(USER, PASSWORD)
    log(f"logging in as {USER} (solving captcha) ...")
    await s.auto_login()
    log(f"logged in as {s.display_name or USER}; auth cookie present="
        f"{bool(s.cookies.get('NAABSUMSMVCFORMSAUTH'))}")

    # ── Pass 1: coarse linear search, STEP-second increments ────────────────
    log(f"\n=== PASS 1: coarse search {START}..{MAXW} step {STEP} ===")
    success_at = None
    fail_below = START - STEP  # last known-too-early wait
    wait = START
    while wait <= MAXW:
        log(f"-- round wait={wait}s --")
        ok, _ = await one_round(s, wait)
        if ok:
            success_at = wait
            break
        fail_below = wait
        wait += STEP

    if success_at is None:
        log(f"\nNo success up to {MAXW}s — timer is longer, or something else is wrong.")
        return

    log(f"\nPASS 1 result: failed at {fail_below}s, FIRST SUCCESS at {success_at}s.")
    log(f"=> timer is in ({fail_below}s, {success_at}s].")

    # ── Pass 2: refine inside the bracket ───────────────────────────────────
    lo, hi = fail_below, success_at  # lo=too early, hi=ok
    if hi - lo > REFINE_STEP:
        log(f"\n=== PASS 2: refining ({lo}, {hi}] step {REFINE_STEP} ===")
        probe = lo + REFINE_STEP
        while probe < hi:
            log(f"-- refine wait={probe}s --")
            ok, _ = await one_round(s, probe)
            if ok:
                hi = probe
                break
            lo = probe
            probe += REFINE_STEP
        log(f"\nPASS 2 result: too early at {lo}s, ok at {hi}s.")

    log(f"\n================ CONCLUSION ================")
    log(f"GetPreReg2 is rejected at {lo}s and accepted at {hi}s after Select2.")
    log(f"Exact required wait is in ({lo}s, {hi}s].")
    log(f"Recommended SELECT2_WAIT_SECONDS = {hi} (or a small margin above).")


if __name__ == "__main__":
    asyncio.run(main())
