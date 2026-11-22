from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from main import (
    ClasseInformation,
    CourseInformation,
    SchoolInformation,
    SchoolStudentInformation,
    SessionLocal,
    StudentGrade,
    StudentLink,
    Teacher,
    TeacherEvaluation,
    TeacherSchoolSpace,
    apply_teacher_classroom_context,
    calculate_classink_period_average,
    compose_teacher_classroom_context,
    consolidate_classink_period_averages,
    generate_suivi_student_code,
    is_suivi_results_allowed,
    teacher_has_verified_school_access,
    teacher_has_school_access,
    teacher_school_spaces,
    TeacherSchoolSpace,
    ClassInkTeacherSpaceSnapshot,
    StudentGrade,
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


def test_classink_period_average_combines_multiple_interrogations_as_one_weighted_mean():
    # 8/10 + 15/20 = 23/30, ramené au barème P1 sur 40 points.
    assert calculate_classink_period_average([(8, 10), (15, 20)], 40, "P1") == 30.67
    # Les examens sont ramenés au double du maximum du cours.
    assert calculate_classink_period_average([(14, 20), (16, 20)], 40, "EX1") == 60.0
    # Une note nulle reste une note valable et participe à la moyenne.
    assert calculate_classink_period_average([(0, 10), (10, 10)], 20, "P2") == 10.0
    # Une période sans note valide ne produit aucune moyenne artificielle.
    assert calculate_classink_period_average([], 40, "P1") is None
    assert calculate_classink_period_average([(15, 10)], 40, "P1") is None


def test_verified_teacher_interrogations_are_consolidated_by_official_student_course_and_period():
    suffix = uuid4().hex[:12].upper()
    school_id = "SCHOOL-CLASSINK-AVERAGE-" + suffix
    teacher_email = "teacher-" + suffix.lower() + "@example.invalid"
    teacher_code = "PROF-" + suffix
    class_id = "CLASS-" + suffix
    course_id = "COURSE-" + suffix
    student_id = "STUDENT-" + suffix
    student_permanent = "PERMANENT-" + suffix
    db = SessionLocal()
    try:
        db.add(SchoolInformation(
            school_id=school_id,
            bulletin_seq_id="BUL-" + suffix,
            code="CODE-" + suffix,
            name_school="École de test moyenne ClassInk",
            city="Bukavu",
            commune="Ibanda",
            name_responsable="Responsable test",
            num_tel="+243000000000",
            adresse_physique="Adresse test",
            email="school-" + suffix.lower() + "@example.invalid",
            pass_word="test-only-password",
            licence_date="2026-12-31",
            is_locked=False,
        ))
        teacher = Teacher(
            teacher_code=teacher_code,
            email=teacher_email,
            full_name="Enseignant Test",
            password="test-only-password",
            school_name="École de test moyenne ClassInk",
        )
        db.add(teacher)
        db.flush()
        db.add(TeacherSchoolSpace(
            teacher_email=teacher_email,
            school_id=school_id,
            is_verified=True,
        ))
        db.add(ClasseInformation(
            id=class_id,
            school_id=school_id,
            class_name="2ème Scientifique",
            titulaire_name=teacher_code,
        ))
        db.add(CourseInformation(
            id=course_id,
            school_id=school_id,
            class_id=class_id,
            course_name="Mathématiques",
            max_per=40,
            category="Sciences",
            titulaire_name=teacher_code,
            sub_category="Algèbre",
        ))
        db.add(SchoolStudentInformation(
            id=student_id,
            school_id=school_id,
            class_id=class_id,
            student_name="KABUYA",
            student_post_name="Jean",
            student_pre_name="Paul",
            student_sexe="M",
            student_n_permanent=student_permanent,
        ))
        db.flush()
        db.add(StudentLink(
            school_id=school_id,
            teacher_email=teacher_email,
            classink_student_id="local-student-" + suffix,
            student_id=student_id,
            match_method="permanent",
        ))
        db.add_all([
            StudentGrade(
                teacher_email=teacher_email,
                student_id="local-student-" + suffix,
                student_name="KABUYA Jean Paul",
                class_name="2ème Scientifique",
                course_name="Mathématiques — Algèbre",
                course_id=course_id,
                eval_id="EV-P1-A-" + suffix,
                eval_name="Interrogation 1",
                eval_date="2026-10-01",
                period="P1",
                score=8,
                max_score=10,
                school_id=school_id,
            ),
            StudentGrade(
                teacher_email=teacher_email,
                student_id="local-student-" + suffix,
                student_name="KABUYA Jean Paul",
                class_name="2ème Scientifique",
                course_name="Mathématiques — Algèbre",
                course_id=course_id,
                eval_id="EV-P1-B-" + suffix,
                eval_name="Interrogation 2",
                eval_date="2026-10-15",
                period="P1",
                score=15,
                max_score=20,
                school_id=school_id,
            ),
            StudentGrade(
                teacher_email=teacher_email,
                student_id="local-student-" + suffix,
                student_name="KABUYA Jean Paul",
                class_name="2ème Scientifique",
                course_name="Mathématiques — Algèbre",
                course_id=course_id,
                eval_id="EV-EX1-A-" + suffix,
                eval_name="Examen 1",
                eval_date="2026-11-01",
                period="EX1",
                score=14,
                max_score=20,
                school_id=school_id,
            ),
            StudentGrade(
                teacher_email=teacher_email,
                student_id="local-student-" + suffix,
                student_name="KABUYA Jean Paul",
                class_name="2ème Scientifique",
                course_name="Mathématiques — Algèbre",
                course_id=course_id,
                eval_id="EV-EX1-B-" + suffix,
                eval_name="Examen 2",
                eval_date="2026-11-15",
                period="EX1",
                score=16,
                max_score=20,
                school_id=school_id,
            ),
        ])
        db.commit()

        report = consolidate_classink_period_averages(db, school_id)
        db.commit()

        assert report == {"teachers": 1, "interrogations": 4, "averages": 2}
        row = db.query(TeacherEvaluation).filter_by(
            school_id=school_id,
            student_n_permanent=student_permanent,
            course_id=course_id,
        ).one()
        assert row.p1 == 30.67
        assert row.ex1 == 60.0
    finally:
        db.rollback()
        db.query(TeacherEvaluation).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(StudentGrade).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(StudentLink).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(TeacherSchoolSpace).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(CourseInformation).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(SchoolStudentInformation).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(ClasseInformation).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(Teacher).filter_by(email=teacher_email).delete(synchronize_session=False)
        db.query(SchoolInformation).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.commit()
        db.close()


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
        titulaire_name="aline@example.cd",
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


def test_one_teacher_keeps_each_primeink_course_separate_in_the_same_class():
    teacher = SimpleNamespace(
        id=3,
        teacher_code="teacher-code-3",
        full_name="Jean Kabuya",
        email="jean@example.cd",
        school_name="École Horizon",
    )
    school = SimpleNamespace(school_id="school-1", name_school="École Horizon")
    classe = SimpleNamespace(id="class-1", class_name="2ème Scientifique", titulaire_name="")
    courses = [
        SimpleNamespace(id="course-math", class_id="class-1", course_name="Mathématiques",
                        max_per=40, category="Sciences", sub_category="Algèbre",
                        titulaire_name="teacher-code-3"),
        SimpleNamespace(id="course-physics", class_id="class-1", course_name="Physique",
                        max_per=30, category="Sciences", sub_category="Sciences physiques",
                        titulaire_name="teacher-code-3"),
        SimpleNamespace(id="course-chemistry", class_id="class-1", course_name="Chimie",
                        max_per=20, category="Sciences", sub_category="Chimie",
                        titulaire_name="teacher-code-3"),
        SimpleNamespace(id="course-french", class_id="class-1", course_name="Français",
                        max_per=50, category="Langues", sub_category="Langues",
                        titulaire_name="another-teacher"),
    ]

    context = compose_teacher_classroom_context(teacher, school, [classe], [], courses)

    assert context["classes"] == ["2ème Scientifique"]
    assert context["courses"]["2ème Scientifique"] == ["Chimie", "Mathématiques", "Physique"]
    assert [item["id"] for item in context["courseCatalog"]["2ème Scientifique"]] == [
        "course-chemistry", "course-math", "course-physics"
    ]
    assert context["courseMax"]["2ème Scientifique_Mathématiques"] == 40
    assert all(item["name"] != "Français" for item in context["courseCatalog"]["2ème Scientifique"])


def test_same_named_primeink_courses_are_disambiguated_and_old_notes_are_preserved():
    teacher = SimpleNamespace(
        id=4,
        teacher_code="teacher-code-4",
        full_name="Aline Mukendi",
        email="aline@example.cd",
        school_name="École Horizon",
    )
    school = SimpleNamespace(school_id="school-1", name_school="École Horizon")
    classe = SimpleNamespace(id="class-1", class_name="1ère Scientifique", titulaire_name="")
    courses = [
        SimpleNamespace(id="course-algebra", class_id="class-1", course_name="Mathématiques",
                        max_per=40, category="Sciences", sub_category="Algèbre",
                        titulaire_name="teacher-code-4"),
        SimpleNamespace(id="course-geometry", class_id="class-1", course_name="Mathématiques",
                        max_per=30, category="Sciences", sub_category="Géométrie",
                        titulaire_name="teacher-code-4"),
    ]

    context = compose_teacher_classroom_context(teacher, school, [classe], [], courses)
    labels = context["courses"]["1ère Scientifique"]

    assert labels == ["Mathématiques — Algèbre", "Mathématiques — Géométrie"]
    assert [item["id"] for item in context["courseCatalog"]["1ère Scientifique"]] == [
        "course-algebra", "course-geometry"
    ]

    previous = {
        "evaluations": {
            "1ère Scientifique": {
                "P1": [{"id": "EV-legacy-math", "course": "Mathématiques", "name": "Interro", "max": 20}]
            }
        },
        "grades": {"student-1_EV-legacy-math": 15},
    }
    updated = apply_teacher_classroom_context(previous, context)
    archive_label = "Mathématiques — Notes historiques à classer"

    assert updated["courses"]["1ère Scientifique"] == labels + [archive_label]
    assert updated["evaluations"]["1ère Scientifique"]["P1"][0]["course"] == archive_label
    assert updated["evaluations"]["1ère Scientifique"]["P1"][0]["legacyCourse"] is True
    assert updated["grades"] == {"student-1_EV-legacy-math": 15}


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

    assert context["classes"] == []
    assert context["students"] == {}
    assert context["courses"] == {}
    assert context["titulaireClasses"] == []
    assert context["classrooms"] == []

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


def test_apply_teacher_classroom_context_replaces_unassigned_catalog_and_keeps_only_assigned_notes():
    local_state = {
        "user": {"email": "aline@example.cd", "password": "local-password"},
        "classes": ["Classe locale", "1ère Scientifique"],
        "students": {
            "Classe locale": [{"id": "local-student", "name": "Local"}],
            "1ère Scientifique": [{"id": "student-1", "name": "MUKENDI Aline"}],
        },
        "courses": {
            "Classe locale": ["Français"],
            "1ère Scientifique": ["Mathématiques", "Français"],
        },
        "evaluations": {
            "Classe locale": {"P1": [{"id": "EV-local", "course": "Français"}]},
            "1ère Scientifique": {"P1": [
                {"id": "EV-math", "course": "Mathématiques"},
                {"id": "EV-french", "course": "Français"},
            ]},
        },
        "grades": {
            "local-student_EV-local": 18,
            "student-1_EV-math": 17,
            "student-1_EV-french": 16,
        },
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

    assert updated["classes"] == ["1ère Scientifique"]
    assert list(updated["students"]) == ["1ère Scientifique"]
    assert updated["students"]["1ère Scientifique"][0]["id"] == "student-1"
    assert updated["courses"] == {"1ère Scientifique": ["Mathématiques"]}
    assert updated["courseMax"] == {"1ère Scientifique_Mathématiques": 40}
    assert updated["evaluations"]["1ère Scientifique"]["P1"] == [
        {"id": "EV-math", "course": "Mathématiques"}
    ]
    assert updated["grades"] == {"student-1_EV-math": 17}
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
        school_name="École Horizon",
        school_access_verified=False,
        full_name="Aline Mukendi",
        teacher_code="prof-123456",
        email="aline@example.cd",
    )

    class EmptyAssociationQuery:
        def filter(self, *_args):
            return self

        def first(self):
            return None

    class DatabaseMustNotLoadSchoolCatalog:
        def query(self, model):
            if model is TeacherSchoolSpace:
                return EmptyAssociationQuery()
            raise AssertionError("Unverified teacher must not query school, class, student or course catalog tables")

    from main import build_teacher_classroom_context
    context = build_teacher_classroom_context(DatabaseMustNotLoadSchoolCatalog(), teacher)

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


class _StaticRulesQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args):
        return self

    def all(self):
        return self.rows


