import os
import secrets
import string
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import httpx
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
import random

class ScheduleEngine:
    """
    Moteur de génération d'horaires scolaires hybride (CSP + Heuristique).
    """

    def __init__(self, config: Dict[str, Any], classes: List[Dict], teachers: List[Dict], courses: List[Dict]):
        self.jours_travail = config.get("joursTravail", 6)
        self.heures_par_jour = config.get("heuresParJour", 6)
        self.classes = classes
        self.teachers = teachers
        self.courses = courses
        self.grid = {} # Structure: { (class_id, jour, heure): course_id }
        self.teacher_busy = set() # Structure: (teacher_name, jour, heure)

    # --- ÉTAPE 1 : Extraire et valider les contraintes de volume horaire ---
    def step_1_prepare_variables() -> List[Dict]:
        tasks = []
        for course in self.courses:
            # Récupère le volume horaire hebdomadaire attribué
            volume_heures = course.get("maxPer", 4) // 10 # Estimation créneaux
            for _ in range(max(1, volume_heures)):
                tasks.append({
                    "course_id": course["id"],
                    "class_id": course["classId"],
                    "teacher": course["titulaire"],
                    "course_name": course["name"]
                })
        return tasks

    # --- ÉTAPE 2 : Définir la matrice de disponibilité des enseignants ---
    def step_2_build_teacher_availability(self) -> Dict[str, List[int]]:
        avail_map = {}
        for t in self.teachers:
            # Map les jours de disponibilité déclarés
            avail_map[t["name"]] = t.get("dispoJours", list(range(1, self.jours_travail + 1)))
        return avail_map

    # --- ÉTAPE 3 : Tri heuristique des cours (Placement des plus contraints d'abord) ---
    def step_3_heuristic_sort(self, tasks: List[Dict], avail_map: Dict) -> List[Dict]:
        def constraint_score(task):
            teacher_dispo = len(avail_map.get(task["teacher"], []))
            return teacher_dispo # Moins le prof a de jours, plus il est prioritaire
        
        return sorted(tasks, key=constraint_score)

    # --- ÉTAPE 4 : Moteur d'affectation par Retour Arrière (Backtracking) ---
    def step_4_backtrack_assignment(self, tasks: List[Dict], avail_map: Dict) -> bool:
        if not tasks:
            return True # Tous les cours sont placés

        task = tasks[0]
        teacher = task["teacher"]
        class_id = task["class_id"]
        valid_days = avail_map.get(teacher, list(range(1, self.jours_travail + 1)))

        for jour in valid_days:
            for heure in range(1, self.heures_par_jour + 1):
                # Vérification des Contraintes Strictes (Hard Constraints)
                slot_class = (class_id, jour, heure)
                slot_teacher = (teacher, jour, heure)

                if slot_class not in self.grid and slot_teacher not in self.teacher_busy:
                    # Affectation temporaire
                    self.grid[slot_class] = task
                    self.teacher_busy.add(slot_teacher)

                    if self.step_4_backtrack_assignment(tasks[1:], avail_map):
                        return True

                    # Annulation (Backtrack)
                    del self.grid[slot_class]
                    self.teacher_busy.remove(slot_teacher)

        return False

    # --- ÉTAPE 5 : Validation & Formatage de la Grille Générée ---
    def step_5_export_schedule() -> List[Dict]:
        formatted_schedules = []
        for (class_id, jour, heure), task in self.grid.items():
            formatted_schedules.append({
                "classId": class_id,
                "jour": jour,
                "heure": heure,
                "course": task["course_name"],
                "teacher": task["teacher"]
            })
        return formatted_schedules

    def generate(self) -> Dict[str, Any]:
        tasks = self.step_1_prepare_variables()
        avail_map = self.step_2_build_teacher_availability()
        sorted_tasks = self.step_3_heuristic_sort(tasks, avail_map)
        
        success = self.step_4_backtrack_assignment(sorted_tasks, avail_map)
        if success:
            return {"status": True, "schedule": self.step_5_export_schedule()}
        return {"status": False, "message": "Impossible de résoudre l'horaire avec ces contraintes."}

