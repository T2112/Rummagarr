"""Detect a Soulseek user who is blocking or stalling a download.

Every phrase and transfer rule lives here so the worker and the tests share
one definition. This module does not send a chat message. A human-check is
left for the person at this PC to answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Case-insensitive substrings. Short tokens such as "bot" are matched as a
# whole word below so "both", "bottle", "bottom", "about", and "robot" do not
# count.
CHALLENGE_PHRASES: tuple[str, ...] = (
    "are you human",
    "are you a human",
    "confirm you are human",
    "confirm you're human",
    "confirm youre human",
    "confirm you are a human",
    "confirm you're a human",
    "prove you are human",
    "prove you're human",
    "prove youre human",
    "human verification",
    "verify you are human",
    "verify you're human",
    "not a bot",
    "are you a bot",
    "are you bot",
    "you a bot",
    "captcha",
    "reply to",
    "anti-bot",
    "antibot",
    "anti bot",
)

# "bot" or "bots" as its own word. "robot" and "both" do not match.
_BOT_WORD = re.compile(r"(?<![a-z0-9])bots?(?![a-z0-9])")

# aioslsk 1.7.1 sets Transfer.fail_reason from the peer:
#   PeerTransferReply.reason when allowed is false (transfer/manager.py)
#   PeerTransferQueueFailed.reason (same file, on_message)
# Library constants the peer often echoes, from aioslsk.transfer.model.FailReason:
#   CANCELLED = "Cancelled", QUEUED = "Queued",
#   FILE_NOT_SHARED = "File not shared.", FILE_READ_ERROR = "File read error."
# AbortReason.REQUESTED = "Requested" is our own cancel, not a remote block.
# AbortReason.BLOCKED = "Blocked" is a remote or local block and counts as a refusal.
FILE_LEVEL_REASONS = frozenset(
    {
        "file not shared.",
        "file not shared",
        "file read error.",
        "file read error",
        "complete",
    }
)
LOCAL_ABORTS = frozenset({"requested"})
_TERMINAL = frozenset({"COMPLETE", "FAILED", "ABORTED"})
_MOVING = frozenset({"DOWNLOADING", "UPLOADING", "INCOMPLETE"})

# Three failed attempts from one user, inside the hold window, is a stall.
REPEATED_FAILURE_LIMIT = 3


@dataclass(frozen=True)
class HoldSignal:
    """Why a user should be held. ``reason`` is challenge, denied, queued, or failures."""

    reason: str
    detail: str


@dataclass(frozen=True)
class TransferView:
    """The fields the hold rules read. The worker fills this from a live transfer."""

    state: str
    fail_reason: str | None = None
    abort_reason: str | None = None
    bytes_done: int = 0
    queued_seconds: float = 0.0


def looks_like_human_challenge(text: str) -> bool:
    """True when an incoming private message asks the downloader to prove they are human.

    Matching is case-insensitive. Ordinary chat ("thanks", "both albums",
    "about the folder") does not match.
    """
    folded = (text or "").casefold()
    if not folded.strip():
        return False
    for phrase in CHALLENGE_PHRASES:
        if phrase in folded:
            return True
    return _BOT_WORD.search(folded) is not None


def _reason_text(fail_reason: str | None, abort_reason: str | None) -> str:
    fail = (fail_reason or "").strip()
    if fail:
        return fail
    return (abort_reason or "").strip()


def transfer_denial(view: TransferView) -> HoldSignal | None:
    """A remote refusal of this transfer.

    File-not-shared and our own cancel are not a user hold. Any other
    fail or abort reason is: aioslsk stores the peer's PeerTransferReply
    reason and PeerTransferQueueFailed reason on the transfer.
    """
    abort = (view.abort_reason or "").strip().casefold()
    if abort in LOCAL_ABORTS:
        return None
    text = _reason_text(view.fail_reason, view.abort_reason)
    if not text:
        return None
    state = (view.state or "").upper()
    if state not in {"FAILED", "ABORTED"} and not (view.fail_reason or "").strip():
        return None
    folded = text.casefold()
    if folded in FILE_LEVEL_REASONS:
        return None
    return HoldSignal("denied", f"Transfer refused: {text}")


def queue_stall_signal(queued_seconds: float, stall_minutes: int) -> HoldSignal | None:
    """Queued, with no bytes, for longer than the stall setting."""
    limit = max(1, int(stall_minutes or 1)) * 60
    if float(queued_seconds or 0) < limit:
        return None
    waited = int(queued_seconds)
    minutes = max(1, waited // 60)
    return HoldSignal("queued", f"Queued {minutes}m with no progress")


def repeated_failure_signal(count: int, limit: int = REPEATED_FAILURE_LIMIT) -> HoldSignal | None:
    if int(count or 0) < int(limit):
        return None
    return HoldSignal("failures", f"{int(count)} downloads from this user failed")


def batch_hold_signal(
    transfers: list[TransferView],
    *,
    stall_minutes: int,
    failure_count: int,
    already: HoldSignal | None = None,
) -> HoldSignal | None:
    """The first reason to hold this user, or None to keep waiting.

    ``already`` is a hold that is already recorded (a human-check message,
    or an earlier stall). A download that is still moving does not clear it.

    Queue-stall applies only when every transfer that is still open is queued
    and none of them have received bytes. One file that is downloading keeps
    the batch on this user.
    """
    if already is not None:
        return already
    for view in transfers:
        denial = transfer_denial(view)
        if denial is not None:
            return denial
    repeated = repeated_failure_signal(failure_count)
    if repeated is not None:
        return repeated
    pending = [view for view in transfers if (view.state or "").upper() not in _TERMINAL]
    if not pending:
        return None
    for view in pending:
        state = (view.state or "").upper()
        if state in _MOVING or int(view.bytes_done or 0) > 0:
            return None
    oldest = max(float(view.queued_seconds or 0) for view in pending)
    return queue_stall_signal(oldest, stall_minutes)
