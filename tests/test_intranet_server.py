import base64
import hashlib
import hmac
import json
import os
import time

os.environ["INTRANET_AUTH_SECRET"] = "test-intranet-secret"

from fastapi.testclient import TestClient
import intranet_server


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _token(role: str, **claims) -> str:
    payload = {"role": role, "exp": int(time.time()) + 3600, **claims}
    encoded = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _b64url(
        hmac.new(
            os.environ["INTRANET_AUTH_SECRET"].encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).digest()
    )
    return encoded + "." + signature


def setup_function():
    intranet_server._PENDING.clear()


def test_teacher_packet_is_relayed_and_acknowledged_without_database():
    client = TestClient(intranet_server.app)
    teacher_token = _token(
        "teacher",
        email="teacher@example.test",
        school_id="SCHOOL-01",
        workspace_mode="school",
    )
    school_token = _token("school", school_id="SCHOOL-01")
    transfer_data = {
        "teachers": ["prof_001"],
        "moyenne_classe": {"3e": {"Math": {"P1": {"__id__STU-01": 12.5}}}},
        "studentAliases": {"3e": {"STU-01": "Amina Test"}},
        "attendanceRecords": [],
        "conduct": {},
        "signatures": {},
    }

    sent = client.post(
        "/api/intranet/sync",
        headers={"Authorization": "Bearer " + teacher_token},
        json={
            "school_id": "SCHOOL-01",
            "teacher_email": "teacher@example.test",
            "data": transfer_data,
        },
    )
    assert sent.status_code == 200
    receipt_id = sent.json()["receipt_id"]

    received = client.get(
        "/api/primenet/sync",
        headers={"Authorization": "Bearer " + school_token},
    )
    assert received.status_code == 200
    submissions = received.json()["data"]["submissions"]
    assert len(submissions) == 1
    assert submissions[0]["receipt_id"] == receipt_id
    assert submissions[0]["data"] == transfer_data

    # La lecture n'est pas destructive ; PrimeInk accuse réception après saveState().
    again = client.get(
        "/api/primenet/sync",
        headers={"Authorization": "Bearer " + school_token},
    )
    assert again.json()["data"]["submissions"][0]["receipt_id"] == receipt_id

    ack = client.post(
        "/api/primenet/ack",
        headers={"Authorization": "Bearer " + school_token},
        json={"receipt_ids": [receipt_id]},
    )
    assert ack.status_code == 200
    assert ack.json()["acknowledged"] == 1

    empty = client.get(
        "/api/primenet/sync",
        headers={"Authorization": "Bearer " + school_token},
    )
    assert empty.json()["data"]["submissions"] == []


def test_latest_teacher_packet_replaces_previous_one():
    client = TestClient(intranet_server.app)
    headers = {"Authorization": "Bearer " + _token(
        "teacher",
        email="teacher@example.test",
        school_id="SCHOOL-01",
        workspace_mode="school",
    )}
    for mark in (10, 15):
        response = client.post(
            "/api/intranet/sync",
            headers=headers,
            json={
                "school_id": "SCHOOL-01",
                "teacher_email": "teacher@example.test",
                "data": {"moyenne_classe": {"3e": {"Math": {"P1": {"__id__S1": mark}}}}},
            },
        )
        assert response.status_code == 200

    school_headers = {"Authorization": "Bearer " + _token("school", school_id="SCHOOL-01")}
    received = client.get("/api/primenet/sync", headers=school_headers)
    rows = received.json()["data"]["submissions"]
    assert len(rows) == 1
    assert rows[0]["data"]["moyenne_classe"]["3e"]["Math"]["P1"]["__id__S1"] == 15


def test_rejects_wrong_school_and_wrong_teacher():
    client = TestClient(intranet_server.app)
    token = _token(
        "teacher",
        email="teacher@example.test",
        school_id="SCHOOL-01",
        workspace_mode="school",
    )
    headers = {"Authorization": "Bearer " + token}
    wrong_school = client.post(
        "/api/intranet/sync",
        headers=headers,
        json={"school_id": "SCHOOL-02", "teacher_email": "teacher@example.test", "data": {}},
    )
    wrong_teacher = client.post(
        "/api/intranet/sync",
        headers=headers,
        json={"school_id": "SCHOOL-01", "teacher_email": "other@example.test", "data": {}},
    )
    assert wrong_school.status_code == 403
    assert wrong_teacher.status_code == 403


def test_rejects_independent_workspace_and_unapproved_fields():
    client = TestClient(intranet_server.app)
    independent = _token(
        "teacher",
        email="teacher@example.test",
        school_id="SCHOOL-01",
        workspace_mode="independent",
    )
    response = client.post(
        "/api/intranet/sync",
        headers={"Authorization": "Bearer " + independent},
        json={"school_id": "SCHOOL-01", "teacher_email": "teacher@example.test", "data": {}},
    )
    assert response.status_code == 403

    school_scope = _token(
        "teacher",
        email="teacher@example.test",
        school_id="SCHOOL-01",
        workspace_mode="school",
    )
    extra = client.post(
        "/api/intranet/sync",
        headers={"Authorization": "Bearer " + school_scope},
        json={
            "school_id": "SCHOOL-01",
            "teacher_email": "teacher@example.test",
            "data": {"password": "should-not-be-here"},
        },
    )
    assert extra.status_code == 422


def test_server_has_no_database_dependency():
    assert not hasattr(intranet_server, "SessionLocal")
    assert not hasattr(intranet_server, "engine")
