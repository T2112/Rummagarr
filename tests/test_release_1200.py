"""Rummagarr 1.20.0: hold a user who is blocking us, and try another source.

No automatic private-message reply is sent. With the setting off, a failed
download still blocklists that folder and moves on, and a human-check message
is only stored.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from activity import format_status_line, hold_phrase
from changelog_notes import changelog_section
from config_loader import default_config, save_config, try_load_config
from source_hold import (
    CHALLENGE_PHRASES,
    FILE_LEVEL_REASONS,
    HoldSignal,
    TransferView,
    batch_hold_signal,
    looks_like_human_challenge,
    repeated_failure_signal,
    transfer_denial,
)
from tests.conftest import make_worker

ROOT = Path(__file__).resolve().parents[1]


class _Transfer:
    def __init__(self, state: str, *, fail_reason: str | None = None, abort_reason: str | None = None, bytes_done: int = 0, username: str = "stall") -> None:
        self.state = state
        self.fail_reason = fail_reason
        self.abort_reason = abort_reason
        self.bytes_transfered = bytes_done
        self.username = username
        self.filename = "01.flac"


class _File:
    filename = r"Ella\01.flac"


class _Candidate:
    def __init__(self, username: str, score: int) -> None:
        self.username = username
        self.remote_dir = "music"
        self.score = score
        self.files = [_File()]


class _Client:
    def __init__(self, transfer: _Transfer) -> None:
        self.transfer = transfer
        self.commands = []
        self.transfers = self

    async def download(self, username: str, filename: str) -> _Transfer:
        return self.transfer

    async def execute(self, command) -> None:
        self.commands.append(command)


def test_challenge_phrases_are_conservative_and_case_insensitive():
    assert CHALLENGE_PHRASES
    assert looks_like_human_challenge("Are You Human??")
    assert looks_like_human_challenge("please CONFIRM YOU ARE HUMAN thanks")
    assert looks_like_human_challenge("confirm you're human")
    assert looks_like_human_challenge("reply to this message")
    assert looks_like_human_challenge("solve the CAPTCHA")
    assert looks_like_human_challenge("are you a bot")
    assert looks_like_human_challenge("bot")
    assert looks_like_human_challenge("anti-bot check")
    for phrase in ("are you human", "confirm you are human", "captcha", "reply to"):
        assert phrase in CHALLENGE_PHRASES
    assert not looks_like_human_challenge("both albums are available")
    assert not looks_like_human_challenge("bottom folder")
    assert not looks_like_human_challenge("about the album")
    assert not looks_like_human_challenge("nice robot")
    assert not looks_like_human_challenge("thanks for the files")
    assert not looks_like_human_challenge("I only share with friends")
    assert not looks_like_human_challenge("")
    assert not looks_like_human_challenge("   ")


def test_transfer_denial_matches_aioslsk_refusal_reasons_and_ignores_file_errors():
    # aioslsk 1.7.1 stores PeerTransferReply.reason and PeerTransferQueueFailed.reason
    # on Transfer.fail_reason. FailReason.CANCELLED is "Cancelled". FILE_NOT_SHARED is not a user hold.
    banned = transfer_denial(TransferView("FAILED", fail_reason="Banned"))
    assert banned is not None and banned.reason == "denied"
    cancelled = transfer_denial(TransferView("FAILED", fail_reason="Cancelled"))
    assert cancelled is not None and "Cancelled" in cancelled.detail
    queued = transfer_denial(TransferView("FAILED", fail_reason="Queued"))
    assert queued is not None
    blocked = transfer_denial(TransferView("ABORTED", abort_reason="Blocked"))
    assert blocked is not None
    assert transfer_denial(TransferView("ABORTED", abort_reason="Requested")) is None
    assert transfer_denial(TransferView("FAILED", fail_reason="File not shared.")) is None
    assert transfer_denial(TransferView("FAILED", fail_reason="File read error.")) is None
    assert transfer_denial(TransferView("QUEUED")) is None
    assert "file not shared." in FILE_LEVEL_REASONS
    assert repeated_failure_signal(2) is None
    assert repeated_failure_signal(3).reason == "failures"
    stall = batch_hold_signal(
        [TransferView("QUEUED", queued_seconds=15 * 60)],
        stall_minutes=15,
        failure_count=0,
    )
    assert stall is not None and stall.reason == "queued"
    assert (
        batch_hold_signal(
            [TransferView("QUEUED", queued_seconds=30)],
            stall_minutes=15,
            failure_count=0,
        )
        is None
    )
    assert (
        batch_hold_signal(
            [TransferView("DOWNLOADING", bytes_done=1000, queued_seconds=3600)],
            stall_minutes=15,
            failure_count=0,
        )
        is None
    )
    moving = batch_hold_signal(
        [
            TransferView("DOWNLOADING", bytes_done=10),
            TransferView("QUEUED", queued_seconds=3600),
        ],
        stall_minutes=15,
        failure_count=0,
    )
    assert moving is None
    already = batch_hold_signal(
        [TransferView("DOWNLOADING", bytes_done=10)],
        stall_minutes=15,
        failure_count=0,
        already=HoldSignal("challenge", "Asked for a human check"),
    )
    assert already is not None and already.reason == "challenge"


def test_settings_default_on_and_round_trip_and_missing_keys_stay_on(tmp_path: Path):
    fresh = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
    )
    assert fresh.smarter_queue is True
    assert fresh.hold_minutes == 180
    assert fresh.queue_stall_minutes == 15
    path = tmp_path / "config.toml"
    save_config(path, replace(fresh, smarter_queue=False, hold_minutes=45, queue_stall_minutes=8))
    loaded = try_load_config(path)
    assert loaded.smarter_queue is False
    assert loaded.hold_minutes == 45
    assert loaded.queue_stall_minutes == 8
    text = path.read_text(encoding="utf-8")
    assert "smarter_queue = false" in text
    stripped = "\n".join(
        line
        for line in text.splitlines()
        if not line.startswith("smarter_queue")
        and not line.startswith("hold_minutes")
        and not line.startswith("queue_stall_minutes")
    )
    path.write_text(stripped + "\n", encoding="utf-8")
    kept = try_load_config(path)
    assert kept.smarter_queue is True
    assert kept.hold_minutes == 180
    assert kept.queue_stall_minutes == 15


def test_status_line_names_a_held_user_and_stays_quiet_without_one():
    assert hold_phrase([]) == ""
    line = format_status_line(
        {
            "run_state": "running",
            "connected_label": "Soulseek connected",
            "logged_in": True,
            "activity": "Downloading 1 of 8",
            "transfers": [],
            "elapsed": 0,
            "holds": [
                {
                    "display": "vinylqueen",
                    "username": "vinylqueen",
                    "message": "are you human?",
                    "detail": "Asked for a human check",
                }
            ],
        }
    )
    assert "Holding vinylqueen (needs reply)" in line
    quiet = format_status_line(
        {
            "run_state": "running",
            "connected_label": "Soulseek connected",
            "logged_in": True,
            "activity": "Idle – nothing selected",
            "transfers": [],
            "elapsed": 0,
        }
    )
    assert "Holding" not in quiet


def test_a_human_check_holds_the_user_and_does_not_send_a_reply(tmp_path: Path):
    from aioslsk.events import PrivateMessageEvent
    from aioslsk.protocol.messages import PrivateChatMessage
    from aioslsk.user.model import ChatMessage, User

    worker = make_worker(tmp_path)
    try:
        from aioslsk.events import EventBus

        client = _Client(_Transfer("QUEUED"))
        client.events = EventBus()
        worker.client = client
        worker._install_private_messages()
        raw = PrivateChatMessage.Response(
            chat_id=9, timestamp=1, username="vinylqueen", message="Are you human?", is_direct=True
        )
        chat = ChatMessage(
            id=9, timestamp=1, user=User(name="vinylqueen"), message="Are you human?", is_direct=True
        )
        asyncio.run(client.events.emit(PrivateMessageEvent(message=chat, raw_message=raw)))
        assert worker.store.user_is_held("VinylQueen")
        row = worker.store.held_user("vinylqueen")
        assert row is not None
        assert row["reason"] == "challenge"
        assert "Are you human?" in row["message"]
        notice = worker.store.recent_events(4)
        assert notice[0]["kind"] == "hold"
        assert "Are you human?" in notice[0]["body"]
        assert client.commands == []
        assert "send_private_message" not in Path("source_hold.py").read_text(encoding="utf-8")
        plain = PrivateChatMessage.Response(
            chat_id=10, timestamp=2, username="friend", message="thanks for the files", is_direct=True
        )
        friend = ChatMessage(
            id=10, timestamp=2, user=User(name="friend"), message="thanks for the files", is_direct=True
        )
        asyncio.run(client.events.emit(PrivateMessageEvent(message=friend, raw_message=plain)))
        assert not worker.store.user_is_held("friend")
        assert len(worker.store.private_history("friend")) == 1
    finally:
        worker.close()


def test_setting_off_stores_the_challenge_and_does_not_hold(tmp_path: Path):
    from aioslsk.events import EventBus, PrivateMessageEvent
    from aioslsk.protocol.messages import PrivateChatMessage
    from aioslsk.user.model import ChatMessage, User

    worker = make_worker(tmp_path)
    try:
        worker.cfg = replace(worker.cfg, smarter_queue=False)
        client = _Client(_Transfer("FAILED", fail_reason="Banned"))
        client.events = EventBus()
        worker.client = client
        worker._install_private_messages()
        raw = PrivateChatMessage.Response(
            chat_id=3, timestamp=1, username="stall", message="confirm you are human", is_direct=True
        )
        chat = ChatMessage(id=3, timestamp=1, user=User(name="stall"), message="confirm you are human", is_direct=True)
        asyncio.run(client.events.emit(PrivateMessageEvent(message=chat, raw_message=raw)))
        assert not worker.store.user_is_held("stall")
        assert worker.store.private_history("stall")[0]["body"] == "confirm you are human"
        assert client.commands == []
        assert worker.ui_snapshot()["holds"] == []
    finally:
        worker.close()


def test_a_refused_transfer_holds_and_the_next_user_is_tried(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.control.start()
        worker.client = _Client(_Transfer("FAILED", fail_reason="Banned"))
        worker.cfg = replace(worker.cfg, smarter_queue=True, download_timeout_minutes=90)

        async def run():
            outcome, detail = await worker._download_one(
                _Candidate("stall", 9),
                {"title": "Hungover"},
                "k",
                7,
                "Ella",
                "Hungover",
                None,
                [],
            )
            return outcome, detail

        outcome, detail = asyncio.run(run())
        assert outcome == "hold"
        assert "Banned" in detail
        assert worker.store.user_is_held("stall")
        assert not worker.store.source_blocked("stall", "music")

        calls: list[str] = []

        async def fake(best, *_args, **_kwargs):
            calls.append(best.username)
            if best.username == "stall":
                return "hold", "Transfer refused: Banned"
            return "ok", ""

        worker._download_one = fake
        picked = asyncio.run(
            worker._download_candidates(
                [_Candidate("stall", 9), _Candidate("open", 1)],
                {},
                "k",
                8,
                "Ella",
                "Hungover",
                None,
                [],
                set(),
            )
        )
        assert picked is True
        assert calls == ["open"]
        assert not worker.store.source_blocked("stall", "music")
        assert not worker.store.source_blocked("open", "music")
    finally:
        worker.close()


def test_setting_off_still_blocklists_a_failed_download_and_tries_the_next_user(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.cfg = replace(worker.cfg, smarter_queue=False)
        worker.control.start()
        worker.client = _Client(_Transfer("FAILED", fail_reason="Banned"))

        async def one():
            return await worker._download_one(
                _Candidate("stall", 9), {}, "k", 4, "Ella", "Hungover", None, []
            )

        outcome, _detail = asyncio.run(one())
        assert outcome == "again"
        assert not worker.store.user_is_held("stall")
        calls: list[str] = []

        async def fake(best, *_args, **_kwargs):
            calls.append(best.username)
            return "again", "timeout"

        worker._download_one = fake
        picked = asyncio.run(
            worker._download_candidates(
                [_Candidate("stall", 9), _Candidate("open", 1)],
                {},
                "k",
                5,
                "Ella",
                "Hungover",
                None,
                [],
                set(),
            )
        )
        assert picked is False
        assert calls[0] == "stall"
        assert "open" in calls
        assert worker.store.source_blocked("stall", "music")
        assert worker.store.source_blocked("open", "music")
    finally:
        worker.close()


def test_held_user_is_skipped_and_limits_still_apply(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.cfg = replace(worker.cfg, browse_daily_files=1, ignored_users={"banned"})
        worker.store.hold_user("stall", "challenge", "Asked for a human check", "are you human", minutes=30)
        worker.store.add_user_day("capped", 1)
        calls: list[str] = []

        async def fake(best, *_args, **_kwargs):
            calls.append(best.username)
            return "ok", ""

        worker._download_one = fake
        picked = asyncio.run(
            worker._download_candidates(
                [
                    _Candidate("stall", 9),
                    _Candidate("banned", 8),
                    _Candidate("capped", 7),
                    _Candidate("open", 1),
                ],
                {},
                "k",
                11,
                "Ella",
                "Hungover",
                None,
                [],
                set(),
            )
        )
        assert picked is True
        assert calls == ["open"]
        assert not worker.store.source_blocked("stall", "music")
        assert not worker.store.source_blocked("capped", "music")
    finally:
        worker.close()


def test_queue_stall_is_a_hold_and_a_short_queue_is_not(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.cfg = replace(worker.cfg, smarter_queue=True, queue_stall_minutes=15)
        transfer = _Transfer("QUEUED")
        since: dict[int, float] = {}
        worker._monotonic = lambda: 0.0
        assert worker._watch_user("stall", [transfer], since) is None
        worker._monotonic = lambda: 14 * 60
        assert worker._watch_user("stall", [transfer], since) is None
        worker._monotonic = lambda: 15 * 60
        signal = worker._watch_user("stall", [transfer], since)
        assert signal is not None and signal.reason == "queued"
        worker.cfg = replace(worker.cfg, smarter_queue=False)
        assert worker._watch_user("stall", [transfer], since) is None
    finally:
        worker.close()


def test_repeated_failures_hold_without_a_permanent_block(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.control.start()
        worker.client = _Client(_Transfer("FAILED", fail_reason="File not shared."))
        worker.cfg = replace(worker.cfg, smarter_queue=True)
        assert worker.store.note_user_failure("stall") == 1
        assert worker.store.note_user_failure("stall") == 2

        async def run():
            return await worker._download_one(
                _Candidate("stall", 3), {}, "k", 12, "Ella", "Hungover", None, []
            )

        outcome, detail = asyncio.run(run())
        assert outcome == "hold"
        assert "failed" in detail.lower()
        assert worker.store.user_is_held("stall")
        assert not worker.store.source_blocked("stall", "music")
    finally:
        worker.close()


def test_cooldown_and_manual_release_put_the_album_back(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker._apply_hold(
            "stall",
            HoldSignal("challenge", "Asked for a human check"),
            message="are you human?",
            item={"kind": "album", "album_id": 21, "artist": "Ella", "title": "Hungover"},
        )
        worker.store.upsert(21, "Ella", "Hungover", "failed", "needs a reply")
        assert worker.store.should_skip(21, 12)
        inside = datetime.now(timezone.utc) + timedelta(minutes=10)
        assert worker.store.release_expired(now=inside) == []
        worker.cfg = replace(worker.cfg, smarter_queue=False)
        assert worker.release_expired_holds() == 0
        assert worker.store.user_is_held("stall")
        assert worker.store.get(21)["status"] == "failed"
        worker.cfg = replace(worker.cfg, smarter_queue=True)
        future = datetime.now(timezone.utc) + timedelta(hours=4)
        released = worker.store.release_expired(now=future)
        assert released and released[0][0] == "stall"
        worker._resume_held_items(released[0][1])
        assert not worker.store.user_is_held("stall")
        assert worker.store.get(21)["status"] == "not_sent"
        assert worker.store.should_skip(21, 12) is False

        worker._apply_hold(
            "other",
            HoldSignal("queued", "Queued 15m with no progress"),
            item={"kind": "album", "album_id": 22, "artist": "Ella", "title": "Still"},
        )
        worker.store.upsert(22, "Ella", "Still", "failed", "queued")
        worker.release_held_user("other")
        assert not worker.store.user_is_held("other")
        assert worker.store.get(22)["status"] == "not_sent"
        assert worker.store.recent_events() == [] or all(event["kind"] != "hold" or event["username"] != "other" for event in worker.store.recent_events())
    finally:
        worker.close()


def test_a_hold_triggers_one_more_search_and_setting_off_does_not(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        calls: list[str] = []
        searches = {"n": 0}

        async def fake(best, *_args, **_kwargs):
            calls.append(best.username)
            if best.username == "stall":
                worker._apply_hold("stall", HoldSignal("queued", "Queued 15m with no progress"))
                return "hold", "queued"
            return "ok", ""

        async def search():
            searches["n"] += 1
            return [_Candidate("open", 4)]

        worker._download_one = fake
        picked = asyncio.run(
            worker._download_album_with_research(
                [_Candidate("stall", 9)],
                search,
                {},
                "k",
                30,
                "Ella",
                "Hungover",
                None,
                [],
                set(),
            )
        )
        assert picked is True
        assert calls == ["stall", "open"]
        assert searches["n"] == 1
        assert worker.store.user_is_held("stall")
        assert not worker.store.source_blocked("stall", "music")

        worker.cfg = replace(worker.cfg, smarter_queue=False)
        calls.clear()

        async def again(best, *_args, **_kwargs):
            calls.append(best.username)
            return "again", "timeout"

        async def explode():
            raise AssertionError("researched while smarter queueing is off")

        worker._download_one = again
        missed = asyncio.run(
            worker._download_album_with_research(
                [_Candidate("stall", 9)],
                explode,
                {},
                "k",
                31,
                "Ella",
                "Hungover",
                None,
                [],
                set(),
            )
        )
        assert missed is False
        assert calls == ["stall"]
        assert worker.store.source_blocked("stall", "music")
    finally:
        worker.close()


def test_browse_download_does_not_queue_a_held_user_unless_the_setting_is_off(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.store.hold_user("goodpeer", "challenge", "Asked for a human check", "captcha", minutes=30)
        specs = [{"username": "goodpeer", "filename": "01.flac"}]
        note = asyncio.run(worker.download_paced(specs, pace_seconds=5, files_at_once=1))
        assert "needs a reply" in note
        assert "Nothing was queued" in note
        worker.cfg = replace(worker.cfg, smarter_queue=False, dry_run=True)
        kept = asyncio.run(worker.download_paced(specs, pace_seconds=5, files_at_once=1))
        assert "would grab 1" in kept
        assert worker._drop_held_specs(specs) == specs
    finally:
        worker.close()


def _widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _widgets(child)


def test_status_and_blocklist_show_the_message_and_release_clears_the_hold(tmp_path: Path):
    import tkinter as tk

    try:
        probe = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    probe.destroy()
    from desktop_app import DesktopApp

    worker = make_worker(tmp_path)
    app = None
    try:
        worker._apply_hold(
            "vinylqueen",
            HoldSignal("challenge", "Asked for a human check"),
            message="are you human?",
            item={"kind": "album", "album_id": 4, "artist": "Ella", "title": "Hungover"},
        )
        worker.store.upsert(4, "Ella", "Hungover", "failed", "needs a reply")
        app = DesktopApp(
            worker,
            tmp_path / "config.toml",
            start_status_server=False,
            honor_start_on_launch=False,
            poll_ms=60_000,
        )
        app.update()
        app.refresh_status()
        app.update()
        assert "Holding vinylqueen (needs reply)" in app.status_line.cget("text")
        labels = [widget.cget("text") for widget in _widgets(app.notices) if widget.winfo_class() == "TLabel"]
        assert any("are you human?" in text for text in labels)
        release = next(widget for widget in _widgets(app.notices) if widget.winfo_class() == "TButton" and widget.cget("text") == "Release")
        release.invoke()
        app.update()
        assert not worker.store.user_is_held("vinylqueen")
        assert "Holding" not in app.status_line.cget("text")
        assert worker.store.get(4)["status"] == "not_sent"
        worker._apply_hold(
            "vinylqueen",
            HoldSignal("challenge", "Asked for a human check"),
            message="reply to this",
        )
        app._ensure_settings()
        app.update()
        form = app._settings_form
        form.notebook.select(3)
        form._fill_holds()
        app.update()
        values = form.hold_tree.item(form.hold_tree.get_children()[0], "values")
        assert "vinylqueen" in values[0].lower() or values[0] == "vinylqueen"
        assert "reply to this" in values[2]
        form.hold_tree.selection_set(form.hold_tree.get_children()[0])
        form._release_selected_hold()
        assert not worker.store.user_is_held("vinylqueen")
        assert form.hold_tree.get_children() == ()
    finally:
        if app is not None:
            app.destroy()
        worker.close()


def test_changelog_and_readme_describe_the_hold():
    section = changelog_section((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), "1.20.0")
    assert "needs a reply" in section
    assert "does not send a chat reply" in section
    assert "1.19.1" not in section
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Version 1.20.0" in text
    assert "does not answer the private message" in text
    assert (ROOT / "VERSION").read_text(encoding="utf-8").strip() == "1.20.0"
