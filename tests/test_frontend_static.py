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
        self.assertIn("String(c.classId ?? '') === studentClassId", source)
        self.assertIn("String(s.classId ?? '') === studentClassId", source)
        self.assertIn("bp-empty-courses", source)
        self.assertIn("page.style.transform = 'none';", source)
        self.assertNotIn("Math.min(sheet.clientWidth / pw, sheet.clientHeight / ph)", source)

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
        for storage_api in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
            self.assertNotIn(storage_api, source)
        self.assertIn("const memoryStore = new Map()", source)
        self.assertIn("cache: 'no-store'", source)
        self.assertIn("pagehide", source)

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


    def test_manager_html_is_authenticated_readonly_and_never_persists_school_data(self):
        source = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        self.inspect_file("manager/index.html")
        for storage_api in ("localStorage", "sessionStorage", "indexedDB", "serviceWorker"):
            self.assertNotIn(storage_api, source)
        self.assertIn("/api/manager/login", source)
        self.assertIn("cache:'no-store'", source)
        self.assertIn("pagehide", source)
        self.assertIn("LECTURE SEULE", source)
        self.assertNotIn("Institut Horizon", source)
        self.assertNotIn("Aline Mukendi", source)

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

    def test_manager_has_crypt_telemetry_and_no_persistent_storage(self):
        source = (ROOT / "manager/index.html").read_text(encoding="utf-8")
        self.assertIn("const APP = 'manager'", source)
        self.assertIn("/api/telemetry/complaints", source)
        self.assertIn("window.addEventListener('pagehide'", source)
        for storage_api in ("localStorage", "sessionStorage", "indexedDB"):
            self.assertNotIn(storage_api, source)

    def test_primeink_has_legacy_schedule_publication(self):
        source = (ROOT / "prime/index.html").read_text(encoding="utf-8")
        self.assertIn("publishExistingScheduleToManager", source)
        self.assertIn("/api/manager/schedule", source)
        self.assertIn("generatedSchedules", source)

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


if __name__ == "__main__":
    unittest.main()
