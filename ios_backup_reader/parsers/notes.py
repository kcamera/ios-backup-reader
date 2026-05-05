"""Parse NoteStore.sqlite into Note dataclasses."""

from __future__ import annotations

import gzip
import sqlite3
import sys
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..backup import Backup

# Cap recursion depth when scanning protobuf bodies — defensive against
# malformed/adversarial blobs that could otherwise blow the stack.
_MAX_PROTO_DEPTH = 20

# Modern iOS (13+): NoteStore lives in an app group container
_DOMAIN = "AppDomainGroup-group.com.apple.notes"
_DB_PATH = "NoteStore.sqlite"

# Legacy fallback paths (older iOS / unusual configurations)
_LEGACY_CANDIDATES = [
    ("HomeDomain", "Library/Notes/NoteStore.sqlite"),
    ("HomeDomain", "Library/Notes/notes.sqlite"),
]

# Notes dates: seconds since 2001-01-01
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _parse_date(raw: float | None) -> Optional[datetime]:
    if raw is None:
        return None
    from datetime import timedelta
    return _MAC_EPOCH + timedelta(seconds=raw)


def _scan_proto_text(data: bytes, text_runs: list[str], depth: int = 0) -> None:
    """
    Recursively scan a protobuf-encoded byte string for length-delimited fields,
    collecting any chunks that decode as mostly-printable UTF-8.
    Notes body is a nested protobuf so we must recurse into sub-messages.

    Recursion is capped at _MAX_PROTO_DEPTH to guard against malformed input.
    """
    if depth > _MAX_PROTO_DEPTH:
        return

    i = 0
    while i < len(data) - 1:
        byte = data[i]
        wire_type = byte & 0x07
        i += 1  # consume tag byte

        if wire_type == 0:
            # Varint — skip it (up to 10 bytes)
            for _ in range(10):
                if i >= len(data):
                    break
                b = data[i]
                i += 1
                if not (b & 0x80):
                    break

        elif wire_type == 2:
            # Length-delimited: could be a string or a nested message
            length = 0
            shift = 0
            while i < len(data):
                b = data[i]
                length |= (b & 0x7F) << shift
                i += 1
                if not (b & 0x80):
                    break
                shift += 7

            if length <= 0 or i + length > len(data):
                break

            chunk = data[i:i + length]
            i += length

            # Try to decode as UTF-8 text first
            try:
                text = chunk.decode("utf-8")
                printable = sum(1 for c in text if c.isprintable() or c in "\n\t")
                if printable > len(text) * 0.85 and len(text.strip()) > 1:
                    text_runs.append(text.strip())
                    continue
            except Exception:
                pass

            # Not valid UTF-8 — recurse as a nested protobuf message
            if length > 2:
                _scan_proto_text(chunk, text_runs, depth + 1)

        elif wire_type == 5:
            i += 4  # 32-bit fixed
        elif wire_type == 1:
            i += 8  # 64-bit fixed
        else:
            # Unknown wire type — can't continue safely
            break


def _extract_text(blob: bytes | None) -> tuple[str, bool]:
    """
    Try to extract plain text from a note body blob.
    Notes are stored as gzip-compressed protobuf. We decompress then recursively
    scan for UTF-8 string fields (best-effort, no schema required).
    Returns (text, has_rich_content).
    """
    if not blob:
        return "", False

    # Try gzip decompress
    try:
        data = gzip.decompress(blob)
    except Exception:
        try:
            data = zlib.decompress(blob)
        except Exception:
            data = bytes(blob) if not isinstance(blob, bytes) else blob

    text_runs: list[str] = []
    _scan_proto_text(data, text_runs)

    # Deduplicate and take the longest coherent run as the body
    if text_runs:
        # The note body is usually the longest string
        body = max(text_runs, key=len)
        has_rich = any(r != body for r in text_runs if len(r) > 10)
        return body, has_rich

    return "", True  # couldn't decode → mark as rich content


