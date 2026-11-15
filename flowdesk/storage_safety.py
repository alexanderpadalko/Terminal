"""Non-destructive, WAL-consistent SQLite backup and explicit recovery.

Never copy a live .sqlite3 file with shutil: uncheckpointed WAL transactions
may be lost. Backups use sqlite3.Connection.backup and are published only after
a complete integrity check. Restoration never overwrites the source.
"""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import sqlite3
import uuid

_SNAPSHOT_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z_[0-9a-f]{12}\.sqlite3$")


def _readonly(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro",
                           uri=True, timeout=30)


def verify_database(path):
    """Validate all SQLite pages, without creating or changing the database."""
    with closing(_readonly(path)) as db:
        result=db.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0]!="ok":
            raise sqlite3.DatabaseError("SQLite integrity_check failed")
        # A generic SQLite file isn't necessarily a FlowDesk candle database.
        tables={row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"candles","finam_pulls","finam_history_progress"} <= tables:
            raise sqlite3.DatabaseError("Not a FlowDesk candle database")
        return db.execute("SELECT COUNT(*) FROM candles").fetchone()[0]


def snapshot_directory(database):
    path=Path(database)
    return path.parent/"backups"/path.stem


def snapshots(database):
    folder=snapshot_directory(database)
    if not folder.exists():
        return ()
    return tuple(sorted((p for p in folder.iterdir()
                         if p.is_file() and _SNAPSHOT_RE.fullmatch(p.name)),
                        reverse=True))


def create_backup(database, *, now=None, retain=3):
    """Create verified atomic backup, then prune only our own older snapshots.

    On any failure, existing data and completed backups are left untouched.
    """
    path=Path(database)
    if retain<1:
        raise ValueError("retain must be positive")
    stamp=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    target_dir=snapshot_directory(path)
    target_dir.mkdir(parents=True,exist_ok=True)
    name=stamp.strftime("%Y%m%dT%H%M%SZ")+"_"+uuid.uuid4().hex[:12]+".sqlite3"
    finished=target_dir/name
    partial=target_dir/("."+name+".partial")
    try:
        with closing(_readonly(path)) as source, \
             closing(sqlite3.connect(partial,timeout=30)) as target:
            # Reject corrupt input; do not rotate away the last good backup.
            verdict=source.execute("PRAGMA integrity_check").fetchone()
            if not verdict or verdict[0]!="ok":
                raise sqlite3.DatabaseError("Source SQLite integrity check failed")
            source.backup(target,pages=256,sleep=0.05)
        count=verify_database(partial)
        os.replace(partial,finished)
        # Avoid deleting prior snapshots until the newest is durable and
        # independently verified. Unrelated files are never pruned.
        for old in snapshots(path)[retain:]:
            try:old.unlink()
            except OSError:pass
        return {"path":str(finished),"candles":count}
    finally:
        try:partial.unlink(missing_ok=True)
        except OSError:pass


def backup_if_due(database, *, now=None, retain=3):
    """At most one valid daily snapshot; safe to call from a worker thread."""
    today=(now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    prefix=today.strftime("%Y%m%dT")
    for snapshot in snapshots(database):
        if snapshot.name.startswith(prefix):
            return {"path":str(snapshot),"created":False}
    # No useful data to back up yet.
    with closing(_readonly(database)) as db:
        if db.execute("SELECT COUNT(*) FROM candles").fetchone()[0]==0:
            return {"path":None,"created":False}
    result=create_backup(database,now=today,retain=retain)
    return {**result,"created":True}


def recover_to_new_file(backup_file, destination):
    """Restore a validated snapshot to a NEW file; never overwrite user data.

    Stop FlowDesk before moving the recovered file into its normal location.
    This routine does not touch the original DB, its WAL, or its SHM file.
    """
    saved=Path(backup_file);target=Path(destination)
    if target.exists():
        raise FileExistsError("Destination exists; refusal to overwrite")
    verify_database(saved)
    if not target.parent.is_dir():
        raise FileNotFoundError("Destination parent folder does not exist")
    partial=target.with_name("."+target.name+"."+uuid.uuid4().hex+".partial")
    try:
        with closing(_readonly(saved)) as source, \
             closing(sqlite3.connect(partial,timeout=30)) as restored:
            source.backup(restored,pages=256,sleep=0.05)
        candles=verify_database(partial)
        # Atomic exclusive creation: an unexpected competing file may not
        # be overwritten even if it appears after our first exists() check.
        os.link(partial,target)
        return {"path":str(target),"candles":candles}
    finally:
        try:partial.unlink(missing_ok=True)
        except OSError:pass
