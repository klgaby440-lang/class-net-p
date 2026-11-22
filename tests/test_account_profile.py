from uuid import uuid4

from fastapi.testclient import TestClient

import main


def _headers(claims):
    return {"Authorization": "Bearer " + main.create_token(claims, 3600)}


def test_teacher_profile_can_update_name_phone_and_password_but_not_identifier():
    suffix = uuid4().hex[:12]
    email = f"teacher-profile-{suffix}@example.invalid"
    teacher_code = "TEACHER-" + suffix.upper()
    current_password = "CurrentPass#123"
    new_password = "UpdatedPass#456"
    db = main.SessionLocal()
    teacher_id = None
    try:
        teacher = main.Teacher(
            teacher_code=teacher_code,
            email=email,
            full_name="Enseignant Test",
            password=main.hash_password(current_password),
            phone_number=None,
        )
        db.add(teacher)
        db.commit()
        teacher_id = teacher.id
    finally:
        db.close()

    headers = _headers({"role": "teacher", "email": email})
    try:
        with TestClient(main.app) as client:
            before = client.get("/api/teacher/profile", headers=headers)
            assert before.status_code == 200
            assert before.json()["data"]["email"] == email
            assert "password" not in before.json()["data"]

            response = client.put(
                "/api/teacher/profile",
                headers=headers,
                json={
                    "current_password": current_password,
                    "new_password": new_password,
                    "full_name": "Enseignant Mis à jour",
                    "phone_number": "0812345678",
                    "email": "attacker@example.invalid",
                    "teacher_code": "CHANGED-CODE",
                },
            )
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["email"] == email
            assert data["teacher_code"] == teacher_code
            assert data["full_name"] == "Enseignant Mis à jour"
            assert data["phone_number"] == "+243812345678"
            assert "password" not in data

            wrong = client.put(
                "/api/teacher/profile",
                headers=headers,
                json={"current_password": current_password, "full_name": "Nope"},
            )
            assert wrong.status_code == 403
    finally:
        db = main.SessionLocal()
        try:
            row = db.query(main.Teacher).filter(main.Teacher.id == teacher_id).first()
            if row:
                assert row.email == email
                assert row.teacher_code == teacher_code
                assert main.verify_password(new_password, row.password)
                db.delete(row)
                db.commit()
        finally:
            db.close()


def test_school_profile_can_update_contact_and_password_but_not_school_id_or_email():
    suffix = uuid4().hex[:12].upper()
    school_id = "SCHOOL-PROFILE-" + suffix
    email = f"school-profile-{suffix.lower()}@example.invalid"
    current_password = "SchoolPass#123"
    new_password = "SchoolPass#456"
    db = main.SessionLocal()
    row_id = None
    try:
        school = main.SchoolInformation(
            school_id=school_id,
            bulletin_seq_id="BUL-" + suffix,
            code="CODE-" + suffix,
            name_school="École de Test",
            city="Bukavu",
            commune="Ibanda",
            name_responsable="Responsable Test",
            num_tel="+243000000001",
            adresse_physique="Adresse de test",
            email=email,
            pass_word=main.hash_password(current_password),
            licence_date="2026-12-31",
            is_locked=False,
        )
        db.add(school)
        db.commit()
        row_id = school.id
    finally:
        db.close()

    headers = _headers({"role": "school", "school_id": school_id})
    try:
        with TestClient(main.app) as client:
            before = client.get("/api/school/profile", headers=headers)
            assert before.status_code == 200
            assert before.json()["data"]["email"] == email
            assert before.json()["data"]["school_id"] == school_id
            assert "pass_word" not in before.json()["data"]

            response = client.put(
                "/api/school/profile",
                headers=headers,
                json={
                    "current_password": current_password,
                    "new_password": new_password,
                    "name_school": "École Actualisée",
                    "name_responsable": "Nouvelle Responsable",
                    "num_tel": "0895281304",
                    "city": "Goma",
                    "commune": "Karisimbi",
                    "adresse_physique": "Nouvelle adresse",
                    "school_id": "ATTACK-SCHOOL-ID",
                    "email": "attacker@example.invalid",
                    "code": "ATTACK-CODE",
                    "bulletin_seq_id": "ATTACK-BULLETIN",
                },
            )
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["school_id"] == school_id
            assert data["email"] == email
            assert data["name_school"] == "École Actualisée"
            assert data["num_tel"] == "+243895281304"
            assert "pass_word" not in data
    finally:
        db = main.SessionLocal()
        try:
            row = db.query(main.SchoolInformation).filter(main.SchoolInformation.id == row_id).first()
            if row:
                assert row.school_id == school_id
                assert row.email == email
                assert row.code == "CODE-" + suffix
                assert row.bulletin_seq_id == "BUL-" + suffix
                assert main.verify_password(new_password, row.pass_word)
                db.delete(row)
                db.commit()
        finally:
            db.close()
