"""Rummagarr 1.18.0: private messages and an optional local crash bundle."""

from __future__ import annotations

import asyncio
import zipfile
from pathlib import Path

from aioslsk.commands import PrivateMessageCommand
from aioslsk.events import PrivateMessageEvent
from aioslsk.protocol.messages import PrivateChatMessage
from aioslsk.user.model import ChatMessage, User
from changelog_notes import changelog_section
from config_loader import default_config, save_config, try_load_config
from crash_bundle import redact, write_crash_bundle
from messages import message_is_sendable, rooms_are_off
from tests.conftest import make_worker
from worker import build_slsk_settings


class _Client:
    def __init__(self) -> None:
        from aioslsk.events import EventBus

        self.session = object()
        self.commands = []
        self.events = EventBus()

    async def execute(self, command) -> None:
        self.commands.append(command)


def test_rooms_stay_off_and_send_uses_the_compose_text(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        settings = build_slsk_settings(worker.cfg)
        assert rooms_are_off(settings)
        source = Path("worker.py").read_text(encoding="utf-8") + Path("desktop_app.py").read_text(encoding="utf-8")
        for name in ("JoinRoomCommand", "RoomMessageCommand", "EnablePublicChat"):
            assert name not in source
        assert not message_is_sendable("  ")
        assert message_is_sendable("hello")

        worker.client = _Client()
        assert "Start the worker" in asyncio.run(worker.send_private_message("peer", "hello"))
        assert worker.client.commands == []

        worker.control.start()
        sent = asyncio.run(worker.send_private_message("peer", "  hello  "))
        assert sent == "Sent to peer."
        assert isinstance(worker.client.commands[0], PrivateMessageCommand)
        assert worker.client.commands[0].username == "peer"
        assert worker.client.commands[0].message == "hello"
        history = worker.store.private_history("peer")
        assert history[-1]["direction"] == "out"
        assert history[-1]["body"] == "hello"
        assert "compose box" in asyncio.run(worker.send_private_message("peer", " "))
        assert len(worker.client.commands) == 1
    finally:
        worker.close()


def test_incoming_private_messages_are_unread_until_opened(tmp_path: Path):
    worker = make_worker(tmp_path)
    try:
        worker.client = _Client()
        worker._install_private_messages()
        raw = PrivateChatMessage.Response(chat_id=4, timestamp=1, username="peer", message="hi", is_direct=True)
        chat = ChatMessage(id=4, timestamp=1, user=User(name="peer"), message="hi", is_direct=True)
        event = PrivateMessageEvent(message=chat, raw_message=raw)
        asyncio.run(worker.client.events.emit(event))
        asyncio.run(worker.client.events.emit(event))
        assert worker.store.unread_private_count() == 1
        assert len(worker.store.private_history("Peer")) == 1
        worker.store.mark_thread_read("peer")
        assert worker.store.unread_private_count() == 0
        threads = worker.store.private_threads()
        assert threads[0]["username"] == "peer"
        assert threads[0]["unread"] == 0
    finally:
        worker.close()


def test_crash_bundle_is_local_and_redacts_secrets(tmp_path: Path):
    assert redact('password = "soulpass"\napi_key = "abc"\nslsk_password = "hide"') == (
        'password = "[redacted]"\napi_key = "[redacted]"\nslsk_password = "[redacted]"'
    )
    log_path = tmp_path / "lidarr-soulseek.log"
    log_path.write_text('password = "soulpass"\nworker started\n', encoding="utf-8")
    config_path = tmp_path / "config.toml"
    config_path.write_text('api_key = "abc123"\nusername = "souluser"\n', encoding="utf-8")
    skipped = write_crash_bundle(
        tmp_path / "crashes",
        version="1.18.0",
        error_text="boom",
        log_path=log_path,
        config_path=config_path,
        enabled=False,
    )
    assert skipped is None
    assert not (tmp_path / "crashes").exists()
    dest = write_crash_bundle(
        tmp_path / "crashes",
        version="1.18.0",
        error_text="ValueError: boom",
        log_path=log_path,
        config_path=config_path,
        enabled=True,
    )
    assert dest is not None and dest.is_file()
    with zipfile.ZipFile(dest) as archive:
        names = set(archive.namelist())
        crash = archive.read("crash.txt").decode("utf-8")
        log_tail = archive.read("log-tail.txt").decode("utf-8")
        config = archive.read("config.toml").decode("utf-8")
    assert names == {"crash.txt", "log-tail.txt", "config.toml"}
    assert "1.18.0" in crash
    assert "not uploaded" in crash
    assert "boom" in crash
    assert "soulpass" not in log_tail
    assert "abc123" not in config
    assert "souluser" in config
    assert "[redacted]" in config


def test_crash_bundle_setting_round_trips(tmp_path: Path):
    cfg = default_config(
        lidarr_api_key="abc123",
        slsk_username="souluser",
        slsk_password="soulpass",
        download_dir=tmp_path / "in",
        complete_dir=tmp_path / "out",
        crash_bundle=True,
    )
    path = tmp_path / "config.toml"
    save_config(path, cfg)
    assert try_load_config(path).crash_bundle is True
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("crash_bundle = true\n", ""), encoding="utf-8")
    assert try_load_config(path).crash_bundle is False


def test_changelog_mentions_messages_and_the_crash_bundle():
    section = changelog_section(Path("CHANGELOG.md").read_text(encoding="utf-8"), "1.18.0")
    assert "Messages" in section
    assert "compose box" in section
    assert "crash bundle" in section
    assert "1.17.0" not in section
