import uuid

import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def crypt_client(monkeypatch):
    monkeypatch.setattr(main, "ADMIN_API_KEY", "ci-only-crypt-admin-key")
    with TestClient(main.app) as client:
        yield client


def create_school_payload():
    suffix = uuid.uuid4().hex[:10]
    return {
        "school_id": "ci-school-" + suffix,
        "bulletin_seq_id": "ci-bulletin-" + suffix,
        "code": "CI-" + suffix.upper(),
        "name_school": "Établissement CI " + suffix,
        "city": "Bukavu",
        "commune": "Ibanda",
        "name_responsable": "Responsable Test",
        "num_tel": "+243000000000",
        "adresse_physique": "Adresse de test",
        "email": "ci-" + suffix + "@example.test",
        "pass_word": "MotDePasse-Test-2026",
    }


def test_crypt_console_create_account_schedule_telemetry_ticket_and_readonly_query(crypt_client):
    admin_login = crypt_client.post(
        "/api/crypt-console/login",
        json={"admin_key": "ci-only-crypt-admin-key"},
    )
    assert admin_login.status_code == 200
    admin_payload = admin_login.json()
    assert admin_payload["status"] is True
    admin_headers = {"Authorization": "Bearer " + admin_payload["token"]}

    school_payload = create_school_payload()
    created = crypt_client.post(
        "/api/crypt-console/schools",
        headers=admin_headers,
        json=school_payload,
    )
    assert created.status_code == 200
    assert created.json()["status"] is True

    school_login = crypt_client.post(
        "/api/manager/login",
        json={
            "identifier": school_payload["school_id"],
            "password": school_payload["pass_word"],
        },
    )
    assert school_login.status_code == 200
    assert school_login.headers.get("cache-control", "").startswith("no-store")
    school_token = school_login.json()["token"]
    school_headers = {
        "Authorization": "Bearer " + school_token,
        "X-CRYPT-App": "manager",
        "X-CRYPT-Device": "ci-manager-device-0001",
    }

    schedule_result = {
        "success": True,
        "schedules": {
            "classes": [
                {
                    "class_name": "1ère Scientifique",
                    "grid": {"Lundi": {"1": {"course": "Mathématiques", "teacher": "Prof Test", "room": "A1"}}},
                }
            ],
            "teachers": [],
        },
    }
    published = crypt_client.put(
        "/api/manager/schedule",
        headers=school_headers,
        json={
            "school_id": school_payload["school_id"],
            "schedule": {
                "academicYear": "2026-2027",
                "generatedAt": "2026-11-10T10:00:00",
                "config": {"jours": 5, "heures": 6},
                "slots": [{"start": "07:30", "end": "08:15"}],
                "teachers": [],
                "result": schedule_result,
            },
        },
    )
    assert published.status_code == 200

    latest_schedule = crypt_client.get("/api/manager/schedule", headers=school_headers)
    assert latest_schedule.status_code == 200
    assert latest_schedule.headers.get("cache-control", "").startswith("no-store")
    assert latest_schedule.json()["data"]["result"]["schedules"]["classes"][0]["class_name"] == "1ère Scientifique"

    device_id = "ci-manager-device-0001"
    event = crypt_client.post(
        "/api/telemetry/events",
        headers={
            "X-CRYPT-App": "manager",
            "X-CRYPT-Device": device_id,
            "X-CRYPT-School": school_payload["school_id"],
        },
        json={
            "app": "manager",
            "device_id": device_id,
            "event_type": "feature",
            "feature": "schedule.refresh",
            "message": "Actualisation de l'horaire",
            "details": {"source": "ci"},
        },
    )
    assert event.status_code == 200

    complaint = crypt_client.post(
        "/api/telemetry/complaints",
        headers={
            "X-CRYPT-App": "manager",
            "X-CRYPT-Device": device_id,
            "X-CRYPT-School": school_payload["school_id"],
        },
        json={
            "app": "manager",
            "device_id": device_id,
            "subject": "Test CI réclamation",
            "message": "Réclamation de test envoyée depuis les tests automatisés.",
            "contact": "ci@example.test",
            "category": "support",
        },
    )
    assert complaint.status_code == 200
    assert complaint.json()["ticket_id"]

    devices = crypt_client.get("/api/crypt-console/devices?limit=50", headers=admin_headers)
    assert devices.status_code == 200
    assert any(row["id"] == device_id and row["school_id"] == school_payload["school_id"] for row in devices.json()["data"])

    events = crypt_client.get("/api/crypt-console/events?limit=50", headers=admin_headers)
    assert events.status_code == 200
    assert any(row["feature"] == "schedule.refresh" and row["school_id"] == school_payload["school_id"] for row in events.json()["data"])

    tickets = crypt_client.get("/api/crypt-console/tickets?limit=50", headers=admin_headers)
    assert tickets.status_code == 200
    ticket = next(row for row in tickets.json()["data"] if row["id"] == complaint.json()["ticket_id"])
    assert ticket["school_id"] == school_payload["school_id"]
    assert ticket["device_id"] == device_id

    query = crypt_client.post(
        "/api/crypt-console/db/query",
        headers=admin_headers,
        json={"query": "SELECT school_id, name_school FROM school_information ORDER BY school_id LIMIT 50"},
    )
    assert query.status_code == 200
    assert any(row["school_id"] == school_payload["school_id"] for row in query.json()["rows"])

    denied = crypt_client.post(
        "/api/crypt-console/db/query",
        headers=admin_headers,
        json={"query": "DELETE FROM school_information"},
    )
    assert denied.status_code == 403


def test_manager_login_passes_through_rate_limit_response(crypt_client, monkeypatch):
    school_payload = create_school_payload()
    db = main.SessionLocal()
    try:
        school_payload["pass_word"] = main.hash_password(school_payload["pass_word"])
        school = main.SchoolInformation(**school_payload)
        db.add(school)
        db.commit()
    finally:
        db.close()

    limiter = main.AttemptLimiter(max_attempts=1, window_seconds=900)
    limiter.record("school:" + school.school_id)
    monkeypatch.setattr(main, "LOGIN_LIMITER", limiter)

    response = crypt_client.post(
        "/api/manager/login",
        json={"identifier": school.school_id, "password": "any-password"},
    )
    assert response.status_code == 429
    assert response.json()["status"] is False