class _StaticRulesDatabase:
    def __init__(self, rows):
        self.rows = rows

    def query(self, _model):
        return _StaticRulesQuery(self.rows)


def test_suivi_student_codes_are_unique_format_and_not_sequential():
    import re

    first = generate_suivi_student_code()
    second = generate_suivi_student_code()
    pattern = re.compile(r"^CI-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{5}-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{5}-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{5}$")

    assert pattern.fullmatch(first)
    assert pattern.fullmatch(second)
    assert first != second


def test_suivi_results_require_the_individual_student_switch():
    student = SimpleNamespace(class_id="class-1", suivi_results_allowed=False)
    db = _StaticRulesDatabase([])

    assert is_suivi_results_allowed(db, "school-1", student) is False


def test_suivi_results_are_visible_only_when_individual_and_school_rules_allow_them():
    student = SimpleNamespace(class_id="class-1", suivi_results_allowed=True)
    db = _StaticRulesDatabase([])

    assert is_suivi_results_allowed(db, "school-1", student) is True


def test_suivi_global_or_period_rule_can_still_block_parent_results():
    student = SimpleNamespace(class_id="class-1", suivi_results_allowed=True)
    rules = [
        SimpleNamespace(
            audience="suivi",
            class_id="class-1",
            period="P1",
            is_allowed=False,
            updated_at=None,
        )
    ]
    db = _StaticRulesDatabase(rules)

    assert is_suivi_results_allowed(db, "school-1", student) is False




