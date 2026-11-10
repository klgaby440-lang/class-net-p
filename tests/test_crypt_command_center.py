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