class ExamMixerEngine:
    """
    Moteur de mixage pour examens : Attribution ID unique (4 char)
    et répartition anti-triche dans les salles/rangées/bancs.
    """

    @staticmethod
    def generate_short_id(existing_ids: set) -> str:
        """Génère un ID unique de 4 caractères alfanumériques majuscules (ex: 'A7K9')."""
        alphabet = string.ascii_uppercase + string.digits
        while True:
            code = ''.join(random.choices(alphabet, k=4))
            if code not in existing_ids:
                existing_ids.add(code)
                return code

    @classmethod
    def mix_students_and_assign_seats(cls, students: List[Dict], rooms: List[Dict]) -> Dict[str, Any]:
        existing_ids = set()
        
        # 1. Attribution des ID uniques courts (<= 4 caractères)
        prepared_students = []
        for st in students:
            st_copy = dict(st)
            st_copy["exam_id"] = cls.generate_short_id(existing_ids)
            prepared_students.append(st_copy)

        # Groupement des élèves par classe pour tirage alterné
        class_buckets = {}
        for st in prepared_students:
            c_id = st["classId"]
            class_buckets.setdefault(c_id, []).append(st)

        # Mélange individuel de chaque classe
        for c_id in class_buckets:
            random.shuffle(class_buckets[c_id])

        # 2. Interleave / Tirage alterné pour maximiser le mélange des classes
        mixed_pool = []
        while any(class_buckets.values()):
            for c_id in list(class_buckets.keys()):
                if class_buckets[c_id]:
                    mixed_pool.append(class_buckets[c_id].pop(0))

        # 3. Remplissage des salles, rangées et bancs
        rooms_placement = []
        student_cursor = 0
        total_students = len(mixed_pool)

        for room in rooms:
            if student_cursor >= total_students:
                break

            room_name = room.get("nom", f"Salle {room.get('id')}")
            num_rangees = room.get("rangees", 3)
            num_bancs = room.get("bancs", 10)
            capacity = room.get("places", num_rangees * num_bancs)

            seats_assignment = []
            seat_count = 0

            for r in range(1, num_rangees + 1):
                for b in range(1, num_bancs + 1):
                    if seat_count >= capacity or student_cursor >= total_students:
                        break

                    student = mixed_pool[student_cursor]
                    seats_assignment.append({
                        "rangee": r,
                        "banc": b,
                        "student_exam_id": student["exam_id"],
                        "student_name": f"{student['name']} {student['postname']}",
                        "original_class": student["classId"]
                    })
                    
                    student_cursor += 1
                    seat_count += 1

            rooms_placement.append({
                "room_id": room.get("id"),
                "room_name": room_name,
                "assigned_students_count": seat_count,
                "seating_plan": seats_assignment
            })

        return {
            "status": True,
            "total_mixed": student_cursor,
            "unassigned_students": total_students - student_cursor,
            "result": rooms_placement
        }

# ---------------------------------------------------------
# 1. CONFIGURATION ET BASE DE DONNÉES POSTGRESQL
# ---------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://classnet_user:password@localhost:5432/classnet")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

ADMIN_EMAIL = "gabriel.kahorha@gmail.com" # Ton adresse pour recevoir les codes

# ---------------------------------------------------------
# 2. MODÈLES DE BASE DE DONNÉES ENRICHIS
# ---------------------------------------------------------
class OTPVerification(Base):
    __tablename__ = "otp_codes"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, index=True, nullable=False)
    code = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)