class _SpaceQuery:
    def __init__(self, rows):
        self.rows = list(rows)

    def filter(self, *conditions):
        filtered = list(self.rows)
        for condition in conditions:
            left = getattr(condition, "left", None)
            right = getattr(condition, "right", None)
            field = getattr(left, "key", None)
            value = getattr(right, "value", right)
            operator_name = getattr(getattr(condition, "operator", None), "__name__", "")
            if operator_name == "is_" and type(right).__name__ == "True_":
                value = True
            elif operator_name == "is_" and type(right).__name__ == "False_":
                value = False
            if not field:
                continue
            if operator_name == "in_op":
                filtered = [row for row in filtered if getattr(row, field, None) in value]
            elif operator_name == "is_":
                filtered = [row for row in filtered if getattr(row, field, None) is value]
            else:
                filtered = [row for row in filtered if getattr(row, field, None) == value]
        self.rows = filtered
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _SpaceDatabase:
    def __init__(self, associations=(), schools=()):
        self.associations = list(associations)
        self.schools = list(schools)

    def query(self, model):
        if model is TeacherSchoolSpace:
            return _SpaceQuery(self.associations)
        return _SpaceQuery(self.schools)


def test_teacher_can_keep_two_verified_school_spaces_without_switching_legacy_primary():
    teacher = SimpleNamespace(
        email="teacher@example.cd",
        school_id="school-a",
        school_access_verified=True,
    )
    spaces = [
        SimpleNamespace(teacher_email=teacher.email, school_id="school-a", is_verified=True),
        SimpleNamespace(teacher_email=teacher.email, school_id="school-b", is_verified=True),
    ]
    schools = [
        SimpleNamespace(school_id="school-a", name_school="École A", city="Bukavu"),
        SimpleNamespace(school_id="school-b", name_school="École B", city="Goma"),
    ]
    db = _SpaceDatabase(spaces, schools)

    assert teacher_has_school_access(db, teacher, "school-a") is True
    assert teacher_has_school_access(db, teacher, "school-b") is True
    assert teacher_has_school_access(db, teacher, "school-c") is False
    assert [item["schoolId"] for item in teacher_school_spaces(db, teacher)] == ["school-a", "school-b"]
    assert teacher.school_id == "school-a"


def test_pending_teacher_school_space_does_not_grant_access_until_school_confirms():
    teacher = SimpleNamespace(
        email="teacher@example.cd",
        school_id=None,
        school_access_verified=False,
    )
    pending = SimpleNamespace(teacher_email=teacher.email, school_id="school-b", is_verified=False)
    db = _SpaceDatabase([pending], [])

    assert teacher_has_school_access(db, teacher, "school-b") is False
    pending.is_verified = True
    assert teacher_has_school_access(db, teacher, "school-b") is True


def test_teacher_space_snapshot_and_grade_records_are_school_scoped():
    assert "school_id" in ClassInkTeacherSpaceSnapshot.__table__.columns
    assert "teacher_email" in ClassInkTeacherSpaceSnapshot.__table__.columns
    assert "school_id" in StudentGrade.__table__.columns
