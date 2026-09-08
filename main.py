import os
import secrets
import string
from datetime import datetime
from typing import Optional, Dict, Any, List
import httpx
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, Float, Date, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship

# ---------------------------------------------------------
# 1. CONFIGURATION ET BASE DE DONNÉES POSTGRESQL
# ---------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://classnet_user:password@localhost:5432/classnet")

# Correction de compatibilité pour Render (postgres:// -> postgresql://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Configuration WhatsApp
WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "")
WHATSAPP_API_KEY = os.getenv("WHATSAPP_API_KEY", "")

# ---------------------------------------------------------
# 2. MODÈLES DE BASE DE DONNÉES ENRICHIS (SQLAlchemy)
# ---------------------------------------------------------

class PresenceSchema(BaseModel):
    id: str
    student_id: str
    student_name: str
    class_name: str
    course_name: str
    date: str
    status: str

class QuizSchema(BaseModel):
    id: str
    title: str
    class_name: str
    course_name: str
    max_score: float
    content: str
    created_at: str

class CloudSyncPayload(BaseModel):
    email: str
    password: str
    school_id: str
    llink_preferences: Optional[str] = None
    presences: List[PresenceSchema] = []
    quizzes: List[QuizSchema] = []
    full_database_json: dict # Pour garder une trace brute si besoin

class SchoolInformation(Base):
    __tablename__ = "school_information"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, unique=True, index=True, nullable=False) # Email ou ID unique
    bulletin_seq_id = Column(String, unique=True, nullable=False) # Suite de nombres pour le bulletin (ex: 63017630119000656)
    code = Column(String, nullable=False) # Code court (ex: 630119)
    name_school = Column(String, nullable=False)
    city = Column(String, nullable=False) # ex: BUKAVU
    commune = Column(String, nullable=False) # ex: IBANDA
    name_responsable = Column(String, nullable=False)
    num_tel = Column(String, nullable=False)
    adresse_physique = Column(String, nullable=False)
    pass_word = Column(String, nullable=False)
    licence_date = Column(String, default="2026-12-31") # Date de validité de la licence
    is_locked = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    classes = relationship("ClasseInformation", back_populates="school", cascade="all, delete-orphan")
    students = relationship("SchoolStudentInformation", back_populates="school", cascade="all, delete-orphan")
    courses = relationship("CourseInformation", back_populates="school", cascade="all, delete-orphan")
    teachers = relationship("Teacher", back_populates="school")

class ClasseInformation(Base):
    __tablename__ = "classe_informations"

    id = Column(String, primary_key=True, index=True) # Ex: 'c1'
    school_id = Column(String, ForeignKey("school_information.school_id"), nullable=False)
    class_name = Column(String, nullable=False) # Ex: '3ème SCIENTIFIQUE'
    titulaire_name = Column(String, nullable=True)
    domaines = Column(JSON, nullable=True) # Liste JSONB ex: ['Domaine des Sciences', 'Domaine des Langues']

    school = relationship("SchoolInformation", back_populates="classes")
    students = relationship("SchoolStudentInformation", back_populates="classe")
    courses = relationship("CourseInformation", back_populates="classe")

class CourseInformation(Base):
    __tablename__ = "course_informations"

    id = Column(String, primary_key=True, index=True) # Ex: 'k1'
    school_id = Column(String, ForeignKey("school_information.school_id"), nullable=False)
    class_id = Column(String, ForeignKey("classe_informations.id"), nullable=False)
    course_name = Column(String, nullable=False) # Ex: 'Mathématiques'
    max_per = Column(Float, nullable=False, default=40.0) # Note maximale par période
    category = Column(String, nullable=False) # Ex: 'Domaine des Sciences'
    titulaire_name = Column(String, nullable=True)

    school = relationship("SchoolInformation", back_populates="courses")
    classe = relationship("ClasseInformation", back_populates="courses")

class SchoolStudentInformation(Base):
    __tablename__ = "school_student_informations"

    id = Column(String, primary_key=True, index=True) # Ex: 's1'
    school_id = Column(String, ForeignKey("school_information.school_id"), nullable=False)
    class_id = Column(String, ForeignKey("classe_informations.id"), nullable=False)
    student_name = Column(String, nullable=False)
    student_post_name = Column(String, nullable=False)
    student_pre_name = Column(String, nullable=False)
    student_sexe = Column(String(1), nullable=False)
    student_born_date = Column(String, nullable=True)
    student_born_place = Column(String, nullable=True)
    student_n_permanent = Column(String, unique=True, index=True, nullable=False)

    school = relationship("SchoolInformation", back_populates="students")
    classe = relationship("ClasseInformation", back_populates="students")
    evaluations = relationship("TeacherEvaluation", back_populates="student")

