"""Parse sms.db into Chat / Message / Attachment dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..backup import Backup

_DOMAIN = "HomeDomain"
_DB_PATH = "Library/SMS/sms.db"

# iOS stores message dates as nanoseconds since 2001-01-01 (Mac absolute time).
# Pre-iOS 13 some timestamps are in seconds; handle both by magnitude.
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_NS_THRESHOLD = 1_000_000_000  # if value > this, treat as nanoseconds


def _parse_date(raw: int | None) -> Optional[datetime]:
    if raw is None or raw == 0:
        return None
    if abs(raw) > _NS_THRESHOLD:
        seconds = raw / 1_000_000_000
    else:
        seconds = float(raw)
    from datetime import timedelta
    return _MAC_EPOCH + timedelta(seconds=seconds)


@dataclass
class Attachment:
    id: int
    filename: str          # original path in backup (e.g. ~/Library/SMS/Attachments/...)
    transfer_name: str     # display filename
    mime_type: str
    total_bytes: int


@dataclass
class Message:
    id: int
    date: Optional[datetime]
    text: Optional[str]
    is_from_me: bool
    handle_id: Optional[int]
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class Chat:
    id: int
    chat_identifier: str   # phone number, email, or group ID
    display_name: str      # empty string for 1-on-1 chats
    service: str           # iMessage or SMS
    message_count: int = 0
    messages: list[Message] = field(default_factory=list)
    # handle_id → id (phone/email) mapping for received messages
    handles: dict[int, str] = field(default_factory=dict)


def load(backup: Backup) -> list[Chat]:
    db = backup.open_db(_DOMAIN, _DB_PATH)
    if db is None:
        return []

    with db:
        # Load all handles
        handle_map: dict[int, str] = {}
        for row in db.execute("SELECT ROWID, id FROM handle"):
            handle_map[row["ROWID"]] = row["id"]

        # Load all chats
        chats: dict[int, Chat] = {}
        for row in db.execute("SELECT ROWID, chat_identifier, display_name, service_name FROM chat"):
            chats[row["ROWID"]] = Chat(
                id=row["ROWID"],
                chat_identifier=row["chat_identifier"] or "",
                display_name=row["display_name"] or "",
                service=row["service_name"] or "",
                handles={},
            )

        # Map handles to chats
        for row in db.execute("SELECT chat_id, handle_id FROM chat_handle_join"):
            c = chats.get(row["chat_id"])
            if c and row["handle_id"] in handle_map:
                c.handles[row["handle_id"]] = handle_map[row["handle_id"]]

        # Load attachments
        attachment_map: dict[int, Attachment] = {}
        for row in db.execute(
            "SELECT ROWID, filename, transfer_name, mime_type, total_bytes FROM attachment"
        ):
            attachment_map[row["ROWID"]] = Attachment(
                id=row["ROWID"],
                filename=row["filename"] or "",
                transfer_name=row["transfer_name"] or "",
                mime_type=row["mime_type"] or "",
                total_bytes=row["total_bytes"] or 0,
            )

        # message → attachment join
        msg_attachments: dict[int, list[Attachment]] = {}
        for row in db.execute("SELECT message_id, attachment_id FROM message_attachment_join"):
            a = attachment_map.get(row["attachment_id"])
            if a:
                msg_attachments.setdefault(row["message_id"], []).append(a)

        # Load messages
        messages: dict[int, Message] = {}
        for row in db.execute(
            "SELECT ROWID, text, handle_id, is_from_me, date FROM message ORDER BY date"
        ):
            msg = Message(
                id=row["ROWID"],
                date=_parse_date(row["date"]),
                text=row["text"],
                is_from_me=bool(row["is_from_me"]),
                handle_id=row["handle_id"] or None,
                attachments=msg_attachments.get(row["ROWID"], []),
            )
            messages[row["ROWID"]] = msg

        # Map messages to chats
        for row in db.execute(
            "SELECT chat_id, message_id FROM chat_message_join"
        ):
            c = chats.get(row["chat_id"])
            msg = messages.get(row["message_id"])
            if c and msg:
                c.messages.append(msg)

        # Sort messages by date and set message counts
        for c in chats.values():
            c.messages.sort(key=lambda m: m.date or datetime.min.replace(tzinfo=timezone.utc))
            c.message_count = len(c.messages)

    return sorted(chats.values(), key=lambda c: c.chat_identifier)
