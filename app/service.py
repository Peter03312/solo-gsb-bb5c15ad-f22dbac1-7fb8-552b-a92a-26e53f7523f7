"""Domain logic: version registration, license changes and permission checks."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .database import Database

# Canonical UTC timestamp used for storage and lexicographic comparison in SQL.
# Example: 2026-03-01T08:30:00.000000Z
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_canonical(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区信息")
    return value.astimezone(timezone.utc).strftime(_TIME_FORMAT)


def parse_canonical(value: str) -> datetime:
    return datetime.strptime(value, _TIME_FORMAT).replace(tzinfo=timezone.utc)


def content_hash(version_id: str, effective_at: str, allowed_uses: list[str]) -> str:
    """Stable hash of a change's semantic content.

    Duplicate submissions are defined as the same change id *and* the same
    semantic content (version, effective instant, allowed-use set). Order of
    the allowed uses is irrelevant; duplicates inside it are ignored.
    """
    payload = {
        "version_id": version_id,
        "effective_at": effective_at,
        "allowed_uses": sorted(set(allowed_uses)),
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# -- domain errors ---------------------------------------------------------


class ServiceError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(ServiceError):
    status_code = 404
    code = "not_found"


class ConflictError(ServiceError):
    status_code = 409
    code = "conflict"


class InvalidTimeError(ServiceError):
    status_code = 422
    code = "invalid_time"


# -- serialisation ---------------------------------------------------------


def _change_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "change_id": row["change_id"],
        "version_id": row["version_id"],
        "effective_at": parse_canonical(row["effective_at"]),
        "allowed_uses": list(row["allowed_uses"]),
        "content_hash": row["content_hash"],
        "created_at": parse_canonical(row["created_at"]),
    }


def _version_out(db: Database, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "version_id": row["version_id"],
        "created_at": parse_canonical(row["created_at"]),
        "note": row["note"],
        "parent_ids": db.get_parents(row["version_id"]),
        "changes": [_change_out(c) for c in db.list_changes(row["version_id"])],
    }


# -- core service ----------------------------------------------------------


class LicenseService:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- versions ---------------------------------------------------------

    def register_version(
        self,
        version_id: str | None,
        parent_ids: list[str],
        note: str = "",
    ) -> dict[str, Any]:
        """Register a new version and immutably pin its parent references."""
        version_id = version_id or f"v-{uuid.uuid4().hex}"

        with self.db.lock:
            missing = [pid for pid in parent_ids if self.db.get_version(pid) is None]
            if missing:
                raise NotFoundError(
                    "以下父版本不存在，引用前必须先登记",
                    {"missing_parent_ids": ",".join(missing)},
                )
            if self.db.get_version(version_id) is not None:
                raise ConflictError("版本标识已存在", {"version_id": version_id})

            created = to_canonical(now_utc())
            try:
                self.db.begin()
                self.db.insert_version(version_id, created, note)
                for position, pid in enumerate(parent_ids):
                    self.db.insert_parent(version_id, pid, position)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

            return _version_out(self.db, self.db.get_version(version_id))  # type: ignore[arg-type]

    def get_version(self, version_id: str) -> dict[str, Any]:
        row = self.db.get_version(version_id)
        if row is None:
            raise NotFoundError("版本不存在", {"version_id": version_id})
        return _version_out(self.db, row)

    def list_versions(self) -> list[dict[str, Any]]:
        return [_version_out(self.db, row) for row in self.db.list_versions()]

    def ordered_closure(self, version_id: str) -> list[str]:
        """Return self plus every transitive ancestor, deduplicated, BFS order.

        Parent edges are frozen at registration time, so the graph is a DAG.
        """
        order: list[str] = []
        seen: set[str] = set()
        queue: deque[str] = deque([version_id])
        while queue:
            current = queue.popleft()
            if current in seen:
                continue
            seen.add(current)
            order.append(current)
            for parent in self.db.get_parents(current):
                if parent not in seen:
                    queue.append(parent)
        return order

    # -- license changes --------------------------------------------------

    def submit_change(
        self,
        change_id: str | None,
        version_id: str,
        effective_at: datetime,
        allowed_uses: list[str],
    ) -> tuple[dict[str, Any], bool]:
        """Submit a license change.

        Returns ``(change, replayed)``. A repeated submission with the same
        ``change_id`` and identical content is a replay and returns the stored
        result unchanged. Same id with different content, or two different
        changes at the same (version, effective instant), are conflicts.
        """
        if effective_at.tzinfo is None:
            raise InvalidTimeError("生效时刻必须带时区信息")

        change_id = change_id or f"c-{uuid.uuid4().hex}"
        effective = to_canonical(effective_at)
        uses = sorted(set(u.strip() for u in allowed_uses if u.strip()))
        digest = content_hash(version_id, effective, uses)

        with self.db.lock:
            if self.db.get_version(version_id) is None:
                raise NotFoundError("版本不存在，无法为其登记许可变更", {"version_id": version_id})

            existing = self.db.get_change(change_id)
            if existing is not None:
                same = (
                    existing["version_id"] == version_id
                    and existing["effective_at"] == effective
                    and existing["content_hash"] == digest
                )
                if not same:
                    raise ConflictError(
                        "相同变更标识已存在但内容不同",
                        {
                            "change_id": change_id,
                            "submitted_version_id": version_id,
                            "existing_version_id": existing["version_id"],
                            "submitted_effective_at": effective,
                            "existing_effective_at": existing["effective_at"],
                        },
                    )
                return _change_out(existing), True

            twin = self.db.change_at_same_time(version_id, effective)
            if twin is not None and twin["change_id"] != change_id:
                raise ConflictError(
                    "该版本在同一生效时刻已存在不同变更",
                    {
                        "version_id": version_id,
                        "effective_at": effective,
                        "existing_change_id": twin["change_id"],
                        "submitted_change_id": change_id,
                    },
                )

            created = to_canonical(now_utc())
            try:
                self.db.begin()
                self.db.insert_change(change_id, version_id, effective, uses, digest, created)
                self.db.commit()
            except Exception:
                self.db.rollback()
                # Concurrent writers may have won the unique constraint; map
                # both id and (version,time) collisions onto clear conflicts.
                existing = self.db.get_change(change_id)
                if existing is not None:
                    if existing["content_hash"] == digest:
                        return _change_out(existing), True
                    raise ConflictError(
                        "相同变更标识已存在但内容不同", {"change_id": change_id}
                    ) from None
                twin = self.db.change_at_same_time(version_id, effective)
                if twin is not None:
                    raise ConflictError(
                        "该版本在同一生效时刻已存在不同变更",
                        {
                            "version_id": version_id,
                            "effective_at": effective,
                            "existing_change_id": twin["change_id"],
                        },
                    ) from None
                raise

            stored = self.db.get_change(change_id)
            assert stored is not None
            return _change_out(stored), False

    def get_change(self, change_id: str) -> dict[str, Any]:
        row = self.db.get_change(change_id)
        if row is None:
            raise NotFoundError("变更不存在", {"change_id": change_id})
        return _change_out(row)

    # -- permission query -------------------------------------------------

    def check_permission(self, version_id: str, use: str, at: datetime) -> dict[str, Any]:
        """Evaluate whether ``use`` is permitted for ``version_id`` at ``at``.

        For the queried version and every ancestor, take the latest change
        effective at or before ``at``; the permitted uses are the intersection
        over them. Any version without an effective change, or whose latest
        change does not include the requested use, blocks the request.
        """
        if at.tzinfo is None:
            raise InvalidTimeError("查询时刻必须带时区信息")
        use = use.strip()
        if not use:
            raise ServiceError("用途不能为空")

        if self.db.get_version(version_id) is None:
            raise NotFoundError("版本不存在", {"version_id": version_id})

        at_canonical = to_canonical(at)
        closure = self.ordered_closure(version_id)

        basis: list[dict[str, Any]] = []
        blockers: list[dict[str, Any]] = []
        intersection: set[str] | None = None
        missing_license = False

        for vid in closure:
            change = self.db.latest_change(vid, at_canonical)
            if change is None:
                missing_license = True
                blockers.append(
                    {"version_id": vid, "reason": "no_valid_license"}
                )
                continue

            allowed = set(change["allowed_uses"])
            basis.append(
                {
                    "version_id": vid,
                    "change_id": change["change_id"],
                    "effective_at": parse_canonical(change["effective_at"]),
                    "allowed_uses": sorted(allowed),
                }
            )
            intersection = allowed if intersection is None else intersection & allowed
            if use not in allowed:
                blockers.append(
                    {
                        "version_id": vid,
                        "reason": "use_not_allowed",
                        "change_id": change["change_id"],
                        "effective_at": parse_canonical(change["effective_at"]),
                        "allowed_uses": sorted(allowed),
                    }
                )

        # When some version has no effective license at all, no intersection
        # is meaningful (the request is denied regardless).
        if missing_license:
            result_intersection: list[str] = []
        else:
            result_intersection = sorted(intersection) if intersection is not None else []
        return {
            "version_id": version_id,
            "use": use,
            "at": at.astimezone(timezone.utc),
            "allowed": not blockers,
            "closure": closure,
            "basis": basis,
            "blockers": blockers,
            "allowed_uses_intersection": result_intersection,
        }
