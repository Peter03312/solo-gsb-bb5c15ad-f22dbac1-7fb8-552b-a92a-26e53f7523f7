"""End-to-end API tests (FastAPI TestClient over a temporary SQLite file)."""

from __future__ import annotations

from datetime import datetime, timezone

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
