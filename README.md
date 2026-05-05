# ios-backup-reader

Personal CLI tool for browsing and exporting iPhone backup data — messages (iMessage & SMS),
contacts, notes, and call history. Supports both unencrypted and encrypted backups, and works
with any iOS version from 14 onwards.

---

## Requirements

- **Python 3.11 or later** (`python3 --version` to check)
- pip and a virtual environment are recommended but not required

## Installation

```sh
# 1. Clone the repo
git clone <repo>
cd ios-backup-reader

# 2. Create and activate a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows

# 3. Install the tool and its dependencies
pip install -e .

# 4. If you have encrypted backups (password set in iTunes or Finder),
#    also install the decryption extra:
pip install -e ".[encrypted]"
```

**Dependencies installed by step 3:**

| Package | Version | Purpose |
|---------|---------|---------|
| `click` | ≥ 8.1 | CLI framework |
| `rich` | ≥ 13.0 | Terminal tables and progress bars |
| `prompt_toolkit` | ≥ 3.0 | Interactive path prompt with tab-completion |

**Additional dependency installed by step 4:**

| Package | Version | Purpose |
|---------|---------|---------|
| `iphone-backup-decrypt` | ≥ 0.9 | AES decryption of encrypted iOS backups |
| `pycryptodome` | (auto) | Pulled in transitively by `iphone-backup-decrypt` |

Everything else the tool uses (`sqlite3`, `plistlib`, `gzip`, etc.) is part of the Python
standard library — no extra installs needed.

> **Encrypted backups:** any command that reads data will prompt for the passphrase at runtime.
> The metadata-only `info` command works without it.

---

## Backup locations

Standard iTunes/Finder backup directories:

| Platform | Path |
|----------|------|
| macOS    | `~/Library/Application Support/MobileSync/Backup/<device-id>` |
| Windows  | `%APPDATA%\Apple Computer\MobileSync\Backup\<device-id>` |

Backups can also be copied anywhere (external drives, archived folders). The tool works with
**read-only volumes** — it never writes into the backup directory.

---

## Quick start

```sh
# Show backup info (no passphrase needed even for encrypted backups)
ios-backup-reader --path /path/to/backup info

# List all conversations
ios-backup-reader --path /path/to/backup messages

# Export everything to JSON
ios-backup-reader --path /path/to/backup export --output ./my-export

# Validate the export against the source backup
python validate_export.py --backup /path/to/backup --export ./my-export
```

If you omit `--path`, the tool prompts with tab-completion.

---

## Command reference

All commands share the top-level `--path` option:

```
ios-backup-reader [--path PATH] COMMAND [OPTIONS]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--path PATH` | `-p` | Path to the iOS backup directory. Prompted interactively (with tab-completion) if omitted. |

---

### `info`

Show device metadata. Works without a passphrase even on encrypted backups.

```sh
ios-backup-reader --path /path/to/backup info
```

**Output:** device name, iOS version, phone number, last backup date, encryption status.

---

### `messages`

Browse conversations and message history.

```sh
ios-backup-reader --path /path/to/backup messages [PATTERN] [OPTIONS]
```

| Argument / Option | Description |
|-------------------|-------------|
| `PATTERN` | Optional glob pattern matched against contact name and identifier (e.g. `"*john*"`, `"+1555*"`). Omit to list all conversations. |
| `--search QUERY` / `-s QUERY` | Full-text search across all messages. Cannot be combined with `PATTERN`. |

**Examples:**

```sh
# List all conversations (ID, identifier, display name, service, message count)
ios-backup-reader -p /path/to/backup messages

# Show all messages in conversations matching a glob
ios-backup-reader -p /path/to/backup messages "*alice*"
ios-backup-reader -p /path/to/backup messages "+15551234567"

# Full-text search across every message
ios-backup-reader -p /path/to/backup messages --search "flight confirmation"
```

Messages marked `(deleted)` are recovered from iOS 16+ "Recently Deleted" — they are real
message content that was soft-deleted but not yet purged. Group chat messages include the
sender's name or number.

---

### `contacts`

List and search your address book.

```sh
# List all contacts
ios-backup-reader --path /path/to/backup contacts

# Search by name, phone number, email, or organization
ios-backup-reader --path /path/to/backup contacts search "smith"
ios-backup-reader --path /path/to/backup contacts search "+44"
```

---

### `notes`

List notes and display their body text.

```sh
# List all notes (ID, title, folder, modified date)
ios-backup-reader --path /path/to/backup notes

# Display the full body of a note by ID
ios-backup-reader --path /path/to/backup notes show 7
```

Notes stored only in iCloud (not locally on-device) will not appear — this is correct
behaviour; they are not included in the local backup.

---

### `calls`

List call history.

```sh
ios-backup-reader --path /path/to/backup calls
```

**Output:** date, number, direction (incoming/outgoing), answered status, duration, service
(Phone, FaceTime, etc.).

Call history synced to iCloud is not stored locally and will not appear.

---

### `export`

Export backup data to files, with attachments copied out of the backup.

