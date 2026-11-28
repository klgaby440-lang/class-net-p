import json
from pathlib import Path

from main import serialize_llink_application_context


def test_llink_context_is_valid_json_and_unchanged_when_within_limit():
    context = {
        "role": "teacher",
        "school": "École Horizon",
        "assignedCourses": [{"name": "Mathématiques"}],
    }

    serialized = serialize_llink_application_context(context, max_chars=30_000)

    assert json.loads(serialized) == context


def test_llink_context_trims_old_records_without_cutting_json():
    context = {
        "role": "teacher",
        "teacher": "Enseignant Test",
        "school": "École Horizon",
        "recentGrades": [
            {"student": f"Élève {index}", "comment": "x" * 700}
            for index in range(30, 0, -1)
        ],
    }

    serialized = serialize_llink_application_context(context, max_chars=3_000)
    decoded = json.loads(serialized)

    assert len(serialized) <= 3_000
    assert decoded["contextTruncated"] is True
    assert decoded["role"] == "teacher"
    assert decoded["school"] == "École Horizon"
    assert decoded["recentGrades"]
    assert decoded["recentGrades"][0]["student"] == "Élève 30"


def test_classnet_llink_request_keeps_instructions_and_context_in_separate_fields():
    source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")

    assert '"application_instructions": system_instruction' in source
    assert '"context": context_text' in source
    assert 'context_text = serialize_llink_application_context(context, max_chars=30_000)' in source
    assert '"preferences": system_instruction' not in source
