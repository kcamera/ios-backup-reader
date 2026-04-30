"""CSV export — normalized multi-file approach to preserve relational integrity."""

from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import track

from ..backup import Backup, attachment_backup_path


def _dt(v) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v is not None else ""


def export_all(backup: Backup, output: Path, types: set[str], console: Console) -> None:
    if "messages" in types:
        _export_messages(backup, output, console)
    if "contacts" in types:
        _export_contacts(backup, output, console)
    if "notes" in types:
        _export_notes(backup, output, console)
    if "calls" in types:
        _export_calls(backup, output, console)

    console.print(f"[green]Export complete:[/green] {output}")


def _writer(path: Path, fieldnames: list[str]):
    f = open(path, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    return f, writer


# ---------------------------------------------------------------------------
# Messages  (3 files + attachments dir)
# ---------------------------------------------------------------------------

def _export_messages(backup: Backup, output: Path, console: Console) -> None:
    from ..parsers import messages as msg_parser

    with console.status("Loading messages…"):
        chats = msg_parser.load(backup)

    if not chats:
        console.print("[yellow]No messages found — skipping.[/yellow]")
        return

    att_root = output / "attachments"

    chats_f, chats_w = _writer(output / "chats.csv", ["chat_id", "chat_identifier", "display_name", "service", "message_count"])
    msgs_f, msgs_w = _writer(output / "messages.csv", ["message_id", "chat_id", "date", "is_from_me", "handle_id", "text"])
    atts_f, atts_w = _writer(output / "message_attachments.csv", ["attachment_id", "message_id", "chat_id", "filename", "mime_type", "total_bytes", "export_path"])

    try:
        for chat in track(chats, description="Exporting messages…", console=console):
            chats_w.writerow({
                "chat_id": chat.id,
                "chat_identifier": chat.chat_identifier,
                "display_name": chat.display_name,
                "service": chat.service,
                "message_count": chat.message_count,
            })

            for msg in chat.messages:
                msgs_w.writerow({
                    "message_id": msg.id,
                    "chat_id": chat.id,
                    "date": _dt(msg.date),
                    "is_from_me": int(msg.is_from_me),
                    "handle_id": msg.handle_id or "",
                    "text": msg.text or "",
                })

                for att in msg.attachments:
                    domain, rel = attachment_backup_path(att.filename)
                    export_name = Path(att.transfer_name or att.filename).name
                    dest_dir = att_root / str(chat.id)
                    dest_path = dest_dir / export_name
                    src = backup.get_file_path(domain, rel)
                    if src:
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest_path)
                        export_path = str(dest_path.relative_to(output))
                    else:
                        export_path = ""
                    atts_w.writerow({
                        "attachment_id": att.id,
                        "message_id": msg.id,
                        "chat_id": chat.id,
                        "filename": export_name,
                        "mime_type": att.mime_type,
                        "total_bytes": att.total_bytes,
                        "export_path": export_path,
                    })
    finally:
        chats_f.close()
        msgs_f.close()
        atts_f.close()

    console.print(f"  [cyan]chats.csv, messages.csv, message_attachments.csv[/cyan] — {len(chats)} conversation(s)")


# ---------------------------------------------------------------------------
# Contacts  (2 files)
# ---------------------------------------------------------------------------

def _export_contacts(backup: Backup, output: Path, console: Console) -> None:
    from ..parsers import contacts as contact_parser

    with console.status("Loading contacts…"):
        all_contacts = contact_parser.load(backup)

    if not all_contacts:
        console.print("[yellow]No contacts found — skipping.[/yellow]")
        return

    c_f, c_w = _writer(output / "contacts.csv", [
        "contact_id", "first", "last", "middle", "prefix", "suffix",
        "nickname", "organization", "department", "job_title",
        "note", "birthday", "created", "modified",
    ])
    v_f, v_w = _writer(output / "contact_values.csv", [
        "contact_id", "type", "label", "value",
    ])

    try:
        for c in all_contacts:
            c_w.writerow({
                "contact_id": c.id,
                "first": c.first or "",
                "last": c.last or "",
                "middle": c.middle or "",
                "prefix": c.prefix or "",
                "suffix": c.suffix or "",
                "nickname": c.nickname or "",
                "organization": c.organization or "",
                "department": c.department or "",
                "job_title": c.job_title or "",
                "note": c.note or "",
                "birthday": c.birthday or "",
                "created": _dt(c.created),
                "modified": _dt(c.modified),
            })
            for v in c.phones:
                v_w.writerow({"contact_id": c.id, "type": "phone", "label": v.label, "value": v.value})
            for v in c.emails:
                v_w.writerow({"contact_id": c.id, "type": "email", "label": v.label, "value": v.value})
            for v in c.urls:
                v_w.writerow({"contact_id": c.id, "type": "url", "label": v.label, "value": v.value})
    finally:
        c_f.close()
        v_f.close()

    console.print(f"  [cyan]contacts.csv, contact_values.csv[/cyan] — {len(all_contacts)} contact(s)")


# ---------------------------------------------------------------------------
# Notes  (1 file)
# ---------------------------------------------------------------------------

def _export_notes(backup: Backup, output: Path, console: Console) -> None:
    from ..parsers import notes as notes_parser

    with console.status("Loading notes…"):
        all_notes = notes_parser.load(backup)

    if not all_notes:
        console.print("[yellow]No notes found — skipping.[/yellow]")
        return

    f, w = _writer(output / "notes.csv", [
        "note_id", "title", "folder", "created", "modified", "has_rich_content", "body_text",
    ])
    try:
        for n in all_notes:
            w.writerow({
                "note_id": n.id,
                "title": n.title or "",
                "folder": n.folder,
                "created": _dt(n.created),
                "modified": _dt(n.modified),
                "has_rich_content": int(n.has_rich_content),
                "body_text": n.body_text or "",
            })
    finally:
        f.close()

    console.print(f"  [cyan]notes.csv[/cyan] — {len(all_notes)} note(s)")


# ---------------------------------------------------------------------------
# Calls  (1 file)
# ---------------------------------------------------------------------------

def _export_calls(backup: Backup, output: Path, console: Console) -> None:
    from ..parsers import calls as calls_parser

    with console.status("Loading call history…"):
        records = calls_parser.load(backup)

    if not records:
        console.print("[yellow]No call records found — skipping.[/yellow]")
        return

    f, w = _writer(output / "calls.csv", [
        "call_id", "date", "duration_seconds", "address", "originated", "answered", "service",
    ])
    try:
        for r in records:
            w.writerow({
                "call_id": r.id,
                "date": _dt(r.date),
                "duration_seconds": r.duration_seconds,
                "address": r.address,
                "originated": int(r.originated),
                "answered": int(r.answered),
                "service": r.service,
            })
    finally:
        f.close()

    console.print(f"  [cyan]calls.csv[/cyan] — {len(records)} record(s)")