class Teacher(Base):
    __tablename__ = "teachers"

    id = Column(Integer, primary_key=True, index=True)
    teacher_code = Column(String(50), unique=True, index=True, nullable=True) # Gardé pour la rétrocompatibilité locale
    email = Column(String, unique=True, index=True, nullable=False) # 🟢 NOUVEAU : Identifiant principal
    full_name = Column(String, nullable=False)
    phone_number = Column(String, nullable=True)
    subject = Column(String, nullable=True) 
    status = Column(String, default="Actif")
    password = Column(String, default="123456")
    school_id = Column(String, ForeignKey("school_information.school_id"), nullable=True)
    llink_preferences = Column(Text, nullable=True) # 🟢 NOUVEAU : Préférences de l'IA

    school = relationship("SchoolInformation", back_populates="teachers")
    attendances = relationship("Attendance", back_populates="teacher", cascade="all, delete-orphan")
    quizzes = relationship("QuizBank", back_populates="teacher", cascade="all, delete-orphan")

class Attendance(Base):
    __tablename__ = "attendances"

    id = Column(String, primary_key=True, index=True)
    teacher_email = Column(String, ForeignKey("teachers.email"), nullable=False)
    student_id = Column(String, nullable=False)
    student_name = Column(String, nullable=False)
    class_name = Column(String, nullable=False)
    course_name = Column(String, nullable=False)
    date = Column(String, nullable=False)
    status = Column(String(1), nullable=False) # P, A, R

    teacher = relationship("Teacher", back_populates="attendances")

class QuizBank(Base):
    __tablename__ = "quizzes"

    id = Column(String, primary_key=True, index=True)
    teacher_email = Column(String, ForeignKey("teachers.email"), nullable=False)
    title = Column(String, nullable=False)
    class_name = Column(String, nullable=False)
    course_name = Column(String, nullable=False)
    max_score = Column(Float, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(String, nullable=False)

    teacher = relationship("Teacher", back_populates="quizzes")
    
class TeacherEvaluation(Base):
    __tablename__ = "teacher_s_evaluations"

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"), nullable=False)
    teacher_id = Column(String, nullable=False)
    student_n_permanent = Column(String, ForeignKey("school_student_informations.student_n_permanent"), nullable=False)
    course_id = Column(String, ForeignKey("course_informations.id"), nullable=False)
    
    # Notes des 6 épreuves officielles RDC
    p1 = Column(Float, nullable=True)
    p2 = Column(Float, nullable=True)
    ex1 = Column(Float, nullable=True)
    p3 = Column(Float, nullable=True)
    p4 = Column(Float, nullable=True)
    ex2 = Column(Float, nullable=True)

    student = relationship("SchoolStudentInformation", back_populates="evaluations")

class AccessCode(Base):
    __tablename__ = "access_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, index=True, nullable=False)
    is_used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    action = Column(String, nullable=False)
    details = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

# Création/Mise à jour automatique des tables PostgreSQL
Base.metadata.create_all(bind=engine)

# ---------------------------------------------------------
# 3. SCHÉMAS PYDANTIC (Validation d'entrée)
# ---------------------------------------------------------

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
    pass_word: str
    licence_date: Optional[str] = "2026-12-31"

class AccessCodeRedeem(BaseModel):
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

async def send_whatsapp_notification(message: str):
    if not WHATSAPP_PHONE or not WHATSAPP_API_KEY:
        print(f"[WhatsApp Alert Simulation]: {message}")
        return

    url = f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_PHONE}&text={message}&apikey={WHATSAPP_API_KEY}"
    async with httpx.AsyncClient() as client:
        try:
            await client.get(url, timeout=10.0)
        except Exception as e:
            print(f"Erreur WhatsApp: {e}")

def generate_20_char_code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(20))

# ---------------------------------------------------------
# 5. INITIALISATION FASTAPI
# ---------------------------------------------------------