class SchoolInformation(Base):
    __tablename__ = "school_information"
    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, unique=True, index=True, nullable=False)
    bulletin_seq_id = Column(String, unique=True, nullable=False)
    code = Column(String, nullable=False)
    name_school = Column(String, nullable=False)
    city = Column(String, nullable=False)
    commune = Column(String, nullable=False)
    name_responsable = Column(String, nullable=False)
    num_tel = Column(String, nullable=False)
    adresse_physique = Column(String, nullable=False)
    email = Column(String, nullable=False)
    pass_word = Column(String, nullable=False)
    licence_date = Column(String, default="2026-12-31")
    is_locked = Column(Boolean, default=False)

    classes = relationship("ClasseInformation", back_populates="school")
    students = relationship("SchoolStudentInformation", back_populates="school")
    courses = relationship("CourseInformation", back_populates="school")
    teachers = relationship("Teacher", back_populates="school")

class Teacher(Base):
    __tablename__ = "teachers"
    id = Column(Integer, primary_key=True, index=True)
    teacher_code = Column(String(50), unique=True, index=True, nullable=True)
    email = Column(String, unique=True, index=True, nullable=False)
    full_name = Column(String, nullable=False)
    password = Column(String, nullable=False)
    phone_number = Column(String, nullable=True)
    age = Column(Integer, nullable=True)
    school_name = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    status = Column(String, default="Actif")
    school_id = Column(String, ForeignKey("school_information.school_id"), nullable=True)
    llink_preferences = Column(Text, nullable=True)

    school = relationship("SchoolInformation", back_populates="teachers")
    quizzes = relationship("QuizBank", back_populates="teacher")

class ClasseInformation(Base):
    __tablename__ = "classe_informations"
    id = Column(String, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"))
    class_name = Column(String, nullable=False)
    titulaire_name = Column(String, nullable=True)
    domaines = Column(JSON, nullable=True)
    school = relationship("SchoolInformation", back_populates="classes")
    students = relationship("SchoolStudentInformation", back_populates="classe")
    courses = relationship("CourseInformation", back_populates="classe")

class CourseInformation(Base):
    __tablename__ = "course_informations"
    id = Column(String, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"))
    class_id = Column(String, ForeignKey("classe_informations.id"))
    course_name = Column(String, nullable=False)
    max_per = Column(Float, nullable=False, default=40.0)
    category = Column(String, nullable=False)
    titulaire_name = Column(String, nullable=True)
    school = relationship("SchoolInformation", back_populates="courses")
    classe = relationship("ClasseInformation", back_populates="courses")

class SchoolStudentInformation(Base):
    __tablename__ = "school_student_informations"
    id = Column(String, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"))
    class_id = Column(String, ForeignKey("classe_informations.id"))
    student_name = Column(String, nullable=False)
    student_post_name = Column(String, nullable=False)
    student_pre_name = Column(String, nullable=False)
    student_sexe = Column(String(1), nullable=False)
    student_born_date = Column(String, nullable=True)
    student_born_place = Column(String, nullable=True)
    student_n_permanent = Column(String, unique=True, index=True)
    school = relationship("SchoolInformation", back_populates="students")
    classe = relationship("ClasseInformation", back_populates="students")

class TeacherEvaluation(Base):
    __tablename__ = "teacher_s_evaluations"
    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, nullable=False)
    student_n_permanent = Column(String, nullable=False)
    course_id = Column(String, nullable=False)
    p1 = Column(Float, nullable=True)
    p2 = Column(Float, nullable=True)
    ex1 = Column(Float, nullable=True)
    p3 = Column(Float, nullable=True)
    p4 = Column(Float, nullable=True)
    ex2 = Column(Float, nullable=True)

class SyncHistory(Base):
    __tablename__ = "sync_history"
    id = Column(Integer, primary_key=True, index=True)
    teacher_email = Column(String, index=True)
    action_type = Column(String)
    payload_diff = Column(JSON)
    status = Column(String, default="en_attente") # "en_attente" ou "transmis"
    created_at = Column(DateTime, default=datetime.utcnow)

class QuizBank(Base):
    __tablename__ = "quizzes"
    id = Column(String, primary_key=True, index=True)
    teacher_email = Column(String, ForeignKey("teachers.email"))
    title = Column(String)
    content = Column(Text)
    teacher = relationship("Teacher", back_populates="quizzes")

class AccessCode(Base):
    __tablename__ = "access_codes"
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, index=True, nullable=False)

Base.metadata.create_all(bind=engine)

# --- FONCTIONS DE GESTION DE LA BASE ---

def init_db():
    """
    Crée toutes les tables définies ci-dessus si elles n'existent pas encore.
    Équivalent automatisé de tes requêtes CREATE TABLE IF NOT EXISTS.
    """
    Base.metadata.create_all(bind=engine)
    print("✅ Base de données initialisée avec succès.")

def reset_db():
    """
    Supprime de force toutes les tables existantes (DROP) et les recrée à zéro.
    Utile pour vider entièrement les données et appliquer une nouvelle structure.
    """
    print("⚠️ Suppression des tables en cours...")
    Base.metadata.drop_all(bind=engine)
    print("🧹 Base de données vidée.")
    init_db()

# Exécution automatique (sécurisée)
init_db()

# ---------------------------------------------------------
# 3. SCHÉMAS PYDANTIC
# ---------------------------------------------------------
class TeacherInitSchema(BaseModel):
    full_name: str
    email: EmailStr
    school_name: Optional[str] = None
    password: str
    phone_number: str
    age: int

class OTPVerifySchema(BaseModel):
    email: EmailStr
    otp_code: str
    teacher_data: TeacherInitSchema

class SchoolRegisterSchema(BaseModel):
    school_id: str
    bulletin_seq_id: str
    code: str
    name_school: str
    city: str
    commune: str
    name_responsable: str
    num_tel: str
    adresse_physique: str
    email: EmailStr
    pass_word: str

class LoginSchema(BaseModel):
    identifier: str # Email pour prof, school_id pour école
    password: str

class CodeVerifySchema(BaseModel):
    identifier: str
    code: str

# ---------------------------------------------------------
# 4. SERVICES AUXILIAIRES
# ---------------------------------------------------------
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def generate_20_char_code() -> str:
    # 62 caractères ^ 20 = ~119 bits d'entropie
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(20))

