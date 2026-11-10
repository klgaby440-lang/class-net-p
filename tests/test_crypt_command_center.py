import pytest
from fastapi import HTTPException

from main import validate_crypt_read_query


def test_crypt_sql_allows_select_from_an_application_table():
    query = "SELECT school_id, name_school, city FROM school_information ORDER BY name_school LIMIT 25;"
    assert validate_crypt_read_query(query) == query.rstrip(";")


def test_crypt_sql_allows_safe_aggregates():
    query = "SELECT school_id, COUNT(*) AS school_count FROM school_information GROUP BY school_id"
    assert validate_crypt_read_query(query) == query


@pytest.mark.parametrize(
    "query",
    [
        "WITH doomed AS (DELETE FROM school_information RETURNING *) SELECT * FROM doomed",
        "UPDATE school_information SET name_school = 'x'",
        "SELECT * INTO copied_schools FROM school_information",
        "SELECT * FROM school_information; DELETE FROM school_information",
        "SELECT password FROM school_information",
        "SELECT pg_sleep(20) FROM school_information",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM unknown_private_table",
        "SELECT * FROM school_information FOR UPDATE",
        "SELECT school_id FROM school_information -- comment injection",
        "SELECT * FROM school_information /* hidden statement */",
        "SELECT s.school_id FROM school_information s, unknown_private_table",
    ],
)
def test_crypt_sql_rejects_non_readonly_or_sensitive_queries(query):
    with pytest.raises(HTTPException) as error:
        validate_crypt_read_query(query)
    assert error.value.status_code in (403, 422)


def test_crypt_command_center_routes_are_registered_in_source():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    for route in (
        '@app.post("/api/crypt-console/login")',
        '@app.get("/api/crypt-console/overview")',
        '@app.get("/api/crypt-console/schools")',
        '@app.post("/api/crypt-console/schools")',
        '@app.get("/api/crypt-console/devices")',
        '@app.get("/api/crypt-console/events")',
        '@app.get("/api/crypt-console/tickets")',
        '@app.post("/api/telemetry/events")',
        '@app.post("/api/telemetry/complaints")',
    ):
        assert route in source


def test_crypt_manager_login_preserves_rate_limit_response():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    manager = source.split('@app.post("/api/manager/login")', 1)[1].split('@app.put("/api/manager/schedule")', 1)[0]
    assert "isinstance(result, JSONResponse)" in manager
    assert "status_code=result.status_code" in manager


def test_crypt_sql_rejects_json_row_serialization_and_database_introspection():
    for query in (
        "SELECT to_jsonb(s) FROM school_information s",
        "SELECT current_setting('DATABASE_URL') FROM school_information",
        "SELECT current_user FROM school_information",
    ):
        with pytest.raises(HTTPException):
            validate_crypt_read_query(query)


def test_crypt_login_wrappers_preserve_rate_limit_response_and_disable_cache(monkeypatch):
    import json
    import main
    from fastapi.responses import JSONResponse
    from main import LoginSchema, PrimeNetLoadSchema

    limited = JSONResponse(status_code=429, content={"status": False, "message": "Trop de tentatives."})
    monkeypatch.setattr(main, "login_school", lambda *_args: limited)

    manager_response = main.manager_login(LoginSchema(identifier="school-1", password="secret"), db=None)
    assert manager_response.status_code == 429
    assert json.loads(manager_response.body)["message"] == "Trop de tentatives."
    assert manager_response.headers["cache-control"].startswith("no-store")

    prime_response = main.load_primenet_state(PrimeNetLoadSchema(school_id="school-1", password="secret"), db=None)
    assert prime_response.status_code == 429
    assert json.loads(prime_response.body)["message"] == "Trop de tentatives."
    assert prime_response.headers["cache-control"].startswith("no-store")


def test_crypt_admin_dependency_rejects_non_admin_claims():
    import main
    with pytest.raises(HTTPException) as error:
        main.require_crypt_admin({"role": "school", "school_id": "school-1"})
    assert error.value.status_code == 403


def test_llink_assistant_requires_authenticated_manager_and_crypt_sessions():
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as client:
        manager = client.post(
            "/api/llink/assist",
            headers={"X-CRYPT-App": "manager"},
            json={"message": "Comment actualiser l'horaire ?", "history": []},
        )
        assert manager.status_code == 401

        crypt = client.post(
            "/api/llink/assist",
            headers={"X-CRYPT-App": "crypt"},
            json={"message": "Comment lire les erreurs ?", "history": []},
        )
        assert crypt.status_code == 403


def test_llink_assistant_rate_limiter_is_defined():
    import main
    assert isinstance(main.CRYPT_LLINK_LIMITER, main.AttemptLimiter)