```sh
ios-backup-reader --path /path/to/backup export [OPTIONS]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--format json\|csv` | `json` | Output format. |
| `--output PATH` / `-o PATH` | `./ios-backup-export` | Output directory (created if it doesn't exist). |
| `--include LIST` | `messages,contacts,notes,calls` | Comma-separated list of data types to export. |

**Examples:**

```sh
# Full JSON export (recommended — lossless, one file per conversation)
ios-backup-reader -p /path/to/backup export --output ./export-json

# Export only messages and contacts
ios-backup-reader -p /path/to/backup export --include messages,contacts --output ./partial

# CSV export (multi-file for messages and contacts — see structure below)
ios-backup-reader -p /path/to/backup export --format csv --output ./export-csv

# Encrypted backup — passphrase is prompted interactively
ios-backup-reader -p /path/to/encrypted-backup export --output ./export
```

#### JSON output structure

```
export/
├── messages/
│   ├── 1.json          # one file per conversation (chat_id)
│   ├── 2.json
│   └── …
├── attachments/
│   ├── 1/              # subdirectory per chat_id
│   │   ├── photo.jpg
│   │   └── FullSizeRender_2.jpg   # _2, _3 … appended on filename collision
│   └── …
├── contacts.json
├── notes.json
└── calls.json
```

Each `messages/<id>.json` contains:

```json
{
  "chat_id": 1,
  "chat_identifier": "+15551234567",
  "display_name": "Alice",
  "service": "iMessage",
  "handles": {"3": "+15551234567"},
  "messages": [
    {
      "id": 456,
      "date": "2023-04-15T14:32:00",
      "is_from_me": true,
      "handle_id": null,
      "text": "Hey!",
      "attachments": [
        {
          "filename": "photo.jpg",
          "mime_type": "image/jpeg",
          "total_bytes": 204800,
          "export_path": "attachments/1/photo.jpg"
        }
      ]
    },
    {
      "id": 789,
      "date": "2023-04-16T09:10:00",
      "is_from_me": false,
      "handle_id": 3,
      "text": "Morning!",
      "attachments": [],
      "is_recovered": true
    }
  ]
}
```

- `handle_id` matches a key in `handles` (note: JSON keys are always strings). `null` for
  outgoing messages.
- `is_recovered: true` appears only on messages recovered from iOS 16+ "Recently Deleted".

#### CSV output structure

```
export/
├── chats.csv                 # one row per conversation
├── messages.csv              # one row per message, chat_id FK
├── message_attachments.csv   # one row per attachment, message_id FK
├── attachments/              # same as JSON
├── contacts.csv              # one row per person
├── contact_values.csv        # phone/email/url rows, contact_id FK
├── notes.csv
└── calls.csv
```

CSV is best for spreadsheet or database import. Use JSON for lossless round-tripping.

---

## Validation

After exporting, run the bundled validator to confirm every message, contact, note, call, and
attachment file matches the source backup databases exactly.

```sh
# Unencrypted backup
python validate_export.py --backup /path/to/backup --export ./my-export

# Encrypted backup (passphrase prompted if --passphrase is omitted)
python validate_export.py --backup /path/to/backup --export ./my-export --passphrase "mypassword"
```

| Option | Short | Description |
|--------|-------|-------------|
| `--backup PATH` | `-b` | Path to iOS backup directory. Required. |
| `--export PATH` | `-e` | Path to the export directory produced by `export`. Required. |
| `--passphrase PW` | `-p` | Backup passphrase for encrypted backups. Prompted if omitted. |

**Checks performed:**

- Chat, message, and attachment counts match `sms.db`
- Recovered (recently deleted) messages match `chat_recoverable_message_join` (iOS 16+)
- Every exported attachment file exists on disk and is non-empty
- Attachments not found in the backup are reported (typically old MMS parts or iCloud-offloaded media)
- Orphan messages (in the DB but not linked to any chat) are classified and reported
- Orphan attachments (in the DB but not linked to any message) are reported
- Contact count matches `AddressBook.sqlitedb`
- Note count matches `NoteStore.sqlite`
- Call count matches `CallHistory.storedata`

**Example output (all passing):**

```
Backup : /path/to/backup
Export : /path/to/export

  PASS  messages: chat count               277
  PASS  messages: message count            38092
  PASS  messages: attachment entries       5804
  PASS  messages: recovered (deleted)…     80
  INFO  messages: attachments w/ path      5798/5804
  PASS  messages: all exported files exist
  WARN  messages: attachments not in backup  6 file(s) — likely old MMS/Parts paths
  PASS  messages: no zero-byte files
  PASS  messages: no orphan messages
  WARN  messages: orphan attachments       443 row(s) in attachment table …
  PASS  contacts: count                    161
  PASS  notes: count                       34
  PASS  calls: count                       492

  All checks passed.
```

`WARN` items are expected: orphan attachments are phantom metadata rows with no backing file,
and missing attachment files are typically media that was never downloaded to the device.

---

## Caveats

- **iCloud-only data is not in the backup.** Notes, call history, photos, or messages that
  were set to store only in iCloud will not appear — the backup contains only on-device data.
- **Encrypted backups decrypt to a private temp directory** that is automatically wiped when
  the process exits (including on crash). No decrypted files are left behind.
- **The backup directory is never modified.** The tool copies databases to temp files before
  opening them, which is required for SQLite on read-only volumes.
- **Attachment filenames are deduplicated.** If multiple attachments share the same
  `transfer_name` (e.g. `FullSizeRender.jpg`), successive copies are renamed `_2`, `_3`, etc.
  to prevent silent overwrites.