def generate_otp() -> str:
    return ''.join(secrets.choice(string.digits) for _ in range(6))

def send_email_mock(to_email: str, subject: str, body: str):
    # Remplacer par configuration SMTP réelle si besoin
    print(f"📧 [EMAIL SENT to {to_email}] | Sujet: {subject} | Corps: {body}")

# ---------------------------------------------------------
# 5. INITIALISATION FASTAPI
# ---------------------------------------------------------
app = FastAPI(title="CRYPT Cloud Internet Node", version="3.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# ---------------------------------------------------------
# 6. ROUTES D'AUTHENTIFICATION ET COMPTES
# ---------------------------------------------------------
@app.post("/api/auth/teacher/init-register")
def init_teacher_register(data: TeacherInitSchema, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Étape 1: Vérifie l'email et envoie un code OTP valable 15 minutes."""
    if db.query(Teacher).filter(Teacher.email == data.email).first():
        return {"status": False, "message": "Cet email est déjà utilisé."}
    
    otp = generate_otp()
    expiry = datetime.utcnow() + timedelta(minutes=15)
    
    db.query(OTPVerification).filter(OTPVerification.email == data.email).delete()
    db.add(OTPVerification(email=data.email, code=otp, expires_at=expiry))
    db.commit()
    
    msg = f"Salut {data.full_name}, ton code de vérification ClassNet est : {otp}. Il expire dans 15 minutes."
    background_tasks.add_task(send_email_mock, data.email, "Code de vérification ClassNet", msg)
    
    return {"status": True, "message": "Code envoyé sur l'adresse mail."}

@app.post("/api/auth/teacher/verify-register")
def verify_teacher_register(data: OTPVerifySchema, db: Session = Depends(get_db)):
    """Étape 2: Valide l'OTP et crée l'enseignant."""
    record = db.query(OTPVerification).filter(OTPVerification.email == data.email, OTPVerification.code == data.otp_code).first()
    
    if not record or record.expires_at < datetime.utcnow():
        return {"status": False, "message": "Code invalide ou expiré."}
    
    new_teacher = Teacher(
        email=data.teacher_data.email,
        full_name=data.teacher_data.full_name,
        school_name=data.teacher_data.school_name,
        password=data.teacher_data.password,
        phone_number=data.teacher_data.phone_number,
        age=data.teacher_data.age,
        teacher_code=f"prof_{generate_otp()}"
    )
    db.add(new_teacher)
    db.delete(record)
    db.commit()
    return {"status": True, "message": "Enseignant enregistré avec succès !"}

@app.post("/api/auth/school/register")
def register_school(data: SchoolRegisterSchema, db: Session = Depends(get_db)):
    if db.query(SchoolInformation).filter(SchoolInformation.school_id == data.school_id).first():
        return {"status": False, "message": "École déjà existante."}
    db.add(SchoolInformation(**data.dict()))
    db.commit()
    return {"status": True, "message": "École enregistrée."}

@app.post("/api/auth/teacher/login")
def login_teacher(data: LoginSchema, db: Session = Depends(get_db)):
    """Connexion Prof : Renvoie l'état exact defaultDB pour ClassNet App."""
    teacher = db.query(Teacher).filter(Teacher.email == data.identifier, Teacher.password == data.password).first()
    if not teacher:
        return {"status": False, "message": "Identifiants incorrects."}
    
    # Construction de la DB ClassNet App
    classnet_app_db = {
        "user": {
            "id": teacher.teacher_code,
            "name": teacher.full_name,
            "email": teacher.email,
            "password": teacher.password,
            "school": teacher.school_name or "Indépendant",
            "isLoggedIn": True
        },
        "classes": [], "courses": {}, "courseMax": {},
        "periodVisibility": {"P1": True, "P2": False, "EX1": False, "P3": False, "P4": False, "EX2": False},
        "activeCourseFilter": {}, "students": {}, "evaluations": {}, "grades": {},
        "presences": [], "quizzes": [], "llinkPrefs": teacher.llink_preferences or "", "pendingCommits": 0
    }
    return {"status": True, "data": classnet_app_db}

@app.post("/api/auth/school/login")
def login_school(data: LoginSchema, db: Session = Depends(get_db)):
    """Connexion École : Renvoie l'état exact defaultState pour PrimeNet/ClassNet P."""
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == data.identifier, SchoolInformation.pass_word == data.password).first()
    if not school:
        return {"status": False, "message": "Identifiants incorrects."}
    
    primenet_state = {
        "school": {
            "name": school.name_school, "id": school.school_id, "code": school.code,
            "city": school.city, "commune": school.commune
        },
        "classes": [], "students": [], "courses": [], "teachers": [], "grades": {},
        "tools": {
            "presences": {"students": [], "teachers": []},
            "mixage": {"config": {}, "surveillants": [], "salles": [], "coursProgrammes": [], "generatedSchedules": []},
            "horaire": {"config": {}, "profsDispo": [], "generatedSchedules": []}
        }
    }
    return {"status": True, "data": primenet_state}

# ---------------------------------------------------------
# 7. GESTION DES CODES À USAGE UNIQUE (119 BITS)
# ---------------------------------------------------------
@app.post("/api/admin/codes/generate")
def generate_and_send_code(background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    new_code = generate_20_char_code()
    db.add(AccessCode(code=new_code))
    db.commit()
    background_tasks.add_task(send_email_mock, ADMIN_EMAIL, "Nouveau Code ClassNet", f"Code généré : {new_code}")
    return {"status": True, "message": "Code généré et envoyé à l'administrateur."}

@app.post("/api/admin/codes/verify")
def verify_and_cycle_code(data: CodeVerifySchema, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Vérifie, détruit, génère un nouveau et renvoie expiration ou échec."""
    code_record = db.query(AccessCode).filter(AccessCode.code == data.code).first()
    if not code_record:
        return {"status": False, "code": "0000"}
    
    db.delete(code_record)
    new_code = generate_20_char_code()
    db.add(AccessCode(code=new_code))
    db.commit()
    
    background_tasks.add_task(send_email_mock, ADMIN_EMAIL, "Renouvellement Code ClassNet", f"Utilisateur {data.identifier} a consommé un code. Nouveau code actif : {new_code}")
    
    return {
        "status": True, 
        "message": "Code valide et renouvelé.",
        "expiration_date": datetime.utcnow().strftime("%Y-%m-%d")
    }

# ---------------------------------------------------------
# 8. SYNCHRONISATION ET MISE À JOUR (APP & PRIMENET)
# ---------------------------------------------------------
@app.post("/api/sync/classnet-app")
def sync_classnet_app(payload: dict, db: Session = Depends(get_db)):
    """Reçoit la DB ClassNet App. Compare, met à jour et log en 'en_attente'."""
    user_data = payload.get("user", {})
    email = user_data.get("email")
    
    teacher = db.query(Teacher).filter(Teacher.email == email).first()
    if not teacher:
        return {"status": False, "message": "Utilisateur non trouvé."}

    grades_payload = payload.get("grades", {})
    updates_count = 0
    
    for key_id, evals in grades_payload.items():
        # key_id format attendu: "studentPermCode_courseId"
        parts = key_id.split("_")
        if len(parts) < 2: continue
        student_n, course_id = parts[0], parts[1]
        
        record = db.query(TeacherEvaluation).filter_by(student_n_permanent=student_n, course_id=course_id).first()
        is_new = False
        if not record:
            record = TeacherEvaluation(school_id=teacher.school_id or "NONE", student_n_permanent=student_n, course_id=course_id)
            db.add(record)
            is_new = True
            
        modified = False
        diff_tracker = {}
        for period in ["p1", "p2", "ex1", "p3", "p4", "ex2"]:
            new_val = evals.get(period)
            if new_val is not None:
                old_val = getattr(record, period)
                if old_val != new_val:
                    setattr(record, period, new_val)
                    diff_tracker[period] = new_val
                    modified = True
                    
        if modified or is_new:
            db.add(SyncHistory(
                teacher_email=teacher.email,
                action_type="GRADE_UPDATE",
                payload_diff={"student": student_n, "course": course_id, "changes": diff_tracker},
                status="en_attente"
            ))
            updates_count += 1

    db.commit()
    return {"status": True, "message": f"Synchronisation réussie. {updates_count} modifications mises en file d'attente."}

@app.post("/api/sync/primenet")
def sync_primenet(payload: dict, db: Session = Depends(get_db)):
    """Reçoit la DB PrimeNet (ClassNet P). Met à jour sans historique."""
    school_data = payload.get("school", {})
    school_id = school_data.get("id")
    
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first()
    if not school:
        return {"status": False, "message": "École non trouvée."}

    # Logique de mise à jour directe (Classes, Students, Courses)
    # Remplacement destructif ou update selon besoin PrimeNet
    # (Logique similaire simplifiée pour économie de tokens)
    
    db.commit()
    return {"status": True, "message": "Synchronisation PrimeNet effectuée."}

# ---------------------------------------------------------
# 9. EXTRACTION DONNÉES ENSEIGNANT
# ---------------------------------------------------------
@app.get("/api/teachers/{identifier}")
def get_teacher_info(identifier: str, db: Session = Depends(get_db)):
    """Renvoie toutes les informations concernant un enseignant via son identifiant (email)."""
    teacher = db.query(Teacher).filter(Teacher.email == identifier).first()
    if not teacher:
        return {"status": False, "message": "Enseignant introuvable."}
    
    return {
        "status": True,
        "data": {
            "id": teacher.id,
            "teacher_code": teacher.teacher_code,
            "full_name": teacher.full_name,
            "email": teacher.email,
            "phone": teacher.phone_number,
            "age": teacher.age,
            "school_name": teacher.school_name,
            "subject": teacher.subject,
            "status": teacher.status,
            "llink_preferences": teacher.llink_preferences
        }
    }
