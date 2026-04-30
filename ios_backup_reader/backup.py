"""Core backup access: Manifest.db lookup and file extraction."""

from __future__ import annotations

import plistlib
import shutil
import sqlite3
import tempfile
import weakref
from pathlib import Path


class BackupError(Exception):
    pass


# Manifest.db `flags` column values: 1=file, 2=directory, 4=symlink.
# We only want files when resolving a logical path → on-disk hash.
_FLAG_FILE = 1


class _TempDB:
    """sqlite3 connection backed by a temporary copy of a backup database."""

    def __init__(self, conn: sqlite3.Connection, tmp_path: str) -> None:
        self._conn = conn
        self._tmp_path = tmp_path

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self._conn.close()
        Path(self._tmp_path).unlink(missing_ok=True)


def _open_db_from_path(src: Path) -> _TempDB:
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp.close()
    shutil.copy2(src, tmp.name)
    conn = sqlite3.connect(tmp.name)
    conn.row_factory = sqlite3.Row
    return _TempDB(conn, tmp.name)


def _cleanup_manifest(state: dict) -> None:
    """Finalizer-safe cleanup of the cached Manifest.db connection + temp file.

    Captures only `state` (a plain dict) so the finalizer doesn't keep the
    Backup instance alive. Called by weakref.finalize when Backup is GC'd
    or by Backup.close() explicitly.
    """
    conn = state.get("conn")
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        state["conn"] = None
    tmp = state.get("tmp")
    if tmp is not None:
        Path(tmp).unlink(missing_ok=True)
        state["tmp"] = None


class Backup:
    def __init__(self, path: str | Path) -> None:
        # Initialize cleanup-relevant state FIRST so the finalizer is safe
        # even if _validate() raises during construction.
        self._info: dict | None = None
        self._manifest_props: dict | None = None
        self._manifest_state: dict = {"conn": None, "tmp": None}
        self._finalizer: weakref.finalize | None = None

        self.path = Path(path)
        self._validate()

        # Register finalizer only after successful validation. The finalizer
        # references self._manifest_state by closure (NOT self), so it doesn't
        # keep this object alive.
        self._finalizer = weakref.finalize(
            self, _cleanup_manifest, self._manifest_state
        )

    def _validate(self) -> None:
        if not self.path.is_dir():
            raise BackupError(f"Not a directory: {self.path}")
        if not (self.path / "Manifest.db").exists():
            raise BackupError(f"No Manifest.db found — not a valid iOS backup: {self.path}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close cached connections and remove temp files. Idempotent."""
        if self._finalizer is not None:
            # finalize.__call__() runs the cleanup and detaches the finalizer
            self._finalizer()

    def __enter__(self) -> "Backup":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def info(self) -> dict:
        if self._info is None:
            info_path = self.path / "Info.plist"
            if not info_path.exists():
                self._info = {}
            else:
                with open(info_path, "rb") as f:
                    self._info = plistlib.load(f)
        return self._info

    @property
    def manifest_props(self) -> dict:
        if self._manifest_props is None:
            manifest_path = self.path / "Manifest.plist"
            if not manifest_path.exists():
                self._manifest_props = {}
            else:
                with open(manifest_path, "rb") as f:
                    self._manifest_props = plistlib.load(f)
        return self._manifest_props

    def is_encrypted(self) -> bool:
        return bool(self.manifest_props.get("IsEncrypted", False))

    def device_name(self) -> str:
        return self.info.get("Display Name", self.info.get("Device Name", "Unknown"))

    def ios_version(self) -> str:
        return self.info.get("Product Version", "Unknown")

    def phone_number(self) -> str:
        return self.info.get("Phone Number", "Unknown")

    def last_backup_date(self) -> str:
        date = self.info.get("Last Backup Date")
        if date is None:
            return "Unknown"
        return str(date)

    # ------------------------------------------------------------------
    # File lookup
    # ------------------------------------------------------------------

    def _manifest_conn(self) -> sqlite3.Connection:
        """Return a cached connection to a temp copy of Manifest.db.

        Copying to a temp file (rather than opening the original via URI)
        avoids SQLite URI encoding issues with paths that contain spaces,
        and ensures a single connection is reused for all lookups instead
        of opening a new one per call.
        """
        if self._manifest_state["conn"] is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
            tmp.close()
            shutil.copy2(self.path / "Manifest.db", tmp.name)
            conn = sqlite3.connect(tmp.name)
            conn.row_factory = sqlite3.Row
            self._manifest_state["conn"] = conn
            self._manifest_state["tmp"] = tmp.name
        return self._manifest_state["conn"]

    def _file_id(self, domain: str, relative_path: str) -> str | None:
        # flags=1 filters to file entries only (excludes directories/symlinks).
        row = self._manifest_conn().execute(
            "SELECT fileID FROM Files WHERE domain = ? AND relativePath = ? AND flags = ?",
            (domain, relative_path, _FLAG_FILE),
        ).fetchone()
        return row[0] if row else None

    def get_file_path(self, domain: str, relative_path: str) -> Path | None:
        """Return the on-disk path of a backed-up file (not a copy)."""
        file_id = self._file_id(domain, relative_path)
        if file_id is None:
            return None
        candidate = self.path / file_id[:2] / file_id
        return candidate if candidate.exists() else None

    def extract_file(self, domain: str, relative_path: str, dest: Path) -> Path | None:
        """Copy a backed-up file to dest (preserving filename). Returns dest path or None."""
        src = self.get_file_path(domain, relative_path)
        if src is None:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return dest

    def open_db(self, domain: str, relative_path: str) -> _TempDB | None:
        """
        Copy the database to a temp file and return a _TempDB connection wrapper.
        The wrapper auto-deletes the temp file when closed (or when used as a
        context manager).
        """
        src = self.get_file_path(domain, relative_path)
        if src is None:
            return None
        return _open_db_from_path(src)


# ---------------------------------------------------------------------------
# Path translation for SMS/iMessage attachments
# ---------------------------------------------------------------------------

def attachment_backup_path(filename: str) -> tuple[str, str]:
    """Convert an attachment filename from sms.db into (domain, relative_path)
    suitable for Manifest.db lookup.

    sms.db stores attachment paths like ``~/Library/SMS/Attachments/ab/12/foo.jpg``
    or ``/var/mobile/Library/SMS/...``. These files actually live in
    ``MediaDomain`` in Manifest.db (NOT HomeDomain), even though the path uses
    the home-directory prefix. Other ~/ paths (rare for attachments) fall back
    to HomeDomain.
    """
    if filename.startswith("~/"):
        rel = filename[2:]
    elif filename.startswith("/var/mobile/"):
        rel = filename[len("/var/mobile/"):]
    else:
        rel = filename

    if rel.startswith("Library/SMS/"):
        return "MediaDomain", rel
    return "HomeDomain", rel