app = FastAPI(
    title="CRYPT Cloud & ClassNet Ecosystem API",
    version="3.0.0",
    description="Backend central unifié : Gestion des écoles, licences, synchronisation PrimeNet et ClassNet App."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# 6. ENDPOINTS D'INSCRIPTION & SÉCURITÉ
# ---------------------------------------------------------

@app.post("/api/schools/register")
def register_school(data: SchoolRegisterSchema, db: Session = Depends(get_db)):
    """Enregistre une nouvelle école avec toutes ses données géographiques et sa licence."""
    existing = db.query(SchoolInformation).filter(SchoolInformation.school_id == data.school_id).first()
    if existing:
        raise HTTPException(status_code=400, detail="Une école avec cet identifiant/email existe déjà.")

    new_school = SchoolInformation(**data.dict())
    db.add(new_school)
    db.commit()
    db.refresh(new_school)
    return {"status": "success", "message": "École créée avec succès", "school_id": new_school.school_id}

@app.post("/api/access-codes/generate")
def generate_access_code(count: int = 1, db: Session = Depends(get_db)):
    """Génère un ou plusieurs codes d'accès sécurisés à 20 caractères."""
    generated_codes = []
    for _ in range(count):
        code_str = generate_20_char_code()
        while db.query(AccessCode).filter(AccessCode.code == code_str).first():
            code_str = generate_20_char_code()
            
        new_code = AccessCode(code=code_str)
        db.add(new_code)
        generated_codes.append(code_str)
    
    db.commit()
    return {"generated_codes": generated_codes, "total": len(generated_codes)}

@app.post("/api/access-codes/use")
def use_access_code(payload: AccessCodeRedeem, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Vérifie, consomme et SUPPRIME le code à 20 caractères, puis notifie Gabriel sur WhatsApp."""
    access_code = db.query(AccessCode).filter(AccessCode.code == payload.code).first()
    
    if not access_code:
        raise HTTPException(status_code=404, detail="Code d'accès invalide ou expiré.")

    code_value = access_code.code
    db.delete(access_code)
    
    log = AuditLog(action="CODE_CONSUMED", details=f"Le code {code_value} a été utilisé et supprimé.")
    db.add(log)
    db.commit()

    message_text = f"🚨 *CRYPT ALERT* 🚨%0ALe code d'accès {code_value} vient d'être utilisé et supprimé !%0AHeure: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    background_tasks.add_task(send_whatsapp_notification, message_text)

    return {"status": "success", "message": f"Code {code_value} consommé et supprimé avec succès."}

# ---------------------------------------------------------
# 7. EXPORTATEURS JSON : PRIMENET & CLASSNET APP
# ---------------------------------------------------------

@app.get("/api/export/primenet/{school_id}")
def export_primenet_json(school_id: str, db: Session = Depends(get_db)):
    """
    Parcourt la base de données, extrait toutes les tables associées à une école
    et génère le JSON exact requis par le LocalStorage / State de PrimeNet.
    """
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="École non trouvée dans le Cloud.")

    # 1. Extraction des Classes
    classes_data = []
    for c in school.classes:
        classes_data.append({
            "id": c.id,
            "name": c.class_name,
            "titulaire": c.titulaire_name or "Non assigné",
            "categories": c.domaines or []
        })

    # 2. Extraction des Élèves
    students_data = []
    for s in school.students:
        students_data.append({
            "id": s.id,
            "classId": s.class_id,
            "name": s.student_name,
            "postname": s.student_post_name,
            "prename": s.student_pre_name,
            "sexe": s.student_sexe,
            "bornDate": s.student_born_date or "",
            "bornWhere": s.student_born_place or "",
            "permi": s.student_n_permanent
        })

    # 3. Extraction des Cours
    courses_data = []
    for cr in school.courses:
        courses_data.append({
            "id": cr.id,
            "classId": cr.class_id,
            "name": cr.course_name,
            "maxPer": cr.max_per,
            "category": cr.category,
            "titulaire": cr.titulaire_name or "Non assigné"
        })

    # 4. Extraction des Enseignants
    teachers_data = []
    for t in school.teachers:
        teachers_data.append({
            "uniqueId": t.teacher_code,
            "name": t.full_name,
            "subject": t.subject or "Général",
            "status": t.status
        })

    # 5. Extraction et Structuration des Notes (Grades Key: studentId_courseId)
    grades_data = {}
    evaluations = db.query(TeacherEvaluation).filter(TeacherEvaluation.school_id == school_id).all()
    for ev in evaluations:
        key = f"{ev.student_n_permanent}_{ev.course_id}"
        grade_entry = {}
        if ev.p1 is not None: grade_entry["p1"] = ev.p1
        if ev.p2 is not None: grade_entry["p2"] = ev.p2
        if ev.ex1 is not None: grade_entry["ex1"] = ev.ex1
        if ev.p3 is not None: grade_entry["p3"] = ev.p3
        if ev.p4 is not None: grade_entry["p4"] = ev.p4
        if ev.ex2 is not None: grade_entry["ex2"] = ev.ex2
        
        grades_data[key] = grade_entry

    # Assemblage de l'État Global PrimeNet
    primenet_state = {
        "school": {
            "name": school.name_school,
            "id": school.bulletin_seq_id,
            "code": school.code,
            "city": school.city,
            "commune": school.commune,
            "licence_date": school.licence_date
        },
        "classes": classes_data,
        "students": students_data,
        "courses": courses_data,
        "teachers": teachers_data,
        "grades": grades_data
    }

    return primenet_state


@app.get("/api/export/classnet/{teacher_code}")
def export_classnet_json(teacher_code: str, db: Session = Depends(get_db)):
    """
    Parcourt la BD et reconstruit l'état exact (defaultDB) pour l'application mobile ClassNet App.
    """
    teacher = db.query(Teacher).filter(Teacher.teacher_code == teacher_code).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Enseignant non trouvé.")

    school = teacher.school
    school_label = f"{school.name_school} - {school.city}" if school else "Indépendant"
    school_id = school.school_id if school else None

    classes_list = []
    courses_dict = {}
    course_max_dict = {}
    students_dict = {}

    if school_id:
        # Recherche des classes et cours où l'enseignant intervient
        courses = db.query(CourseInformation).filter(
            CourseInformation.school_id == school_id,
            CourseInformation.titulaire_name == teacher.full_name
        ).all()

        for cr in courses:
            classe = db.query(ClasseInformation).filter(ClasseInformation.id == cr.class_id).first()
            if not classe:
                continue

            c_name = classe.class_name
            if c_name not in classes_list:
                classes_list.append(c_name)

            # Insertion des cours par classe
            if c_name not in courses_dict:
                courses_dict[c_name] = []
            courses_dict[c_name].append(cr.course_name)

            # Max du cours
            max_key = f"{c_name}_{cr.course_name}"
            course_max_dict[max_key] = cr.max_per

            # Insertion des élèves pour cette classe
            if c_name not in students_dict:
                students_dict[c_name] = []
                for st in classe.students:
                    students_dict[c_name].append({
                        "id": st.id,
                        "name": st.student_name,
                        "postName": st.student_post_name,
                        "preName": st.student_pre_name,
                        "gender": st.student_sexe,
                        "permCode": st.student_n_permanent
                    })

    # Assemblage de l'objet defaultDB pour ClassNet App
    classnet_db = {
        "user": {
            "id": teacher.teacher_code,
            "name": teacher.full_name,
            "password": teacher.password,
            "school": school_label,
            "isLoggedIn": True
        },
        "classes": classes_list,
        "courses": courses_dict,
        "courseMax": course_max_dict,
        "periodVisibility": {
            "P1": True, "P2": False, "EX1": False,
            "P3": False, "P4": False, "EX2": False
        },
        "activeCourseFilter": {},
        "students": students_dict,
        "evaluations": {},
        "grades": {},
        "presences": [],
        "pendingCommits": 0
    }

    return classnet_db

@app.post("/api/cloud/sync")
def sync_cloud_data(payload: CloudSyncPayload, db: Session = Depends(get_db)):
    """Endpoint pour le Push On - Basé sur l'e-mail"""
    
    # 1. Vérification de l'enseignant via l'e-mail
    teacher = db.query(Teacher).filter(Teacher.email == payload.email).first()
    
    if not teacher:
        # Création automatique si l'enseignant n'existe pas (utile pour le premier déploiement)
        teacher = Teacher(
            email=payload.email,
            full_name=payload.full_database_json.get("user", {}).get("name", "Enseignant Inconnu"),
            password=payload.password,
            school_id=payload.school_id,
            llink_preferences=payload.llink_preferences
        )
        db.add(teacher)
        db.commit()
        db.refresh(teacher)
    else:
        # Vérification du mot de passe
        if teacher.password != payload.password:
            raise HTTPException(status_code=401, detail="Mot de passe incorrect pour cet e-mail.")
        
        # Mise à jour des préférences Llink
        teacher.llink_preferences = payload.llink_preferences
        db.commit()

    # 2. Synchronisation des Présences (Upsert)
    for p_data in payload.presences:
        existing_presence = db.query(Attendance).filter(Attendance.id == p_data.id).first()
        if existing_presence:
            existing_presence.status = p_data.status
        else:
            new_presence = Attendance(
                id=p_data.id,
                teacher_email=teacher.email,
                student_id=p_data.student_id,
                student_name=p_data.student_name,
                class_name=p_data.class_name,
                course_name=p_data.course_name,
                date=p_data.date,
                status=p_data.status
            )
            db.add(new_presence)

    # 3. Synchronisation des Interrogations (Upsert)
    for q_data in payload.quizzes:
        existing_quiz = db.query(QuizBank).filter(QuizBank.id == q_data.id).first()
        if not existing_quiz:
            new_quiz = QuizBank(
                id=q_data.id,
                teacher_email=teacher.email,
                title=q_data.title,
                class_name=q_data.class_name,
                course_name=q_data.course_name,
                max_score=q_data.max_score,
                content=q_data.content,
                created_at=q_data.created_at
            )
            db.add(new_quiz)

    db.commit()
    return {"status": "success", "message": "Synchronisation Cloud réussie avec succès !"}

