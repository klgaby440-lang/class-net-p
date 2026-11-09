from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from main import (
    apply_teacher_classroom_context,
    compose_teacher_classroom_context,
    teacher_has_verified_school_access,
    teacher_is_titulaire,
    teacher_matches_school_roster,
    validate_school_class_catalog,
)


def sample_catalog():
    return {
        "school": {
            "id": "school-1",
            "name": "École Horizon",
            "code": "63017",
            "city": "Bukavu",
            "commune": "Ibanda",
        },
        "classes": [
            {
                "id": "class-1",
                "name": "1ère Scientifique",
                "titulaire": "Aline Mukendi",
                "categories": ["Sciences", "Langues"],
                "bulletinModel": "classink",
                "bulletinTitle": "1ère Scientifique",
                "signatureSource": "titulaire",
            }
        ],
        "students": [
            {
                "id": "student-1",
                "classId": "class-1",
                "name": "MUKENDI",
                "postname": "Aline",
                "prename": "Jeanne",
                "sexe": "F",
                "permi": "PERM-001",
            }
        ],
        "courses": [
            {
                "id": "course-1",
                "classId": "class-1",
                "name": "Mathématiques",
                "maxPer": 40,
                "category": "Sciences",
                "titulaire": "Aline Mukendi",
                "subCategory": "Algèbre",
            }
        ],
        "teachers": [
            {
                "uniqueId": "teacher-code-1",
                "name": "Aline Mukendi",
                "subject": "Mathématiques",
                "status": "Actif",
            }
        ],
    }


def test_school_catalog_validation_keeps_classes_students_courses_and_titulars():
    catalog = validate_school_class_catalog(sample_catalog(), "school-1")

    assert catalog["school"]["id"] == "school-1"
    assert catalog["classes"][0]["titulaire"] == "Aline Mukendi"
    assert catalog["students"][0]["classId"] == "class-1"
    assert catalog["students"][0]["sexe"] == "F"
    assert catalog["courses"][0]["name"] == "Mathématiques"
    assert catalog["courses"][0]["maxPer"] == 40.0
    assert catalog["teachers"][0]["uniqueId"] == "teacher-code-1"


def test_school_catalog_rejects_payload_from_another_school():
    with pytest.raises(HTTPException) as error:
        validate_school_class_catalog(sample_catalog(), "another-school")
    assert error.value.status_code == 403


def test_school_catalog_rejects_duplicate_class_ids():
    payload = sample_catalog()
    payload["classes"].append(dict(payload["classes"][0]))

    with pytest.raises(HTTPException) as error:
        validate_school_class_catalog(payload, "school-1")
    assert error.value.status_code == 422
    assert "double" in error.value.detail.lower()


def test_school_catalog_rejects_student_or_course_with_unknown_class():
    payload = sample_catalog()
    payload["students"][0]["classId"] = "missing-class"

    with pytest.raises(HTTPException) as error:
        validate_school_class_catalog(payload, "school-1")
    assert error.value.status_code == 422


def test_school_catalog_rejects_duplicate_student_ids():
    payload = sample_catalog()
    payload["students"].append(dict(payload["students"][0]))

    with pytest.raises(HTTPException) as error:
        validate_school_class_catalog(payload, "school-1")
    assert error.value.status_code == 422


def test_school_catalog_rejects_duplicate_course_ids():
    payload = sample_catalog()
    payload["courses"].append(dict(payload["courses"][0]))

    with pytest.raises(HTTPException) as error:
        validate_school_class_catalog(payload, "school-1")
    assert error.value.status_code == 422


def test_teacher_classroom_context_identifies_only_the_official_titular():
    teacher = SimpleNamespace(
        id=1,
        teacher_code="teacher-code-1",
        full_name="Aline Mukendi",
        email="aline@example.cd",
        school_name="École Horizon",
    )
    school = SimpleNamespace(school_id="school-1", name_school="École Horizon")
    classe = SimpleNamespace(
        id="class-1",
        class_name="1ère Scientifique",
        titulaire_name="ALINE MUKENDI",
    )
    student = SimpleNamespace(
        id="student-1",
        class_id="class-1",
        student_name="MUKENDI",
        student_post_name="Aline",
        student_pre_name="Jeanne",
        student_sexe="F",
    )
    course = SimpleNamespace(
        id="course-1",
        class_id="class-1",
        course_name="Mathématiques",
        max_per=40,
        category="Sciences",
    )

    context = compose_teacher_classroom_context(
        teacher, school, [classe], [student], [course]
    )

    assert context["classes"] == ["1ère Scientifique"]
    assert context["titulaireClasses"] == ["1ère Scientifique"]
    assert context["classrooms"][0]["isTitulaire"] is True
    assert context["students"]["1ère Scientifique"][0]["id"] == "student-1"
    assert context["courses"]["1ère Scientifique"] == ["Mathématiques"]
    assert context["courseMax"]["1ère Scientifique_Mathématiques"] == 40


