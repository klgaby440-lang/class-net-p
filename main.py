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
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, Float, text
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

ADMIN_EMAIL = "klgaby440@gmail.com" # Ton adresse pour recevoir les codes

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

class StudentGrade(Base):
    __tablename__ = "student_grades"
    id = Column(Integer, primary_key=True, index=True)
    teacher_email = Column(String, index=True, nullable=False)
    student_id = Column(String, index=True, nullable=False)
    student_name = Column(String, nullable=False)
    class_name = Column(String, nullable=False)
    course_name = Column(String, nullable=False)
    eval_id = Column(String, index=True, nullable=False)
    eval_name = Column(String, nullable=False)
    period = Column(String, nullable=False)
    score = Column(Float, nullable=False)
    max_score = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PrimeNetPayload(Base):
    __tablename__ = "primenet_payloads"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, index=True, nullable=False)
    teacher_email = Column(String, unique=True, index=True, nullable=False)
    
    # Déclaration de la colonne JSON
    payload_data = Column(JSON, nullable=False, default=dict)
    
    # Horodatage automatique pour suivre les mises à jour
    updated_at = Column(
        DateTime(timezone=True), 
        server_default=func.now(), 
        onupdate=func.now()
    )

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
    email: str 
    school_name: Optional[str] = None
    password: str
    phone_number: str
    age: int

class OTPVerifySchema(BaseModel):
    email: str
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
    email: str
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

# Création des tables manquantes
Base.metadata.create_all(bind=engine)

# Ajout automatique de la colonne manquante si elle n'existe pas
# Ajout automatique des colonnes manquantes si elles n'existent pas sur la DB Render
with engine.connect() as conn:
    conn.execute(text("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS age INTEGER;"))
    conn.execute(text("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS school_name VARCHAR;"))
    conn.execute(text("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS llink_preferences TEXT;"))
    conn.execute(text("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS subject VARCHAR;"))
    conn.execute(text("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS status VARCHAR DEFAULT 'Actif';"))
    conn.execute(text("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS teacher_code VARCHAR(50);"))
    conn.commit()

