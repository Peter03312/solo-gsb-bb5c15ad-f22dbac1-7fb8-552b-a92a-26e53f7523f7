"""SQLite persistence layer.

A single :class:`Database` owns the process-wide connection. SQLite itself
serialises writes; a process-level re-entrant lock additionally guards the
read-then-write transactions in the service layer so duplicate submissions
remain deterministic.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any


class UniqueViolation(sqlite3.IntegrityError):
    """Marker raised by :meth:`Database.commit` on a UNIQUE constraint."""


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS versions (
        version_id   TEXT PRIMARY KEY,
        created_at   TEXT NOT NULL,
        note         TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS version_parents (
        version_id TEXT NOT NULL REFERENCES versions(version_id),
        parent_id  TEXT NOT NULL REFERENCES versions(version_id),
        position   INTEGER NOT NULL,
        PRIMARY KEY (version_id, parent_id)
    )
    """,
    # The same parent may legitimately be referenced by different child
    # versions, so only the pair itself is unique.
    """
    CREATE TABLE IF NOT EXISTS license_changes (
        change_id     TEXT PRIMARY KEY,
        version_id    TEXT NOT NULL REFERENCES versions(version_id),
        effective_at  TEXT NOT NULL,
        allowed_uses  TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        created_at    TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_changes_version_time "
    "ON license_changes(version_id, effective_at)",
    "CREATE INDEX IF NOT EXISTS idx_parents_version ON version_parents(version_id)",
    "CREATE INDEX IF NOT EXISTS idx_parents_parent ON version_parents(parent_id)",
]


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    if "allowed_uses" in data and isinstance(data["allowed_uses"], str):
        data["allowed_uses"] = json.loads(data["allowed_uses"])
    return data


class Database:
    """Thin wrapper around a SQLite database connection."""

    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; explicit BEGIN/COMMIT below
            timeout=30.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 30000")

    # -- lifecycle -------------------------------------------------------

    def init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON")
            for statement in SCHEMA_STATEMENTS:
                cur.execute(statement)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # -- raw helpers ------------------------------------------------------

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        return row_to_dict(self._conn.execute(sql, params).fetchone())

    def query_all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [row_to_dict(row) for row in self._conn.execute(sql, params).fetchall()]  # type: ignore[list-item]

    def begin(self) -> None:
        self._conn.execute("BEGIN IMMEDIATE")

    def begin_read(self) -> None:
        """Open a deferred read transaction.

        In WAL mode the first SELECT pins a consistent snapshot that stays
        stable until :meth:`rollback`; being deferred it takes no write lock.
        Previews read versions, parent edges and changes through one such
        transaction so concurrent commits cannot mix snapshots.
        """
        self._conn.execute("BEGIN")

    def commit(self) -> None:
        try:
            self._conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            self._conn.execute("ROLLBACK")
            raise

    def rollback(self) -> None:
        self._conn.execute("ROLLBACK")

    # -- versions ---------------------------------------------------------

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM versions WHERE version_id = ?", (version_id,))

    def list_versions(self) -> list[dict[str, Any]]:
        return self.query_all("SELECT * FROM versions ORDER BY created_at, version_id")

    def count_versions(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM versions").fetchone()
        return int(row["n"])

    def insert_version(self, version_id: str, created_at: str, note: str) -> None:
        self._conn.execute(
            "INSERT INTO versions (version_id, created_at, note) VALUES (?, ?, ?)",
            (version_id, created_at, note),
        )

    def insert_parent(self, version_id: str, parent_id: str, position: int) -> None:
        self._conn.execute(
            "INSERT INTO version_parents (version_id, parent_id, position) VALUES (?, ?, ?)",
            (version_id, parent_id, position),
        )

    def get_parents(self, version_id: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT parent_id FROM version_parents WHERE version_id = ? ORDER BY position, parent_id",
            (version_id,),
        ).fetchall()
        return [row["parent_id"] for row in rows]

    def get_children(self, version_id: str) -> list[str]:
        """All versions that reference ``version_id`` directly as a parent."""
        rows = self._conn.execute(
            "SELECT DISTINCT version_id FROM version_parents WHERE parent_id = ? "
            "ORDER BY version_id",
            (version_id,),
        ).fetchall()
        return [row["version_id"] for row in rows]

    def all_parent_edges(self) -> list[tuple[str, str]]:
        rows = self._conn.execute(
            "SELECT version_id, parent_id FROM version_parents ORDER BY position, parent_id"
        ).fetchall()
        return [(row["version_id"], row["parent_id"]) for row in rows]

    # -- license changes --------------------------------------------------

    def get_change(self, change_id: str) -> dict[str, Any] | None:
        return self.query_one("SELECT * FROM license_changes WHERE change_id = ?", (change_id,))

    def list_changes(self, version_id: str) -> list[dict[str, Any]]:
        return self.query_all(
            "SELECT * FROM license_changes WHERE version_id = ? "
            "ORDER BY effective_at, change_id",
            (version_id,),
        )

    def insert_change(
        self,
        change_id: str,
        version_id: str,
        effective_at: str,
        allowed_uses: list[str],
        content_hash: str,
        created_at: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO license_changes "
            "(change_id, version_id, effective_at, allowed_uses, content_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                change_id,
                version_id,
                effective_at,
                json.dumps(allowed_uses, ensure_ascii=False),
                content_hash,
                created_at,
            ),
        )

    def change_at_same_time(self, version_id: str, effective_at: str) -> dict[str, Any] | None:
        """Return the (single) change of a version at the exact same instant.

        Multiple changes for one version with one effective time are forbidden
        ("同版本同生效时刻出现不同变更应报冲突"). Replays with the same change id
        are handled before reaching this query.
        """
        return self.query_one(
            "SELECT * FROM license_changes WHERE version_id = ? AND effective_at = ? "
            "ORDER BY change_id LIMIT 1",
            (version_id, effective_at),
        )

    def latest_change(self, version_id: str, at: str) -> dict[str, Any] | None:
        return self.query_one(
            "SELECT * FROM license_changes WHERE version_id = ? AND effective_at <= ? "
            "ORDER BY effective_at DESC, change_id LIMIT 1",
            (version_id, at),
        )
