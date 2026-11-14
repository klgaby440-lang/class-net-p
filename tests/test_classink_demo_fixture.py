import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from main import validate_school_class_catalog


FIXTURE = Path(__file__).parent / "fixtures" / "classink_school_test_seed.json"


def load_seed():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def build_catalog_payload(seed):
    return {
        "school": seed["school"],
        "classes": [
            {
                "id": item["id"],
                "name": item["name"],
                "titulaire": item["titulaire"],
                "categories": ["Démonstration"],
                "bulletinModel": "classink",
                "bulletinTitle": item["name"],
                "signatureSource": "titulaire",
            }
            for item in seed["classes"]
        ],
        "students": seed["students"],
        "courses": seed["courses"],
        "teachers": seed["teachers"],
    }


def test_classink_integration_fixture_has_expected_school_structure():
    seed = load_seed()
    class_sizes = [len(item["studentIds"]) for item in seed["classes"]]
    assert seed["expected"] == {
        "teachers": 6,
        "classes": 3,
        "students": 15,
        "classSizes": [5, 6, 4],
        "courses": 6,
        "parents": 10,
        "parentsAllowed": 5,
        "parentsBlocked": 5,
    }
    assert class_sizes == [5, 6, 4]
    assert len(seed["teachers"]) == 6
    assert len(seed["students"]) == 15
    assert len(seed["courses"]) == 6
    assert len(seed["parents"]) == 10
    assert sum(bool(parent["individualAccessAllowed"]) for parent in seed["parents"]) == 5
    assert sum(not bool(parent["individualAccessAllowed"]) for parent in seed["parents"]) == 5
    assert seed["prefect"]["role"] == "prefect"
    assert seed["cryptConsole"]["role"] == "crypt_admin"


def test_classink_integration_fixture_passes_primeink_server_catalog_validation():
    seed = load_seed()
    payload = build_catalog_payload(seed)
    catalog = validate_school_class_catalog(payload, seed["school"]["id"])
    assert len(catalog["classes"]) == 3
    assert len(catalog["students"]) == 15
    assert len(catalog["courses"]) == 6
    assert len(catalog["teachers"]) == 6
    assert {student["classId"] for student in catalog["students"]} == {
        "demo-class-sci", "demo-class-lit", "demo-class-com"
    }


def test_classink_catalog_rejects_fixture_when_school_id_does_not_match_token():
    seed = load_seed()
    payload = build_catalog_payload(seed)
    with pytest.raises(HTTPException) as exc:
        validate_school_class_catalog(payload, "ANOTHER-SCHOOL")
    assert exc.value.status_code == 403
