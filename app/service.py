"""Domain logic: version registration, license changes and permission checks."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import deque
from collections.abc import Callable
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


# -- permission evaluation helpers -----------------------------------------


def _latest_effective(changes: list[dict[str, Any]], at: str) -> dict[str, Any] | None:
    """In-memory equivalent of :meth:`Database.latest_change`.

    ``changes`` are rows of one version with canonical (UTC, zero-padded)
    timestamps, so lexicographic comparison matches chronological order.
    """
    best: dict[str, Any] | None = None
    for change in changes:
        if change["effective_at"] > at:
            continue
        if (
            best is None
            or change["effective_at"] > best["effective_at"]
            or (
                change["effective_at"] == best["effective_at"]
                and change["change_id"] < best["change_id"]
            )
        ):
            best = change
    return best


def _closure_from(parents_map: dict[str, list[str]], version_id: str) -> list[str]:
    """BFS over a pre-loaded parent map; same ordering as ``ordered_closure``."""
    order: list[str] = []
    seen: set[str] = set()
    queue: deque[str] = deque([version_id])
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        order.append(current)
        for parent in parents_map.get(current, []):
            if parent not in seen:
                queue.append(parent)
    return order


def _evaluate_closure(
    version_id: str,
    use: str,
    at: datetime,
    closure: list[str],
    latest_for: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any]:
    """Shared permission evaluation over an ancestor closure.

    ``latest_for`` supplies each version's latest change effective at or
    before the query instant (already filtered), so the exact same
    intersection rules serve both live checks and dry-run previews.
    """
    basis: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    intersection: set[str] | None = None
    missing_license = False

    for vid in closure:
        change = latest_for(vid)
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
        "at": at,
        "allowed": not blockers,
        "closure": closure,
        "basis": basis,
        "blockers": blockers,
        "allowed_uses_intersection": result_intersection,
    }


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
        return _evaluate_closure(
            version_id,
            use,
            at.astimezone(timezone.utc),
            closure,
            lambda vid: self.db.latest_change(vid, at_canonical),
        )

    # -- change impact preview (dry-run, nothing is written) ---------------

    def ordered_descendants(self, version_id: str) -> list[str]:
        """Return self plus every transitive descendant, deduplicated, BFS order.

        Mirrors :meth:`ordered_closure` but follows child references, so a
        descendant reached through diamond inheritance is reported once.
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
            for child in self.db.get_children(current):
                if child not in seen:
                    queue.append(child)
        return order

    def preview_change(
        self,
        change_id: str | None,
        version_id: str,
        effective_at: datetime,
        allowed_uses: list[str],
        use: str,
        at: datetime,
    ) -> dict[str, Any]:
        """Dry-run a license change against the target and all descendants.

        Nothing is written. The current and the hypothetical post-commit
        decisions are both computed from one consistent snapshot: the
        database lock is held for the whole preview, so a concurrent commit
        cannot interleave and mix versions/changes from different snapshots.
        Only versions whose ``allowed`` status would flip are reported.

        Error semantics match :meth:`submit_change` (unknown version, change
        id conflict, same version+instant with a different change); replaying
        an identical existing change is a no-op reported as ``replayed``.
        """
        if effective_at.tzinfo is None:
            raise InvalidTimeError("生效时刻必须带时区信息")
        if at.tzinfo is None:
            raise InvalidTimeError("查询时刻必须带时区信息")
        use = use.strip()
        if not use:
            raise ServiceError("用途不能为空")

        change_id = change_id or f"c-{uuid.uuid4().hex}"
        effective = to_canonical(effective_at)
        uses = sorted(set(u.strip() for u in allowed_uses if u.strip()))
        digest = content_hash(version_id, effective, uses)
        at_utc = at.astimezone(timezone.utc)
        at_canonical = to_canonical(at)

        with self.db.lock:
            if self.db.get_version(version_id) is None:
                raise NotFoundError(
                    "版本不存在，无法为其预演许可变更", {"version_id": version_id}
                )

            replayed = False
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
                # Identical change already stored: a replay has no effect.
                replayed = True
            else:
                twin = self.db.change_at_same_time(version_id, effective)
                if twin is not None:
                    raise ConflictError(
                        "该版本在同一生效时刻已存在不同变更",
                        {
                            "version_id": version_id,
                            "effective_at": effective,
                            "existing_change_id": twin["change_id"],
                            "submitted_change_id": change_id,
                        },
                    )

            # -- consistent in-memory snapshot (lock still held) -----------
            descendants = self.ordered_descendants(version_id)
            parents_map: dict[str, list[str]] = {}
            involved: list[str] = []
            seen: set[str] = set()
            queue: deque[str] = deque(descendants)
            while queue:
                current = queue.popleft()
                if current in seen:
                    continue
                seen.add(current)
                involved.append(current)
                parents = self.db.get_parents(current)
                parents_map[current] = parents
                for parent in parents:
                    if parent not in seen:
                        queue.append(parent)
            changes_map = {vid: self.db.list_changes(vid) for vid in involved}

            hypothetical = {
                "change_id": change_id,
                "version_id": version_id,
                "effective_at": effective,
                "allowed_uses": uses,
                "content_hash": digest,
            }

            def latest_before(vid: str) -> dict[str, Any] | None:
                return _latest_effective(changes_map.get(vid, []), at_canonical)

            def latest_after(vid: str) -> dict[str, Any] | None:
                changes = changes_map.get(vid, [])
                if vid == version_id:
                    changes = [*changes, hypothetical]
                return _latest_effective(changes, at_canonical)

            impacts: list[dict[str, Any]] = []
            for vid in descendants:
                closure = _closure_from(parents_map, vid)
                before = _evaluate_closure(vid, use, at_utc, closure, latest_before)
                after = _evaluate_closure(vid, use, at_utc, closure, latest_after)
                if before["allowed"] != after["allowed"]:
                    impacts.append(
                        {
                            "version_id": vid,
                            "before": {
                                "allowed": before["allowed"],
                                "blockers": before["blockers"],
                            },
                            "after": {
                                "allowed": after["allowed"],
                                "blockers": after["blockers"],
                            },
                        }
                    )

            return {
                "change": {
                    "change_id": change_id,
                    "version_id": version_id,
                    "effective_at": parse_canonical(effective),
                    "allowed_uses": uses,
                },
                "use": use,
                "at": at_utc,
                "replayed": replayed,
                "evaluated_versions": descendants,
                "impacts": impacts,
            }
