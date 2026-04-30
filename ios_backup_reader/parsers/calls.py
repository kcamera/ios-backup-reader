"""Parse CallHistory.storedata into CallRecord dataclasses."""

from __future__ import annotations

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
        for row in db.execute(
            """SELECT ROWID, ZDATE, ZDURATION, ZADDRESS, ZORIGINATED, ZANSWERED, ZSERVICE_PROVIDER
               FROM ZCALLRECORD
               ORDER BY ZDATE DESC"""
        ):
            records.append(CallRecord(
                id=row["ROWID"],
                date=_parse_date(row["ZDATE"]),
                duration_seconds=row["ZDURATION"] or 0.0,
                address=row["ZADDRESS"] or "",
                originated=bool(row["ZORIGINATED"]),
                answered=bool(row["ZANSWERED"]),
                service=row["ZSERVICE_PROVIDER"] or "Phone",
            ))

    return records
