"""Parse AddressBook.sqlitedb into Contact dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..backup import Backup

_DOMAIN = "HomeDomain"
_DB_PATH = "Library/AddressBook/AddressBook.sqlitedb"

# AddressBook dates are seconds since 2001-01-01 (same Mac absolute time as messages)
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _parse_date(raw: float | None) -> Optional[datetime]:
    if raw is None:
        return None
    from datetime import timedelta
    return _MAC_EPOCH + timedelta(seconds=raw)


@dataclass
class ContactValue:
    label: str
    value: str


@dataclass
class Contact:
    id: int
    first: Optional[str]
    last: Optional[str]
    middle: Optional[str]
    prefix: Optional[str]
    suffix: Optional[str]
    nickname: Optional[str]
    organization: Optional[str]
    department: Optional[str]
    job_title: Optional[str]
    note: Optional[str]
    birthday: Optional[str]
    created: Optional[datetime]
    modified: Optional[datetime]
    phones: list[ContactValue] = field(default_factory=list)
    emails: list[ContactValue] = field(default_factory=list)
    urls: list[ContactValue] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.first, self.middle, self.last) if p]
        if parts:
            return " ".join(parts)
        return self.organization or self.nickname or f"<Contact {self.id}>"


# ABMultiValue property codes
_PHONE = 3
_EMAIL = 4
_URL = 22


def load(backup: Backup) -> list[Contact]:
    db = backup.open_db(_DOMAIN, _DB_PATH)
    if db is None:
        return []

    with db:
        # label lookup
        label_map: dict[int, str] = {}
        try:
            for row in db.execute("SELECT ROWID, value FROM ABMultiValueLabel"):
                label_map[row["ROWID"]] = row["value"] or ""
        except Exception:
            pass

        # multi-values per person
        phones: dict[int, list[ContactValue]] = {}
        emails: dict[int, list[ContactValue]] = {}
        urls: dict[int, list[ContactValue]] = {}

        for row in db.execute(
            "SELECT record_id, property, label, value FROM ABMultiValue WHERE value IS NOT NULL"
        ):
            label_str = label_map.get(row["label"], "") if row["label"] else ""
            # strip leading underscore from Apple label keys (e.g. _$!<Mobile>!$_)
            label_clean = label_str.strip("_$!<>").split("!")[0] or label_str
            cv = ContactValue(label=label_clean, value=row["value"])
            prop = row["property"]
            rid = row["record_id"]
            if prop == _PHONE:
                phones.setdefault(rid, []).append(cv)
            elif prop == _EMAIL:
                emails.setdefault(rid, []).append(cv)
            elif prop == _URL:
                urls.setdefault(rid, []).append(cv)

        contacts: list[Contact] = []
        for row in db.execute(
            """SELECT ROWID, First, Last, Middle, Prefix, Suffix, Nickname,
                      Organization, Department, JobTitle, Note, Birthday,
                      CreationDate, ModificationDate
               FROM ABPerson"""
        ):
            rid = row["ROWID"]
            birthday = None
            if row["Birthday"]:
                try:
                    bd = _parse_date(row["Birthday"])
                    birthday = bd.strftime("%Y-%m-%d") if bd else None
                except Exception:
                    pass

            contacts.append(Contact(
                id=rid,
                first=row["First"],
                last=row["Last"],
                middle=row["Middle"],
                prefix=row["Prefix"],
                suffix=row["Suffix"],
                nickname=row["Nickname"],
                organization=row["Organization"],
                department=row["Department"],
                job_title=row["JobTitle"],
                note=row["Note"],
                birthday=birthday,
                created=_parse_date(row["CreationDate"]),
                modified=_parse_date(row["ModificationDate"]),
                phones=phones.get(rid, []),
                emails=emails.get(rid, []),
                urls=urls.get(rid, []),
            ))

    contacts.sort(key=lambda c: (c.last or "", c.first or "", c.organization or ""))
    return contacts