def test_teacher_classroom_context_does_not_grant_titular_role_by_school_membership_alone():
    teacher = SimpleNamespace(
        id=2,
        teacher_code="teacher-code-2",
        full_name="Jean Kabuya",
        email="jean@example.cd",
        school_name="École Horizon",
    )
    school = SimpleNamespace(school_id="school-1", name_school="École Horizon")
    classe = SimpleNamespace(
        id="class-1",
        class_name="1ère Scientifique",
        titulaire_name="Aline Mukendi",
    )

    context = compose_teacher_classroom_context(teacher, school, [classe], [], [])

    assert context["classes"] == ["1ère Scientifique"]
    assert context["titulaireClasses"] == []
    assert context["classrooms"][0]["isTitulaire"] is False

def test_titular_matching_accepts_honorific_and_reordered_full_name():
    teacher = SimpleNamespace(
        teacher_code="prof-123456",
        full_name="Aline Mukendi",
        email="aline@example.cd",
    )
    classe = SimpleNamespace(titulaire_name="Prof. Mukendi Aline")

    assert teacher_is_titulaire(teacher, classe) is True


def test_titular_matching_does_not_grant_access_by_single_name_token():
    teacher = SimpleNamespace(
        teacher_code="prof-123456",
        full_name="Aline Mukendi",
        email="aline@example.cd",
    )
    classe = SimpleNamespace(titulaire_name="Prof. Mukendi")

    assert teacher_is_titulaire(teacher, classe) is False


def test_apply_teacher_classroom_context_preserves_local_notes_and_merges_students():
    local_state = {
        "user": {"email": "aline@example.cd", "password": "local-password"},
        "classes": ["Classe locale"],
        "students": {"Classe locale": [{"id": "local-student", "name": "Local"}]},
        "courses": {"Classe locale": ["Français"]},
        "grades": {"student_course": {"p1": 18}},
        "pendingCommits": 1,
    }
    context = {
        "schoolId": "school-1",
        "schoolName": "École Horizon",
        "classes": ["1ère Scientifique"],
        "students": {
            "1ère Scientifique": [{"id": "student-1", "name": "MUKENDI Aline"}]
        },
        "courses": {"1ère Scientifique": ["Mathématiques"]},
        "courseMax": {"1ère Scientifique_Mathématiques": 40},
        "titulaireClasses": ["1ère Scientifique"],
        "classrooms": [{"id": "class-1", "name": "1ère Scientifique", "isTitulaire": True}],
    }

    updated = apply_teacher_classroom_context(local_state, context)

    assert updated["classes"] == ["Classe locale", "1ère Scientifique"]
    assert updated["students"]["Classe locale"][0]["id"] == "local-student"
    assert updated["students"]["1ère Scientifique"][0]["id"] == "student-1"
    assert updated["courses"]["1ère Scientifique"] == ["Mathématiques"]
    assert updated["grades"] == {"student_course": {"p1": 18}}
    assert updated["pendingCommits"] == 1
    assert updated["user"]["password"] == "local-password"
    assert updated["user"]["titulaireOf"] == "1ère Scientifique"


def test_teacher_roster_link_requires_the_official_code_and_registered_name():
    teacher = SimpleNamespace(
        teacher_code="prof-123456",
        full_name="Aline Mukendi",
        school_name="École Horizon",
        school_id=None,
    )
    roster_entry = {"uniqueId": "prof-123456", "name": "ALINE MUKENDI"}

    assert teacher_matches_school_roster(teacher, roster_entry) is True

def test_school_classroom_access_requires_verified_school_roster_membership():
    not_verified = SimpleNamespace(school_id="school-1", school_access_verified=False)
    verified = SimpleNamespace(school_id="school-1", school_access_verified=True)
    no_school = SimpleNamespace(school_id=None, school_access_verified=True)

    assert teacher_has_verified_school_access(not_verified) is False
    assert teacher_has_verified_school_access(no_school) is False
    assert teacher_has_verified_school_access(verified) is True


def test_unverified_teacher_cannot_load_the_school_catalog_even_with_a_stale_school_id():
    teacher = SimpleNamespace(
        school_id="school-1",
        school_access_verified=False,
        full_name="Aline Mukendi",
        teacher_code="prof-123456",
        email="aline@example.cd",
    )

    class DatabaseMustNotBeQueried:
        def query(self, _model):
            raise AssertionError("Unverified teacher must not query school tables")

    from main import build_teacher_classroom_context
    context = build_teacher_classroom_context(DatabaseMustNotBeQueried(), teacher)

    assert context["schoolId"] == ""
    assert context["classes"] == []
    assert context["titulaireClasses"] == []


def test_teacher_roster_link_does_not_grant_access_by_school_name_or_name_alone():
    teacher = SimpleNamespace(
        teacher_code="prof-123456",
        full_name="Aline Mukendi",
        school_name="École Horizon",
        school_id=None,
    )

    assert teacher_matches_school_roster(teacher, {"uniqueId": "", "name": "Aline Mukendi"}) is False
    assert teacher_matches_school_roster(teacher, {"uniqueId": "other-code", "name": "Aline Mukendi"}) is False
    # The unique ID is the account credential for linking; a display-name difference must not reject it.
    assert teacher_matches_school_roster(teacher, {"uniqueId": "prof-123456", "name": "Aline Mukendi (enseignante)"}) is True
