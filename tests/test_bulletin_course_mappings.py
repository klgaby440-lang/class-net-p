import uuid

from fastapi.testclient import TestClient

import main


def _school_with_class_and_courses():
    suffix = uuid.uuid4().hex[:12]
    school_id = "bulletin-courses-" + suffix
    password = "Bulletin-Course-2026"
    class_id = "class-" + suffix
    course_ids = ["geology-" + suffix, "ecology-" + suffix]
    db = main.SessionLocal()
    try:
        school = main.SchoolInformation(
            school_id=school_id,
            bulletin_seq_id="bulletin-" + suffix,
            code="BC-" + suffix.upper(),
            name_school="École cours bulletins " + suffix,
            city="Bukavu",
            commune="Ibanda",
            name_responsable="Direction",
            num_tel="+243000000000",
            adresse_physique="Bukavu",
            email="bulletin-" + suffix + "@example.test",
            pass_word=main.hash_password(password),
        )
        classe = main.ClasseInformation(
            id=class_id,
            school_id=school_id,
            class_name="3ème Scientifique",
        )
        db.add_all([school, classe])
        db.flush()
        db.add_all([
            main.CourseInformation(
                id=course_ids[0], school_id=school_id, class_id=class_id,
                course_name="Géologie", max_per=20, category="Sciences",
                titulaire_name="Prof. A",
            ),
            main.CourseInformation(
                id=course_ids[1], school_id=school_id, class_id=class_id,
                course_name="Écologie", max_per=20, category="Sciences",
                titulaire_name="Prof. B",
            ),
        ])
        db.commit()
    finally:
        db.close()
    token = main.create_token({"role": "school", "school_id": school_id}, 3600)
    return school_id, password, class_id, course_ids, token


def test_bulletin_courses_are_composed_from_taught_courses_and_reload():
    school_id, password, class_id, course_ids, token = _school_with_class_and_courses()
    headers = {"Authorization": "Bearer " + token, "X-CRYPT-App": "primeink"}
    config = [
        {
            "id": "bc-geologie-ecologie",
            "name": "Géologie / Écologie",
            "maxPer": 20,
            "category": "Sciences",
            "subCategory": "Sciences de la Terre",
            "sourceCourseIds": course_ids,
        }
    ]
    with TestClient(main.app) as client:
        saved = client.put(
            f"/api/primenet/classes/{class_id}/bulletin-courses",
            headers=headers,
            json={"bulletinCourses": config},
        )
        assert saved.status_code == 200
        assert saved.json()["data"]["bulletinCourses"][0]["sourceCourseIds"] == course_ids

        loaded = client.post(
            "/api/primenet/load",
            json={"school_id": school_id, "password": password},
        )
        assert loaded.status_code == 200
        assert loaded.json()["status"] is True
        classes = loaded.json()["data"]["classes"]
        assert len(classes) == 1
        assert classes[0]["bulletinCourses"][0]["name"] == "Géologie / Écologie"
        assert classes[0]["bulletinCourses"][0]["sourceCourseIds"] == course_ids
        # Le catalogue des cours enseignés reste composé des deux matières distinctes.
        assert {row["name"] for row in loaded.json()["data"]["courses"]} == {"Géologie", "Écologie"}


def test_bulletin_course_mapping_rejects_unknown_source_course():
    _, _, class_id, _, token = _school_with_class_and_courses()
    headers = {"Authorization": "Bearer " + token, "X-CRYPT-App": "primeink"}
    with TestClient(main.app) as client:
        response = client.put(
            f"/api/primenet/classes/{class_id}/bulletin-courses",
            headers=headers,
            json={"bulletinCourses": [{
                "id": "bc-invalid",
                "name": "Cours invalide",
                "maxPer": 20,
                "sourceCourseIds": ["course-from-another-class"],
            }]},
        )
    assert response.status_code == 422
