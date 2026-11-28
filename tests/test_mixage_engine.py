from collections import Counter
from pathlib import Path

from main import ExamMixerEngine


def test_mixage_uses_total_benches_and_two_students_per_bench():
    students = [
        {
            "id": f"student-{index}",
            "name": f"Nom{index}",
            "postname": "Test",
            "classId": ["class-a", "class-b", "class-c"][index % 3],
        }
        for index in range(16)
    ]
    rooms = [{"id": 1, "nom": "Salle 1", "places": 16, "rangees": 3, "bancs": 8}]

    result = ExamMixerEngine.mix_students_and_assign_seats(students, rooms)

    assert result["total_mixed"] == 16
    assert result["unassigned_students"] == 0
    assert len(result["result"]) == 1
    benches = result["result"][0]["seating_plan"]
    assert len(benches) == 8
    assert Counter(bench["rangee"] for bench in benches) == {1: 3, 2: 3, 3: 2}

    assigned = []
    for bench in benches:
        assert bench["student_left"] is not None
        assert bench["student_right"] is not None
        assigned.extend([bench["student_left"], bench["student_right"]])

    assert len(assigned) == 16
    assert len({student["student_id"] for student in assigned}) == 16
    # La rotation alterne les trois classes afin d'éviter deux voisins de même classe.
    classes_in_seat_order = [student["original_class"] for student in assigned]
    assert all(a != b for a, b in zip(classes_in_seat_order, classes_in_seat_order[1:]))


def test_mixage_keeps_last_bench_half_empty_when_only_one_place_remains():
    students = [
        {"id": f"student-{index}", "name": f"Nom{index}", "postname": "", "classId": "class-a"}
        for index in range(9)
    ]
    rooms = [{"id": 1, "nom": "Salle 1", "places": 9, "rangees": 3, "bancs": 8}]

    result = ExamMixerEngine.mix_students_and_assign_seats(students, rooms)

    benches = result["result"][0]["seating_plan"]
    occupied = [
        student
        for bench in benches
        for student in (bench["student_left"], bench["student_right"])
        if student is not None
    ]
    assert len(occupied) == 9
    assert sum(bench["student_right"] is None for bench in benches) == 4


def test_mixage_ui_updates_class_counts_and_keeps_the_posted_session_visible():
    source = (Path(__file__).parents[1] / "prime" / "index.html").read_text(encoding="utf-8")
    assert "window.renderMixClassChoices = function()" in source
    assert "' + count + ' élève(s)</small>'" in source
    assert "String(student.classId ?? '') === classId" in source
    assert "await showMixageResultsView({ skipReload: true });" in source
    assert "class_student_counts" in source
