"""Private Soulseek messages.

Chat rooms stay off. A message is sent only when the compose box asks for it.
"""

from __future__ import annotations

MAX_PRIVATE_MESSAGE = 2000


def compose_text(raw: str) -> str:
    """The text that would be sent. Blank means nothing is sent."""
    return (raw or "").strip()


def message_is_sendable(raw: str) -> bool:
    text = compose_text(raw)
    return bool(text) and len(text) <= MAX_PRIVATE_MESSAGE


def rooms_are_off(settings) -> bool:
    """aioslsk joins favorite rooms only when auto_join is false.

    Leaving auto_join on, with an empty favorite list and room invites declined,
    keeps this app out of rooms.
    """
    rooms = settings.rooms
    return bool(rooms.auto_join) and not bool(rooms.private_room_invites) and not set(rooms.favorites)
