from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.ids = defaultdict(list)
        self.scripts = []
        self._script_parts = None
        self._script_is_javascript = False

    def handle_starttag(self, tag, attrs):
        attr_map = dict(attrs)
        element_id = attr_map.get("id")
        if element_id:
            self.ids[element_id].append(tag)
        if tag.lower() == "script":
            script_type = (attr_map.get("type") or "").split(";", 1)[0].strip().lower()
            is_js = script_type in ("", "text/javascript", "application/javascript", "module")
            self._script_is_javascript = is_js and not attr_map.get("src")
            self._script_parts = []

    def handle_endtag(self, tag):
        if tag.lower() == "script" and self._script_parts is not None:
            if self._script_is_javascript:
                source = "".join(self._script_parts)
                if source.strip():
                    self.scripts.append(source)
            self._script_parts = None
            self._script_is_javascript = False

    def handle_data(self, data):
        if self._script_parts is not None:
            self._script_parts.append(data)


class FrontendStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = shutil.which("node")

    def inspect_file(self, relative):
        parser = FrontendParser()
        parser.feed((ROOT / relative).read_text(encoding="utf-8"))
        parser.close()
        duplicates = {key: tags for key, tags in parser.ids.items() if len(tags) > 1}
        self.assertFalse(duplicates, f"{relative}: IDs HTML dupliqués : {duplicates}")
        self.assertTrue(parser.scripts, f"{relative}: aucun script JavaScript inline détecté")

        if not self.node:
            self.skipTest("Node.js absent : contrôle de syntaxe JS ignoré.")
        with tempfile.TemporaryDirectory() as tmp:
            for index, source in enumerate(parser.scripts):
                path = Path(tmp) / f"inline-{index}.js"
                path.write_text(source, encoding="utf-8")
                result = subprocess.run(
                    [self.node, "--check", str(path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"{relative}, script inline {index}:\n{result.stdout}\n{result.stderr}",
                )

    def test_classink_html_ids_and_inline_javascript(self):
        self.inspect_file("app/index.html")

    def test_primeink_html_ids_and_inline_javascript(self):
        self.inspect_file("prime/index.html")

    def test_suivi_html_ids_and_inline_javascript(self):
        self.inspect_file("suivi/index.html")

    def test_primeink_schedule_ranges_and_manager_planned_hours_render_consistently(self):
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("function bpScheduleTimeToMinutes(value)", prime)
        self.assertIn("const startMinutes = bpScheduleTimeToMinutes(slot.start)", prime)
        self.assertIn("const endMinutes = bpScheduleTimeToMinutes(slot.end)", prime)
        self.assertIn("previousEndMinutes !== null && startMinutes < previousEndMinutes", prime)
        self.assertNotIn(r"!/^([01]\\d|2[0-3]):[0-5]\\d$/.test(slot.start)", prime)
        self.assertIn("function bpScheduleTableHTML(grid, cellFn)", prime)
        self.assertIn("for (let hour = 1; hour <= hourCount; hour++)", prime)
        self.assertIn('id="horClassGridMount"', prime)
        self.assertIn('id="horTeacherGridMount"', prime)
        self.assertIn('id="horExemplaires"', prime)
        self.assertIn('id="horExemplarSelect"', prime)
        self.assertIn("window.selectScheduleExemplar", prime)
        self.assertIn("window.publishSelectedScheduleExemplar", prime)
        self.assertIn("exemplaires: exemplarCount", prime)
        self.assertNotIn("horModelSelector", prime)
        self.assertNotIn("mixModelSelector", prime)

        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        self.assertIn("const configuredHours = Number(managerSchedule.config && managerSchedule.config.heures) || 0;", manager)
        self.assertIn("Math.max(configuredHours, configuredSlots.length, usedHours)", manager)
        self.assertIn("configuredSlots.find(item => Number(item && item.hour) === hour)", manager)
        self.assertIn("Tranche horaire", manager)

        self.assertIn("horaireState.generatedSchedules", prime)
        self.assertIn("async function bpLoadPublishedScheduleFromServer()", prime)
        self.assertIn("API_BASE_URL + '/api/manager/schedule'", prime)
        self.assertIn("appState.managerSchedule.result.published = true", prime)

        backend = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("exemplaires: int = 1", backend)
        self.assertIn("maximum_attempts = exemplaires * 12", backend)
        self.assertIn('candidate["published"] = False', backend)
        self.assertIn('"published": False', backend)
        self.assertIn('result["published"] = True', backend)
        self.assertIn('Seul un exemplaire complet, sans cours non placé, peut être publié.', backend)
        self.assertIn('slot_by_hour = {str(item.get("hour")): item for item in slots if isinstance(item, dict)}', backend)

        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        self.assertIn("if(page==='schedule')refreshSchedule();", manager)

    def test_primeink_schedule_result_tabs_are_isolated_from_global_subnavigation(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")

        # The schedule's two tabs use their dedicated switch function and cannot submit a form.
        self.assertIn('type="button" class="sub-nav-btn active" id="btnHorResultClasses"', source)
        self.assertIn('type="button" class="sub-nav-btn" id="btnHorResultEnseignants"', source)
        self.assertIn("window.switchHoraireResultSubView = function(view)", source)
        self.assertIn("classesView.classList.toggle('hidden', !isClasses)", source)
        self.assertIn("teachersView.classList.toggle('hidden', isClasses)", source)

        # Bulletin navigation is restricted to its own container. No global listener may
        # clear .active from the timetable or mixage result panes.
        self.assertIn("const classDetailSection = document.getElementById('section-class-detail');", source)
        self.assertIn("classDetailSection.querySelectorAll('.sub-nav-btn[data-sub]')", source)
        self.assertIn("classDetailSection.querySelectorAll('.sub-view-content')", source)
        self.assertIn(
            'document.querySelectorAll(\'#section-class-detail .sub-nav-btn[data-sub="view-deliberation"]\')',
            source,
        )
        self.assertNotIn("document.querySelectorAll('.sub-nav-btn')", source)
        self.assertNotIn("const subNavBtns = document.querySelectorAll('.sub-nav-btn')", source)
        self.assertNotIn("const subToolBtns = document.querySelectorAll('#section-about .sub-nav-btn')", source)

    def test_no_unsupported_abort_signal_timeout_usage(self):
        for relative in ("app/index.html", "prime/index.html"):
            source = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn(
                "AbortSignal.timeout(",
                source,
                f"{relative}: utilise AbortSignal.timeout sans compatibilité navigateur.",
            )

    def test_primeink_deliberation_handlers_are_implemented(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        for function_name in ("resetDeliberation", "applyDeliberationAll", "saveDeliberation"):
            self.assertIn(f"function {function_name}(", source)

    def test_primeink_student_management_uses_shared_class_lookup(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        helper_index = source.index("function getManagedStudentsByClass(")
        modal_index = source.index("window.openManageStudentsModal = function(")
        self.assertLess(helper_index, modal_index)
        self.assertIn("getManagedStudentsByClass(activeClassId)", source)
        self.assertIn("renderManagedStudents(overlay.dataset.classId || '')", source)
        self.assertIn("String(st.classId ?? '') === targetClassId", source)

    def test_primeink_bulletin_handles_class_ids_and_print_scale(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("function bpBulletinCoursesForClass(cls)", source)
        self.assertIn("String(course.classId ?? '') === String(cls && cls.id || '')", source)
        self.assertIn("String(s.classId ?? '') === studentClassId", source)
        self.assertIn("bp-empty-courses", source)
        self.assertIn("page.style.transform = 'none';", source)
        self.assertNotIn("Math.min(sheet.clientWidth / pw, sheet.clientHeight / ph)", source)

    def test_primeink_class_settings_group_actions_and_paginate_students(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('data-sub="view-settings">PARAMÈTRES</button>', source)
        self.assertIn('id="view-settings"', source)
        for action in (
            "Ajouter un élève / importer une liste",
            "Gérer les élèves existants",
            "Ajouter un cours / importer un bulletin",
            "Gérer les cours existants",
            "Éditer la classe",
            "Imprimer les bulletins de la classe",
            "Supprimer la classe",
        ):
            self.assertIn(action, source)
        self.assertNotIn('onclick="exportClassBulletinsPDF()"', source)
        self.assertIn("const PRIMEINK_STUDENTS_PER_PAGE = 15", source)
        self.assertIn("students.slice(start, start + PRIMEINK_STUDENTS_PER_PAGE)", source)
        self.assertIn("const pageSize = 15", source)
        self.assertIn("visibleStudents.map(st =>", source)
        student_form = source.split('id="interface-eleve"', 1)[1].split('id="interface-classe"', 1)[0]
        course_form = source.split('id="interface-cours"', 1)[1].split('id="modal-select-target-course"', 1)[0]
        self.assertIn('id="primeInkStudentPhotoInput"', student_form)
        self.assertNotIn('primeInkCoursePhotoInput', student_form)
        self.assertIn('id="primeInkCoursePhotoInput"', course_form)
        self.assertNotIn('primeInkStudentPhotoInput', course_form)

    def test_primeink_parent_identifier_copy_has_legacy_fallback_and_clear_block_label(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        copy_logic = source.split("window.bpSuiviCopyCode = async function()", 1)[1].split("window.bpSuiviToggleAccess", 1)[0]
        self.assertIn("navigator.clipboard.writeText(row.code)", copy_logic)
        self.assertIn("document.execCommand('copy')", copy_logic)
        self.assertIn("Identifiant parent copié", copy_logic)
        self.assertIn("toggleButton.textContent = row.individualAccessAllowed ? 'Bloquer les notes' : 'Autoriser les résultats';", source)
        self.assertIn("parent sera informé qu’il doit régler les frais", source)

    def test_suivi_individual_block_hides_results_and_requests_payment(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        endpoint = source.split('@app.post("/api/suivi/student")', 1)[1].split('@app.get("/api/suivi/school/classes/{class_id}/students")', 1)[0]
        self.assertIn('access_reason = "payment_required"', endpoint)
        self.assertIn('"paymentRequired": bool(not results_allowed and not individual_access_allowed)', endpoint)
        self.assertIn('"results": results', endpoint)
        self.assertIn("results = None", endpoint)
        self.assertIn("le parent doit régler", endpoint)

    def test_crypt_school_account_creation_generates_and_hands_off_credentials(self):
        source = (ROOT / "crypt/index.html").read_text(encoding="utf-8")
        self.inspect_file("crypt/index.html")
        for item in (
            'id="schoolForm"',
            'id="schoolIdInput"',
            'id="bulletinSeqIdInput"',
            'id="schoolPasswordInput"',
            'id="generateSchoolId"',
            'id="generateBulletinId"',
            'id="generateSchoolPassword"',
            'id="createdAccountPanel"',
            'data-copy-created="school_id"',
            'data-copy-created="bulletin_seq_id"',
            'data-copy-created="pass_word"',
            'id="copyCreatedCredentials"',
        ):
            self.assertIn(item, source)
        self.assertIn("function secureRandomHex", source)
        self.assertIn("window.crypto.getRandomValues", source)
        self.assertIn("function showCreatedAccount(data)", source)
        self.assertIn("function copyConsoleText(value)", source)
        self.assertIn("document.execCommand('copy')", source)
        self.assertIn("createdAccount=null", source)

    def test_primeink_prepares_shared_class_catalog(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('id="prepareClassInformationBtn"', source)
        self.assertIn("async function prepareClassInformation()", source)
        self.assertIn("/api/primenet/prepare-classes", source)
        for field in ("classes:", "students:", "courses:", "teachers:"):
            self.assertIn(field, source)

    def test_classink_loads_official_school_classroom_and_titular_roles(self):
        source = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("/api/teacher/classes", source)
        self.assertIn("db.titulaireClasses", source)
        self.assertIn("refreshTeacherClassroomData", source)
        self.assertIn("merged.classrooms", source)
        self.assertIn("allowed.includes(chosen)", source)

    def test_classink_shows_teacher_code_for_school_linking(self):
        source = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn('id="setting-teacher-access-code"', source)
        self.assertIn("async function copyTeacherAccessCode()", source)
        self.assertIn("(db.user && db.user.id)", source)
        self.assertIn("Communiquez votre identifiant ClassInk", source)

    def test_classink_never_shares_local_temporary_id_as_school_access_code(self):
        source = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("db.user && db.user.isLoggedIn && db.user.id", source)
        self.assertIn("db.user && db.user.isLoggedIn ? String(db.user.id || '')", source)

    def test_primeink_prints_all_students_from_the_active_class(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("window.printClassBulletins = function()", source)
        self.assertIn("const classCandidates = [activeClassId, bpDisplayedClassId]", source)
        self.assertIn("const students = appState.students.filter(student =>", source)
        self.assertIn("bpPrint(students.map(student => student.id), cls.id);", source)
        self.assertNotIn("bpPrint(getSortedStudentsByClass(activeClassId).map(s => s.id), activeClassId)", source)

    def test_primeink_manual_add_chooser_reuses_legacy_student_and_course_forms(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('id="primeInkManualAddButton"', source)
        self.assertIn('id="primeInkManualAddChooser"', source)
        self.assertIn("window.openPrimeInkManualAdd = function()", source)
        self.assertIn("window.openPrimeInkManualStudent = function()", source)
        self.assertIn("window.openPrimeInkManualCourse = function()", source)
        self.assertIn("document.getElementById('interface-eleve').classList.remove('hidden')", source)
        self.assertIn("document.getElementById('interface-cours').classList.remove('hidden')", source)
        self.assertIn('id="interface-eleve"', source)
        self.assertIn('id="interface-cours"', source)
        self.assertIn("populateCategoryDropdown()", source)

    def test_primeink_class_list_has_no_inline_edit_or_delete_actions(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertNotIn('data-class-action="edit"', source)
        self.assertNotIn('data-class-action="delete"', source)
        self.assertIn("editClassById(activeClassId)", source)
        self.assertIn("deleteManagedClass(activeClassId)", source)

    def test_primeink_global_settings_are_grouped_without_removing_existing_controls(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        settings = source.split('<div id="section-settings" class="app-section">', 1)[1].split(
            '<!-- Interface Tools', 1
        )[0]
        for heading in (
            "GESTION ET SYNCHRONISATION",
            "SAUVEGARDE ET RESTAURATION",
            "LICENCE ET APPARENCE",
            "À PROPOS DE PRIMEINK",
            "INFORMATIONS DE L'ÉTABLISSEMENT",
        ):
            self.assertIn(heading, settings)
        for control_id in (
            "prepareClassInformationBtn",
            "primeInkExcelImportInput",
            "activationCodeInput",
            "themeSelect",
            "syncModeSelect",
            "identityCard",
            "teacherUniqueIdInput",
            "teachersTableBody",
        ):
            self.assertIn(f'id="{control_id}"', settings)

    def test_primeink_deliberation_lists_students_of_the_selected_class(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('id="delibStudentListSidebar"', source)
        self.assertIn('id="delibStudentSearch"', source)
        self.assertIn("function renderDeliberationStudentList(classId)", source)
        self.assertIn('String(student.classId ?? "") === String(cls.id ?? "")', source)
        self.assertIn('button.addEventListener("click", () => {', source)
        self.assertIn('delibStudentSearch.addEventListener("input"', source)
        self.assertIn("renderDeliberationStudentList(activeClassId);", source)
        self.assertIn("renderDeliberationView(activeStudentId, false);", source)


    def test_primeink_student_attendance_refreshes_after_sync_and_renders_daily_summary(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("function refreshPresenceClassOptions()", source)
        self.assertIn("function refreshPrimeInkStudentPresenceView()", source)
        self.assertIn("refreshPrimeInkStudentPresenceView();", source)
        self.assertIn("if (tabId === 'tab-presence') {", source)
        self.assertIn("if (type === 'eleve') {", source)
        self.assertIn("select.disabled = classes.length === 0;", source)
        self.assertIn("window.renderPresencesEleveTable = function()", source)
        self.assertIn("Statut(s)", source)
        self.assertIn("studentRows.map(record =>", source)
        self.assertIn("const summary = primeInkDailyAttendanceSummary(studentRecords, courses);", source)
        self.assertIn("safeHTML(details)", source)

        # Le tri des élèves est partagé avec le rendu des présences, qui est hors de
        # la portée du gestionnaire DOMContentLoaded contenant les anciens écrans.
        helper_start = source.index("function getSortedStudentsByClass(classId)")
        dom_ready_start = source.index("document.addEventListener('DOMContentLoaded', () => {", helper_start)
        self.assertLess(helper_start, dom_ready_start)
        self.assertIn("return students.filter(st => String(st.classId ?? '') === String(classId ?? ''));", source)

        apply_data = source.split("function applyMergedPrimenetData(mergedData)", 1)[1].split(
            "// Jeton de session Cloud", 1
        )[0]
        self.assertIn("appState.tools.presences.students = mergedData.attendanceRecords.map", apply_data)
        self.assertIn("refreshPrimeInkStudentPresenceView();", apply_data)

        summary_start = source.index("function primeInkDailyAttendanceSummary(studentRecords, courses)")
        summary_end = source.index("function primeInkAuthHeaders()", summary_start)
        helper = source[summary_start:summary_end]
        self.assertIn("statuses.includes('A')", helper)
        self.assertIn("allCoursesRecorded && statuses.length > 0", helper)
        self.assertIn("statuses.every(value => value === 'P')", helper)
        self.assertIn("label = 'Présent';", helper)
        self.assertIn("label = 'Absent';", helper)
        self.assertIn("label = 'Présent avec retard';", helper)
        self.assertIn("label = 'Incomplet';", helper)

    def test_primeink_teacher_attendance_uses_server_roster_and_reloads_selected_date(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('onchange="loadPrimeInkTeacherAttendance()"', source)
        self.assertIn("let __primeInkTeacherAttendanceRoster = null;", source)
        self.assertIn("if (Array.isArray(result.teachers))", source)
        self.assertIn("__primeInkTeacherAttendanceRoster = result.teachers.map", source)
        self.assertIn("const teachers = Array.isArray(__primeInkTeacherAttendanceRoster)", source)
        self.assertIn("saveState();", source[source.index("window.savePrimeInkTeacherAttendance = async function"):source.index("function renderPresencesProfTable()")])
        self.assertIn("await loadPrimeInkTeacherAttendance();", source[source.index("window.savePrimeInkTeacherAttendance = async function"):source.index("function renderPresencesProfTable()")])
        self.assertIn('id="saveAllTeacherAttendanceButton"', source)
        self.assertIn('onclick="saveAllPrimeInkTeacherAttendance()"', source)
        bulk_save = source[source.index("window.saveAllPrimeInkTeacherAttendance = async function"):source.index("function renderPresencesProfTable()")]
        self.assertIn("for (let index = 0; index < entries.length; index++)", bulk_save)
        self.assertIn("teacher_email: entry.email", bulk_save)
        self.assertIn("if (!checkIn && !checkOut && !hasExistingRecord) { skipped++; return; }", bulk_save)
        self.assertIn("failures.slice(0, 3)", bulk_save)
        self.assertIn("saveState();", bulk_save)

    def test_primeink_daily_attendance_summary_truth_table(self):
        if not self.node:
            self.skipTest("Node.js absent : test du résumé quotidien ignoré.")
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        helper_start = source.index("function primeInkDailyAttendanceSummary(studentRecords, courses)")
        helper_end = source.index("window.renderPresencesEleveTable = function()", helper_start)
        helper = source[helper_start:helper_end]
        script = helper + """
const courses = [{id: "c1"}, {id: "c2"}];
const cases = [
  primeInkDailyAttendanceSummary([
    {courseId: "c1", status: "P"},
    {courseId: "c2", status: "P"}
  ], courses),
  primeInkDailyAttendanceSummary([
    {courseId: "c1", status: "A"}
  ], courses),
  primeInkDailyAttendanceSummary([
    {courseId: "c1", status: "P"}
  ], courses),
  primeInkDailyAttendanceSummary([
    {courseId: "c1", status: "R"},
    {courseId: "c2", status: "P"}
  ], courses)
];
process.stdout.write(JSON.stringify(cases.map(item => ({code:item.code,label:item.label}))));
"""
        result = subprocess.run(
            [self.node, "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        self.assertEqual(
            json.loads(result.stdout),
            [
                {"code": "P", "label": "Présent"},
                {"code": "A", "label": "Absent"},
                {"code": "INCOMPLETE", "label": "Incomplet"},
                {"code": "R", "label": "Présent avec retard"},
            ],
        )

    def test_primeink_mixage_layout_has_two_places_per_bench_and_database_publication(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("function bpMixageCreateSeatPlan(seats, config = {}, overrides = {})", source)
        self.assertIn("function bpMixageSeatingPlanHTML(title, supervisor, seats, config = {}, overrides = {})", source)
        self.assertIn("bpMixageSeatHTML(bench.left) + bpMixageSeatHTML(bench.right)", source)
        self.assertIn("Coord. serveur : R", source)
        self.assertIn("function bpMixageClassesHTML(result, rooms)", source)
        self.assertIn("function bpMixageRoomsHTML(result, rooms)", source)
        self.assertIn('id="mixPublishToSuivi" checked', source)
        self.assertIn("/api/mixage/generate", source)
        self.assertIn("publish_to_suivi: publishToSuivi", source)
        self.assertIn("function bpLoadMixagesFromDatabase()", source)
        self.assertIn("/api/manager/mixages", source)
        self.assertIn("Le mixage a été enregistré dans la base de données", source)
        self.assertIn("Chaque parent ne verra que les informations de son enfant", source)
        self.assertIn('id="mixRoomSelect"', source)
        self.assertIn('id="mixClassSelect"', source)
        self.assertIn("function bpMixageClassPlacementHTML(classId, result, printMode = false)", source)
        self.assertIn("function bpMixageClassSheetsHTML(result)", source)
        self.assertIn("function bpMixageRoomSheetsHTML(result, rooms)", source)
        self.assertIn("bpMixageRoomKey(room, index) === String(__selectedMixageRoomKey)", source)

    def test_primeink_course_statistics_follow_selected_period_or_exam(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('id="statsPeriod"', source)
        self.assertIn("const COURSE_STATS_PERIOD_FIELDS = {", source)
        self.assertIn("const fields = COURSE_STATS_PERIOD_FIELDS[period] || COURSE_STATS_PERIOD_FIELDS.TG;", source)
        self.assertIn("const score = ficheSumRequired(fields.map(field => v[field]));", source)
        self.assertIn("const maxSelected = fields.reduce", source)
        self.assertIn("primeink-stats-divider", source)
        self.assertIn("NOTE OBTENUE", source)
        self.assertIn("MAXIMUM", source)

    def test_primeink_mixage_seat_plan_distributes_eight_benches_over_three_rows(self):
        if not self.node:
            self.skipTest("Node.js absent : test du plan de bancs ignoré.")
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        helper_start = source.index("function bpMixageCreateSeatPlan(seats, config = {}, overrides = {})")
        helper_end = source.index("function bpMixageSeatHTML(seat)", helper_start)
        helper = source[helper_start:helper_end]
        script = helper + """
const seats = Array.from({length:16}, (_, index) => ({id: "S" + (index + 1)}));
const plan = bpMixageCreateSeatPlan(seats, {places:16, bancs:8, rangees:3});
process.stdout.write(JSON.stringify({
  benches: plan.benchCount,
  places: plan.availablePlaces,
  rows: plan.rowCount,
  assigned: plan.assignedSeats,
  free: plan.freePlaces,
  benchesPerRow: plan.rows.map(row => row.benches.length),
  firstBench: [plan.rows[0].benches[0].left.id, plan.rows[0].benches[0].right.id],
  lastBench: [plan.rows[2].benches[1].left.id, plan.rows[2].benches[1].right.id]
}));
"""
        result = subprocess.run([self.node, "-e", script], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        self.assertEqual(json.loads(result.stdout), {
            "benches": 8,
            "places": 16,
            "rows": 3,
            "assigned": 16,
            "free": 0,
            "benchesPerRow": [3, 3, 2],
            "firstBench": ["S1", "S2"],
            "lastBench": ["S15", "S16"],
        })


    def test_suivi_parent_html_ids_and_inline_javascript(self):
        source = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        self.inspect_file("suivi/index.html")
        self.assertIn("/api/suivi/schools", source)
        self.assertIn("/api/suivi/student", source)
        self.assertIn("Identifiant élève", source)
        self.assertIn("d.resultsAccess.message", source)
        self.assertIn("function redactedParentData(data)", source)
        self.assertIn("liveData.set(key, entry)", source)
        self.assertIn("safe.results = null", source)
        self.assertIn("Hors connexion : les résultats restent masqués", source)
        self.assertIn("f.configured === false", source)
        # Le seul stockage persistant autorisé est la préférence de thème.
        self.assertIn("localStorage.setItem('classink_suivi_theme'", source)
        for storage_api in ("sessionStorage", "indexedDB", "document.cookie"):
            self.assertNotIn(storage_api, source)
        self.assertNotIn("localStorage.setItem(STORE_KEY", source)
        self.assertNotIn("localStorage.setItem(CACHE_PREFIX", source)
        self.assertIn("const memoryStore = new Map()", source)
        self.assertIn("cache: 'no-store'", source)
        self.assertIn("pagehide", source)
        main_script_end = source.index("</script>", source.index("const memoryStore"))
        cleanup_handler = source.index("window.addEventListener('pagehide'", source.index("const memoryStore"))
        self.assertLess(cleanup_handler, main_script_end)
        self.assertNotIn("liveData.clear();", source[source.rfind("</script>"):])

    def test_primeink_keeps_local_database_persistence(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("classnet_p_db", source)
        self.assertIn("primeink_cloud_token", source)
        self.assertIn("window.localStorage.setItem('classnet_p_db'", source)
        self.assertIn("window.localStorage.getItem('classnet_p_db'", source)
        self.assertIn('id="crypt-client-telemetry"', source)

    def test_classink_app_keeps_local_persistence(self):
        source = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertTrue("localStorage" in source or "indexedDB" in source)
        self.assertIn('id="crypt-client-telemetry"', source)

    def test_primeink_exposes_parent_code_and_controls_student_result_access(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('id="primeInkSuiviAccessPanel"', source)
        self.assertIn('id="primeInkSuiviStudentCode"', source)
        self.assertIn('id="primeInkSuiviToggle"', source)
        self.assertIn("function bpSuiviLoadRoster(classId)", source)
        self.assertIn("/api/suivi/school/classes/", source)
        self.assertIn("/api/suivi/school/students/", source)
        self.assertIn("row.individualAccessAllowed", source)
        self.assertIn("ID parent :", source)

    def test_classnet_has_public_parent_lookup_and_school_only_access_routes(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/suivi/schools")', source)
        self.assertIn('@app.post("/api/suivi/student")', source)
        self.assertIn('@app.get("/api/suivi/school/classes/{class_id}/students")', source)
        self.assertIn('@app.put("/api/suivi/school/students/{student_id}/access")', source)
        self.assertIn("Depends(require_active_school)", source)
        self.assertIn("results = None", source)
        self.assertIn("is_suivi_results_allowed(db, school_id, student)", source)
        self.assertIn("suivi_results_allowed = Column(Boolean", source)
        self.assertIn("suivi_code = Column(String(40)", source)


    def test_primeink_photo_import_duplicate_keys_and_student_name_split(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("function primeInkImportNormalize(value)", source)
        self.assertIn(r'.replace(/[\u0300-\u036f]/g, "")', source)
        self.assertNotIn(r'.replace(/[\\u0300-\\u036f]/g', source)
        self.assertIn("function primeInkNormalizeStudentRow(row)", source)
        self.assertIn("postname = parts[1];", source)
        self.assertIn('postname = parts.slice(1, -1).join(" ");', source)
        self.assertIn("prename = parts[parts.length - 1];", source)
        self.assertIn("previewStudents(classId, rows)", source)
        self.assertIn("finalPreview.duplicates", source)

    def test_primeink_photo_course_import_uses_detected_max_and_shared_normalizer(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("function primeInkCourseIdentityKey(value)", source)
        self.assertIn("previewCourses(classId, rows)", source)
        self.assertIn("const suggestedMax = Number.isFinite(Number(row.maxPer))", source)
        self.assertIn("existing.maxPer = maxPer", source)
        self.assertIn("finalPreview.duplicates", source)


    def test_manager_auth_overlay_is_hidden_after_successful_login(self):
        source = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        self.assertIn(".auth-screen[hidden],.app[hidden]{display:none!important}", source)
        self.assertIn("$('#authScreen').hidden=true", source)
        self.assertIn("$('#appRoot').hidden=false", source)
        self.assertIn("@media(max-width:480px){.auth-screen{padding:14px;overflow-y:auto}", source)

    def test_official_classink_and_crypt_logos_replace_manager_and_suivi_placeholders(self):
        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")

        self.assertGreaterEqual(manager.count('../assets/classink-logo.svg'), 2)
        self.assertGreaterEqual(manager.count('../assets/crypt-logo.svg'), 2)
        self.assertNotIn('M4 19V5l8 7 8-7v14', manager)
        self.assertIn('../assets/classink-logo.svg', suivi)
        self.assertNotIn('M48 17C48 17 28 38 28 54', suivi)
        self.assertNotIn('href="#logo"', suivi)
        self.assertIn('<img class="splash-logo-icon" src="../assets/classink-logo.svg"', app)
        self.assertNotIn('<!-- Logo Vectoriel Officiel ClassInk -->', app)

    def test_manager_html_is_authenticated_readonly_and_never_persists_school_data(self):
        source = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        self.inspect_file("manager/index.html")
        for storage_api in ("sessionStorage", "indexedDB", "serviceWorker"):
            self.assertNotIn(storage_api, source)
        self.assertIn("classink_manager_theme", source)
        self.assertIn("classink_manager_theme", source)
        self.assertIn("/api/manager/login", source)
        self.assertIn("cache:'no-store'", source)
        self.assertIn("pagehide", source)
        self.assertIn("LECTURE SEULE", source)
        self.assertNotIn("Institut Horizon", source)
        self.assertNotIn("Aline Mukendi", source)
        self.assertIn("window.__CLASSINK_MANAGER_TOKEN__=managerToken", source)
        self.assertIn('data-page="llink"', source)
        self.assertIn('id="page-llink"', source)
        self.assertIn("classink-llink-sessions.js", source)
        self.assertNotIn('id="llink-assist-widget"', source)

    def test_classink_and_crypt_brand_assets_are_wired_into_frontends_and_heads(self):
        classink_logo = (ROOT / "assets/classink-logo.svg").read_text(encoding="utf-8")
        crypt_logo = (ROOT / "assets/crypt-logo.svg").read_text(encoding="utf-8")
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        crypt = (ROOT / "crypt/index.html").read_text(encoding="utf-8")
        self.assertIn("<svg", classink_logo)
        self.assertIn("<svg", crypt_logo)
        self.assertIn("../assets/classink-logo.svg", app)
        self.assertIn("../assets/crypt-logo.svg", app)
        self.assertIn('alt="Logo CRYPT"', app)
        self.assertIn("../assets/classink-logo.svg", prime)
        self.assertIn("../assets/crypt-logo.svg", prime)
        self.assertIn('rel="icon"', prime)
        self.assertIn("../assets/crypt-logo.svg", crypt)
        self.assertIn('rel="icon"', crypt)
        self.assertIn('alt="Logo CRYPT"', crypt)
        self.assertIn('alt="Logo ClassInk"', prime)

    def test_crypt_command_center_frontend_is_interactive_and_has_no_storage(self):
        source = (ROOT / "crypt/index.html").read_text(encoding="utf-8")
        self.inspect_file("crypt/index.html")
        for storage_api in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
            self.assertNotIn(storage_api, source)
        for route in (
            "/api/crypt-console/login",
            "/api/crypt-console/overview",
            "/api/crypt-console/schools",
            "/api/crypt-console/devices",
            "/api/crypt-console/events",
            "/api/crypt-console/tickets",
            "/api/crypt-console/db/query",
        ):
            self.assertIn(route, source)
        self.assertIn("Seules les requêtes SELECT", source)
        self.assertIn("Créer un établissement", source)
        self.assertIn("Déconnexion", source)
        self.assertIn("window.__CRYPT_ADMIN_TOKEN__=token", source)
        self.assertIn('id="llink-assist-widget"', source)

    def test_llink_is_embedded_in_manager_and_suivi_navigation_with_persistent_sessions(self):
        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        client = (ROOT / "assets/classink-llink-sessions.js").read_text(encoding="utf-8")

        for relative, source, app_name in (
            ("manager/index.html", manager, "manager"),
            ("suivi/index.html", suivi, "suivi"),
        ):
            self.inspect_file(relative)
            self.assertIn('data-llink-app="' + app_name + '"', source)
            self.assertIn("classink-llink-sessions.js", source)
            self.assertNotIn('id="llink-assist-widget"', source)
            self.assertNotIn('id="llink-assist-widget-style"', source)

        self.assertIn('data-page="llink"', manager)
        self.assertIn('id="page-llink"', manager)
        self.assertIn('data-go="llink"', suivi)
        self.assertIn("llink: viewLlink", suivi)
        self.assertIn("classink_manager_theme", manager)
        self.assertIn("classink_suivi_theme", suivi)
        self.assertIn("localStorage.setItem('classink_suivi_theme'", suivi)
        self.assertIn("/api/llink/assist", client)
        self.assertIn("history.slice(0, -1).slice(-8)", client)
        self.assertIn("window.localStorage.setItem(key, JSON.stringify(value))", client)

    def test_manager_has_crypt_telemetry_and_no_persistent_storage(self):
        source = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        self.assertIn("const APP = 'manager'", source)
        self.assertIn("/api/telemetry/complaints", source)
        self.assertIn("window.addEventListener('pagehide'", source)
        self.assertIn("classink_manager_theme", source)
        for storage_api in ("sessionStorage", "indexedDB"):
            self.assertNotIn(storage_api, source)

    def test_primeink_has_legacy_schedule_publication_and_preserves_old_schedules_at_login(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("publishExistingScheduleToManager", source)
        self.assertIn("/api/manager/schedule", source)
        self.assertIn("generatedSchedules", source)
        snapshot = source.split("function bpSnapshotLocalOnly()", 1)[1].split("function bpRestoreLocalOnly", 1)[0]
        restore = source.split("function bpRestoreLocalOnly", 1)[1].split("// GESTION DU REGISTRE", 1)[0]
        self.assertIn("horaire.generatedSchedules", snapshot)
        self.assertIn("mixage.generatedSchedules", snapshot)
        self.assertIn("target.generatedSchedules = combined.filter", restore)
        self.assertIn("localSchoolId === remoteSchoolId", source)
        self.assertIn("_managerSchedule", source)
        self.assertIn("slots: Array.isArray(scheduleMeta.slots)", source)
        self.assertIn("['surveillants', 'salles', 'coursProgrammes']", source)

    def test_manager_and_primeink_publish_server_schedule_and_share_suivi_access(self):
        server = (ROOT / "main.py").read_text(encoding="utf-8")
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('@app.post("/api/manager/login")', server)
        self.assertIn('@app.put("/api/manager/schedule")', server)
        self.assertIn('"Cache-Control": "no-store, private, max-age=0"', server)
        self.assertIn('"managerSchedule"', server)
        self.assertIn('"suiviIndividualAccessAllowed": bool(s.suivi_results_allowed)', server)
        self.assertIn("/api/manager/schedule", prime)
        self.assertIn("suiviResultsAllowed", server)


class TestNewFeatureStatic(unittest.TestCase):
    def test_primeink_timetable_slots_initialize_validate_and_render_for_classes_and_teachers(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("updateHoraireStructure();", source)
        self.assertIn('id="timeSlotsContainer"', source)
        self.assertIn('id="horTimeSlotsStatus"', source)
        self.assertIn("function bpScheduleTimeToMinutes(value)", source)
        self.assertIn("startMinutes < previousEndMinutes", source)
        self.assertIn("updateScheduleResultTables(schedule.jours, schedule.heures)", source)
        self.assertIn('id="horClassGridMount"', source)
        self.assertIn('id="horTeacherGridMount"', source)
        self.assertIn("bpClassCell", source)
        self.assertIn("bpTeacherCell", source)
        self.assertIn("horScheduleGenerationNotice", source)
        self.assertIn("Comparer les exemplaires", source)
        self.assertIn("schedule-no-results", source)

        backend = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('previous_end = None', backend)
        self.assertIn('start < previous_end', backend)
        self.assertIn("Les tranches horaires doivent être classées dans l'ordre chronologique", backend)

    def test_primeink_palmares_uses_all_periods_school_scope_and_complete_grade_calculations(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        for marker in (
            "PALMARES_PERIOD_FIELDS",
            "buildPalmaresRecords",
            "classPalmaresRanking",
            "schoolPalmaresRanking",
            "classPalmaresPercentChart",
            "classPalmaresRankChart",
            "schoolPalmaresPercentChart",
            "renderPalmaresStudentDetail",
            "Aucun élève n'a encore toutes les notes",
        ):
            self.assertIn(marker, source)
        self.assertIn("record.percent >= 50", source)
        self.assertIn("courseResults.every(item => item.result.complete)", source)
        self.assertIn("openSchoolPalmaresBtn", source)
    
    
    def test_primeink_grade_visibility_button_is_connected_to_suivi_server_api(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("student-note-access-button", source)
        self.assertIn("window.bpSuiviToggleStudentAccess(st.id)", source)
        self.assertIn("window.bpSuiviToggleStudentAccess = async function(studentId)", source)
        self.assertIn("await bpSuiviLoadRoster(student.classId)", source)
        self.assertNotIn("student-note-access-placeholder", source)
        self.assertNotIn('title="Commande prévue dans une prochaine version"', source)
    
    
    def test_classink_teacher_spaces_are_masked_and_selected_on_login(self):
        source = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn('id="login-school-id"', source)
        self.assertIn('type="password" id="login-school-id"', source)
        self.assertIn('id="setting-school-space-id"', source)
        self.assertIn('type="password" id="setting-school-space-id"', source)
        self.assertIn("/api/teacher/spaces/request", source)
        self.assertIn("/api/teacher/spaces", source)
        self.assertIn("teacherSpaces", source)
        self.assertIn("localSpace !== serverSpace", source)
    
    
    def test_teacher_schedule_is_published_per_verified_school_space(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('@app.get("/api/teacher/schedule")', source)
        self.assertIn('@app.put("/api/teacher/schedule")', source)
        self.assertIn('published = (school.pn_extra or {}).get("managerSchedule")', source)
        self.assertIn('teacher_ids = {str(teacher.teacher_code or ""), f"prof_{teacher.id}", str(teacher.email)}', source)
        self.assertIn('state["teacherSchedule"] = clean_entries', source)
        self.assertIn('L\'horaire scolaire est publié par PrimeInk. L\'enseignant le consulte en lecture seule.', source)
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("/api/teacher/schedule", app)
        self.assertIn("loadTeacherScheduleFromServer", app)
        self.assertIn("method: 'GET'", app)
        self.assertIn("renderTeacherScheduleEditor", app)
        self.assertNotIn("function saveTeacherSchedule()", app)

    def test_classink_schedule_views_use_primeink_weekly_table_layout(self):
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("teacher-schedule-table-wrap", app)
        self.assertIn("table.teacher-schedule-table", app)
        self.assertIn("db.teacherScheduleDays = Array.isArray(result.data.days)", app)
        self.assertIn("db.teacherScheduleSlots = Array.isArray(result.data.slots)", app)
        self.assertIn("db.teacherScheduleHours = Number(result.data.hours)", app)
        self.assertIn("Tranche horaire", app)
        self.assertIn("teacher-schedule-empty-cell", app)

        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        self.assertIn("schedule-grid-wrap", suivi)
        self.assertIn('table class="t schedule-grid"', suivi)
        self.assertIn("Number(schedule.hours)", suivi)
        self.assertIn("slotByHour", suivi)
        self.assertIn("schedule-empty-cell", suivi)

        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        self.assertIn("const dayOrder = ['Lundi', 'Mardi', 'Mercredi', 'Jeudi', 'Vendredi', 'Samedi'];", manager)
        self.assertIn(".schedule-table{width:100%;min-width:760px;table-layout:fixed}", manager)
        self.assertIn("Tranche horaire", manager)

        backend = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn("def published_schedule_table_metadata(published: dict, grid: Optional[dict] = None)", backend)
        self.assertIn('"days": list(SCHEDULE_DAY_NAMES[:day_count])', backend)
        self.assertIn('"slots": slots', backend)
        self.assertIn('"hours": hour_count', backend)
        self.assertIn("**published_schedule_table_metadata(published, grid)", backend)
        self.assertIn("**metadata,", backend)

    def test_quiz_data_and_schema_migrations_are_scoped_safely_by_school(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('school_id = Column(String, ForeignKey("school_information.school_id"), nullable=True, index=True)', source)
        self.assertIn('QuizBank.school_id == active_school_id if active_school_id else QuizBank.school_id.is_(None)', source)
        add_grade_column = source.index('ALTER TABLE student_grades ADD COLUMN IF NOT EXISTS school_id VARCHAR;')
        migrate_grades = source.index('UPDATE student_grades sg SET school_id = t.school_id')
        add_quiz_column = source.index('ALTER TABLE quizzes ADD COLUMN IF NOT EXISTS school_id VARCHAR;')
        migrate_quizzes = source.index('UPDATE quizzes q SET school_id = t.school_id')
        self.assertLess(add_grade_column, migrate_grades)
        self.assertLess(add_quiz_column, migrate_quizzes)

    def test_classink_teacher_schedule_is_read_only_and_drives_bell_timing(self):
        source = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("Cet horaire est publié par l'établissement depuis PrimeInk", source)
        self.assertIn("ne peut pas être modifié depuis l'espace enseignant", source)
        self.assertIn("function loadTeacherScheduleFromServer", source)
        self.assertIn("function renderTeacherScheduleEditor", source)
        self.assertIn("function teacherScheduleTick()", source)
        self.assertIn("entry.end", source)
        self.assertNotIn('type="time" data-field="start"', source)
        self.assertNotIn('type="time" data-field="end"', source)
        self.assertNotIn("function saveTeacherSchedule()", source)
        self.assertNotIn("hours === '08' && minutes === '30'", source)
    
    
    def test_themes_are_persisted_with_dark_default_across_classink_surfaces(self):
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        self.assertIn("db.theme || 'dark'", app)
        self.assertIn("primeink_theme", prime)
        self.assertIn('<body class="theme-dark">', prime)
        self.assertIn("classink_manager_theme", manager)
        self.assertIn("classink_suivi_theme", suivi)
        self.assertIn("theme: 'dark'", suivi)
    
    
    
    def test_classnet_backend_scopes_teacher_spaces_and_notes_by_active_school(self):
        source = (ROOT / "main.py").read_text(encoding="utf-8")
        for marker in (
            "class TeacherSchoolSpace(Base)",
            "class ClassInkTeacherSpaceSnapshot(Base)",
            "teacher_has_school_access(db, teacher, selected_school_id)",
            '@app.post("/api/teacher/spaces/request")',
            '@app.get("/api/teacher/spaces")',
            '@app.get("/api/primenet/teachers/{teacher_code}")',
            "school_id=verified_school_id",
            'test_registration_mode = os.getenv("APP_ENV", "production")',
        ):
            self.assertIn(marker, source)
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("if (localSpace !== serverSpace && (localSpace || serverSpace))", app)


    def test_primeink_school_id_update_command_is_isolated_and_authenticates_new_school(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('id="primeInkSchoolIdUpdatePanel"', source)
        self.assertIn('id="primeInkSchoolIdUpdateInput"', source)
        self.assertIn('id="primeInkSchoolIdUpdatePassword"', source)
        self.assertIn('function isPrimeInkSchoolIdUpdateCommand(value)', source)
        self.assertIn('replace(/\\s+/g, " ")', source)
        activation = source.split("async function verifyActivationCode()", 1)[1].split(
            "// 5. Petite fonction", 1
        )[0]
        self.assertIn("if (isPrimeInkSchoolIdUpdateCommand(code))", activation)
        self.assertLess(
            activation.index("if (isPrimeInkSchoolIdUpdateCommand(code))"),
            activation.index("fetch(`${API_BASE_URL}/api/admin/codes/verify`"),
        )
        self.assertIn("async function applyPrimeInkSchoolIdUpdate()", source)
        self.assertIn("fetch(`${API_BASE_URL}/api/primenet/load`", source)
        self.assertIn("school_id: newSchoolId, password: password", source)
        self.assertIn("if (!response.ok || !result.status || !result.data)", source)
        self.assertIn("if (loadedSchoolId !== newSchoolId)", source)
        self.assertIn("window.localStorage.setItem('primeink_cloud_token', result.token)", source)
        self.assertIn("La base actuelle n'a pas été modifiée.", source)
        self.assertIn("openPrimeInkSchoolIdUpdate(true)", source)
        self.assertIn("panel._primeInkSchoolIdUpdateModalState", source)
        self.assertIn("panel.style.cssText += ';position:fixed;z-index:100001", source)


    def test_teacher_workspaces_refresh_without_relogin_and_are_scoped_by_assignment(self):
        backend = (ROOT / "main.py").read_text(encoding="utf-8")
        classroom = backend.split("def compose_teacher_classroom_context(", 1)[1].split(
            "def apply_teacher_classroom_context(", 1
        )[0]
        self.assertIn('if not teacher_matches_catalog_name(teacher, getattr(course, "titulaire_name", "")):', classroom)
        self.assertIn("assigned_courses_by_class", classroom)
        self.assertIn("visible_class_names", classroom)
        apply_context = backend.split("def apply_teacher_classroom_context(", 1)[1].split(
            "def build_teacher_classroom_context(", 1
        )[0]
        self.assertIn('str(name): list(values or [])', apply_context)
        self.assertIn('state["courseCatalog"] = dict(context.get("courseCatalog") or {})', apply_context)
        self.assertIn('state["students"] = dict(context.get("students") or {})', apply_context)
        self.assertIn('state["teacherCatalogScope"] = "assigned"', apply_context)

        self.assertIn('class ClassInkTeacherIndependentSnapshot(Base)', backend)
        self.assertIn('@app.get("/api/teacher/session/status")', backend)
        self.assertIn('@app.post("/api/teacher/workspace/independent")', backend)
        self.assertIn('claims.get("workspace_mode") or "").strip().lower() == "independent"', backend)
        self.assertIn("if not independent_workspace and not verified_school_id and teacher_has_verified_school_access(teacher):", backend)

        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("async function refreshTeacherSessionStatus(options = {})", app)
        self.assertIn("/api/teacher/session/status", app)
        self.assertIn("/api/teacher/workspace/independent", app)
        self.assertIn("function openIndependentTeacherWorkspace()", app)
        self.assertIn("setInterval(() => refreshTeacherSessionStatus", app)
        self.assertIn("server.teacherCatalogScope === 'assigned'", app)
        self.assertIn("allowedCourses.has(courseName)", app)
        self.assertIn("async function refreshTeacherClassroomData(silent = false)", app)

    def test_classink_grade_sync_and_keyboard_navigation_are_enabled(self):
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("<title>ClassInk App</title>", app)
        self.assertIn("class=\"classink-grade-input\"", app)
        self.assertIn("findClassInkGradeNeighbor(input, direction)", app)
        self.assertIn("data-grid-row=", app)
        self.assertIn("Enregistrer mes notes et présences sur le serveur", app)
        self.assertIn('id="presence-slot"', app)
        self.assertIn("slotKey: selectedSlot", app)
        sync = app.split("async function syncWithCloud()", 1)[1].split("/* ==========================================================================", 1)[0]
        self.assertIn("cloudAuthHeaders()", sync)
        self.assertNotIn("showCustomPrompt", sync)
        self.assertIn("courseCatalog: data.courseCatalog || {}", app)
        self.assertIn("button.style.cssText = 'position:fixed;right:18px;bottom:84px", app)
        self.assertIn("bottom: 16px;", app)
        self.assertIn("left: 12px;", app)

    def test_classink_suivi_persists_only_student_identifiers_not_results(self):
        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        self.assertIn("localStorage.getItem(key)", suivi)
        self.assertIn("localStorage.setItem(key, serialized)", suivi)
        redaction = suivi.split("function redactedParentData(data)", 1)[1].split("function cacheGet(", 1)[0]
        self.assertIn("safe.results = null;", redaction)
        self.assertIn("safe.evaluations = null;", redaction)
        self.assertIn("safe.grades = null;", redaction)
        self.assertIn("Toutes les interrogations", suivi)
        self.assertIn("Téléphone du professeur", suivi)
        self.assertNotIn("N° du professeur", suivi)
        self.assertIn("teacherName", suivi)
        self.assertIn("teacherPhone", suivi)
        self.assertNotIn("teacherCode", suivi)
        self.assertIn("function viewAttendance()", suivi)
        self.assertIn("safe.attendance = null;", redaction)
        self.assertIn("window.__CLASSINK_SUIVI_LLINK_CONTEXT__", suivi)
        self.assertIn("student:studentContext", suivi)

    def test_attendance_communications_and_llink_are_connected_across_classink_surfaces(self):
        backend = (ROOT / "main.py").read_text(encoding="utf-8")
        for marker in (
            "class StudentAttendance(Base)",
            "class TeacherAttendance(Base)",
            "class SchoolCommunication(Base)",
            "class TeacherMessage(Base)",
            '@app.get("/api/primenet/parent-communications")',
            '@app.post("/api/primenet/parent-communications")',
            '@app.get("/api/primenet/teacher-messages")',
            '@app.post("/api/primenet/teacher-messages")',
            '@app.get("/api/teacher/messages")',
            '@app.post("/api/teacher/messages")',
            "accepted_attendance = 0",
            'merged_data["attendanceRecords"]',
            "def build_suivi_attendance_payload",
            "full_day_recorded",
            "context_text = serialize_llink_application_context(context, max_chars=30_000)",
            '"application_instructions": system_instruction',
            '"context": context_text',
            "contextTruncated",
            "student_code = str(parent_ref.get(\"student_code\")",
        ):
            self.assertIn(marker, backend)

        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        llink_client = (ROOT / "assets/classink-llink-sessions.js").read_text(encoding="utf-8")
        self.assertIn("/api/teacher/messages", app)
        self.assertIn('data-llink-app="classink"', app)
        self.assertIn("headers.set('X-CRYPT-App', app)", llink_client)
        self.assertIn("/api/llink/assist", llink_client)
        self.assertIn("slotKey: selectedSlot", app)

        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        for marker in (
            'id="section-communications"',
            'id="primeParentCommunicationTitle"',
            'id="primeTeacherMessageSelect"',
            "/api/primenet/parent-communications",
            "/api/primenet/teacher-messages",
            "/api/primenet/teacher-attendance",
            "mergedData.attendanceRecords",
            'data-llink-app="primeink"',
            "data-llink-sessions",
            "window.renderPresencesEleveTable = function",
        ):
            self.assertIn(marker, prime)

        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        self.assertIn('data-go="attendance"', suivi)
        self.assertIn("Présent toute la journée", suivi)
        self.assertIn("window.__CLASSINK_SUIVI_LLINK_CONTEXT__", suivi)
        self.assertIn("position:fixed;right:18px;bottom:calc(84px + env(safe-area-inset-bottom))", suivi)

    def test_server_persists_exact_course_and_evaluation_date_and_consolidates_averages(self):
        backend = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('course_id = Column(String, index=True, nullable=True)', backend)
        self.assertIn('eval_date = Column(String(32), nullable=True)', backend)
        self.assertIn('def consolidate_classink_period_averages(db: Session, school_id: str)', backend)
        self.assertIn('def calculate_classink_period_average(scores, course_max: float, period: str)', backend)
        self.assertIn('TeacherEvaluation.course_id == course_id', backend)
        self.assertIn('merged_data["moyenne_classe"] = {}', backend)
        self.assertIn('"__id__" + str(student.id)', backend)
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('String(c.id) === String(courseName)', prime)
        self.assertIn("String(studentName).startsWith('__id__')", prime)

    def test_classink_displays_each_assigned_course_as_an_independent_choice(self):
        source = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("const activeCourses = Array.isArray(db.courses[activeClass]) ? db.courses[activeClass] : [];", source)
        self.assertIn("chip.textContent = courseName;", source)
        self.assertIn("chip.onclick = () => changeGradeCourseFilter(courseName);", source)
        self.assertNotIn("const classWithCourse =", source)
        self.assertIn("courseCatalog: data.courseCatalog || {}", source)
        self.assertIn("legacyCourseLabels: data.legacyCourseLabels || {}", source)

    def test_primeink_teacher_assignments_use_available_teacher_selects_and_suspend_auto_restore(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn('<select id="name-titulaire-added-class"', source)
        self.assertIn('<select id="name-titulaire-added-cours"', source)
        self.assertIn('data-field="titulaire">${primeInkTeacherSelectOptions(crs.titulaire || \'\')}', source)
        self.assertIn("function primeInkTeacherSelectOptions(rawValue = '')", source)
        self.assertIn("teacher.uniqueId", source)
        self.assertIn("function refreshPrimeInkTeacherAssignmentSelects()", source)
        self.assertIn("primeInkOriginalLockBoxHtml", source)
        self.assertIn("if (primeInkSchoolSuspensionDetected)", source)
        self.assertIn("if (lockOverlay) lockOverlay.classList.add('hidden');", source)
        self.assertIn("setInterval(checkSchoolStatus, 15000)", source)

        backend = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertIn('if str(claims.get("workspace_mode") or "").strip().lower() == "independent":', backend)
        self.assertIn('"workspaceMode": "independent", "schedule"', backend)
        self.assertIn('state["teacherSchedule"] = clean_entries', backend)

        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("const independent = String(db.workspaceMode || '').toLowerCase() === 'independent';", app)
        self.assertIn("if ((!schoolId && !independent) || !token) return false;", app)


class TestAccountProfileThemeAndRelease(unittest.TestCase):
    def test_account_profile_routes_require_current_password_and_keep_identifiers_immutable(self):
        backend = (ROOT / "main.py").read_text(encoding="utf-8")
        for marker in (
            '@app.get("/api/teacher/profile")',
            '@app.put("/api/teacher/profile")',
            '@app.get("/api/school/profile")',
            '@app.put("/api/school/profile")',
            'PROFILE_UPDATE_LIMITER = AttemptLimiter(max_attempts=5, window_seconds=900)',
            'verify_password(current_password, teacher.password)',
            'verify_password(current_password, school.pass_word)',
            'teacher.password = hash_password(new_password)',
            'school.pass_word = hash_password(new_password)',
            'Les champs email, school_id, code et bulletin_seq_id ne sont volontairement',
            'L\'adresse e-mail, le code enseignant, les associations scolaires',
        ):
            self.assertIn(marker, backend)

        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        for marker in (
            'id="teacher-profile-email"',
            'id="teacher-profile-phone"',
            'id="teacher-profile-current-password"',
            'id="teacher-profile-new-password"',
            'async function loadTeacherAccountProfile',
            'async function saveTeacherAccountProfile',
            "CLOUD_AUTH_URL + '/api/teacher/profile'",
            'aria-readonly="true"',
        ):
            self.assertIn(marker, app)

        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        for marker in (
            'id="page-account"',
            'id="manager-account-school-id"',
            'id="manager-account-email"',
            'id="manager-account-phone"',
            'async function loadManagerAccountProfile',
            'async function saveManagerAccountProfile',
            "API_BASE_URL+'/api/school/profile'",
        ):
            self.assertIn(marker, manager)

        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        for marker in (
            'id="primeInkAccountProfileCard"',
            'id="primeSchoolProfileId"',
            'id="primeSchoolProfileEmail"',
            'id="primeSchoolProfilePhone"',
            'async function loadPrimeInkSchoolProfile',
            'window.savePrimeInkSchoolProfile = async function',
            "API_BASE_URL + '/api/school/profile'",
        ):
            self.assertIn(marker, prime)
        self.assertNotIn("appPasswordInput", prime)
        self.assertNotIn('value="12345678"', prime)

    def test_theme_contrast_overrides_cover_all_four_apps(self):
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        llink_client = (ROOT / "assets/classink-llink-sessions.js").read_text(encoding="utf-8")
        self.assertIn('[data-theme="dark"] tr:nth-child(even)', app)
        self.assertIn('data-llink-messages', suivi)
        self.assertIn('data-llink-messages', manager)
        self.assertIn('var(--text-primary, var(--text', llink_client)
        self.assertIn('var(--border-color, var(--line', llink_client)
        self.assertIn('body.theme-dark .fiche-grade-input', prime)
        self.assertIn('color-scheme:dark', prime)

    def test_public_website_and_privacy_supplement_include_november_updates(self):
        site = (ROOT / "index.html").read_text(encoding="utf-8")
        privacy = (ROOT / "confidentialite/index.html").read_text(encoding="utf-8")
        for marker in (
            'id="updates"',
            '<h2>Nouveautés</h2>',
            '<time datetime="2026-11-22">22 novembre 2026</time>',
            "Présences synchronisées",
            "Communiqués destinés aux parents",
            "Llink connecté au contexte autorisé",
            "Profil et sécurité",
        ):
            self.assertIn(marker, site)
        for marker in (
            "Complément d'information — Fonctionnalités mises à jour le 22 novembre 2026",
            "PBKDF2-SHA256",
            "le texte de la question et les éléments du contexte nécessaires",
            "Les notes, résultats et présences détaillés ne sont pas conservés dans son cache local",
            "Les conversations privées des enseignants",
        ):
            self.assertIn(marker, privacy)
        # Les six articles existants sont conservés ; le complément est séparé et ajouté après eux.
        for heading in (
            "Article 1. Architecture Technique et Isolement du Code",
            "Article 2. Souveraineté des Données et Contrôle par Mot de Passe",
            "Article 3. Anonymisation et Amélioration Continue des Produits",
            "Article 4. Option 100% Réseau Local (Mode Isolateur LAN / PrimeNet)",
            "Article 5. Système Cryptographique d'Intégrité et Comparaison Anti-Altération",
            "Article 6. Droits d'Accès et Contact",
        ):
            self.assertIn(heading, privacy)


class TestCourseSeparationAndDeviceUnlock(unittest.TestCase):
    def test_primeink_local_unlock_and_bulletin_course_composition(self):
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        backend = (ROOT / "main.py").read_text(encoding="utf-8")

        for marker in (
            "window.primeInkRememberLocalPassword = async function(password, schoolId)",
            "window.primeInkVerifyLocalPassword = async function(password, schoolId)",
            "PBKDF2-SHA-256",
            "primeink_local_unlock_v1:",
            "const localResult = await window.primeInkVerifyLocalPassword(pass, schoolId)",
            "if (localResult === true)",
            "if (localResult === false)",
            "await window.primeInkRememberLocalPassword(password, schoolId)",
        ):
            self.assertIn(marker, prime)

        unlock_start = prime.index("// 7. Déverrouillage Écran")
        unlock_end = prime.index("// Chargement initial des vues", unlock_start)
        unlock_handler = prime[unlock_start:unlock_end]
        self.assertIn("window.primeInkVerifyLocalPassword", unlock_handler)
        self.assertIn("Migration des installations plus anciennes", unlock_handler)
        self.assertIn("if (!schoolId)", unlock_handler)
        self.assertIn("Impossible de vérifier le mot de passe", unlock_handler)
        self.assertNotIn("overlay.classList.add('hidden');\\n                    return;", unlock_handler)

        save_start = prime.index("window.savePrimeInkBulletinCourses = async function")
        save_end = prime.index("window.deleteCourse = async function", save_start)
        save_handler = prime[save_start:save_end]
        self.assertIn("if (typeof bpRefreshBulletin === 'function') bpRefreshBulletin();", save_handler)
        self.assertLess(save_handler.index("bpRefreshBulletin"), save_handler.index("const headers = primeInkAuthHeaders()"))

        delete_start = prime.index("window.deleteCourse = async function(courseId)")
        delete_end = prime.index("// INITIALISATION ET ÉCOUTEURS D'ÉVÉNEMENTS", delete_start)
        delete_handler = prime[delete_start:delete_end]
        self.assertIn("'/api/primenet/classes/'", delete_handler)
        self.assertIn("'/bulletin-courses'", delete_handler)
        self.assertIn("method: 'PUT'", delete_handler)
        self.assertIn("bpRefreshBulletin", delete_handler)

        for marker in (
            'bulletin_courses = Column(JSON, nullable=True)',
            'ADD COLUMN IF NOT EXISTS bulletin_courses JSON',
            '"bulletinCourses": c.bulletin_courses if isinstance(c.bulletin_courses, list) else None',
            '@app.put("/api/primenet/classes/{class_id}/bulletin-courses")',
            "clean_bulletin_course_mappings",
            "sourceCourseIds",
        ):
            self.assertIn(marker, backend)

        self.assertIn("function bpBulletinCoursesForClass(cls)", prime)
        self.assertIn("function bpCourseValues(studentId, crs)", prime)
        self.assertIn("courseMax = (bpNum(course.maxPer) || 0) * factor", prime)
        self.assertIn("ratios.reduce((sum, value) => sum + value, 0) / ratios.length", prime)
        builder = prime[prime.index("function bpBuildPage(student, opts)"):prime.index("function bpFitScreen", prime.index("function bpBuildPage(student, opts)"))]
        self.assertIn("const classCourses = bpBulletinCoursesForClass(cls);", builder)

        fiche = prime[prime.index("function renderCoursesSidebar"):prime.index("let currentFicheCourseId")]
        stats = prime[prime.index("function renderStatsSidebar"):prime.index("function renderCourseStats")]
        self.assertIn("appState.courses.filter", fiche)
        self.assertIn("appState.courses.filter", stats)
        self.assertIn("FICHES DES COURS ENSEIGNÉS", prime)
        self.assertIn("STATISTIQUES DES COURS ENSEIGNÉS", prime)

    def test_bulletin_course_average_uses_normalized_taught_course_scores(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("Node.js absent : calcul de moyenne des cours-bulletins ignoré.")
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        display_start = prime.index("function primeInkDisplayTeacherName(rawValue)")
        display_end = prime.index("function primeInkTeacherSelectOptions", display_start)
        num_start = prime.index("function bpNum(v)")
        num_end = prime.index("function bpAcc()", num_start)
        values_start = prime.index("function bpBulletinCoursesForClass(cls)")
        values_end = prime.index("function bpStudentTotals", values_start)
        helper = prime[display_start:display_end] + prime[num_start:num_end] + """
function bpSum(values) {
    if (!Array.isArray(values) || !values.length || values.some(value => value === null || value === undefined)) return null;
    return values.reduce((sum, value) => sum + value, 0);
}
""" + prime[values_start:values_end]
        script = helper + """
const appState = {
  courses: [
    {id:"geo", classId:"c1", maxPer:20, titulaire:"Prof. A"},
    {id:"eco", classId:"c1", maxPer:10, titulaire:"Prof. B"}
  ],
  grades: {
    "s1_geo": {p1:15, p2:16, ex1:30, p3:14, p4:15, ex2:32},
    "s1_eco": {p1:5, p2:8, ex1:15, p3:7, p4:8, ex2:16}
  }
};
const cls = {id:"c1", titulaire:"Direction", bulletinCourses:[{
  id:"bc1", name:"Géologie / Écologie", maxPer:20,
  sourceCourseIds:["geo","eco"]
}]};
const mapped = bpBulletinCoursesForClass(cls);
const values = bpCourseValues("s1", mapped[0]);
const missing = bpCourseValues("s2", mapped[0]);
process.stdout.write(JSON.stringify({
  count:mapped.length,
  sourceCount:mapped[0].sourceCourseIds.length,
  p1:values.p1, ex1:values.ex1, s1:values.s1, tg:values.tg,
  missingP1:missing.p1
}));
"""
        result = subprocess.run(
            [node, "-e", script], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        self.assertEqual(
            json.loads(result.stdout),
            {"count":1,"sourceCount":2,"p1":12.5,"ex1":30.0,"s1":58.5,"tg":120.0,"missingP1":None},
        )

    def test_classink_app_can_print_complete_taught_course_sheets(self):
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("<div>ClassInk APP ⚡</div>", app)
        self.assertNotIn("CLASS-INK SUITE", app)
        self.assertIn('onclick="printActiveCourseSheet()"', app)
        self.assertIn("function printActiveCourseSheet()", app)
        for marker in (
            "FICHE DE NOTES",
            "<strong>Classe :</strong>",
            "<strong>Cours :</strong>",
            "<strong>Enseignant :</strong>",
            "<strong>E-mail :</strong>",
            "<strong>Barème :</strong>",
            "window.print()",
        ):
            self.assertIn(marker, app)



class TestLlinkSessionsKeepaliveAndClassDomains(unittest.TestCase):
    def test_shared_llink_sessions_and_keepalive_are_enabled_in_all_four_apps(self):
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        client = (ROOT / "assets/classink-llink-sessions.js").read_text(encoding="utf-8")
        backend = (ROOT / "main.py").read_text(encoding="utf-8")

        for source in (app, suivi, manager, prime):
            self.assertIn("classink-llink-sessions.js", source)
            self.assertIn("CLASSINK_LLINK_CONFIG", source)

        for app_name, source in (
            ("classink", app), ("suivi", suivi), ("manager", manager), ("primeink", prime)
        ):
            self.assertIn('data-llink-app="' + app_name + '"', source)

        self.assertIn('data-llink-app="classink"', app)
        self.assertIn("String(teacher) + ':' + String(school)", app)
        for marker in (
            "classink:llink:sessions:v1:",
            "window.localStorage.setItem(key, JSON.stringify(value))",
            "/api/llink/conversations",
            "/api/llink/assist",
            "data-llink-sessions",
            "data-llink-new",
            "data-llink-delete",
            "ping();",
            "5 * 60 * 1000",
        ):
            self.assertIn(marker, client)

        self.assertIn('@app.get("/api/keepalive")', backend)
        self.assertIn("LLINK_UPSTREAM_SEMAPHORE = asyncio.Semaphore(3)", backend)
        self.assertIn("for attempt in range(2)", backend)
        self.assertIn("await asyncio.sleep(max(0.5, retry_delay))", backend)
        self.assertIn('@app.get("/api/llink/conversations")', backend)
        self.assertIn('@app.put("/api/llink/conversations/{conversation_id}")', backend)
        self.assertIn('@app.delete("/api/llink/conversations/{conversation_id}")', backend)
        self.assertIn("class LlinkConversation(Base)", backend)
        self.assertIn("owner_key", backend)

    def test_suivi_and_manager_use_navigation_instead_of_floating_llink_widget(self):
        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        manager = (ROOT / "manager/index.html").read_text(encoding="utf-8")

        self.assertIn('data-go="llink"', suivi)
        self.assertIn("llink: viewLlink", suivi)
        self.assertNotIn('id="llink-assist-widget-style"', suivi)
        self.assertNotIn('id="llink-assist-toggle"', suivi)

        self.assertIn('data-page="llink"', manager)
        self.assertIn('id="page-llink"', manager)
        self.assertNotIn('id="llink-assist-widget-style"', manager)
        self.assertNotIn('id="llink-assist-toggle"', manager)

    def test_classink_app_navigation_is_content_sized(self):
        app = (ROOT / "app/index.html").read_text(encoding="utf-8")
        self.assertIn("width: max-content", app)
        self.assertIn("max-width: calc(100vw - 112px)", app)
        self.assertIn('data-llink-sessions', app)
        self.assertIn('data-llink-form', app)

    def test_suivi_menu_keeps_settings_visible_and_shows_teacher_phone(self):
        suivi = (ROOT / "suivi/index.html").read_text(encoding="utf-8")
        self.assertIn("grid-template-columns: repeat(4, minmax(0, 1fr))", suivi)
        self.assertIn("grid-template-columns: repeat(5, minmax(0, 1fr))", suivi)
        self.assertIn("Téléphone du professeur", suivi)
        self.assertNotIn("N° du professeur", suivi)

    def test_primeink_uses_class_configured_domains_and_displays_teacher_names(self):
        prime = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        backend = (ROOT / "main.py").read_text(encoding="utf-8")

        self.assertIn('<select id="sub-category-cours"', prime)
        self.assertIn("function primeInkClassDomains(cls)", prime)
        self.assertIn("function primeInkClassSubdomains(cls, domain)", prime)
        self.assertIn("data-open-class-domains", prime)
        self.assertIn("window.openPrimeInkClassDomainsModal", prime)
        self.assertIn("window.savePrimeInkClassDomains", prime)
        self.assertIn("primeInkFillSubdomainSelect", prime)
        self.assertIn("function bpTeacherDisplayName(value)", prime)
        self.assertIn("function primeInkDisplayTeacherName(rawValue)", prime)
        self.assertIn("currentCourseTitulaire').textContent = primeInkDisplayTeacherName(course.titulaire)", prime)
        self.assertIn("...(cls.subDomains && typeof cls.subDomains === 'object' ? { subDomains: cls.subDomains } : {})", prime)

        self.assertIn("sub_domains = Column(JSON, nullable=True)", backend)
        self.assertIn("ADD COLUMN IF NOT EXISTS sub_domains JSON", backend)
        self.assertIn('"subDomains": c.sub_domains if isinstance(c.sub_domains, dict) else {}', backend)
        self.assertIn('@app.put("/api/primenet/classes/{class_id}/domains")', backend)
        self.assertIn("clean_class_domain_configuration", backend)

if __name__ == "__main__":
    unittest.main()
