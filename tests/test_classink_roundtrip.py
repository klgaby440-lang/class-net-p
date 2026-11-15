import json
from pathlib import Path
from uuid import uuid4

from main import (
    ClasseInformation,
    CourseInformation,
    SchoolInformation,
    SchoolStudentInformation,
    SessionLocal,
    Teacher,
    TeacherSchoolSpace,
    get_teacher_classroom_information,
    is_suivi_results_allowed,
    lookup_teacher_for_school,
    prepare_classroom_information,
)


FIXTURE = Path(__file__).parent / "fixtures" / "classink_school_test_seed.json"


def build_roundtrip_payload(seed, suffix, school_id):
    class_ids = {row["id"]: row["id"] + "-" + suffix for row in seed["classes"]}
    teacher_ids = {row["uniqueId"]: row["uniqueId"] + "-" + suffix for row in seed["teachers"]}
    return {
        "school": {
            "id": school_id,
            "name": seed["school"]["name"],
            "code": "T-" + suffix,
            "city": seed["school"]["city"],
            "commune": seed["school"]["commune"],
        },
        "classes": [
            {
                "id": class_ids[row["id"]],
                "name": row["name"],
                "titulaire": teacher_ids.get(row["titulaire"], row["titulaire"]),
                "categories": ["Démonstration"],
                "bulletinModel": "classink",
                "bulletinTitle": row["name"],
                "signatureSource": "titulaire",
            }
            for row in seed["classes"]
        ],
        "students": [
            {
                **row,
                "id": row["id"] + "-" + suffix,
                "classId": class_ids[row["classId"]],
                "permi": row["permi"] + "-" + suffix,
            }
            for row in seed["students"]
        ],
        "courses": [
            {
                **row,
                "id": row["id"] + "-" + suffix,
                "classId": class_ids[row["classId"]],
            }
            for row in seed["courses"]
        ],
        "teachers": [
            {
                **row,
                "uniqueId": teacher_ids[row["uniqueId"]],
            }
            for row in seed["teachers"]
        ],
    }


def test_primeink_catalog_write_and_classink_teacher_read_roundtrip_for_all_six_teachers():
    seed = json.loads(FIXTURE.read_text(encoding="utf-8"))
    suffix = uuid4().hex[:10].upper()
    school_id = "SCHOOL-ROUNDTRIP-" + suffix
    payload = build_roundtrip_payload(seed, suffix, school_id)
    teachers = []
    db = SessionLocal()

    try:
        school = SchoolInformation(
            school_id=school_id,
            bulletin_seq_id="BUL-" + suffix,
            code="T-" + suffix,
            name_school=seed["school"]["name"],
            city=seed["school"]["city"],
            commune=seed["school"]["commune"],
            name_responsable=seed["school"]["responsable"],
            num_tel="+243000000000",
            adresse_physique="Adresse synthétique de recette",
            email="school-" + suffix.lower() + "@example.invalid",
            pass_word="test-only-password-hash",
            licence_date="2026-12-31",
            is_locked=False,
        )
        db.add(school)

        for item in payload["teachers"]:
            teacher = Teacher(
                teacher_code=item["uniqueId"],
                email=(item["uniqueId"].lower() + "@example.invalid"),
                full_name=item["name"],
                password="test-only-password-hash",
                school_name=seed["school"]["name"],
            )
            db.add(teacher)
            teachers.append(teacher)

        db.flush()
        for teacher in teachers:
            # Simule la demande de l'espace faite depuis ClassInk, avant l'ajout dans PrimeInk.
            db.add(TeacherSchoolSpace(
                teacher_email=teacher.email,
                school_id=school_id,
                is_verified=False,
            ))
        db.commit()

        # PrimeInk recherche les vrais comptes avant l'ajout à la liste officielle.
        for item, teacher in zip(payload["teachers"], teachers):
            profile = lookup_teacher_for_school(
                item["uniqueId"], db, {"school_id": school_id, "role": "school"}
            )
            assert profile["status"] is True
            assert profile["data"]["name"] == teacher.full_name
            assert profile["data"]["alreadyLinked"] is False

        # PrimeInk publie ensuite les classes, élèves, cours et enseignants sur le serveur.
        published = prepare_classroom_information(payload, db, {"school_id": school_id})
        assert published["status"] is True
        assert published["counts"] == {
            "classes": 3,
            "students": 15,
            "courses": 6,
            "linkedTeachers": 6,
        }

        # Applique les 10 états parent du jeu de recette et vérifie la règle d'accès réelle.
        for parent in seed["parents"]:
            student = db.query(SchoolStudentInformation).filter_by(
                id=parent["studentId"] + "-" + suffix,
                school_id=school_id,
            ).first()
            assert student is not None
            assert student.suivi_code
            student.suivi_results_allowed = bool(parent["individualAccessAllowed"])
        db.commit()
        for parent in seed["parents"]:
            student = db.query(SchoolStudentInformation).filter_by(
                id=parent["studentId"] + "-" + suffix,
                school_id=school_id,
            ).first()
            assert is_suivi_results_allowed(db, school_id, student) is bool(parent["individualAccessAllowed"])

        # Contrôle de la persistance en base, après la publication.
        assert db.query(ClasseInformation).filter_by(school_id=school_id).count() == 3
        assert db.query(SchoolStudentInformation).filter_by(school_id=school_id).count() == 15
        assert db.query(CourseInformation).filter_by(school_id=school_id).count() == 6
        assert db.query(TeacherSchoolSpace).filter_by(
            school_id=school_id, is_verified=True
        ).count() == 6

        # Chaque compte enseignant récupère le catalogue officiel du même espace.
        for teacher in teachers:
            recovered = get_teacher_classroom_information(
                school_id=school_id,
                db=db,
                claims={
                    "email": teacher.email,
                    "school_id": school_id,
                    "role": "teacher",
                },
            )
            assert recovered["status"] is True
            context = recovered["data"]
            assert context["schoolId"] == school_id
            assert len(context["classes"]) == 3
            assert sum(len(rows) for rows in context["students"].values()) == 15
            assert sum(len(rows) for rows in context["courses"].values()) == 6
    finally:
        # Le jeu de recette est supprimé même si une assertion échoue.
        db.rollback()
        db.query(TeacherSchoolSpace).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(CourseInformation).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(SchoolStudentInformation).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.query(ClasseInformation).filter_by(school_id=school_id).delete(synchronize_session=False)
        if teachers:
            db.query(Teacher).filter(
                Teacher.email.in_([teacher.email for teacher in teachers])
            ).delete(synchronize_session=False)
        db.query(SchoolInformation).filter_by(school_id=school_id).delete(synchronize_session=False)
        db.commit()
        db.close()
