import uuid

from fastapi.testclient import TestClient

import main


def _create_linked_school_and_teacher():
    suffix = uuid.uuid4().hex[:12]
    school_id = "attendance-school-" + suffix
    teacher_email = "attendance-" + suffix + "@example.test"
    db = main.SessionLocal()
    try:
        school = main.SchoolInformation(
            school_id=school_id,
            bulletin_seq_id="attendance-bulletin-" + suffix,
            code="AT-" + suffix.upper(),
            name_school="Établissement Présences " + suffix,
            city="Bukavu",
            commune="Ibanda",
            name_responsable="Direction",
            num_tel="+243000000000",
            adresse_physique="Bukavu",
            email="school-" + suffix + "@example.test",
            pass_word=main.hash_password("Test-Presence-2026"),
        )
        teacher = main.Teacher(
            teacher_code="PROF_" + suffix,
            email=teacher_email,
            full_name="Enseignant Présence " + suffix,
            password=main.hash_password("Teacher-Presence-2026"),
            school_name=school.name_school,
            school_id=school_id,
            school_access_verified=True,
            status="Actif",
        )
        db.add(school)
        db.add(teacher)
        db.flush()
        db.add(main.TeacherSchoolSpace(
            teacher_email=teacher_email,
            school_id=school_id,
            is_verified=True,
        ))
        db.commit()
    finally:
        db.close()
    token = main.create_token({"role": "school", "school_id": school_id}, 3600)
    return school_id, teacher_email, token


def test_teacher_attendance_roster_and_records_persist_and_update():
    school_id, teacher_email, token = _create_linked_school_and_teacher()
    headers = {"Authorization": "Bearer " + token, "X-CRYPT-App": "primeink"}
    date = "2026-11-28"

    with TestClient(main.app) as client:
        first_load = client.get(
            "/api/primenet/teacher-attendance",
            params={"date": date},
            headers=headers,
        )
        assert first_load.status_code == 200
        assert first_load.json()["status"] is True
        assert any(row["email"] == teacher_email for row in first_load.json()["teachers"])
        assert first_load.json()["data"] == []

        saved = client.post(
            "/api/primenet/teacher-attendance",
            headers=headers,
            json={
                "teacher_email": teacher_email.upper(),
                "date": date,
                "check_in": "07:25",
                "check_out": "15:30",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["data"]["checkIn"] == "07:25"
        assert saved.json()["data"]["checkOut"] == "15:30"

        # Une deuxième sauvegarde modifie la fiche du même enseignant/date,
        # sans créer un doublon ni effacer l'heure qui vient d'être changée.
        updated = client.post(
            "/api/primenet/teacher-attendance",
            headers=headers,
            json={
                "teacher_email": teacher_email,
                "date": date,
                "check_in": "07:40",
                "check_out": "15:45",
            },
        )
        assert updated.status_code == 200

        reloaded = client.get(
            "/api/primenet/teacher-attendance",
            params={"date": date},
            headers=headers,
        )
        assert reloaded.status_code == 200
        records = reloaded.json()["data"]
        assert len(records) == 1
        assert records[0]["teacherEmail"] == teacher_email
        assert records[0]["checkIn"] == "07:40"
        assert records[0]["checkOut"] == "15:45"

        manager_view = client.get(
            "/api/manager/attendance",
            params={"date": date},
            headers=headers,
        )
        assert manager_view.status_code == 200
        assert len(manager_view.json()["data"]["teachers"]) == 1
        assert manager_view.json()["data"]["teachers"][0]["checkIn"] == "07:40"


def test_teacher_attendance_rejects_teacher_from_another_school():
    school_id, teacher_email, token = _create_linked_school_and_teacher()
    other_school_id = "other-school-" + uuid.uuid4().hex[:12]
    db = main.SessionLocal()
    try:
        db.add(main.SchoolInformation(
            school_id=other_school_id,
            bulletin_seq_id="other-bulletin-" + uuid.uuid4().hex[:12],
            code="OTH-" + uuid.uuid4().hex[:8].upper(),
            name_school="Autre établissement",
            city="Bukavu",
            commune="Ibanda",
            name_responsable="Direction",
            num_tel="+243000000000",
            adresse_physique="Bukavu",
            email="other-" + uuid.uuid4().hex[:8] + "@example.test",
            pass_word=main.hash_password("Other-School-2026"),
        ))
        db.commit()
    finally:
        db.close()

    headers = {"Authorization": "Bearer " + main.create_token({"role": "school", "school_id": other_school_id}, 3600)}
    with TestClient(main.app) as client:
        response = client.post(
            "/api/primenet/teacher-attendance",
            headers=headers,
            json={
                "teacher_email": teacher_email,
                "date": "2026-11-28",
                "check_in": "07:30",
                "check_out": "",
            },
        )
    assert response.status_code == 404