@dataclass
class Note:
    id: int
    title: str
    folder: str
    created: Optional[datetime]
    modified: Optional[datetime]
    body_text: str
    has_rich_content: bool


def load(backup: Backup) -> list[Note]:
    db = backup.open_db(_DOMAIN, _DB_PATH)
    if db is None:
        for domain, path in _LEGACY_CANDIDATES:
            db = backup.open_db(domain, path)
            if db is not None:
                break
    if db is None:
        return []

    notes: list[Note] = []
    with db:
        # Folder names — schema mismatch is expected on the legacy notes.sqlite
        # path; other errors should surface.
        folder_map: dict[int, str] = {}
        try:
            for row in db.execute(
                "SELECT Z_PK, ZTITLE2 FROM ZICCLOUDSYNCINGOBJECT "
                "WHERE ZTITLE2 IS NOT NULL AND ZNOTE IS NULL"
            ):
                folder_map[row[0]] = row[1]
        except sqlite3.Error:
            # OperationalError = schema mismatch; DatabaseError = truncated/corrupt file.
            # Either way, continue — folder names are cosmetic.
            pass

        # Try modern schema (iOS 13+): body is in ZICNOTEDATA joined by ZNOTE FK
        modern_rows = None
        try:
            modern_rows = list(db.execute(
                """SELECT o.Z_PK, o.ZTITLE1, o.ZFOLDER, o.ZCREATIONDATE1, o.ZMODIFICATIONDATE1,
                          nd.ZDATA
                   FROM ZICCLOUDSYNCINGOBJECT o
                   LEFT JOIN ZICNOTEDATA nd ON nd.ZNOTE = o.Z_PK
                   WHERE o.ZNOTE IS NULL AND o.ZTITLE1 IS NOT NULL
                   ORDER BY o.ZMODIFICATIONDATE1 DESC"""
            ))
        except sqlite3.Error as e:
            # OperationalError = schema mismatch → try legacy path.
            # DatabaseError = malformed/truncated file → also fall through (returns []).
            print(f"notes: modern schema query failed ({type(e).__name__}: {e})", file=sys.stderr)
            modern_rows = None

        if modern_rows is not None:
            for row in modern_rows:
                # Skip individual bad rows but keep going. Don't silently
                # abort the whole load on one corrupt entry.
                try:
                    body, rich = _extract_text(row["ZDATA"])
                    notes.append(Note(
                        id=row["Z_PK"],
                        title=row["ZTITLE1"] or "",
                        folder=folder_map.get(row["ZFOLDER"], "Notes"),
                        created=_parse_date(row["ZCREATIONDATE1"]),
                        modified=_parse_date(row["ZMODIFICATIONDATE1"]),
                        body_text=body,
                        has_rich_content=rich,
                    ))
                except Exception as e:
                    print(
                        f"notes: skipping row Z_PK={row['Z_PK']}: {e}",
                        file=sys.stderr,
                    )
        else:
            # Legacy schema (pre-iOS 9 notes.sqlite)
            try:
                for row in db.execute(
                    """SELECT n.Z_PK, n.ZTITLE, n.ZCREATIONDATE, n.ZMODIFICATIONDATE, b.ZCONTENT
                       FROM ZNOTE n LEFT JOIN ZNOTEBODY b ON b.ZNOTE = n.Z_PK
                       ORDER BY n.ZMODIFICATIONDATE DESC"""
                ):
                    body = row["ZCONTENT"] or ""
                    notes.append(Note(
                        id=row["Z_PK"],
                        title=row["ZTITLE"] or "",
                        folder="Notes",
                        created=_parse_date(row["ZCREATIONDATE"]),
                        modified=_parse_date(row["ZMODIFICATIONDATE"]),
                        body_text=body,
                        has_rich_content=False,
                    ))
            except sqlite3.Error as e:
                # Neither schema matched — surface a hint to the user.
                print(
                    f"notes: no compatible schema in NoteStore.sqlite ({type(e).__name__}: {e})",
                    file=sys.stderr,
                )

    return notes