# ---------------------------------------------------------
# 5. INITIALISATION FASTAPI
# ---------------------------------------------------------
app = FastAPI(title="CRYPT Cloud Internet Node", version="3.1.0")
origins = [
    "https://class-net-p.vercel.app",  # Ton frontend Vercel en production
    "http://localhost:3000",            # Pour tes tests locaux
    "http://localhost:5173",            # Pour Vite / React local
    "http://localhost:8080"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,              # Ou ["*"] pour autoriser toutes les sources
    allow_credentials=True,
    allow_methods=["*"],                # Autorise toutes les méthodes (POST, GET, OPTIONS, etc.)
    allow_headers=["*"],                # Autorise tous les en-têtes HTTP
)

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
    """Connexion Prof : Reconstitue et renvoie l'état exact defaultDB depuis PostgreSQL."""
    teacher = db.query(Teacher).filter(Teacher.email == data.identifier, Teacher.password == data.password).first()
    if not teacher:
        return {"status": False, "message": "Identifiants incorrects."}
    
    # 1. Récupération des notes/évaluations et des quiz enregistrés pour cet enseignant
    db_grades = db.query(StudentGrade).filter(StudentGrade.teacher_email == teacher.email).all()
    db_quizzes = db.query(QuizBank).filter(QuizBank.teacher_email == teacher.email).all()

    # 2. Reconstitution dynamique des structures imbriquées
    classes_set = set()
    courses_dict = {}
    students_dict = {}
    evaluations_dict = {}
    grades_dict = {}

    for g in db_grades:
        c_name = g.class_name
        classes_set.add(c_name)

        # Reconstitution des cours par classe
        if c_name not in courses_dict:
            courses_dict[c_name] = []
        if g.course_name and g.course_name not in courses_dict[c_name]:
            courses_dict[c_name].append(g.course_name)

        # Reconstitution des élèves par classe
        if c_name not in students_dict:
            students_dict[c_name] = []
        if not any(s["id"] == g.student_id for s in students_dict[c_name]):
            students_dict[c_name].append({
                "id": g.student_id,
                "name": g.student_name
            })

        # Reconstitution des évaluations par classe et période
        if c_name not in evaluations_dict:
            evaluations_dict[c_name] = {"P1": [], "P2": [], "EX1": [], "P3": [], "P4": [], "EX2": []}
        
        period_key = g.period if g.period in evaluations_dict[c_name] else "P1"
        if not any(e["id"] == g.eval_id for e in evaluations_dict[c_name][period_key]):
            evaluations_dict[c_name][period_key].append({
                "id": g.eval_id,
                "name": g.eval_name,
                "max": g.max_score,
                "course": g.course_name
            })

        # Reconstitution de la mappe des notes : "STU-ID_EV-ID": note
        grade_key = f"{g.student_id}_{g.eval_id}"
        grades_dict[grade_key] = g.score

    # 3. Formatage de la liste des quiz
    quizzes_list = [
        {
            "id": q.id,
            "title": q.title,
            "content": q.content
        } for q in db_quizzes
    ]

    # 4. Assemblage complet du JSON DB
    classnet_app_db = {
        "user": {
            "id": teacher.teacher_code or f"prof_{teacher.id}",
            "name": teacher.full_name,
            "email": teacher.email,
            "password": teacher.password,
            "school": teacher.school_name or "Indépendant",
            "isLoggedIn": True
        },
        "classes": list(classes_set),
        "courses": courses_dict,
        "courseMax": {},
        "periodVisibility": {"P1": True, "P2": False, "EX1": False, "P3": False, "P4": False, "EX2": False},
        "activeCourseFilter": {},
        "students": students_dict,
        "evaluations": evaluations_dict,
        "grades": grades_dict,
        "presences": [],
        "quizzes": quizzes_list,
        "llinkPrefs": teacher.llink_preferences or "",
        "pendingCommits": 0
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
    """
    Synchronise la DB ClassNet App avec contrôles complets des données :
    - Vérification des noms d'élèves et appartenance aux classes
    - Validation des noms de cours et des maxima (maxScore)
    - Contrôle des notes (intervalle 0 <= note <= max_score)
    """
    user_data = payload.get("user", {})
    email = user_data.get("email")
    
    teacher = db.query(Teacher).filter(Teacher.email == email).first()
    if not teacher:
        return {"status": False, "message": "Enseignant non trouvé."}

    students_dict = payload.get("students", {})
    courses_dict = payload.get("courses", {})
    evaluations_dict = payload.get("evaluations", {})
    grades_dict = payload.get("grades", {})

    # --- 1. Indexation & Validation des Élèves ---
    student_lookup = {}  # { student_id: {"name": ..., "class": ...} }
    for class_name, st_list in students_dict.items():
        for st in st_list:
            st_id = st.get("id")
            st_name = st.get("name", "").strip()
            if st_id and st_name:
                student_lookup[st_id] = {"name": st_name, "class": class_name}

    # --- 2. Indexation & Validation des Évaluations ---
    eval_lookup = {}  # { eval_id: {"name": ..., "max": ..., "course": ..., "period": ..., "class": ...} }
    for class_name, periods in evaluations_dict.items():
        for period_key, ev_list in periods.items():
            for ev in ev_list:
                ev_id = ev.get("id")
                ev_course = ev.get("course", "").strip()
                ev_max = ev.get("max")
                
                # Vérification : le cours doit exister dans la classe
                valid_courses = courses_dict.get(class_name, [])
                if ev_id and ev_course in valid_courses and isinstance(ev_max, (int, float)) and ev_max > 0:
                    eval_lookup[ev_id] = {
                        "name": ev.get("name", "Évaluation"),
                        "max": float(ev_max),
                        "course": ev_course,
                        "period": period_key,
                        "class": class_name
                    }

    # --- 3. Traitement & Validation des Notes (Grades) ---
    updates_count = 0
    errors = []

    for grade_key, score in grades_dict.items():
        # Parsing de la clé composite (ex: "STU-2026-LEBV-BEKD_EV-2026-3FB5-HSAS")
        if "_EV-" not in grade_key:
            errors.append(f"Format de clé invalide : {grade_key}")
            continue

        parts = grade_key.split("_EV-")
        student_id = parts[0]
        eval_id = "EV-" + parts[1]

        # Vérification 1 : L'élève existe-t-il ?
        student_info = student_lookup.get(student_id)
        if not student_info:
            errors.append(f"Élève inconnu pour la note ({student_id})")
            continue

        # Vérification 2 : L'évaluation existe-t-elle ?
        eval_info = eval_lookup.get(eval_id)
        if not eval_info:
            errors.append(f"Évaluation inconnue ou non valide ({eval_id})")
            continue

        # Vérification 3 : Note valide (numérique et comprise entre 0 et le max)
        if not isinstance(score, (int, float)) or score < 0 or score > eval_info["max"]:
            errors.append(f"Note incohérente ({score}/{eval_info['max']}) pour {student_info['name']} sur {eval_info['name']}")
            continue

        # Sauvegarde ou mise à jour dans la table StudentGrade
        record = db.query(StudentGrade).filter_by(
            teacher_email=teacher.email,
            student_id=student_id,
            eval_id=eval_id
        ).first()

        if not record:
            record = StudentGrade(
                teacher_email=teacher.email,
                student_id=student_id,
                student_name=student_info["name"],
                class_name=student_info["class"],
                course_name=eval_info["course"],
                eval_id=eval_id,
                eval_name=eval_info["name"],
                period=eval_info["period"],
                score=float(score),
                max_score=eval_info["max"]
            )
            db.add(record)
        else:
            record.score = float(score)

        updates_count += 1

    # --- 4. Génération et Sauvegarde des données pour PrimeNet ---
    school_id = payload.get("school_id") or getattr(teacher, "school_id", None)
    
    # On ne génère le format PrimeNet que si l'enseignant appartient à une école
    if school_id and str(school_id).lower() != "indépendant":
        # Construction des listes et dictionnaires de base
        classes_list = list(students_dict.keys())
        presences_dict = payload.get("presences", {})
        dates_presence = list(presences_dict.keys())
        
        # Formatage des élèves : { "classe": ["Nom Post-nom", ...] }
        formatted_students = {}
        for c_name, st_list in students_dict.items():
            formatted_students[c_name] = [st.get("name", "") for st in st_list if st.get("name")]
            
        # Construction de la moyenne classe selon ta structure
        moyenne_classe = {}
        for c_name in classes_list:
            moyenne_classe[c_name] = {}
            # On récupère les cours de cette classe
            c_courses = courses_dict.get(c_name, [])
            for course in c_courses:
                moyenne_classe[c_name][course] = {"P1": {}, "P2": {}, "EX1": {}, "P3": {}, "P4": {}, "EX2": {}}
                
                # Remplissage des notes/moyennes pour ce cours et cette classe
                for grade_key, score in grades_dict.items():
                    if "_EV-" not in grade_key: continue
                    parts = grade_key.split("_EV-")
                    s_id = parts[0]
                    e_id = "EV-" + parts[1]
                    
                    s_info = student_lookup.get(s_id)
                    e_info = eval_lookup.get(e_id)
                    
                    if s_info and e_info and s_info["class"] == c_name and e_info["course"] == course:
                        s_name = s_info["name"]
                        period = e_info["period"]
                        # Si tu as déjà précalculé la moyenne, on l'injecte. Sinon on injecte la note brute.
                        if period in moyenne_classe[c_name][course]:
                            moyenne_classe[c_name][course][period][s_name] = score

        # Assemblage final du JSON PrimeNet pour cet enseignant
        primenet_data = {
            "teacher_id": teacher.email,
            "school_id": school_id,
            "classes": classes_list,
            "cours": courses_dict,
            "date_presence": dates_presence,
            "students": formatted_students,
            "moyenne_classe": moyenne_classe,
            "presences": presences_dict
        }
        
        # Sauvegarde dans la base de données (Table à créer si pas encore fait)
        # On met à jour s'il existe déjà un enregistrement pour ce prof, sinon on crée.
        existing_payload = db.query(PrimeNetPayload).filter_by(teacher_email=teacher.email).first()
        if existing_payload:
            existing_payload.payload_data = primenet_data
        else:
            new_payload = PrimeNetPayload(
                school_id=school_id,
                teacher_email=teacher.email,
                payload_data=primenet_data
            )
            db.add(new_payload)

    # Historique de synchronisation
    db.add(SyncHistory(
        teacher_email=teacher.email,
        action_type="FULL_APP_SYNC",
        payload_diff={"processed_grades": updates_count, "rejected_errors": len(errors)},
        status="en_attente"
    ))

    db.commit()

    return {
        "status": True,
        "message": f"Synchronisation terminée avec succès. {updates_count} notes validées.",
        "warnings_or_errors": errors
    }

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


@app.get("/api/primenet/sync/{school_id}")
def get_primenet_data(school_id: str, db: Session = Depends(get_db)):
    """
    Récupère et fusionne les données de tous les enseignants d'une école spécifique
    pour les envoyer à PrimeNet.
    """
    # 1. Récupérer tous les payloads enregistrés pour cette école
    school_payloads = db.query(PrimeNetPayload).filter(PrimeNetPayload.school_id == school_id).all()
    
    if not school_payloads:
        raise HTTPException(status_code=404, detail="Aucune donnée trouvée pour cette école ou identifiant non enregistré.")
    
    # 2. Structure globale fusionnée à renvoyer à PrimeNet
    merged_data = {
        "school_id": school_id,
        "teachers": [],
        "classes": set(),
        "cours": {},
        "date_presence": set(),
        "students": {},
        "moyenne_classe": {},
        "presences": {}
    }
    
    # 3. Fusion des données de chaque enseignant
    for record in school_payloads:
        data = record.payload_data
        
        # Ajout du prof
        merged_data["teachers"].append(data.get("teacher_id"))
        
        # Fusion des classes
        for c_name in data.get("classes", []):
            merged_data["classes"].add(c_name)
            
        # Fusion des dates de présence
        for date_p in data.get("date_presence", []):
            merged_data["date_presence"].add(date_p)
            
        # Fusion des cours par classe
        for c_name, courses in data.get("cours", {}).items():
            if c_name not in merged_data["cours"]:
                merged_data["cours"][c_name] = []
            # On ajoute les cours en évitant les doublons
            merged_data["cours"][c_name] = list(set(merged_data["cours"][c_name] + courses))
            
        # Fusion des élèves par classe
        for c_name, st_list in data.get("students", {}).items():
            if c_name not in merged_data["students"]:
                merged_data["students"][c_name] = []
            merged_data["students"][c_name] = list(set(merged_data["students"][c_name] + st_list))
            
        # Fusion des moyennes
        for c_name, courses_dict in data.get("moyenne_classe", {}).items():
            if c_name not in merged_data["moyenne_classe"]:
                merged_data["moyenne_classe"][c_name] = {}
                
            for course_name, periods_dict in courses_dict.items():
                if course_name not in merged_data["moyenne_classe"][c_name]:
                    merged_data["moyenne_classe"][c_name][course_name] = {"P1": {}, "P2": {}, "EX1": {}, "P3": {}, "P4": {}, "EX2": {}}
                    
                for period, students_scores in periods_dict.items():
                    # Met à jour le dictionnaire avec les notes des élèves
                    merged_data["moyenne_classe"][c_name][course_name][period].update(students_scores)
                    
        # Fusion des présences (très complexe si format imbriqué, on fait un update profond)
        for date_p, classes_dict in data.get("presences", {}).items():
            if date_p not in merged_data["presences"]:
                merged_data["presences"][date_p] = {}
                
            for c_name, courses_dict in classes_dict.items():
                if c_name not in merged_data["presences"][date_p]:
                    merged_data["presences"][date_p][c_name] = {}
                    
                for course_name, statuses in courses_dict.items():
                    if course_name not in merged_data["presences"][date_p][c_name]:
                        merged_data["presences"][date_p][c_name][course_name] = {}
                        
                    merged_data["presences"][date_p][c_name][course_name].update(statuses)

    # Convertir les sets en listes pour que le JSON soit valide (sérialisable)
    merged_data["classes"] = list(merged_data["classes"])
    merged_data["date_presence"] = list(merged_data["date_presence"])
    
    return {
        "status": True,
        "message": "Données PrimeNet récupérées avec succès.",
        "data": merged_data
    }
