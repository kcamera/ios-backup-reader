"""Parse CallHistory.storedata into CallRecord dataclasses."""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..backup import Backup

_DOMAIN = "HomeDomain"
_DB_PATH = "Library/CallHistoryDB/CallHistory.storedata"

# CallHistory uses Mac absolute time (seconds since 2001-01-01), same as Messages
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _parse_date(raw: float | None) -> Optional[datetime]:
    if raw is None:
        return None
    from datetime import timedelta
    return _MAC_EPOCH + timedelta(seconds=raw)


@dataclass
class CallRecord:
    id: int
    date: Optional[datetime]
    duration_seconds: float
    address: str
    originated: bool    # True = outgoing, False = incoming
    answered: bool
    service: str        # "Phone", "FaceTime", etc.


def load(backup: Backup) -> list[CallRecord]:
    db = backup.open_db(_DOMAIN, _DB_PATH)
    if db is None:
        return []

    records: list[CallRecord] = []
    with db:
        # Probe the actual schema — iOS versions differ in column names.
        try:
            cols = {row[1] for row in db.execute("PRAGMA table_info(ZCALLRECORD)")}
        except sqlite3.OperationalError:
            return []

        if not cols:
            return []

        # Primary key: Core Data uses Z_PK; some builds only expose ROWID.
        if "Z_PK" in cols:
            pk_expr = "Z_PK AS call_id"
        else:
            # Alias ROWID so sqlite3.Row can look it up by a stable name.
            pk_expr = "ROWID AS call_id"

        # Service column was renamed across iOS versions.
        if "ZSERVICE_PROVIDER" in cols:
            svc_expr = "ZSERVICE_PROVIDER AS svc"
        elif "ZSERVICE" in cols:
            svc_expr = "ZSERVICE AS svc"
        else:
            svc_expr = "NULL AS svc"

        try:
            rows = list(db.execute(
                f"SELECT {pk_expr}, ZDATE, ZDURATION, ZADDRESS, "
                f"ZORIGINATED, ZANSWERED, {svc_expr} "
                "FROM ZCALLRECORD ORDER BY ZDATE DESC"
            ))
        except sqlite3.OperationalError as e:
            print(f"calls: unable to query ZCALLRECORD: {e}", file=sys.stderr)
            return []

        for i, row in enumerate(rows):
            try:
                raw_addr = row["ZADDRESS"]
                if isinstance(raw_addr, (bytes, bytearray)):
                    raw_addr = raw_addr.decode("utf-8", errors="replace")
                raw_svc = row["svc"]
                if isinstance(raw_svc, (bytes, bytearray)):
                    raw_svc = raw_svc.decode("utf-8", errors="replace")
                records.append(CallRecord(
                    id=row["call_id"],
                    date=_parse_date(row["ZDATE"]),
                    duration_seconds=row["ZDURATION"] or 0.0,
                    address=raw_addr or "",
                    originated=bool(row["ZORIGINATED"]),
                    answered=bool(row["ZANSWERED"]),
                    service=raw_svc or "Phone",
                ))
            except Exception as e:
                print(f"calls: skipping row {i}: {e}", file=sys.stderr)

    return records
