#!/usr/bin/env python3
"""
Validate a ios-backup-reader JSON export against the source backup databases.

Checks:
  - Chat, message, and attachment counts match sms.db
  - Recoverable (recently deleted) messages are accounted for
  - Orphan messages and attachments are reported
  - Every exported attachment file exists on disk and is non-empty
  - Contact count matches AddressBook.sqlitedb
  - Note count matches NoteStore.sqlite
  - Call count matches CallHistory.storedata

Usage:
  python3 validate_export.py --backup <path> --export <path>
  python3 validate_export.py --backup <path> --export <path> --passphrase <pw>
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

# Colour codes (disabled automatically if not a TTY)
_TTY = sys.stdout.isatty()
_C = {
    "PASS": "\033[32m" if _TTY else "",
    "FAIL": "\033[31m" if _TTY else "",
    "WARN": "\033[33m" if _TTY else "",
    "SKIP": "\033[90m" if _TTY else "",
    "INFO": "\033[36m" if _TTY else "",
    "RST":  "\033[0m"  if _TTY else "",
}


# ---------------------------------------------------------------------------
# Result accumulator
# ---------------------------------------------------------------------------

class Results:
    def __init__(self):
        self._rows: list[tuple[str, str, str]] = []

    def add(self, status: str, label: str, detail: str = "") -> None:
        self._rows.append((status, label, detail))

    def check(self, label: str, expected, actual) -> None:
        if expected == actual:
            self.add("PASS", label, str(actual))
        else:
            self.add("FAIL", label, f"expected {expected}, got {actual}")

    def print_summary(self) -> int:
        """Print all rows; return number of FAILs."""
        w = max((len(r[1]) for r in self._rows if r[1]), default=0) + 2
        print()
        for status, label, detail in self._rows:
            c, rst = _C.get(status, ""), _C["RST"]
            if label:
                print(f"  {c}{status:<4}{rst}  {label:<{w}}  {detail}")
            else:
                print(f"              {detail}")  # continuation / indent
        print()

        fails = sum(1 for s, _, _ in self._rows if s == "FAIL")
        if fails:
            print(f"  {_C['FAIL']}{fails} check(s) FAILED{_C['RST']}\n")
        else:
            print(f"  {_C['PASS']}All checks passed.{_C['RST']}\n")
        return fails


# ---------------------------------------------------------------------------
# Backup helpers
# ---------------------------------------------------------------------------

def open_backup(backup_path: Path, passphrase: str | None):
    sys.path.insert(0, str(Path(__file__).parent))
    from ios_backup_reader.backup import Backup, DecryptedBackup, BackupError

    try:
        backup = Backup(backup_path)
    except BackupError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if backup.is_encrypted():
        try:
            import iphone_backup_decrypt  # noqa: F401
        except ImportError:
            print('Error: pip install "ios-backup-reader[encrypted]"', file=sys.stderr)
            sys.exit(1)

        backup.close()  # close the lightweight instance before creating decrypted one
        pw = passphrase or getpass.getpass("Backup passphrase: ")
        try:
            backup = DecryptedBackup(backup_path, pw)
        except BackupError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

    return backup


# ---------------------------------------------------------------------------
# Per-type checks
# ---------------------------------------------------------------------------

def check_messages(backup, export: Path, r: Results) -> None:
    db = backup.open_db("HomeDomain", "Library/SMS/sms.db")
    if db is None:
        r.add("SKIP", "messages", "sms.db not found in backup")
        return

    with db:
        db_chats = db.execute("SELECT COUNT(*) FROM chat").fetchone()[0]

        # COUNT(*) not COUNT(DISTINCT): the same message can appear in two
        # chats (group threads), matching how the parser counts them.
        db_msgs = db.execute("SELECT COUNT(*) FROM chat_message_join").fetchone()[0]

        # Check for chat_recoverable_message_join (iOS 16+)
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        db_recovered_msgs = 0
        if "chat_recoverable_message_join" in tables:
            db_recovered_msgs = db.execute(
                "SELECT COUNT(*) FROM chat_recoverable_message_join"
            ).fetchone()[0]

        # Count only attachments on messages that are actually exportable
        # (in chat_message_join or chat_recoverable_message_join), not orphans.
        db_atts = db.execute(
            "SELECT COUNT(*) FROM message_attachment_join "
            "WHERE message_id IN (SELECT message_id FROM chat_message_join)"
        ).fetchone()[0]
        if db_recovered_msgs > 0:
            db_atts += db.execute(
                "SELECT COUNT(*) FROM message_attachment_join "
                "WHERE message_id IN (SELECT message_id FROM chat_recoverable_message_join)"
            ).fetchone()[0]

        # Orphan analysis: messages in message table but not in any join table
        total_in_message_table = db.execute("SELECT COUNT(*) FROM message").fetchone()[0]
        joined_distinct = db.execute(
            "SELECT COUNT(DISTINCT message_id) FROM chat_message_join"
        ).fetchone()[0]
        recovered_distinct = 0
        if db_recovered_msgs > 0:
            recovered_distinct = db.execute(
                "SELECT COUNT(DISTINCT message_id) FROM chat_recoverable_message_join"
            ).fetchone()[0]

        orphan_total = total_in_message_table - joined_distinct - recovered_distinct
        orphan_with_text = 0
        orphan_system = 0
        if orphan_total > 0:
            orphan_with_text = db.execute(
                "SELECT COUNT(*) FROM message "
                "WHERE ROWID NOT IN (SELECT message_id FROM chat_message_join) "
                + ("AND ROWID NOT IN (SELECT message_id FROM chat_recoverable_message_join) "
                   if db_recovered_msgs > 0 else "")
                + "AND text IS NOT NULL AND text != ''"
            ).fetchone()[0]
            orphan_system = db.execute(
                "SELECT COUNT(*) FROM message "
                "WHERE ROWID NOT IN (SELECT message_id FROM chat_message_join) "
                + ("AND ROWID NOT IN (SELECT message_id FROM chat_recoverable_message_join) "
                   if db_recovered_msgs > 0 else "")
                + "AND item_type = 1"
            ).fetchone()[0]

        # Orphan attachments: in attachment table but not in message_attachment_join
        total_in_att_table = db.execute("SELECT COUNT(*) FROM attachment").fetchone()[0]
        att_joined_distinct = db.execute(
            "SELECT COUNT(DISTINCT attachment_id) FROM message_attachment_join"
        ).fetchone()[0]
        orphan_atts = total_in_att_table - att_joined_distinct

    msg_dir = export / "messages"
    if not msg_dir.exists():
        r.add("SKIP", "messages", "messages/ directory not in export")
        return

    chat_files = list(msg_dir.glob("*.json"))
    r.check("messages: chat count", db_chats, len(chat_files))

    # Walk every chat JSON once and accumulate all stats
    export_msgs = 0
    export_recovered = 0
    export_atts = 0
    missing: list[str] = []
    zero_byte: list[str] = []
    null_path_atts: list[str] = []
    exported_with_path = 0

    for f in chat_files:
        data = json.loads(f.read_text(encoding="utf-8"))
        for msg in data.get("messages", []):
            export_msgs += 1
            if msg.get("is_recovered"):
                export_recovered += 1
            for att in msg.get("attachments", []):
                export_atts += 1
                ep = att.get("export_path")
                if ep:
                    exported_with_path += 1
                    dest = export / ep
                    if not dest.exists():
                        missing.append(ep)
                    elif dest.stat().st_size == 0:
                        zero_byte.append(ep)
                else:
                    null_path_atts.append(
                        f"{att.get('filename', '?')}  [{att.get('mime_type', '?')}]"
                    )

    # Expected total = chat_message_join + chat_recoverable_message_join
    expected_msgs = db_msgs + db_recovered_msgs
    r.check("messages: message count", expected_msgs, export_msgs)
    r.check("messages: attachment entries", db_atts, export_atts)

    if db_recovered_msgs > 0:
        r.check("messages: recovered (deleted) messages", db_recovered_msgs, export_recovered)
    elif export_recovered > 0:
        r.add("WARN", "messages: export has recovered messages but DB table absent",
              str(export_recovered))

    # Attachment file integrity
    r.add("INFO", "messages: attachments with export_path",
          f"{exported_with_path}/{export_atts}")

    if missing:
        r.add("FAIL", "messages: missing exported attachment files",
              f"{len(missing)} file(s)")
        for p in missing[:5]:
            r.add("", "", f"  {p}")
        if len(missing) > 5:
            r.add("", "", f"  … and {len(missing) - 5} more")
    else:
        r.add("PASS", "messages: all exported attachment files exist")

    # Attachments with null export_path were not found in the backup at all.
    if null_path_atts:
        r.add("WARN", "messages: attachments not found in backup",
              f"{len(null_path_atts)} file(s) — likely old MMS/Parts paths")
        for name in null_path_atts[:10]:
            r.add("", "", f"  {name}")
        if len(null_path_atts) > 10:
            r.add("", "", f"  … and {len(null_path_atts) - 10} more")
    else:
        r.add("PASS", "messages: all attachment files resolved from backup")

    if zero_byte:
        r.add("FAIL", "messages: zero-byte attachment files",
              f"{len(zero_byte)} file(s)")
        for p in zero_byte[:5]:
            r.add("", "", f"  {p}")
    else:
        r.add("PASS", "messages: no zero-byte attachment files")

    # Orphan report — messages and attachments in DB but not exportable
    if orphan_total > 0:
        detail = f"{orphan_total} message(s)"
        parts = []
        if orphan_system > 0:
            parts.append(f"{orphan_system} system events")
        if orphan_with_text > 0:
            parts.append(f"{orphan_with_text} with text")
        remainder = orphan_total - orphan_system - orphan_with_text
        if remainder > 0:
            parts.append(f"{remainder} empty/other")
        if parts:
            detail += f" ({', '.join(parts)})"
        r.add("WARN", "messages: orphan messages (no chat association)", detail)
    else:
        r.add("PASS", "messages: no orphan messages")

    if orphan_atts > 0:
        r.add("WARN", "messages: orphan attachments (no message association)",
              f"{orphan_atts} row(s) in attachment table not referenced by any message")
    else:
        r.add("PASS", "messages: no orphan attachments")


def check_contacts(backup, export: Path, r: Results) -> None:
    db = backup.open_db("HomeDomain", "Library/AddressBook/AddressBook.sqlitedb")
    if db is None:
        r.add("SKIP", "contacts", "AddressBook.sqlitedb not found")
        return

    with db:
        db_count = db.execute("SELECT COUNT(*) FROM ABPerson").fetchone()[0]

    f = export / "contacts.json"
    if not f.exists():
        r.add("SKIP", "contacts", "contacts.json not in export")
        return

    r.check("contacts: count", db_count, len(json.loads(f.read_text(encoding="utf-8"))))


def check_notes(backup, export: Path, r: Results) -> None:
    import sqlite3
    db = backup.open_db("AppDomainGroup-group.com.apple.notes", "NoteStore.sqlite")
    if db is None:
        r.add("SKIP", "notes", "NoteStore.sqlite not found")
        return

    with db:
        try:
            db_count = db.execute(
                "SELECT COUNT(*) FROM ZICCLOUDSYNCINGOBJECT "
                "WHERE ZNOTE IS NULL AND ZTITLE1 IS NOT NULL"
            ).fetchone()[0]
        except sqlite3.Error as e:
            r.add("WARN", "notes", f"could not query NoteStore ({e})")
            return

    f = export / "notes.json"
    if not f.exists():
        if db_count == 0:
            r.add("SKIP", "notes", "no local notes in backup and notes.json absent — correct")
        else:
            r.add("FAIL", "notes", f"notes.json missing but backup has {db_count} note(s)")
        return

    r.check("notes: count", db_count, len(json.loads(f.read_text(encoding="utf-8"))))


def check_calls(backup, export: Path, r: Results) -> None:
    import sqlite3
    db = backup.open_db("HomeDomain", "Library/CallHistoryDB/CallHistory.storedata")
    if db is None:
        r.add("SKIP", "calls", "CallHistory.storedata not found (iCloud-synced)")
        return

    with db:
        try:
            db_count = db.execute("SELECT COUNT(*) FROM ZCALLRECORD").fetchone()[0]
        except sqlite3.Error as e:
            r.add("WARN", "calls", f"could not query ZCALLRECORD ({e})")
            return

    f = export / "calls.json"
    if not f.exists():
        r.add("SKIP", "calls", "calls.json not in export")
        return

    r.check("calls: count", db_count, len(json.loads(f.read_text(encoding="utf-8"))))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate a ios-backup-reader JSON export against the source backup."
    )
    parser.add_argument("--backup", "-b", required=True, type=Path,
                        help="Path to iOS backup directory")
    parser.add_argument("--export", "-e", required=True, type=Path,
                        help="Path to JSON export directory")
    parser.add_argument("--passphrase", "-p", default=None,
                        help="Backup passphrase (prompted if encrypted and omitted)")
    args = parser.parse_args()

    if not args.export.is_dir():
        print(f"Error: export directory not found: {args.export}", file=sys.stderr)
        sys.exit(1)

    print(f"\nBackup : {args.backup}")
    print(f"Export : {args.export}")

    backup = open_backup(args.backup, args.passphrase)
    r = Results()

    check_messages(backup, args.export, r)
    check_contacts(backup, args.export, r)
    check_notes(backup, args.export, r)
    check_calls(backup, args.export, r)

    backup.close()
    sys.exit(r.print_summary())


if __name__ == "__main__":
    main()
