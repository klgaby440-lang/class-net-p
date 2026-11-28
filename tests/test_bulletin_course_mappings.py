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
            licence_date="2026-12-31",
            is_locked=False,
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


def test_class_domains_and_linked_subdomains_save_and_reload():
    school_id, password, class_id, _, token = _school_with_class_and_courses()
    headers = {"Authorization": "Bearer " + token, "X-CRYPT-App": "primeink"}
    subdomains = {
        "Sciences": ["Géologie", "Écologie"],
        "Langues": ["Anglais", "Français"],
    }
    with TestClient(main.app) as client:
        saved = client.put(
            f"/api/primenet/classes/{class_id}/domains",
            headers=headers,
            json={"categories": ["Sciences", "Langues"], "subDomains": subdomains},
        )
        assert saved.status_code == 200
        assert saved.json()["data"]["subDomains"] == subdomains

        loaded = client.post(
            "/api/primenet/load",
            json={"school_id": school_id, "password": password},
        )
        assert loaded.status_code == 200
        classe = loaded.json()["data"]["classes"][0]
        assert classe["categories"] == ["Sciences", "Langues"]
        assert classe["subDomains"] == subdomains


def test_class_domains_reject_subdomain_linked_to_unknown_domain():
    _, _, class_id, _, token = _school_with_class_and_courses()
    headers = {"Authorization": "Bearer " + token, "X-CRYPT-App": "primeink"}
    with TestClient(main.app) as client:
        response = client.put(
            f"/api/primenet/classes/{class_id}/domains",
            headers=headers,
            json={
                "categories": ["Sciences"],
                "subDomains": {"Langues": ["Anglais"]},
            },
        )
    assert response.status_code == 422


def test_llink_conversations_are_persisted_and_separated_by_app():
    _, _, _, _, token = _school_with_class_and_courses()
    conversation_id = "llink-conv-" + uuid.uuid4().hex
    prime_headers = {"Authorization": "Bearer " + token, "X-CRYPT-App": "primeink"}
    manager_headers = {"Authorization": "Bearer " + token, "X-CRYPT-App": "manager"}
    payload = {
        "title": "Analyse de la classe",
        "messages": [
            {"role": "user", "text": "Analyse les résultats de la classe."},
            {"role": "assistant", "text": "Je peux examiner les données disponibles."},
        ],
    }

    with TestClient(main.app) as client:
        saved = client.put(
            f"/api/llink/conversations/{conversation_id}",
            headers=prime_headers,
            json=payload,
        )
        assert saved.status_code == 200
        assert saved.json()["data"]["title"] == payload["title"]

        prime_sessions = client.get("/api/llink/conversations", headers=prime_headers)
        assert prime_sessions.status_code == 200
        assert any(row["id"] == conversation_id for row in prime_sessions.json()["data"])

        manager_sessions = client.get("/api/llink/conversations", headers=manager_headers)
        assert manager_sessions.status_code == 200
        assert all(row["id"] != conversation_id for row in manager_sessions.json()["data"])

        deleted = client.delete(
            f"/api/llink/conversations/{conversation_id}",
            headers=prime_headers,
        )
        assert deleted.status_code == 200
        after_delete = client.get("/api/llink/conversations", headers=prime_headers)
        assert all(row["id"] != conversation_id for row in after_delete.json()["data"])
