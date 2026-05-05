"""JSON export — lossless for all data types."""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from ..backup import Backup, attachment_backup_path


def _dt(v) -> str | None:
    if isinstance(v, datetime):
        return v.isoformat()
    return v


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


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _export_messages(backup: Backup, output: Path, console: Console) -> None:
    from ..parsers import messages as msg_parser

    with console.status("Loading messages…"):
        chats = msg_parser.load(backup)

    if not chats:
        console.print("[yellow]No messages found — skipping.[/yellow]")
        return

    msg_dir = output / "messages"
    msg_dir.mkdir(parents=True, exist_ok=True)
    att_root = output / "attachments"

    total_atts = sum(len(msg.attachments) for chat in chats for msg in chat.messages)

    # Track per-attachment so the bar advances on every decrypt/copy, not per-chat.
    # Omit TimeRemainingColumn entirely — ETA is meaningless when item cost varies.
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            f"Exporting attachments…",
            total=total_atts if total_atts else 1,
        )

        for chat in chats:
            messages_out = []
            used_names: set[str] = set()  # track filenames per chat to avoid collisions
            for msg in chat.messages:
                atts_out = []
                for att in msg.attachments:
                    domain, rel = attachment_backup_path(att.filename)
                    export_name = Path(att.transfer_name or att.filename).name
                    # Deduplicate: append _2, _3, etc. if name already used in this chat
                    if export_name in used_names:
                        stem = Path(export_name).stem
                        suffix = Path(export_name).suffix
                        counter = 2
                        while f"{stem}_{counter}{suffix}" in used_names:
                            counter += 1
                        export_name = f"{stem}_{counter}{suffix}"
                    used_names.add(export_name)
                    dest_dir = att_root / str(chat.id)
                    dest_path = dest_dir / export_name
                    src = backup.get_file_path(domain, rel)
                    if src:
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dest_path)
                        backup.reclaim(src)  # free temp space on encrypted backups
                        export_path = str(dest_path.relative_to(output))
                    else:
                        export_path = None
                    atts_out.append({
                        "filename": export_name,
                        "mime_type": att.mime_type,
                        "total_bytes": att.total_bytes,
                        "export_path": export_path,
                    })
                    progress.advance(task)

                msg_dict = {
                    "id": msg.id,
                    "date": _dt(msg.date),
                    "is_from_me": msg.is_from_me,
                    "handle_id": msg.handle_id,
                    "text": msg.text,
                    "attachments": atts_out,
                }
                if msg.is_recovered:
                    msg_dict["is_recovered"] = True
                messages_out.append(msg_dict)

            payload = {
                "chat_id": chat.id,
                "chat_identifier": chat.chat_identifier,
                "display_name": chat.display_name,
                "service": chat.service,
                "handles": chat.handles,
                "messages": messages_out,
            }
            out_file = msg_dir / f"{chat.id}.json"
            out_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    console.print(f"  [cyan]messages/[/cyan] — {len(chats)} conversation(s), {total_atts} attachment(s)")


# ---------------------------------------------------------------------------
# Contacts
# ---------------------------------------------------------------------------

def _export_contacts(backup: Backup, output: Path, console: Console) -> None:
    from ..parsers import contacts as contact_parser

    with console.status("Loading contacts…"):
        all_contacts = contact_parser.load(backup)

    if not all_contacts:
        console.print("[yellow]No contacts found — skipping.[/yellow]")
        return

    data = []
    for c in all_contacts:
        data.append({
            "id": c.id,
            "first": c.first,
            "last": c.last,
            "middle": c.middle,
            "prefix": c.prefix,
            "suffix": c.suffix,
            "nickname": c.nickname,
            "organization": c.organization,
            "department": c.department,
            "job_title": c.job_title,
            "note": c.note,
            "birthday": c.birthday,
            "created": _dt(c.created),
            "modified": _dt(c.modified),
            "phones": [{"label": v.label, "value": v.value} for v in c.phones],
            "emails": [{"label": v.label, "value": v.value} for v in c.emails],
            "urls": [{"label": v.label, "value": v.value} for v in c.urls],
        })

    out_file = output / "contacts.json"
    out_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"  [cyan]contacts.json[/cyan] — {len(data)} contact(s)")


# ---------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------

def _export_notes(backup: Backup, output: Path, console: Console) -> None:
    from ..parsers import notes as notes_parser

    with console.status("Loading notes…"):
        all_notes = notes_parser.load(backup)

    if not all_notes:
        console.print("[yellow]No notes found — skipping.[/yellow]")
        return

    data = [
        {
            "id": n.id,
            "title": n.title,
            "folder": n.folder,
            "created": _dt(n.created),
            "modified": _dt(n.modified),
            "body_text": n.body_text,
            "has_rich_content": n.has_rich_content,
        }
        for n in all_notes
    ]

    out_file = output / "notes.json"
    out_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"  [cyan]notes.json[/cyan] — {len(data)} note(s)")


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------

def _export_calls(backup: Backup, output: Path, console: Console) -> None:
    from ..parsers import calls as calls_parser

    with console.status("Loading call history…"):
        records = calls_parser.load(backup)

    if not records:
        console.print("[yellow]No call records found — skipping.[/yellow]")
        return

    data = [
        {
            "id": r.id,
            "date": _dt(r.date),
            "duration_seconds": r.duration_seconds,
            "address": r.address,
            "originated": r.originated,
            "answered": r.answered,
            "service": r.service,
        }
        for r in records
    ]

    out_file = output / "calls.json"
    out_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"  [cyan]calls.json[/cyan] — {len(data)} record(s)")
