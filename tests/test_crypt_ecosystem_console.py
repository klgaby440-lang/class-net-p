import pytest
from fastapi.testclient import TestClient

import main


@pytest.fixture
def ecosystem_console(monkeypatch):
    monkeypatch.setattr(main, "CRYPT_CONSOLE_ADMIN_KEY", "ci-global-crypt-console-key")
    monkeypatch.setattr(main, "ADMIN_API_KEY", "")
    monkeypatch.setattr(main, "THINK_CONSOLE_API_URL", "")
    monkeypatch.setattr(main, "THINK_CONSOLE_SERVICE_TOKEN", "")
    monkeypatch.setattr(main, "LLINK_CONSOLE_API_URL", "")
    monkeypatch.setattr(main, "LLINK_CONSOLE_SERVICE_TOKEN", "")
    monkeypatch.setattr(main, "SOKO_DATABASE_READONLY_URL", "")
    with TestClient(main.app) as client:
        login = client.post(
            "/api/crypt-console/login",
            json={"admin_key": "ci-global-crypt-console-key"},
        )
        assert login.status_code == 200, login.text
        yield client, {"Authorization": "Bearer " + login.json()["token"]}


def test_global_crypt_console_uses_a_separate_admin_key(ecosystem_console):
    client, headers = ecosystem_console
    assert headers["Authorization"].startswith("Bearer ")


def test_global_ecosystem_dashboard_reports_local_classink_and_unconfigured_services(ecosystem_console):
    client, headers = ecosystem_console
    response = client.get("/api/crypt-console/ecosystem", headers=headers)
    assert response.status_code == 200, response.text
    apps = response.json()["data"]["apps"]
    by_name = {row["name"]: row for row in apps}
    assert by_name["ClassInk / PrimeInk"]["status"] == "online"
    assert by_name["Think"]["status"] == "not_configured"
    assert by_name["Llink"]["status"] == "not_configured"
    assert by_name["Soko Market"]["status"] == "not_configured"


def test_crypt_console_identity_lookup_fails_closed_without_think_service_token(ecosystem_console, monkeypatch):
    client, headers = ecosystem_console
    monkeypatch.setattr(main, "THINK_CONSOLE_API_URL", "https://think.example.test")
    response = client.get(
        "/api/crypt-console/identities?query=INK_ABCDEFGHJKLMNPQ",
        headers=headers,
    )
    assert response.status_code == 503
    assert "jeton" in response.json()["detail"].lower()


def test_crypt_console_soko_queries_fail_closed_without_readonly_database(ecosystem_console):
    client, headers = ecosystem_console
    response = client.get("/api/crypt-console/soko/products", headers=headers)
    assert response.status_code == 503
    assert "SOKO_DATABASE_READONLY_URL" in response.json()["detail"]
