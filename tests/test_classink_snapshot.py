from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from main import (
    build_classink_teacher_snapshot,
    restore_classink_teacher_state,
)


def sample_payload():
    return {
        "user": {
            "id": "teacher-old",
            "name": "Prof Test",
            "email": "prof@example.cd",
            "password": "must-not-be-persisted",
            "school": "École Test",
            "isLoggedIn": True,
        },
        "classes": ["4e Scientifique"],
        "courses": {"4e Scientifique": ["Mathématiques", "Physique"]},
        "students": {
            "4e Scientifique": [
                {"id": "student-no-grade", "name": "MUKENDI Aline"},
                {"id": "student-with-grade", "name": "KABUYA Jean"},
            ]
        },
        "evaluations": {"4e Scientifique": {"P1": []}},
        "grades": {},
        "presences": [],
        "quizzes": [],
        "pendingCommits": 2,
    }


def test_snapshot_keeps_ungraded_classes_students_and_courses_and_removes_password():
    payload = sample_payload()
    snapshot = build_classink_teacher_snapshot(payload, "prof@example.cd")

    assert snapshot["classes"] == ["4e Scientifique"]
    assert snapshot["courses"]["4e Scientifique"] == ["Mathématiques", "Physique"]
    assert [s["id"] for s in snapshot["students"]["4e Scientifique"]] == [
        "student-no-grade",
        "student-with-grade",
    ]
    assert snapshot["grades"] == {}
    assert "password" not in snapshot["user"]
    assert snapshot["user"]["isLoggedIn"] is True
    assert snapshot["pendingCommits"] == 0


def test_snapshot_is_a_copy_and_does_not_mutate_the_received_payload():
    payload = sample_payload()
    snapshot = build_classink_teacher_snapshot(payload, "prof@example.cd")
    snapshot["students"]["4e Scientifique"].clear()

    assert len(payload["students"]["4e Scientifique"]) == 2
    assert payload["user"]["password"] == "must-not-be-persisted"


def test_snapshot_rejects_another_teacher_email():
    with pytest.raises(HTTPException) as error:
        build_classink_teacher_snapshot(sample_payload(), "other@example.cd")
    assert error.value.status_code == 403


def test_snapshot_rejects_non_json_numbers():
    payload = sample_payload()
    payload["invalid"] = float("nan")
    with pytest.raises(HTTPException) as error:
        build_classink_teacher_snapshot(payload, "prof@example.cd")
    assert error.value.status_code == 422


def test_snapshot_rejects_payload_above_eight_megabytes():
    payload = sample_payload()
    payload["large_field"] = "x" * (8 * 1024 * 1024 + 1024)
    with pytest.raises(HTTPException) as error:
        build_classink_teacher_snapshot(payload, "prof@example.cd")
    assert error.value.status_code == 413


def test_login_restore_prefers_full_snapshot_over_legacy_grade_reconstruction():
    snapshot = sample_payload()
    teacher = SimpleNamespace(
        id=27,
        teacher_code="CN-2026-ABCD-EFGH",
        full_name="Professeur Canonique",
        email="prof@example.cd",
        school_name="École Test",
    )
    legacy = {
        "classes": [],
        "students": {},
        "courses": {},
        "grades": {"legacy-grade": 12},
    }

    restored = restore_classink_teacher_state(
        build_classink_teacher_snapshot(snapshot, "prof@example.cd"),
        legacy,
        teacher,
        "new-local-password",
    )

    assert restored["classes"] == ["4e Scientifique"]
    assert "student-no-grade" == restored["students"]["4e Scientifique"][0]["id"]
    assert restored["courses"]["4e Scientifique"] == ["Mathématiques", "Physique"]
    assert restored["grades"] == {}
    assert restored["user"]["id"] == "CN-2026-ABCD-EFGH"
    assert restored["user"]["name"] == "Professeur Canonique"
    assert restored["user"]["password"] == "new-local-password"
    assert restored["user"]["isLoggedIn"] is True
