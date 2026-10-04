"""End-to-end API tests (FastAPI TestClient over a temporary SQLite file)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.seed import seed_demo_data
from app.service import LicenseService


@pytest.fixture()
def client(tmp_path):
    db_path = str(tmp_path / "test.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=False, echo=False)
    app = create_app(settings)
    # The lifespan creates the schema (INIT_DB=true) and owns the connection.
    with TestClient(app) as test_client:
        yield test_client


def _iso(y: int, m: int, d: int, hh: int = 0) -> str:
    return datetime(y, m, d, hh, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _register(client, version_id, parents=None, note=""):
    resp = client.post(
        "/versions",
        json={"version_id": version_id, "parent_ids": parents or [], "note": note},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _change(client, change_id, version_id, at, uses):
    resp = client.post(
        "/license-changes",
        json={
            "change_id": change_id,
            "version_id": version_id,
            "effective_at": at,
            "allowed_uses": uses,
        },
    )
    return resp


def _check(client, version_id, use, at):
    return client.get(
        "/permissions/check",
        params={"version_id": version_id, "use": use, "at": at},
    )


def _preview(client, change_id, version_id, effective_at, uses, use, at):
    return client.post(
        "/license-changes/preview",
        json={
            "change_id": change_id,
            "version_id": version_id,
            "effective_at": effective_at,
            "allowed_uses": uses,
            "use": use,
            "at": at,
        },
    )


# -- registration ----------------------------------------------------------


def test_register_version_with_unknown_parent_is_404(client):
    resp = client.post(
        "/versions", json={"version_id": "v1", "parent_ids": ["ghost"]}
    )
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"


def test_parent_ids_deduplicated_and_immutable(client):
    _register(client, "p1")
    body = _register(client, "c1", ["p1", "p1"])
    assert body["version"]["parent_ids"] == ["p1"]

    # Re-registering the same id conflicts; there is no edit endpoint.
    resp = client.post("/versions", json={"version_id": "c1", "parent_ids": []})
    assert resp.status_code == 409


def test_unknown_version_query_is_404(client):
    assert client.get("/versions/nope").status_code == 404
    assert _check(client, "nope", "research", _iso(2026, 5, 1)).status_code == 404


# -- license changes -------------------------------------------------------


def test_change_requires_existing_version(client):
    resp = _change(client, "c1", "nope", _iso(2026, 1, 1), ["research"])
    assert resp.status_code == 404


def test_duplicate_submission_same_id_same_content_is_idempotent(client):
    _register(client, "v1")
    payload = {
        "change_id": "c1",
        "version_id": "v1",
        "effective_at": _iso(2026, 1, 1),
        "allowed_uses": ["research", "review"],
    }
    first = client.post("/license-changes", json=payload)
    assert first.status_code == 201
    assert first.json()["replayed"] is False

    # Same content, uses reordered and duplicated -> identical result.
    second = client.post(
        "/license-changes",
        json={**payload, "allowed_uses": ["review", "research", "research"]},
    )
    assert second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["change"] == first.json()["change"]


def test_same_id_different_content_conflicts(client):
    _register(client, "v1")
    assert _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"]).status_code == 201

    # Different uses.
    resp = _change(client, "c1", "v1", _iso(2026, 1, 1), ["commercial"])
    assert resp.status_code == 409
    # Different effective time with the same id.
    resp = _change(client, "c1", "v1", _iso(2026, 2, 1), ["research"])
    assert resp.status_code == 409
    # Different target version with the same id.
    _register(client, "v2")
    resp = _change(client, "c1", "v2", _iso(2026, 1, 1), ["research"])
    assert resp.status_code == 409


def test_same_version_same_time_different_change_conflicts(client):
    _register(client, "v1")
    assert _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"]).status_code == 201
    resp = _change(client, "c2", "v1", _iso(2026, 1, 1), ["research"])
    assert resp.status_code == 409
    assert "同一生效时刻" in resp.json()["message"]


def test_latest_change_wins_and_naive_time_rejected(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "c2", "v1", _iso(2026, 6, 1), ["research"])

    before = _check(client, "v1", "review", _iso(2026, 5, 1)).json()
    assert before["allowed"] is True
    after = _check(client, "v1", "review", _iso(2026, 6, 1)).json()
    assert after["allowed"] is False
    assert after["blockers"][0]["reason"] == "use_not_allowed"
    assert after["blockers"][0]["change_id"] == "c2"

    # Timezone-aware values in other offsets are accepted; naive is 422.
    resp = client.post(
        "/license-changes",
        json={
            "change_id": "c3",
            "version_id": "v1",
            "effective_at": "2026-09-01T08:00:00+08:00",
            "allowed_uses": ["research"],
        },
    )
    assert resp.status_code == 201
    assert resp.json()["change"]["effective_at"].startswith("2026-09-01T00:00:00")

    bad = client.post(
        "/license-changes",
        json={
            "change_id": "c4",
            "version_id": "v1",
            "effective_at": "2026-09-01T00:00:00",
            "allowed_uses": ["research"],
        },
    )
    assert bad.status_code == 422


# -- permission evaluation -------------------------------------------------


def test_ancestor_intersection_denies_and_reports_blockers(client):
    _register(client, "a")
    _register(client, "b")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research", "commercial"])
    _register(client, "d1", ["a", "b"])
    _change(client, "cd1", "d1", _iso(2026, 2, 1), ["research", "review", "commercial"])

    at = _iso(2026, 3, 1)
    ok = _check(client, "d1", "research", at).json()
    assert ok["allowed"] is True
    assert ok["closure"] == ["d1", "a", "b"]
    assert {entry["version_id"] for entry in ok["basis"]} == {"d1", "a", "b"}
    assert ok["allowed_uses_intersection"] == ["research"]

    denied = _check(client, "d1", "review", at).json()
    assert denied["allowed"] is False
    reasons = {(bl["version_id"], bl["reason"]) for bl in denied["blockers"]}
    assert ("b", "use_not_allowed") in reasons
    assert all(bl["version_id"] != "a" for bl in denied["blockers"])


def test_diamond_ancestors_deduplicated(client):
    # a <- b <- d1, a <- d1 : a must only appear once in the closure.
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    _register(client, "b", ["a"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research"])
    _register(client, "d1", ["b", "a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research"])

    body = _check(client, "d1", "research", _iso(2026, 2, 1)).json()
    assert body["closure"] == ["d1", "b", "a"]
    assert body["allowed"] is True


def test_no_valid_license_blocks(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 3, 1), ["research"])
    # Before the change takes effect there is no valid license.
    body = _check(client, "a", "research", _iso(2026, 2, 1)).json()
    assert body["allowed"] is False
    assert body["blockers"] == [
        {"version_id": "a", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]
    assert body["basis"] == []
    assert body["allowed_uses_intersection"] == []

    # A child inherits the block from its parent.
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "commercial"])
    body = _check(client, "d1", "research", _iso(2026, 2, 1)).json()
    assert body["allowed"] is False
    assert body["blockers"] == [
        {"version_id": "a", "reason": "no_valid_license", "change_id": None, "effective_at": None, "allowed_uses": []}
    ]


def test_naive_query_at_is_422(client):
    _register(client, "a")
    resp = client.get(
        "/permissions/check",
        params={"version_id": "a", "use": "research", "at": "2026-01-01T00:00:00"},
    )
    assert resp.status_code == 422


# -- demo seed -------------------------------------------------------------


def test_demo_seed_is_idempotent(tmp_path):
    db_path = str(tmp_path / "seed.db")
    db = Database(db_path)
    db.init_schema()
    assert seed_demo_data(db) is True
    # Second run on the non-empty DB is skipped; resubmitting directly still
    # returns the same stored change (replay).
    assert seed_demo_data(db) is False
    change, replayed = LicenseService(db).submit_change(
        change_id="demo-chg-a-1",
        version_id="demo-root-a",
        effective_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        allowed_uses=["research", "review"],
    )
    assert replayed is True
    assert change["change_id"] == "demo-chg-a-1"
    db.close()


def test_demo_fixture_permissions(tmp_path):
    db_path = str(tmp_path / "seed2.db")
    settings = Settings(db_path=db_path, init_db=True, seed_demo=True, echo=False)
    with TestClient(create_app(settings)) as test_client:
        at = _iso(2026, 3, 1)
        research = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-derived-1", "use": "research", "at": at},
        ).json()
        assert research["allowed"] is True

        commercial = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-derived-1", "use": "commercial", "at": at},
        ).json()
        assert commercial["allowed"] is False
        assert commercial["blockers"][0]["version_id"] == "demo-root-a"

        nolicense = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-no-license", "use": "research", "at": at},
        ).json()
        assert nolicense["allowed"] is False
        assert nolicense["blockers"][0]["reason"] == "no_valid_license"

        # After root-a's June change drops review, intersection still allows
        # only research; research stays allowed.
        later = test_client.get(
            "/permissions/check",
            params={"version_id": "demo-derived-1", "use": "research", "at": _iso(2026, 7, 1)},
        ).json()
        assert later["allowed"] is True


# -- change impact preview (dry-run) ----------------------------------------


def test_preview_reports_only_status_changes_and_does_not_write(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d2", ["a"])
    _change(client, "cd2", "d2", _iso(2026, 1, 1), ["research"])
    _register(client, "u")
    _change(client, "cu", "u", _iso(2026, 1, 1), ["research", "review"])

    # Narrowing "a" to research-only flips a and d1; d2 was already blocked by
    # its own license (status unchanged) and u is not a descendant of a.
    resp = _preview(client, "px", "a", _iso(2026, 6, 1), ["research"], "review", _iso(2026, 7, 1))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["replayed"] is False
    assert body["change"] == {
        "change_id": "px",
        "version_id": "a",
        "effective_at": "2026-06-01T00:00:00Z",
        "allowed_uses": ["research"],
    }
    assert body["use"] == "review"
    assert body["evaluated_versions"] == ["a", "d1", "d2"]
    assert [i["version_id"] for i in body["impacts"]] == ["a", "d1"]
    for impact in body["impacts"]:
        assert impact["before"] == {"allowed": True, "blockers": []}
        assert impact["after"]["allowed"] is False
        (blocker,) = impact["after"]["blockers"]
        assert blocker["version_id"] == "a"
        assert blocker["reason"] == "use_not_allowed"
        assert blocker["change_id"] == "px"

    # Dry-run writes nothing: the change does not exist and the version is
    # untouched.
    assert client.get("/license-changes/px").status_code == 404
    assert [c["change_id"] for c in client.get("/versions/a").json()["changes"]] == ["ca"]

    # Actually submitting the previewed change yields exactly the previewed
    # "after" decisions.
    assert _change(client, "px", "a", _iso(2026, 6, 1), ["research"]).status_code == 201
    for impact in body["impacts"]:
        actual = _check(client, impact["version_id"], "review", _iso(2026, 7, 1)).json()
        assert actual["allowed"] == impact["after"]["allowed"]
        assert [b["version_id"] for b in actual["blockers"]] == [
            b["version_id"] for b in impact["after"]["blockers"]
        ]


def test_preview_widening_change_reports_newly_allowed(client):
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research"])
    _register(client, "d1", ["a"])
    _change(client, "cd1", "d1", _iso(2026, 1, 1), ["research", "review"])

    resp = _preview(client, "px", "a", _iso(2026, 6, 1), ["research", "review"], "review", _iso(2026, 7, 1))
    assert resp.status_code == 200
    body = resp.json()
    assert [i["version_id"] for i in body["impacts"]] == ["a", "d1"]
    for impact in body["impacts"]:
        assert impact["before"]["allowed"] is False
        assert impact["before"]["blockers"][0]["reason"] == "use_not_allowed"
        assert impact["before"]["blockers"][0]["version_id"] == "a"
        assert impact["after"] == {"allowed": True, "blockers": []}


def test_preview_no_false_positive_before_effective_or_when_overridden(client):
    _register(client, "a")
    _change(client, "c1", "a", _iso(2026, 1, 1), ["research", "review"])
    _change(client, "c2", "a", _iso(2026, 8, 1), ["research"])

    # Query time earlier than the hypothetical effective time: not in effect.
    resp = _preview(client, "px", "a", _iso(2026, 6, 1), ["research"], "review", _iso(2026, 5, 1))
    assert resp.status_code == 200
    assert resp.json()["impacts"] == []

    # A later existing change (c2 @ 2026-08-01) overrides the hypothetical
    # 2026-06-01 change at the query time: no impact may be reported.
    resp = _preview(client, "px", "a", _iso(2026, 6, 1), ["research"], "review", _iso(2026, 9, 1))
    assert resp.status_code == 200
    assert resp.json()["impacts"] == []


def test_preview_diamond_descendants_counted_once(client):
    # a <- b <- d, a <- c <- d : d must be evaluated and reported once.
    _register(client, "a")
    _change(client, "ca", "a", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "b", ["a"])
    _change(client, "cb", "b", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "c", ["a"])
    _change(client, "cc", "c", _iso(2026, 1, 1), ["research", "review"])
    _register(client, "d", ["b", "c"])
    _change(client, "cd", "d", _iso(2026, 1, 1), ["research", "review"])

    resp = _preview(client, "px", "a", _iso(2026, 6, 1), ["research"], "review", _iso(2026, 7, 1))
    assert resp.status_code == 200
    body = resp.json()
    assert body["evaluated_versions"] == ["a", "b", "c", "d"]
    assert [i["version_id"] for i in body["impacts"]] == ["a", "b", "c", "d"]


def test_preview_error_semantics_match_submission(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])

    # Unknown version -> 404.
    resp = _preview(client, "px", "nope", _iso(2026, 2, 1), ["research"], "research", _iso(2026, 3, 1))
    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"

    # Same change id with different content -> 409.
    resp = _preview(client, "c1", "v1", _iso(2026, 1, 1), ["commercial"], "research", _iso(2026, 3, 1))
    assert resp.status_code == 409
    assert resp.json()["error"] == "conflict"

    # Different change at the same (version, effective instant) -> 409.
    resp = _preview(client, "c9", "v1", _iso(2026, 1, 1), ["research"], "research", _iso(2026, 3, 1))
    assert resp.status_code == 409
    assert "同一生效时刻" in resp.json()["message"]

    # Naive effective_at / at -> 422.
    resp = client.post(
        "/license-changes/preview",
        json={
            "change_id": "px",
            "version_id": "v1",
            "effective_at": "2026-02-01T00:00:00",
            "allowed_uses": ["research"],
            "use": "research",
            "at": "2026-03-01T00:00:00Z",
        },
    )
    assert resp.status_code == 422
    resp = client.post(
        "/license-changes/preview",
        json={
            "change_id": "px",
            "version_id": "v1",
            "effective_at": "2026-02-01T00:00:00Z",
            "allowed_uses": ["research"],
            "use": "research",
            "at": "2026-03-01T00:00:00",
        },
    )
    assert resp.status_code == 422

    # Blank use -> 400, same as the live check.
    resp = _preview(client, "px", "v1", _iso(2026, 2, 1), ["research"], "  ", _iso(2026, 3, 1))
    assert resp.status_code == 400


def test_preview_replay_of_existing_change_has_no_effect(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research", "review"])

    # Same id and same semantic content (uses reordered/duplicated): a replay.
    resp = _preview(
        client, "c1", "v1", _iso(2026, 1, 1), ["review", "research", "research"], "review", _iso(2026, 3, 1)
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["replayed"] is True
    assert body["impacts"] == []


def test_preview_generates_change_id_and_normalises_timezones(client):
    _register(client, "v1")
    _change(client, "c1", "v1", _iso(2026, 1, 1), ["research"])

    resp = client.post(
        "/license-changes/preview",
        json={
            "version_id": "v1",
            "effective_at": "2026-02-01T08:00:00+08:00",
            "allowed_uses": ["research", "review"],
            "use": "review",
            "at": "2026-03-01T00:00:00Z",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["change"]["change_id"]
    assert body["change"]["effective_at"].startswith("2026-02-01T00:00:00")
    assert [i["version_id"] for i in body["impacts"]] == ["v1"]


def test_preview_is_snapshot_consistent_under_concurrent_submits(tmp_path):
    """Concurrent commits must not leak into a preview's snapshot.

    The preview holds the database lock for its whole read+compute phase, so
    writers either commit entirely before or entirely after it; no preview may
    observe a mix. This smoke test hammers that path from several threads.
    """
    db = Database(str(tmp_path / "conc.db"))
    db.init_schema()
    service = LicenseService(db)
    service.register_version("root", [], "")
    service.register_version("child", ["root"], "")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    service.submit_change("seed-root", "root", base, ["research", "review"])
    service.submit_change("seed-child", "child", base, ["research", "review"])

    errors: list[Exception] = []

    def writer() -> None:
        try:
            for i in range(30):
                service.submit_change(
                    f"w-{i}",
                    "root",
                    base + timedelta(days=30, seconds=i),
                    ["research"],
                )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def reader() -> None:
        try:
            for i in range(60):
                result = service.preview_change(
                    f"p-{i}",
                    "root",
                    base + timedelta(days=60),
                    ["research"],
                    "review",
                    base + timedelta(days=90),
                )
                assert result["evaluated_versions"] == ["root", "child"]
                assert all(
                    impact["before"]["allowed"] != impact["after"]["allowed"]
                    for impact in result["impacts"]
                )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    db.close()
    assert not errors
