import os
import secrets
import string
import uuid
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import httpx
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, status, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, Float, text, func, UniqueConstraint, CheckConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
import random
import re
from classnet_common import (
    PERIODS, CONDUCT_KINDS, CONDUCT_MAX_LEN, SIGNER_ROLES, ACCESS_AUDIENCES, MATCH_METHODS, sql_in,
    validate_conduct_value, normalize_name, name_tokens_key, resolve_grade_access,
)

import os
import httpx

RESEND_API_KEY = os.getenv("RESEND_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")

async def send_email_via_resend(to_email: str, subject: str, html_content: str) -> bool:
    """Envoie un e-mail via l'API HTTP Resend"""
    if not RESEND_API_KEY:
        print("⚠️ [RESEND] RESEND_API_KEY non configurée.")
        return False
        
    payload = {
        "from": "ClassNet <onboarding@resend.dev>",
        "to": [to_email],
        "subject": subject,
        "html": html_content
    }
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post("https://api.resend.com/emails", json=payload, headers=headers)
            if response.status_code in [200, 201]:
                print(f"✅ [RESEND] Email envoyé avec succès à {to_email}")
                return True
            else:
                print(f"❌ [RESEND ERREUR] Code {response.status_code} : {response.text}")
                return False
    except Exception as e:
        print(f"❌ [EMAIL ERREUR] Exception lors de l'appel HTTP Resend : {str(e)}")
        return False

class ScheduleEngine:
    """
    Moteur de génération d'horaires scolaires robuste :
    - Gère les cours individuels et les cours de tronc commun (multi-classes simultanées).
    - Respecte les disponibilités et préférences de chaque enseignant.
    - Évite en priorité la dernière heure de chaque journée (heures de fatigue).
    - Garantit l'absence totale de conflits (double-booking prof ou classe).
    """
    def __init__(self, config: Dict[str, Any], teachers: List[Dict[str, Any]]):
        self.config = config
        self.jours_travail = config.get("jours_travail", 5)
        self.heures_par_jour = config.get("heures_par_jour", 6)
        self.heures_utilisables_max = max(1, self.heures_par_jour - 1)
        
        all_days = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi"]
        self.days_list = all_days[:self.jours_travail]
        self.teachers = teachers

        # Grilles de suivi pour éviter les conflits
        self.teacher_grid = {t["teacher_id"]: {d: {} for d in self.days_list} for t in teachers}
        self.class_grid = {} # { class_name: { day: { hour_str: { course, teacher, room } } } }

    def _prepare_and_group_tasks(self):
        """
        Analyse les cours des enseignants, isole les cours individuels 
        et regroupe les cours de tronc commun par (trunk_id, course_name, teacher_id).
        """
        trunk_groups = {} 
        individual_tasks = []

        for teacher in self.teachers:
            t_id = teacher["teacher_id"]
            t_name = teacher["name"]
            availability = teacher["availability"]

            for course in teacher.get("assigned_courses", []):
                c_name = course["course_name"]
                class_name = course["class_name"]
                hours = course["hours_per_week"]
                trunk = course.get("trunk_common")

                if class_name not in self.class_grid:
                    self.class_grid[class_name] = {d: {} for d in self.days_list}

                if trunk:
                    group_key = (trunk, c_name, t_id)
                    if group_key not in trunk_groups:
                        trunk_groups[group_key] = {
                            "trunk_id": trunk,
                            "course_name": c_name,
                            "teacher_id": t_id,
                            "teacher_name": t_name,
                            "hours_per_week": hours,
                            "classes": set(),
                            "availability": availability
                        }
                    trunk_groups[group_key]["classes"].add(class_name)
                else:
                    for _ in range(hours):
                        individual_tasks.append({
                            "type": "individual",
                            "teacher_id": t_id,
                            "teacher_name": t_name,
                            "course_name": c_name,
                            "class_name": class_name,
                            "availability": availability
                        })

        # Aplatissement des troncs communs en tâches unitaires de 1 heure
        trunk_tasks = []
        for group_key, data in trunk_groups.items():
            classes_list = list(data["classes"])
            for _ in range(data["hours_per_week"]):
                trunk_tasks.append({
                    "type": "trunk",
                    "trunk_id": data["trunk_id"],
                    "teacher_id": data["teacher_id"],
                    "teacher_name": data["teacher_name"],
                    "course_name": data["course_name"],
                    "classes": classes_list,
                    "availability": data["availability"]
                })

        # Tri MRV (Priorité absolue aux troncs communs impliquant le plus de classes)
        sorted_trunk = sorted(trunk_tasks, key=lambda x: len(x["classes"]), reverse=True)
        random.shuffle(individual_tasks)

        return sorted_trunk + individual_tasks

    def generate(self) -> Dict[str, Any]:
        tasks = self._prepare_and_group_tasks()

        # Création des créneaux priorisés (Heures 1 à H-1 en premier, dernière heure H en secours)
        primary_slots = []
        fallback_slots = []

        for d in self.days_list:
            for h in range(1, self.heures_par_jour + 1):
                h_str = str(h)
                if h <= self.heures_utilisables_max:
                    primary_slots.append((d, h_str))
                else:
                    fallback_slots.append((d, h_str))

        ordered_slots = primary_slots + fallback_slots
        unassigned_tasks = []

        for task in tasks:
            placed = False
            t_id = task["teacher_id"]
            availability = task["availability"]

            for day, h_str in ordered_slots:
                # 1. Disponibilité de l'enseignant ce jour/heure
                if not availability.get(day, {}).get(h_str, True):
                    continue
                # 2. Prof libre ?
                if h_str in self.teacher_grid[t_id][day]:
                    continue

                if task["type"] == "individual":
                    c_name = task["class_name"]
                    # 3. Classe libre ?
                    if h_str in self.class_grid[c_name][day]:
                        continue

                    # Placement cours individuel
                    room = "Salle N°1"
                    self.teacher_grid[t_id][day][h_str] = {
                        "class": c_name,
                        "course": task["course_name"]
                    }
                    self.class_grid[c_name][day][h_str] = {
                        "course": task["course_name"],
                        "teacher": task["teacher_name"],
                        "room": room
                    }
                    placed = True
                    break

                elif task["type"] == "trunk":
                    target_classes = task["classes"]
                    # 3. Est-ce que TOUTES les classes du tronc commun sont libres simultanément ?
                    all_classes_free = all(h_str not in self.class_grid[c][day] for c in target_classes)
                    if not all_classes_free:
                        continue

                    # Placement simultané pour le tronc commun
                    classes_label = " + ".join(target_classes)
                    self.teacher_grid[t_id][day][h_str] = {
                        "class": f"TRONC COMMUN ({classes_label})",
                        "course": task["course_name"]
                    }
                    for c_name in target_classes:
                        self.class_grid[c_name][day][h_str] = {
                            "course": task["course_name"],
                            "teacher": task["teacher_name"],
                            "room": "Auditoire / Salle Principale"
                        }
                    placed = True
                    break

            if not placed:
                unassigned_tasks.append(task)

        # Formatage des sorties conforme au contrat
        formatted_classes = [
            {"class_name": c_name, "grid": c_grid}
            for c_name, c_grid in self.class_grid.items()
        ]
        formatted_teachers = [
            {"teacher_id": t_id, "grid": t_grid}
            for t_id, t_grid in self.teacher_grid.items()
        ]

        return {
            "success": len(unassigned_tasks) == 0,
            "message": "Horaire généré et optimisé avec succès." if len(unassigned_tasks) == 0 else f"Horaire généré avec {len(unassigned_tasks)} créneaux non placés.",
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "schedules": {
                "classes": formatted_classes,
                "teachers": formatted_teachers
            },
            "unassigned_count": len(unassigned_tasks)
        }

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

ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "klgaby440@gmail.com")  # adresse qui reçoit les codes (définir ADMIN_EMAIL sur Render)
# Clé réservée à l'administrateur ClassNet. Sans elle, la création d'école, la génération de codes
# et le verrouillage d'une école sont impossibles (routes fermées).
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")

# ---------------------------------------------------------
# 2. MODÈLES DE BASE DE DONNÉES ENRICHIS
# ---------------------------------------------------------
class OTPVerification(Base):
    __tablename__ = "otp_codes"
    id = Column(Integer, primary_key=True, index=True)
    identifier = Column(String, index=True, nullable=False)  # Remplacement de 'email' par 'identifier'
    code = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)
    attempts = Column(Integer, default=0)  # essais de saisie du code (limite : OTP_MAX_ATTEMPTS)

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
    # Identité du bulletin (réglée dans PrimeInk) : conservée ici pour survivre à un changement d'appareil
    province = Column(String, nullable=True)
    head_name = Column(String, nullable=True)
    school_year = Column(String, nullable=True)
    seal = Column(Text, nullable=True)            # sceau de l'école (image)
    head_signature = Column(Text, nullable=True)  # signature du chef d'établissement (image)
    armoiries = Column(Text, nullable=True)
    pn_extra = Column(JSON, nullable=True)        # réglages PrimeInk : signatureBook, repechage, printSettings, behaviour

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
    school_access_verified = Column(Boolean, nullable=False, default=False, server_default="false")
    llink_preferences = Column(Text, nullable=True)
    signature = Column(Text, nullable=True)  # photo de la signature, prise dans ClassInk

    school = relationship("SchoolInformation", back_populates="teachers")
    quizzes = relationship("QuizBank", back_populates="teacher")

class ClasseInformation(Base):
    __tablename__ = "classe_informations"
    id = Column(String, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"))
    class_name = Column(String, nullable=False)
    titulaire_name = Column(String, nullable=True)
    domaines = Column(JSON, nullable=True)
    conduite_enabled = Column(Boolean, nullable=False, default=False, server_default="false")  # option par classe (étape 4)
    bulletin_type_id = Column(Integer, ForeignKey("bulletin_types.id"), nullable=True)  # NULL = bulletin standard
    bulletin_model = Column(String(20), nullable=True)      # classink | ige010 | repechage
    bulletin_title = Column(String, nullable=True)
    signature_source = Column(String(20), nullable=True)    # titulaire | responsable | default | none
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
    sub_category = Column(String, nullable=True)
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
    # Identifiant public, imprévisible et permanent communiqué aux parents.
    suivi_code = Column(String(40), nullable=True)
    # Les résultats restent invisibles aux parents tant que l'école ne les autorise pas.
    suivi_results_allowed = Column(Boolean, nullable=False, default=False, server_default="false")
    suivi_results_updated_at = Column(DateTime, nullable=True)
    suivi_results_updated_by = Column(String, nullable=True)
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


class CryptDeviceSession(Base):
    __tablename__ = "crypt_device_sessions"
    id = Column(String(100), primary_key=True)
    app = Column(String(30), nullable=False, index=True)
    school_id = Column(String(100), nullable=True, index=True)
    account_identifier = Column(String(180), nullable=True, index=True)
    platform = Column(String(80), nullable=True)
    app_version = Column(String(40), nullable=True)
    user_agent = Column(Text, nullable=True)
    ip_address = Column(String(80), nullable=True)
    first_seen = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_seen = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    contact_count = Column(Integer, nullable=False, default=0)
    last_path = Column(String(240), nullable=True)


class CryptTelemetryEvent(Base):
    __tablename__ = "crypt_telemetry_events"
    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(String(100), nullable=True, index=True)
    app = Column(String(30), nullable=False, index=True)
    school_id = Column(String(100), nullable=True, index=True)
    account_identifier = Column(String(180), nullable=True, index=True)
    event_type = Column(String(40), nullable=False, index=True)
    feature = Column(String(160), nullable=True)
    message = Column(Text, nullable=True)
    details = Column(JSON, nullable=True)
    http_method = Column(String(12), nullable=True)
    endpoint = Column(String(240), nullable=True)
    status_code = Column(Integer, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class CryptSupportTicket(Base):
    __tablename__ = "crypt_support_tickets"
    id = Column(String(40), primary_key=True, default=lambda: str(uuid.uuid4()))
    app = Column(String(30), nullable=False, index=True)
    device_id = Column(String(100), nullable=True, index=True)
    school_id = Column(String(100), nullable=True, index=True)
    account_identifier = Column(String(180), nullable=True)
    contact = Column(String(180), nullable=True)
    category = Column(String(60), nullable=False, default="support")
    subject = Column(String(180), nullable=False)
    message = Column(Text, nullable=False)
    status = Column(String(24), nullable=False, default="open", index=True)
    admin_note = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CryptAdminAudit(Base):
    __tablename__ = "crypt_admin_audit"
    id = Column(Integer, primary_key=True, index=True)
    action = Column(String(80), nullable=False, index=True)
    actor = Column(String(120), nullable=False, default="crypt_admin")
    target = Column(String(180), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


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

class ClassInkTeacherSnapshot(Base):
    """État complet ClassInk d'un enseignant, y compris les classes et élèves sans note."""
    __tablename__ = "classink_teacher_snapshots"

    teacher_email = Column(String, primary_key=True, index=True)
    payload_data = Column(JSON, nullable=False, default=dict)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

# ---------------------------------------------------------
# MODÈLE COMMUN (étape 3) : types de bulletin, conduite, signatures, accès aux notes, liens d'élèves.
# Les valeurs autorisées viennent de classnet_common.py : ne pas les recopier ici.
# ---------------------------------------------------------
class BulletinType(Base):
    __tablename__ = "bulletin_types"
    __table_args__ = (UniqueConstraint("school_id", "code", name="uq_bulletin_type_school_code"),)

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"), index=True, nullable=False)
    code = Column(String(30), nullable=False)
    label = Column(String, nullable=False)
    # {"periods": [...], "show_application": bool, "show_conduite": bool, "signatures": [...]}
    # à valider avec classnet_common.validate_bulletin_config avant écriture
    config = Column(JSON, nullable=False, default=dict)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class StudentConduct(Base):
    """Application / Conduite : une ligne par (élève, nature, période). Les totaux ne sont pas stockés."""
    __tablename__ = "student_conduct"
    __table_args__ = (
        UniqueConstraint("school_id", "student_id", "kind", "period", name="uq_conduct_student_kind_period"),
        CheckConstraint(sql_in("kind", CONDUCT_KINDS), name="ck_conduct_kind"),
        CheckConstraint(sql_in("period", PERIODS), name="ck_conduct_period"),
    )

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"), index=True, nullable=False)
    student_id = Column(String, ForeignKey("school_student_informations.id"), index=True, nullable=False)
    kind = Column(String(4), nullable=False)
    period = Column(String(3), nullable=False)
    value = Column(String(CONDUCT_MAX_LEN), nullable=False)
    updated_by = Column(String, nullable=True)  # e-mail de l'enseignant ou school_id
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BulletinSignature(Base):
    """
    Signature d'un bulletin. student_id vide = signature valable pour toute la classe ;
    period vide = signature valable pour toute l'année. L'unicité n'est pas imposée par la base
    (NULL distincts en PostgreSQL) : les routes doivent mettre à jour la ligne existante au lieu d'en ajouter.
    """
    __tablename__ = "bulletin_signatures"
    __table_args__ = (
        CheckConstraint(sql_in("signer_role", SIGNER_ROLES), name="ck_signature_role"),
        CheckConstraint(sql_in("period", PERIODS, allow_null=True), name="ck_signature_period"),
    )

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"), index=True, nullable=False)
    class_id = Column(String, ForeignKey("classe_informations.id"), index=True, nullable=False)
    student_id = Column(String, ForeignKey("school_student_informations.id"), index=True, nullable=True)
    signer_role = Column(String(30), nullable=False)
    period = Column(String(3), nullable=True)
    signer_name = Column(String, nullable=True)
    signature_data = Column(Text, nullable=True)  # image encodée (facultative) ; vide = simple mention du nom
    signed_by = Column(String, nullable=True)
    signed_at = Column(DateTime, default=datetime.utcnow)


class GradeAccessRule(Base):
    """
    Règle d'accès aux notes, appliquée côté serveur. class_id vide = toute l'école, period vide = toutes
    les périodes. Choix de la règle applicable : classnet_common.resolve_grade_access (étape 6).
    """
    __tablename__ = "grade_access_rules"
    __table_args__ = (
        CheckConstraint(sql_in("audience", ACCESS_AUDIENCES), name="ck_access_audience"),
        CheckConstraint(sql_in("period", PERIODS, allow_null=True), name="ck_access_period"),
    )

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"), index=True, nullable=False)
    class_id = Column(String, ForeignKey("classe_informations.id"), index=True, nullable=True)
    period = Column(String(3), nullable=True)
    audience = Column(String(10), nullable=False)
    is_allowed = Column(Boolean, nullable=False)
    reason = Column(Text, nullable=True)
    updated_by = Column(String, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StudentLink(Base):
    """Relie l'id d'élève d'un enseignant ClassInk (STU-...) à l'élève de l'école (celui de PrimeInk)."""
    __tablename__ = "student_links"
    __table_args__ = (
        UniqueConstraint("school_id", "teacher_email", "classink_student_id", name="uq_student_link"),
        CheckConstraint(sql_in("match_method", MATCH_METHODS), name="ck_link_method"),
    )

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(String, ForeignKey("school_information.school_id"), index=True, nullable=False)
    teacher_email = Column(String, index=True, nullable=False)
    classink_student_id = Column(String, nullable=False)
    student_id = Column(String, ForeignKey("school_student_informations.id"), index=True, nullable=False)
    match_method = Column(String(12), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# --- FONCTIONS DE GESTION DE LA BASE ---

def is_email(identifier: str) -> bool:
    """Détecte si la chaîne est un e-mail valide."""
    pattern = r"^[\w\.-]+@[\w\.-]+\.\w+$"
    return bool(re.match(pattern, identifier.strip()))

def format_phone_e164(phone: str, default_country_code: str = "+243") -> str:
    """
    Nettoie et formate le numéro au format E.164 international pour Twilio (+243...).
    Exemple : '0812345678' -> '+243812345678'
    """
    # Conserve uniquement les chiffres et le signe '+'
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    
    if cleaned.startswith("+"):
        return cleaned
    elif cleaned.startswith("0"):
        return f"{default_country_code}{cleaned[1:]}"
    else:
        return f"{default_country_code}{cleaned}"

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
    identifier: Optional[str] = None
    email: Optional[str] = None  # Conservé pour éviter toute rupture
    code: str
    teacher_data: Optional[TeacherInitSchema] = None

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
    school_id: Optional[str] = None    # champ réellement envoyé par PrimeInk
    code: str

class ScheduleGenerationRequest(BaseModel):
    school_id: str
    academic_year: str
    config: Dict[str, Any]
    time_slots: List[Dict[str, Any]]
    teachers: List[Dict[str, Any]]

# ---------------------------------------------------------
# 4. SERVICES AUXILIAIRES
# ---------------------------------------------------------
# BEGIN SECURITY
# ---------------------------------------------------------
# SÉCURITÉ : mots de passe hachés (PBKDF2), jetons signés (HMAC-SHA256), limite d'essais.
# Aucune dépendance externe : uniquement la bibliothèque standard Python.
# Variables d'environnement (Render) : AUTH_SECRET (obligatoire en production),
# TEACHER_TOKEN_DAYS (défaut 14), SCHOOL_TOKEN_HOURS (défaut 12).
# ---------------------------------------------------------
import base64
import hashlib
import hmac
import json
import time

PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ITERATIONS = 600_000

AUTH_SECRET = os.getenv("AUTH_SECRET")
if not AUTH_SECRET:
    AUTH_SECRET = secrets.token_urlsafe(48)
    print("⚠️ [SÉCURITÉ] AUTH_SECRET absente : un secret aléatoire est utilisé et toutes les sessions seront "
          "invalidées à chaque redémarrage. Définissez AUTH_SECRET dans les variables d'environnement Render.")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


TEACHER_TOKEN_TTL = _env_int("TEACHER_TOKEN_DAYS", 14) * 86400
SCHOOL_TOKEN_TTL = _env_int("SCHOOL_TOKEN_HOURS", 12) * 3600
LICENCE_GRACE_DAYS = _env_int("LICENCE_GRACE_DAYS", 7)  # jours de tolérance après la date de fin de licence
OTP_MAX_ATTEMPTS = 5


def hash_password(password: str, iterations: int = PASSWORD_ITERATIONS) -> str:
    """Renvoie  pbkdf2_sha256$<itérations>$<sel>$<empreinte>  (sel aléatoire unique par mot de passe)."""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{PASSWORD_SCHEME}${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def is_password_hashed(stored) -> bool:
    return isinstance(stored, str) and stored.startswith(PASSWORD_SCHEME + "$")


def verify_password(password: str, stored) -> bool:
    """Accepte aussi l'ancien format en clair (migré automatiquement à la prochaine connexion réussie)."""
    if not stored or not isinstance(password, str):
        return False
    if not is_password_hashed(stored):
        return hmac.compare_digest(password.encode("utf-8"), str(stored).encode("utf-8"))
    try:
        _, iterations, salt_b64, hash_b64 = stored.split("$")
        expected = base64.b64decode(hash_b64)
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt_b64), int(iterations))
        return hmac.compare_digest(candidate, expected)
    except Exception:
        return False


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def create_token(claims: dict, ttl_seconds: int) -> str:
    """Jeton signé  <charge>.<signature>  contenant les revendications et une date d'expiration."""
    body = dict(claims)
    body["exp"] = int(time.time()) + int(ttl_seconds)
    payload = _b64url(json.dumps(body, separators=(",", ":")).encode("utf-8"))
    signature = _b64url(hmac.new(AUTH_SECRET.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
    return f"{payload}.{signature}"


def verify_token(token: str):
    """Renvoie les revendications si le jeton est authentique et non expiré, sinon None."""
    try:
        payload, signature = token.split(".")
        expected = _b64url(hmac.new(AUTH_SECRET.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return None
        claims = json.loads(_b64url_decode(payload))
        if int(claims.get("exp", 0)) < int(time.time()):
            return None
        return claims
    except Exception:
        return None


class AttemptLimiter:
    """Compteur en mémoire : bloque une clé après N événements dans une fenêtre de temps."""

    def __init__(self, max_attempts: int = 5, window_seconds: int = 900):
        self.max_attempts = max_attempts
        self.window = window_seconds
        self._events = {}

    def _recent(self, key: str) -> list:
        now = time.time()
        recent = [t for t in self._events.get(key, []) if now - t < self.window]
        if recent:
            self._events[key] = recent
        else:
            self._events.pop(key, None)
        return recent

    def is_blocked(self, key: str) -> bool:
        return len(self._recent(key)) >= self.max_attempts

    def record(self, key: str) -> None:
        if len(self._events) > 10000:  # évite une croissance sans limite de la mémoire
            for k in list(self._events.keys()):
                self._recent(k)
        self._events.setdefault(key, []).append(time.time())

    def reset(self, key: str) -> None:
        self._events.pop(key, None)


LOGIN_LIMITER = AttemptLimiter(max_attempts=5, window_seconds=900)       # 5 échecs / 15 min
OTP_REQUEST_LIMITER = AttemptLimiter(max_attempts=5, window_seconds=3600)  # 5 demandes de code / heure
CODE_LIMITER = AttemptLimiter(max_attempts=10, window_seconds=900)          # 10 codes de licence faux / 15 min / adresse
SUIVI_LOOKUP_LIMITER = AttemptLimiter(max_attempts=10, window_seconds=900) # 10 identifiants parent erronés / 15 min / adresse
CRYPT_CONSOLE_LOGIN_LIMITER = AttemptLimiter(max_attempts=5, window_seconds=900)
CRYPT_TELEMETRY_LIMITER = AttemptLimiter(max_attempts=180, window_seconds=60)
CRYPT_TICKET_LIMITER = AttemptLimiter(max_attempts=5, window_seconds=3600)
CRYPT_LLINK_LIMITER = AttemptLimiter(max_attempts=12, window_seconds=300)



def get_bearer_claims(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentification requise : connectez-vous d'abord.")
    claims = verify_token(authorization[7:].strip())
    if not claims:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée : reconnectez-vous.")
    return claims


def require_teacher(claims: dict = Depends(get_bearer_claims)) -> dict:
    if claims.get("role") != "teacher":
        raise HTTPException(status_code=403, detail="Accès réservé aux enseignants.")
    return claims


def require_school(claims: dict = Depends(get_bearer_claims)) -> dict:
    if claims.get("role") != "school":
        raise HTTPException(status_code=403, detail="Accès réservé à l'administration de l'école.")
    return claims
# END SECURITY

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_admin(x_admin_key: Optional[str] = Header(None)) -> bool:
    """Réservé à l'administrateur ClassNet : en-tête X-Admin-Key égal à la variable ADMIN_API_KEY."""
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Administration désactivée : définissez ADMIN_API_KEY sur le serveur.")
    if not x_admin_key or not hmac.compare_digest(x_admin_key.encode("utf-8"), ADMIN_API_KEY.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Clé d'administration invalide.")
    return True

def require_crypt_admin(claims: dict = Depends(get_bearer_claims)) -> dict:
    """Accès au centre CRYPT réservé à une session issue de la clé administrative."""
    if claims.get("role") != "crypt_admin":
        raise HTTPException(status_code=403, detail="Accès réservé au centre de commande CRYPT.")
    return claims




LICENCE_MESSAGES = {
    "locked": "Ce compte est suspendu par l'administrateur ClassNet. Contactez ClassNet pour le réactiver.",
    "expired": "La licence de l'école a expiré. Saisissez un code de renouvellement ou contactez ClassNet.",
}


def licence_state(school) -> dict:
    """État d'une école : {'locked': bool, 'reason': '' | 'locked' | 'expired', 'licence_date': str}."""
    if school is None:
        return {"locked": False, "reason": "", "licence_date": ""}
    licence_date = school.licence_date or ""
    if school.is_locked:
        return {"locked": True, "reason": "locked", "licence_date": licence_date}
    try:
        end = datetime.strptime(licence_date[:10], "%Y-%m-%d").date() + timedelta(days=LICENCE_GRACE_DAYS)
        if datetime.utcnow().date() > end:
            return {"locked": True, "reason": "expired", "licence_date": licence_date}
    except ValueError:
        pass  # date illisible : on ne bloque pas par erreur
    return {"locked": False, "reason": "", "licence_date": licence_date}


def require_active_school(claims: dict = Depends(require_school), db: Session = Depends(get_db)) -> dict:
    """Comme require_school, mais refuse une école suspendue ou dont la licence a expiré."""
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == claims.get("school_id")).first()
    state = licence_state(school)
    if state["locked"]:
        raise HTTPException(status_code=403, detail=LICENCE_MESSAGES[state["reason"]])
    return claims


def teacher_school_block(db: Session, teacher) -> Optional[str]:
    """Message de refus si l'école de l'enseignant est suspendue ou expirée, sinon None."""
    if not teacher or not teacher.school_id:
        return None
    state = licence_state(db.query(SchoolInformation).filter(SchoolInformation.school_id == teacher.school_id).first())
    return LICENCE_MESSAGES[state["reason"]] if state["locked"] else None

def generate_20_char_code() -> str:
    # 62 caractères ^ 20 = ~119 bits d'entropie
    alphabet = string.ascii_letters + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(20))

def generate_otp() -> str:
    return ''.join(secrets.choice(string.digits) for _ in range(6))

def send_email_mock(to_email: str, subject: str, body: str):
    # Remplacer par configuration SMTP réelle si besoin
    print(f"📧 [EMAIL SENT to {to_email}] | Sujet: {subject} | Corps: {body}")

async def send_sms_via_twilio(to_phone: str, body: str) -> bool:
    """Envoie un SMS via l'API REST de Twilio (variables TWILIO_* lues en haut du fichier)."""
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER):
        print("⚠️ [TWILIO] Variables TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER non configurées.")
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                url,
                data={"To": to_phone, "From": TWILIO_PHONE_NUMBER, "Body": body},
                auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
            )
            if response.status_code in [200, 201]:
                print(f"✅ [SMS ENVOYÉ] Code envoyé à {to_phone}")
                return True
            print(f"❌ [TWILIO ERREUR] {response.status_code} : {response.text}")
            return False
    except Exception as e:
        print(f"❌ [SMS ERREUR] Exception lors de l'envoi : {str(e)}")
        return False

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
    conn.execute(text("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS school_access_verified BOOLEAN NOT NULL DEFAULT FALSE;"))
    conn.execute(text("ALTER TABLE school_information ADD COLUMN IF NOT EXISTS email TEXT;"))
    conn.execute(text("ALTER TABLE school_information ADD COLUMN IF NOT EXISTS province VARCHAR(255);"))
    conn.execute(text("ALTER TABLE school_information ADD COLUMN IF NOT EXISTS province VARCHAR(255);"))
    conn.execute(text("ALTER TABLE school_information ADD COLUMN IF NOT EXISTS head_name VARCHAR(255);"))
    conn.execute(text("ALTER TABLE school_information ADD COLUMN IF NOT EXISTS school_year VARCHAR(50);"))
    conn.execute(text("ALTER TABLE school_information ADD COLUMN IF NOT EXISTS seal TEXT;"))
    conn.execute(text("ALTER TABLE school_information ADD COLUMN IF NOT EXISTS head_signature TEXT;"))
    conn.execute(text("ALTER TABLE school_information ADD COLUMN IF NOT EXISTS armoiries TEXT;"))
    conn.execute(text("ALTER TABLE school_information ADD COLUMN IF NOT EXISTS pn_extra TEXT;"))
    conn.execute(text("ALTER TABLE teachers ADD COLUMN IF NOT EXISTS signature TEXT;"))
    conn.execute(text("ALTER TABLE school_student_informations ADD COLUMN IF NOT EXISTS suivi_code VARCHAR(40);"))
    conn.execute(text("ALTER TABLE school_student_informations ADD COLUMN IF NOT EXISTS suivi_results_allowed BOOLEAN NOT NULL DEFAULT FALSE;"))
    conn.execute(text("ALTER TABLE school_student_informations ADD COLUMN IF NOT EXISTS suivi_results_updated_at TIMESTAMP;"))
    conn.execute(text("ALTER TABLE school_student_informations ADD COLUMN IF NOT EXISTS suivi_results_updated_by VARCHAR;"))
    conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_school_student_suivi_code ON school_student_informations (suivi_code) WHERE suivi_code IS NOT NULL;"))
    conn.execute(text("""
    ALTER TABLE course_informations
    ADD COLUMN IF NOT EXISTS sub_category VARCHAR;"""))
    # Mise à jour des colonnes manquantes de classe_informations
    conn.execute(text("""
        ALTER TABLE classe_informations
        ADD COLUMN IF NOT EXISTS conduite_enabled
        BOOLEAN NOT NULL DEFAULT FALSE;
    """))
    
    conn.execute(text("""
        ALTER TABLE classe_informations
        ADD COLUMN IF NOT EXISTS bulletin_type_id INTEGER;
    """))
    
    conn.execute(text("""
        ALTER TABLE classe_informations
        ADD COLUMN IF NOT EXISTS bulletin_model VARCHAR(20);
    """))
    
    conn.execute(text("""
        ALTER TABLE classe_informations
        ADD COLUMN IF NOT EXISTS bulletin_title VARCHAR;
    """))
    
    conn.execute(text("""
        ALTER TABLE classe_informations
        ADD COLUMN IF NOT EXISTS signature_source VARCHAR(20);
    """))
    
    # S'assurer que la colonne identifier existe dans otp_codes
    conn.execute(text("ALTER TABLE otp_codes ADD COLUMN IF NOT EXISTS identifier VARCHAR;"))
    
    # Vérification sécurisée avant d'altérer la colonne email si elle existe
    conn.execute(text("""
        DO $$ 
        BEGIN 
            IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='otp_codes' and column_name='email') THEN
                ALTER TABLE otp_codes ALTER COLUMN email DROP NOT NULL;
            END IF;
        END $$;
    """))
    conn.execute(text("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'school_information'
                  AND column_name = 'pn_extra'
                  AND data_type IN ('text', 'character varying')
            ) THEN
                ALTER TABLE school_information
                ALTER COLUMN pn_extra TYPE JSON
                USING NULLIF(BTRIM(pn_extra), '')::json;
            END IF;
        END $$;
        """))
    
    conn.execute(text("ALTER TABLE otp_codes ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0;"))
    conn.commit()
# ---------------------------------------------------------
# 5. INITIALISATION FASTAPI
# ---------------------------------------------------------
app = FastAPI(title="CRYPT Cloud Internet Node", version="3.1.0")
origins = [
    "https://class-net-p.vercel.app",  # Ton frontend Vercel en production
    "https://klgaby440-lang.github.io", # Class-Ink Suivi sur GitHub Pages
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

@app.middleware("http")
async def crypt_observe_client_requests(request: Request, call_next):
    """Enregistre les contacts des applications clientes sans lire leurs corps de requête."""
    response = await call_next(request)
    path = request.url.path or ""
    app_name = (request.headers.get("x-crypt-app") or "").strip().lower()
    device_id = (request.headers.get("x-crypt-device") or "").strip()
    if app_name in ("suivi", "manager"):
        # Les applications parent et Manager ne doivent jamais mettre en cache les réponses API.
        response.headers["Cache-Control"] = "no-store, private, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    if (
        request.method == "OPTIONS"
        or not path.startswith("/api/")
        or path.startswith("/api/crypt-console/")
        or path in ("/api/telemetry/events", "/api/telemetry/complaints")
        or app_name not in ("primeink", "classink", "suivi", "manager")
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", device_id)
    ):
        return response

    db = None
    try:
        db = SessionLocal()
        claims = {}
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer "):
            claims = verify_token(authorization[7:].strip()) or {}
        school_id = str(claims.get("school_id") or request.headers.get("x-crypt-school") or "")[:100] or None
        email = str(claims.get("email") or "")[:180] or None
        if not school_id and email and claims.get("role") == "teacher":
            teacher_record = db.query(Teacher).filter(Teacher.email == email).first()
            school_id = str(teacher_record.school_id or "")[:100] or None if teacher_record else None
        account = str(email or school_id or request.headers.get("x-crypt-account") or "")[:180] or None
        remote = request.client.host if request.client else ""
        user_agent = (request.headers.get("user-agent") or "")[:1000]
        platform = (request.headers.get("sec-ch-ua-platform") or request.headers.get("x-crypt-platform") or "")[:80]
        now = datetime.utcnow()
        device = db.query(CryptDeviceSession).filter(CryptDeviceSession.id == device_id).first()
        if device is None:
            device = CryptDeviceSession(
                id=device_id, app=app_name, school_id=school_id, account_identifier=account,
                platform=platform, app_version=(request.headers.get("x-crypt-version") or "")[:40],
                user_agent=user_agent, ip_address=remote[:80], first_seen=now, last_seen=now,
                contact_count=0, last_path=path[:240],
            )
            db.add(device)
        device.app = app_name
        device.school_id = school_id or device.school_id
        device.account_identifier = account or device.account_identifier
        device.platform = platform or device.platform
        device.user_agent = user_agent or device.user_agent
        device.ip_address = remote[:80] or device.ip_address
        device.last_seen = now
        device.contact_count = int(device.contact_count or 0) + 1
        device.last_path = path[:240]
        db.add(CryptTelemetryEvent(
            device_id=device_id, app=app_name, school_id=school_id, account_identifier=account,
            event_type="api_error" if response.status_code >= 400 else "server_contact",
            feature=(request.headers.get("x-crypt-feature") or path)[:160],
            message=("Réponse HTTP " + str(response.status_code)) if response.status_code >= 400 else "Requête reçue",
            details={"method": request.method, "status": response.status_code},
            http_method=request.method[:12], endpoint=path[:240], status_code=response.status_code,
            created_at=now,
        ))
        db.commit()
    except Exception as telemetry_error:
        if db is not None:
            db.rollback()
        print("[CRYPT telemetry] " + str(telemetry_error)[:240])
    finally:
        if db is not None:
            db.close()
    return response



# ---------------------------------------------------------
# 6. ROUTES D'AUTHENTIFICATION ET COMPTES
# ---------------------------------------------------------
@app.post("/api/auth/teacher/init-register")
async def init_teacher_register(data: TeacherInitSchema, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Détecte automatiquement si l'entrée est un numéro de téléphone ou un e-mail 
    et adapte l'envoi de l'OTP en conséquence.
    """
    # L'utilisateur envoie l'identifiant dans le champ email ou phone_number
    raw_identifier = (data.email or data.phone_number or "").strip()
    
    if not raw_identifier:
        return {"status": False, "message": "Veuillez fournir un e-mail ou un numéro de téléphone."}

    # 1. Détection du canal et normalisation
    if is_email(raw_identifier):
        channel = "email"
        identifier = raw_identifier.lower()
    else:
        channel = "phone"
        identifier = format_phone_e164(raw_identifier)

    # 2. Vérification de l'existence du compte dans la DB
    existing_teacher = db.query(Teacher).filter(
        (Teacher.email == identifier) | (Teacher.phone_number == identifier)
    ).first()
    
    if existing_teacher:
        return {"status": False, "message": "Cet identifiant est déjà associé à un compte."}

    # Limite les demandes de code (coût SMS/e-mail et force brute par renouvellement du code)
    if OTP_REQUEST_LIMITER.is_blocked(identifier):
        return {"status": False, "message": "Trop de demandes de code. Réessayez dans une heure."}
    OTP_REQUEST_LIMITER.record(identifier)

    # 3. Génération du code OTP et sauvegarde
    otp = generate_otp()
    expiry = datetime.utcnow() + timedelta(minutes=15)

    db.query(OTPVerification).filter(OTPVerification.identifier == identifier).delete()
    db.add(OTPVerification(identifier=identifier, code=otp, expires_at=expiry))
    db.commit()

    # 4. Routage automatique selon le canal détecté
    if channel == "email":
        email_html = (
            f"<h3>Salut {data.full_name} ⚡</h3>"
            f"<p>Ton code de vérification ClassNet est : <b>{otp}</b></p>"
            f"<p>Il expire dans 15 minutes.</p>"
        )
        background_tasks.add_task(send_email_via_resend, identifier, "Code de vérification ClassNet", email_html)
        return {"status": True, "message": f"Code envoyé à l'adresse email {identifier}."}

    else:  # SMS / Téléphone
        sms_text = f"ClassNet : Ton code de vérification est {otp}. Valable 15 minutes."
        background_tasks.add_task(send_sms_via_twilio, identifier, sms_text)
        return {"status": True, "message": f"Code envoyé par SMS au {identifier}."}

@app.post("/api/auth/teacher/verify-register")
def verify_teacher_register(data: OTPVerifySchema, db: Session = Depends(get_db)):
    # 1. Extraction et nettoyage de l'identifiant
    raw_identifier = (
        data.identifier 
        or data.email 
        or (data.teacher_data.phone_number if data.teacher_data else "") 
        or (data.teacher_data.email if data.teacher_data else "")
        or ""
    ).strip()

    if not raw_identifier:
        raise HTTPException(status_code=400, detail="Identifiant (e-mail ou téléphone) manquant.")

    # Même normalisation que dans init-register, sinon le code OTP ne sera jamais retrouvé
    raw_identifier = raw_identifier.lower() if is_email(raw_identifier) else format_phone_e164(raw_identifier)

    # 2. Vérification dans la table des codes OTP (avec limite d'essais)
    otp_record = db.query(OTPVerification).filter(OTPVerification.identifier == raw_identifier).first()

    if not otp_record:
        raise HTTPException(status_code=400, detail="Code OTP invalide ou expiré.")

    if (otp_record.attempts or 0) >= OTP_MAX_ATTEMPTS:
        db.delete(otp_record)
        db.commit()
        raise HTTPException(status_code=429, detail="Trop de tentatives. Demandez un nouveau code.")

    if not hmac.compare_digest(str(otp_record.code), str(data.code or "").strip()):
        otp_record.attempts = (otp_record.attempts or 0) + 1
        db.commit()
        raise HTTPException(status_code=400, detail="Code OTP invalide ou expiré.")

    if otp_record.expires_at < datetime.utcnow():
        db.delete(otp_record)
        db.commit()
        raise HTTPException(status_code=400, detail="Code OTP expiré. Veuillez demander un nouveau code.")

    # 3. Validation des données de l'enseignant
    t_data = data.teacher_data
    if not t_data:
        raise HTTPException(status_code=400, detail="Données de profil enseignant manquantes.")

    # Détermination de l'e-mail et du téléphone selon la nature de l'identifiant
    email_val = raw_identifier if is_email(raw_identifier) else t_data.email
    email_val = str(email_val or "").strip().lower()
    if not is_email(email_val):
        raise HTTPException(status_code=400, detail="Adresse e-mail invalide.")
    phone_val = raw_identifier if not is_email(raw_identifier) else t_data.phone_number

    # 4. Création du compte enseignant.
    # Le nom de l'école saisi dans le formulaire n'accorde aucun accès : l'établissement
    # doit ajouter le teacher_code du compte dans sa liste officielle PrimeInk.
    new_teacher = Teacher(
        email=email_val,
        phone_number=phone_val,
        full_name=t_data.full_name,
        school_name=t_data.school_name,
        password=hash_password(t_data.password),
        age=t_data.age,
        teacher_code=f"prof_{generate_otp()}"
    )

    db.add(new_teacher)
    db.delete(otp_record)
    db.commit()

    message = "Compte enseignant créé avec succès !"
    message += " Pour recevoir les classes de votre école, communiquez votre identifiant ClassInk à l'administration. Elle devra l'ajouter dans PrimeInk puis cliquer sur « Préparer les informations des classes »."
    return {"status": True, "message": message, "teacher_code": new_teacher.teacher_code}

@app.post("/api/auth/school/register")
def register_school(data: SchoolRegisterSchema, db: Session = Depends(get_db), _admin: bool = Depends(require_admin)):
    """Création d'un compte d'école : réservée à l'administrateur ClassNet (en-tête X-Admin-Key)."""
    if db.query(SchoolInformation).filter(SchoolInformation.school_id == data.school_id).first():
        return {"status": False, "message": "École déjà existante."}
    school_fields = data.dict()
    school_fields["pass_word"] = hash_password(school_fields["pass_word"])
    db.add(SchoolInformation(**school_fields))
    db.commit()
    return {"status": True, "message": "École enregistrée."}

@app.api_route("/app/version", methods=["GET", "POST"])
def get_classnet_app_version():
    """Renvoie la dernière version disponible pour ClassNet App."""
    version = os.getenv("class-net-app-last-version", "1.0.0")
    return {
        "status": True,
        "app": "ClassNet App",
        "latest_version": version
    }

@app.api_route("/prime/version", methods=["GET", "POST"])
def get_classnet_p_version():
    """Renvoie la dernière version disponible pour ClassNet P."""
    version = os.getenv("class-net-p-last-version", "1.0.0")
    return {
        "status": True,
        "app": "ClassNet P",
        "latest_version": version
    }

@app.post("/api/auth/teacher/login")
def login_teacher(data: LoginSchema, db: Session = Depends(get_db)):
    """Connexion Prof : Reconstitue et renvoie l'état exact defaultDB depuis PostgreSQL."""
    limiter_key = "teacher:" + data.identifier.strip().lower()
    if LOGIN_LIMITER.is_blocked(limiter_key):
        return JSONResponse(status_code=429, content={"status": False, "message": "Trop de tentatives échouées. Réessayez dans 15 minutes."})

    teacher = db.query(Teacher).filter(Teacher.email == data.identifier.strip().lower()).first()
    if not teacher or not verify_password(data.password, teacher.password):
        LOGIN_LIMITER.record(limiter_key)
        return {"status": False, "message": "Identifiants incorrects."}
    LOGIN_LIMITER.reset(limiter_key)

    blocked = teacher_school_block(db, teacher)
    if blocked:
        return {"status": False, "locked": True, "message": blocked}

    if not is_password_hashed(teacher.password):
        teacher.password = hash_password(data.password)  # migration silencieuse de l'ancien format en clair
        db.commit()
    
    # Récupération des entités
    db_grades = db.query(StudentGrade).filter(StudentGrade.teacher_email == teacher.email).all()
    db_quizzes = db.query(QuizBank).filter(QuizBank.teacher_email == teacher.email).all()
    # Si vous avez un modèle Attendance pour les présences :
    db_presences = db.query(Attendance).filter(Attendance.teacher_email == teacher.email).all() if 'Attendance' in globals() else []

    classes_set = set()
    courses_dict = {}
    students_dict = {}
    evaluations_dict = {}
    grades_dict = {}
    course_max_dict = {}

    for g in db_grades:
        c_name = g.class_name
        classes_set.add(c_name)

        # Reconstitution des cours
        if c_name not in courses_dict:
            courses_dict[c_name] = []
        if g.course_name and g.course_name not in courses_dict[c_name]:
            courses_dict[c_name].append(g.course_name)

        # Reconstitution des élèves
        if c_name not in students_dict:
            students_dict[c_name] = []
        if not any(s["id"] == g.student_id for s in students_dict[c_name]):
            students_dict[c_name].append({
                "id": g.student_id,
                "name": g.student_name
            })

        # Reconstitution des évaluations avec la date
        if c_name not in evaluations_dict:
            evaluations_dict[c_name] = {"P1": [], "P2": [], "EX1": [], "P3": [], "P4": [], "EX2": []}
        
        period_key = g.period if g.period in evaluations_dict[c_name] else "P1"
        if not any(e["id"] == g.eval_id for e in evaluations_dict[c_name][period_key]):
            eval_item = {
                "id": g.eval_id,
                "name": g.eval_name,
                "max": g.max_score,
                "course": g.course_name
            }
            if hasattr(g, 'eval_date') and g.eval_date:
                eval_item["date"] = str(g.eval_date)
            evaluations_dict[c_name][period_key].append(eval_item)

        # Reconstitution des notes
        grade_key = f"{g.student_id}_{g.eval_id}"
        grades_dict[grade_key] = g.score
        
        # Max par cours
        if g.course_name and hasattr(g, 'course_max'):
            course_max_dict[f"{c_name}_{g.course_name}"] = g.course_max

    # Formatage de la liste des quiz conforme
    quizzes_list = [
        {
            "id": q.id,
            "title": q.title,
            "class": getattr(q, 'class_name', ''),
            "course": getattr(q, 'course_name', ''),
            "maxScore": getattr(q, 'max_score', 20),
            "textContent": getattr(q, 'content', getattr(q, 'text_content', '')),
            "createdAt": str(getattr(q, 'created_at', ''))
        } for q in db_quizzes
    ]

    # Formatage des présences
    presences_list = [
        {
            "id": p.id,
            "date": str(p.date),
            "class": p.class_name,
            "course": p.course_name,
            "studentId": p.student_id,
            "studentName": p.student_name,
            "status": p.status
        } for p in db_presences
    ]

    # Assemblage complet du JSON DB
    classnet_app_db = {
        "user": {
            "id": teacher.teacher_code or f"prof_{teacher.id}",
            "name": teacher.full_name,
            "email": teacher.email,
            "password": data.password,  # jamais l'empreinte : le client garde le mot de passe saisi (mode hors-ligne)
            "school": teacher.school_name or "Indépendant",
            "isLoggedIn": True
        },
        "classes": list(classes_set),
        "courses": courses_dict,
        "courseMax": course_max_dict,
        "periodVisibility": {"P1": True, "P2": False, "EX1": False, "P3": False, "P4": False, "EX2": False},
        "activeCourseFilter": {},
        "students": students_dict,
        "evaluations": evaluations_dict,
        "grades": grades_dict,
        "presences": presences_list,
        "quizzes": quizzes_list,
        "school_id": getattr(teacher, 'school_id', ''),
        "theme": getattr(teacher, 'theme', 'dark'),
        "pendingCommits": 0
    }
    
    # Si une sauvegarde complète existe, elle est prioritaire : elle contient aussi
    # les classes, cours, élèves sans notes et les réglages que StudentGrade ne peut pas reconstruire.
    snapshot_row = db.query(ClassInkTeacherSnapshot).filter_by(teacher_email=teacher.email).first()
    classnet_app_db = restore_classink_teacher_state(
        snapshot_row.payload_data if snapshot_row else None,
        classnet_app_db,
        teacher,
        data.password,
    )
    # Les classes officielles de l'école sont ajoutées à chaque connexion, même sans aucune note.
    classnet_app_db = apply_teacher_classroom_context(
        classnet_app_db,
        build_teacher_classroom_context(db, teacher),
    )

    return {"status": True, "data": classnet_app_db,
            "token": create_token({"role": "teacher", "email": teacher.email}, TEACHER_TOKEN_TTL)}

@app.post("/api/auth/school/login")
def login_school(data: LoginSchema, db: Session = Depends(get_db)):
    """Connexion École : Charge et renvoie l'état exact defaultState pour PrimeNet/ClassNet P depuis la DB."""
    school = db.query(SchoolInformation).filter(
        SchoolInformation.school_id == data.identifier
    ).first()
    
    limiter_key = "school:" + str(data.identifier).strip()
    if LOGIN_LIMITER.is_blocked(limiter_key):
        return JSONResponse(status_code=429, content={"status": False, "message": "Trop de tentatives échouées. Réessayez dans 15 minutes."})

    if not school or not verify_password(data.password, school.pass_word):
        LOGIN_LIMITER.record(limiter_key)
        return {"status": False, "message": "Identifiants incorrects."}
    LOGIN_LIMITER.reset(limiter_key)

    state = licence_state(school)
    if state["locked"]:
        return {"status": False, "locked": True, "reason": state["reason"], "message": LICENCE_MESSAGES[state["reason"]]}

    if not is_password_hashed(school.pass_word):
        school.pass_word = hash_password(data.password)  # migration silencieuse de l'ancien format en clair
        db.commit()
    
    # 1. Chargement des classes
    classes_data = []
    for c in school.classes:
        classes_data.append({
            "id": c.id,
            "name": c.class_name,
            "titulaire": c.titulaire_name or "",
            "categories": c.domaines if isinstance(c.domaines, list) else [],
            "bulletinModel": c.bulletin_model or "classink",
            "bulletinTitle": c.bulletin_title or "",
            "signatureSource": c.signature_source or "titulaire"
        })

    # 2. Chargement des élèves
    students_data = []
    student_id_map = {}  # Mappe le numéro permanent vers l'id de l'élève
    for s in school.students:
        suivi_code = ensure_suivi_student_code(db, s)
        students_data.append({
            "id": s.id,
            "name": s.student_name,
            "postname": s.student_post_name,
            "prename": s.student_pre_name,
            "sexe": s.student_sexe,
            "bornDate": s.student_born_date or "",
            "bornWhere": s.student_born_place or "",
            "permi": s.student_n_permanent or "",
            "classId": s.class_id,
            "suiviCode": suivi_code,
            "suiviResultsAllowed": is_suivi_results_allowed(db, school.school_id, s),
            "suiviIndividualAccessAllowed": bool(s.suivi_results_allowed)
        })
        if s.student_n_permanent:
            student_id_map[s.student_n_permanent] = s.id
    # Persiste également les codes Suivi attribués lors du premier chargement Manager/PrimeInk.
    db.commit()

    # 3. Chargement des cours
    courses_data = []
    for crs in school.courses:
        courses_data.append({
            "id": crs.id,
            "name": crs.course_name,
            "maxPer": float(crs.max_per),
            "category": crs.category,
            "classId": crs.class_id,
            "titulaire": crs.titulaire_name or "",
            "subCategory": crs.sub_category or ""
        })

    # 4. Chargement des enseignants
    teachers_data = []
    for t in school.teachers:
        teachers_data.append({
            "uniqueId": t.teacher_code or f"prof_{t.id}",
            "name": t.full_name,
            "subject": t.subject or "",
            "status": t.status or "Actif",
            "signature": clean_image(t.signature) or ""
        })

    # 5. Chargement des notes (TeacherEvaluation)
    evaluations = db.query(TeacherEvaluation).filter(TeacherEvaluation.school_id == school.school_id).all()
    grades_data = {}
    for ev in evaluations:
        st_id = student_id_map.get(ev.student_n_permanent, ev.student_n_permanent)
        grade_key = f"{st_id}_{ev.course_id}"
        
        grade_obj = {}
        if ev.p1 is not None: grade_obj["p1"] = ev.p1
        if ev.p2 is not None: grade_obj["p2"] = ev.p2
        if ev.ex1 is not None: grade_obj["ex1"] = ev.ex1
        if ev.p3 is not None: grade_obj["p3"] = ev.p3
        if ev.p4 is not None: grade_obj["p4"] = ev.p4
        if ev.ex2 is not None: grade_obj["ex2"] = ev.ex2

        if grade_obj:
            grades_data[grade_key] = grade_obj

    # 6. Assemblage de l'état complet
    primenet_state = {
        "school": {
            "id": school.school_id,
            "name": school.name_school,
            "code": school.code,
            "city": school.city,
            "commune": school.commune,
            "province": school.province or "",
            "headName": school.head_name or "",
            "schoolYear": school.school_year or "",
            "responsable": school.name_responsable or "",
            "phone": school.num_tel or "",
            "email": school.email or "",
            "address": school.adresse_physique or ""
        },
        "classes": classes_data,
        "students": students_data,
        "courses": courses_data,
        "teachers": teachers_data,
        "grades": grades_data,
        "tools": {
            "presences": {"students": [], "teachers": []},
            "mixage": {"config": {}, "surveillants": [], "salles": [], "coursProgrammes": [], "generatedSchedules": []},
            "horaire": {"config": {}, "profsDispo": [], "generatedSchedules": []}
        }
    }

    # Identité du bulletin et réglages conservés (sceau, signatures, conduite vérifiée...)
    for key, attr in (("province", "province"), ("headName", "head_name"), ("schoolYear", "school_year"),
                      ("seal", "seal"), ("headSignature", "head_signature"), ("armoiries", "armoiries")):
        value = getattr(school, attr)
        if value:
            primenet_state["school"][key] = value
    primenet_state.update(school_state_extras(db, school))

    return {"status": True, "data": primenet_state,
            "token": create_token({"role": "school", "school_id": school.school_id}, SCHOOL_TOKEN_TTL)}

# ---------------------------------------------------------
# Alias utilisé par PrimeInk : chargement de l'état complet de l'école
# ---------------------------------------------------------
class PrimeNetLoadSchema(BaseModel):
    school_id: str
    password: str

@app.post("/api/primenet/load")
def load_primenet_state(data: PrimeNetLoadSchema, db: Session = Depends(get_db)):
    """PrimeInk envoie { school_id, password } ; le navigateur ne met jamais la réponse en cache."""
    result = login_school(LoginSchema(identifier=data.school_id, password=data.password), db)
    headers = {"Cache-Control": "no-store, private, max-age=0", "Pragma": "no-cache", "Expires": "0"}
    if isinstance(result, JSONResponse):
        try:
            payload = json.loads(result.body.decode("utf-8"))
        except Exception:
            payload = {"status": False, "message": "Réponse du serveur illisible."}
        return JSONResponse(content=payload, status_code=result.status_code, headers=headers)
    return JSONResponse(content=result, headers=headers)


@app.post("/api/manager/login")
def manager_login(data: LoginSchema, db: Session = Depends(get_db)):
    """
    Connexion Manager avec les mêmes identifiants que PrimeInk.
    Les données sont renvoyées sans cache HTTP ; le client les garde uniquement en mémoire.
    """
    result = login_school(data, db)
    headers = {
        "Cache-Control": "no-store, private, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
        "Vary": "Authorization",
    }
    # login_school peut déjà avoir construit une réponse HTTP (ex. limitation 429).
    # Ne jamais appeler .get() sur un objet JSONResponse.
    if isinstance(result, JSONResponse):
        try:
            payload = json.loads(result.body.decode("utf-8"))
        except Exception:
            payload = {"status": False, "message": "Réponse d'authentification indisponible."}
        return JSONResponse(content=payload, status_code=result.status_code, headers=headers)
    if result.get("status") and isinstance(result.get("data"), dict):
        state = result["data"]
        # Manager n'a pas besoin des signatures ou images d'identité intégrées aux bulletins.
        for teacher in state.get("teachers", []):
            teacher.pop("signature", None)
        school_data = state.get("school") or {}
        for key in ("seal", "headSignature", "armoiries"):
            school_data.pop(key, None)
        state.pop("signatureBook", None)
    return JSONResponse(content=result, headers=headers)


@app.get("/api/manager/schedule")
def manager_get_schedule(
    db: Session = Depends(get_db),
    claims: dict = Depends(require_active_school),
):
    """Manager récupère le dernier horaire publié par PrimeInk pour l'établissement connecté."""
    school_id = str(claims.get("school_id") or "")
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="Établissement introuvable.")
    schedule = (school.pn_extra or {}).get("managerSchedule")
    return JSONResponse(
        content={"status": True, "data": schedule if isinstance(schedule, dict) else None},
        headers={"Cache-Control": "no-store, private, max-age=0", "Pragma": "no-cache", "Expires": "0"},
    )


@app.put("/api/manager/schedule")
def manager_publish_schedule(
    payload: dict,
    db: Session = Depends(get_db),
    claims: dict = Depends(require_active_school),
):
    """PrimeInk publie l'horaire généré afin que Manager puisse le consulter depuis le serveur."""
    school_id = str(claims.get("school_id") or "")
    if str(payload.get("school_id") or "") != school_id:
        raise HTTPException(status_code=403, detail="Accès refusé aux données d'une autre école.")
    schedule = payload.get("schedule")
    if not isinstance(schedule, dict):
        raise HTTPException(status_code=422, detail="Horaire manquant ou invalide.")
    result = schedule.get("result")
    schedules = result.get("schedules") if isinstance(result, dict) else None
    if not isinstance(schedules, dict) or not isinstance(schedules.get("classes"), list):
        raise HTTPException(status_code=422, detail="Le résultat de l'horaire est incomplet.")
    try:
        serialized_size = len(json.dumps(schedule, ensure_ascii=False))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="L'horaire contient des données non valides.")
    if serialized_size > 1500000:
        raise HTTPException(status_code=413, detail="L'horaire est trop volumineux pour être publié.")
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="Établissement introuvable.")
    stored = {
        "academicYear": str(schedule.get("academicYear") or school.school_year or "")[:30],
        "generatedAt": str(schedule.get("generatedAt") or datetime.utcnow().isoformat())[:60],
        "config": schedule.get("config") if isinstance(schedule.get("config"), dict) else {},
        "slots": schedule.get("slots") if isinstance(schedule.get("slots"), list) else [],
        "teachers": schedule.get("teachers") if isinstance(schedule.get("teachers"), list) else [],
        "result": result,
    }
    extra = dict(school.pn_extra or {})
    extra["managerSchedule"] = stored
    school.pn_extra = extra
    db.commit()
    return JSONResponse(
        content={"status": True, "message": "Horaire publié pour ClassInk Manager."},
        headers={"Cache-Control": "no-store, private, max-age=0", "Pragma": "no-cache", "Expires": "0"},
    )


# ---------------------------------------------------------
# 7. GESTION DES CODES À USAGE UNIQUE (119 BITS)
# ---------------------------------------------------------
import os
import httpx

RESEND_API_KEY = os.getenv("RESEND_API_KEY")

async def send_email_via_resend(to_email: str, subject: str, html_body: str) -> bool:
    """Envoie un e-mail réel à l'aide de l'API REST de Resend."""
    if not RESEND_API_KEY:
        print("⚠️ [RESEND] Variable RESEND_API_KEY non configurée.")
        return False
        
    payload = {
        "from": "ClassNet Admin <onboarding@resend.dev>",
        "to": [to_email],
        "subject": subject,
        "html": html_body
    }
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post("https://api.resend.com/emails", json=payload, headers=headers)
            if response.status_code in [200, 201]:
                print(f"✅ [EMAIL ENVOYÉ] Code envoyé avec succès à {to_email}")
                return True
            else:
                print(f"❌ [RESEND ERREUR] {response.status_code} : {response.text}")
                return False
    except Exception as e:
        print(f"❌ [EMAIL ERREUR] Exception lors de l'envoi : {str(e)}")
        return False
        
@app.post("/api/admin/codes/generate")
def generate_and_send_code(background_tasks: BackgroundTasks, db: Session = Depends(get_db), _admin: bool = Depends(require_admin)):
    """Génère un code de 20 caractères et l'envoie par e-mail à l'administrateur (réservé à l'administrateur)."""
    new_code = generate_20_char_code()
    
    # 1. Sauvegarde en base de données
    db.add(AccessCode(code=new_code))
    db.commit()
    
    # 2. Préparation du template d'e-mail
    subject = "Nouveau Code d'Accès ClassNet Généré"
    email_html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; color: #1a1a1a; border: 1px solid #e0e0e0; border-radius: 8px;">
        <h2 style="color: #2563eb;">Notification Administrateur ClassNet ⚡</h2>
        <p>Un nouveau code d'accès à usage unique de 20 caractères a été généré :</p>
        <div style="background-color: #f3f4f6; padding: 15px; border-radius: 6px; font-family: monospace; font-size: 18px; font-weight: bold; text-align: center; letter-spacing: 2px;">
            {new_code}
        </div>
        <p style="margin-top: 20px; font-size: 12px; color: #6b7280;">
            Ce code expirera ou sera recyclé dès sa première utilisation.
        </p>
    </div>
    """
    
    # 3. Envoi effectif de l'e-mail en arrière-plan
    background_tasks.add_task(send_email_via_resend, ADMIN_EMAIL, subject, email_html)
    
    return {
        "status": True, 
        "message": f"Code généré avec succès et envoyé à l'adresse {ADMIN_EMAIL}."
    }

class LockdownSchema(BaseModel):
    target: str          # identifiant (school_id) de l'école
    status: str          # "LOCKED" ou "UNLOCKED"


@app.post("/api/admin/lockdown")
def admin_lockdown(data: LockdownSchema, db: Session = Depends(get_db), _admin: bool = Depends(require_admin)):
    """Suspend ou réactive une école (réservé à l'administrateur)."""
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == data.target).first()
    if not school:
        return {"status": False, "message": "École introuvable."}
    wanted = data.status.strip().upper()
    if wanted not in ("LOCKED", "UNLOCKED"):
        return {"status": False, "message": "Statut inconnu : utilisez LOCKED ou UNLOCKED."}
    school.is_locked = (wanted == "LOCKED")
    db.commit()
    return {"status": True, "message": "École " + ("suspendue." if school.is_locked else "réactivée.")}


@app.get("/api/admin/schools")
def admin_list_schools(db: Session = Depends(get_db), _admin: bool = Depends(require_admin)):
    """Liste des écoles avec leur état de licence (réservé à l'administrateur)."""
    rows = []
    for s in db.query(SchoolInformation).order_by(SchoolInformation.id).all():
        st = licence_state(s)
        rows.append({"school_id": s.school_id, "name": s.name_school, "city": s.city, "licence_date": s.licence_date,
                     "is_locked": bool(s.is_locked), "state": st["reason"] or "ok"})
    return {"status": True, "data": rows}



# ---------------------------------------------------------------------------
# CRYPT COMMAND CENTER — administration, télémétrie, réclamations et inspection
# ---------------------------------------------------------------------------

def _crypt_audit(db: Session, action: str, target: Optional[str] = None, details: Optional[dict] = None):
    db.add(CryptAdminAudit(
        action=str(action)[:80], actor="crypt_admin", target=str(target or "")[:180] or None,
        details=details or {}, created_at=datetime.utcnow(),
    ))


def _crypt_public_school(school, db: Session) -> dict:
    state = licence_state(school)
    return {
        "id": school.school_id,
        "name": school.name_school,
        "code": school.code,
        "city": school.city,
        "commune": school.commune,
        "responsable": school.name_responsable,
        "phone": school.num_tel,
        "email": school.email,
        "licenceDate": school.licence_date or "",
        "locked": bool(school.is_locked),
        "licenceState": state["reason"] or "ok",
        "classes": db.query(ClasseInformation).filter(ClasseInformation.school_id == school.school_id).count(),
        "students": db.query(SchoolStudentInformation).filter(SchoolStudentInformation.school_id == school.school_id).count(),
        "teachers": db.query(Teacher).filter(Teacher.school_id == school.school_id).count(),
    }


def _crypt_touch_public_device(db: Session, request: Request, app_name: str, device_id: str):
    now = datetime.utcnow()
    remote = request.client.host if request.client else ""
    device = db.query(CryptDeviceSession).filter(CryptDeviceSession.id == device_id).first()
    if device is None:
        device = CryptDeviceSession(id=device_id, app=app_name, first_seen=now, last_seen=now, contact_count=0)
        db.add(device)
    device.app = app_name[:30]
    device.last_seen = now
    device.contact_count = int(device.contact_count or 0) + 1
    device.ip_address = (remote or "")[:80] or device.ip_address
    device.user_agent = (request.headers.get("user-agent") or "")[:1000] or device.user_agent
    authorization = request.headers.get("authorization", "")
    claims = verify_token(authorization[7:].strip()) if authorization.lower().startswith("bearer ") else None
    claims = claims or {}
    device.school_id = str(claims.get("school_id") or request.headers.get("x-crypt-school") or device.school_id or "")[:100] or None
    device.account_identifier = str(claims.get("email") or device.school_id or "")[:180] or None
    device.platform = (request.headers.get("sec-ch-ua-platform") or request.headers.get("x-crypt-platform") or device.platform or "")[:80]
    device.last_path = request.url.path[:240]
    return device


@app.post("/api/crypt-console/login")
def crypt_console_login(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Échange la clé ADMIN_API_KEY contre un jeton administratif temporaire."""
    remote = request.client.host if request.client else "unknown"
    limiter_key = "crypt-console:" + remote
    if CRYPT_CONSOLE_LOGIN_LIMITER.is_blocked(limiter_key):
        raise HTTPException(status_code=429, detail="Trop de tentatives. Réessayez dans 15 minutes.")
    supplied = str(payload.get("admin_key") or "")
    if not ADMIN_API_KEY:
        raise HTTPException(status_code=503, detail="Centre CRYPT désactivé : définissez ADMIN_API_KEY dans les variables Render.")
    if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), ADMIN_API_KEY.encode("utf-8")):
        CRYPT_CONSOLE_LOGIN_LIMITER.record(limiter_key)
        raise HTTPException(status_code=401, detail="Clé administrative invalide.")
    CRYPT_CONSOLE_LOGIN_LIMITER.reset(limiter_key)
    _crypt_audit(db, "admin_login", remote, {"result": "success"})
    db.commit()
    return JSONResponse(
        content={"status": True, "token": create_token({"role": "crypt_admin", "sub": "crypt-console"}, 28800)},
        headers={"Cache-Control": "no-store, private, max-age=0", "Pragma": "no-cache", "Expires": "0"},
    )


@app.get("/api/crypt-console/overview")
def crypt_console_overview(db: Session = Depends(get_db), _admin: dict = Depends(require_crypt_admin)):
    now = datetime.utcnow()
    return {
        "status": True,
        "data": {
            "schools": db.query(SchoolInformation).count(),
            "teachers": db.query(Teacher).count(),
            "students": db.query(SchoolStudentInformation).count(),
            "devices_online": db.query(CryptDeviceSession).filter(CryptDeviceSession.last_seen >= now - timedelta(minutes=5)).count(),
            "devices_total": db.query(CryptDeviceSession).count(),
            "events_24h": db.query(CryptTelemetryEvent).filter(CryptTelemetryEvent.created_at >= now - timedelta(hours=24)).count(),
            "errors_24h": db.query(CryptTelemetryEvent).filter(
                CryptTelemetryEvent.created_at >= now - timedelta(hours=24),
                CryptTelemetryEvent.event_type.in_(("api_error", "client_error")),
            ).count(),
            "open_tickets": db.query(CryptSupportTicket).filter(CryptSupportTicket.status == "open").count(),
            "recent_events": [
                {"id": x.id, "app": x.app, "school_id": x.school_id, "event_type": x.event_type,
                 "feature": x.feature, "message": x.message, "status_code": x.status_code,
                 "created_at": x.created_at.isoformat() + "Z" if x.created_at else ""}
                for x in db.query(CryptTelemetryEvent).order_by(CryptTelemetryEvent.created_at.desc()).limit(12).all()
            ],
        },
    }


@app.get("/api/crypt-console/schools")
def crypt_console_schools(db: Session = Depends(get_db), _admin: dict = Depends(require_crypt_admin)):
    rows = db.query(SchoolInformation).order_by(SchoolInformation.name_school.asc()).limit(500).all()
    return {"status": True, "data": [_crypt_public_school(s, db) for s in rows]}


@app.post("/api/crypt-console/schools")
def crypt_console_create_school(data: SchoolRegisterSchema, db: Session = Depends(get_db), _admin: dict = Depends(require_crypt_admin)):
    if len(str(data.pass_word or "")) < 10:
        raise HTTPException(status_code=422, detail="Le mot de passe de l'établissement doit contenir au moins 10 caractères.")
    if db.query(SchoolInformation).filter(SchoolInformation.school_id == data.school_id).first():
        raise HTTPException(status_code=409, detail="Cet identifiant d'établissement existe déjà.")
    if db.query(SchoolInformation).filter(SchoolInformation.bulletin_seq_id == data.bulletin_seq_id).first():
        raise HTTPException(status_code=409, detail="Cet identifiant de bulletin existe déjà.")
    fields = data.dict()
    fields["school_id"] = clean_text(fields["school_id"], 80)
    fields["name_school"] = clean_text(fields["name_school"], 180)
    fields["pass_word"] = hash_password(fields["pass_word"])
    school = SchoolInformation(**fields)
    db.add(school)
    db.flush()
    _crypt_audit(db, "school_created", school.school_id, {"school_name": school.name_school})
    db.commit()
    return {"status": True, "message": "Compte PrimeInk créé.", "data": {"school_id": school.school_id, "name": school.name_school}}


@app.put("/api/crypt-console/schools/{school_id}/status")
def crypt_console_school_status(school_id: str, payload: dict, db: Session = Depends(get_db), _admin: dict = Depends(require_crypt_admin)):
    wanted = str(payload.get("status") or "").upper()
    if wanted not in ("LOCKED", "UNLOCKED"):
        raise HTTPException(status_code=422, detail="Utilisez LOCKED ou UNLOCKED.")
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="Établissement introuvable.")
    school.is_locked = wanted == "LOCKED"
    _crypt_audit(db, "school_status_changed", school_id, {"status": wanted})
    db.commit()
    return {"status": True, "message": "Statut de l'établissement mis à jour."}


@app.get("/api/crypt-console/devices")
def crypt_console_devices(limit: int = 100, db: Session = Depends(get_db), _admin: dict = Depends(require_crypt_admin)):
    limit = max(1, min(int(limit or 100), 300))
    rows = db.query(CryptDeviceSession).order_by(CryptDeviceSession.last_seen.desc()).limit(limit).all()
    return {"status": True, "data": [{
        "id": d.id, "app": d.app, "school_id": d.school_id, "account": d.account_identifier,
        "platform": d.platform, "version": d.app_version, "ip": d.ip_address,
        "user_agent": d.user_agent, "first_seen": d.first_seen.isoformat() + "Z" if d.first_seen else "",
        "last_seen": d.last_seen.isoformat() + "Z" if d.last_seen else "",
        "contact_count": d.contact_count, "last_path": d.last_path,
        "online": bool(d.last_seen and d.last_seen >= datetime.utcnow() - timedelta(minutes=5)),
    } for d in rows]}


@app.get("/api/crypt-console/events")
def crypt_console_events(limit: int = 100, event_type: Optional[str] = None, app_name: Optional[str] = None,
                         db: Session = Depends(get_db), _admin: dict = Depends(require_crypt_admin)):
    limit = max(1, min(int(limit or 100), 300))
    query = db.query(CryptTelemetryEvent)
    if event_type:
        query = query.filter(CryptTelemetryEvent.event_type == event_type[:40])
    if app_name:
        query = query.filter(CryptTelemetryEvent.app == app_name[:30])
    rows = query.order_by(CryptTelemetryEvent.created_at.desc()).limit(limit).all()
    return {"status": True, "data": [{
        "id": e.id, "app": e.app, "school_id": e.school_id, "account": e.account_identifier,
        "device_id": e.device_id, "type": e.event_type, "feature": e.feature,
        "message": e.message, "endpoint": e.endpoint, "method": e.http_method,
        "status": e.status_code, "details": e.details or {},
        "created_at": e.created_at.isoformat() + "Z" if e.created_at else "",
    } for e in rows]}


@app.get("/api/crypt-console/tickets")
def crypt_console_tickets(status_filter: Optional[str] = None, limit: int = 100,
                          db: Session = Depends(get_db), _admin: dict = Depends(require_crypt_admin)):
    limit = max(1, min(int(limit or 100), 300))
    query = db.query(CryptSupportTicket)
    if status_filter:
        query = query.filter(CryptSupportTicket.status == status_filter[:24])
    rows = query.order_by(CryptSupportTicket.created_at.desc()).limit(limit).all()
    return {"status": True, "data": [{
        "id": t.id, "app": t.app, "school_id": t.school_id, "account": t.account_identifier,
        "device_id": t.device_id, "contact": t.contact, "category": t.category, "subject": t.subject,
        "message": t.message, "status": t.status, "admin_note": t.admin_note,
        "created_at": t.created_at.isoformat() + "Z" if t.created_at else "",
        "updated_at": t.updated_at.isoformat() + "Z" if t.updated_at else "",
    } for t in rows]}


@app.put("/api/crypt-console/tickets/{ticket_id}")
def crypt_console_update_ticket(ticket_id: str, payload: dict, db: Session = Depends(get_db), _admin: dict = Depends(require_crypt_admin)):
    wanted = str(payload.get("status") or "").lower()
    if wanted not in ("open", "in_progress", "resolved", "closed"):
        raise HTTPException(status_code=422, detail="Statut de réclamation inconnu.")
    ticket = db.query(CryptSupportTicket).filter(CryptSupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=404, detail="Réclamation introuvable.")
    ticket.status = wanted
    ticket.admin_note = clean_text(payload.get("admin_note"), 2000) if payload.get("admin_note") is not None else ticket.admin_note
    ticket.updated_at = datetime.utcnow()
    _crypt_audit(db, "ticket_updated", ticket_id, {"status": wanted})
    db.commit()
    return {"status": True, "message": "Réclamation mise à jour."}


@app.get("/api/crypt-console/db/schema")
def crypt_console_db_schema(_admin: dict = Depends(require_crypt_admin)):
    hidden = {"pass_word", "password", "secret", "token", "signature", "head_signature", "seal", "armoiries"}
    tables = []
    for table_name, table in Base.metadata.tables.items():
        if table_name in ("otp_codes", "access_codes"):
            continue
        tables.append({
            "name": table_name,
            "columns": [{"name": column.name, "type": str(column.type), "nullable": bool(column.nullable)}
                        for column in table.columns if column.name.lower() not in hidden],
        })
    return {"status": True, "data": tables}


def validate_crypt_read_query(query: str) -> str:
    """Valide une requête de consultation CRYPT avant son exécution en transaction read-only."""
    sql = str(query or "").strip()
    if not sql or len(sql) > 6000:
        raise HTTPException(status_code=422, detail="La requête est vide ou trop longue (maximum 6 000 caractères).")
    sql = sql.rstrip().rstrip(";").strip()
    if ";" in sql or "--" in sql or "/*" in sql or "*/" in sql:
        raise HTTPException(status_code=403, detail="Une seule instruction sans commentaires SQL est autorisée.")
    if not re.match(r"^SELECT\b", sql, re.IGNORECASE):
        raise HTTPException(status_code=403, detail="Seules les requêtes SELECT simples sont autorisées.")
    forbidden = r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|DO|CALL|EXECUTE|LOCK|SET|VACUUM|ANALYZE|INTO|NEXTVAL|SETVAL|CURRENT_USER|SESSION_USER|CURRENT_ROLE|SESSION_ROLE|CURRENT_SCHEMA|CURRENT_CATALOG)\b|\bFOR\s+(UPDATE|SHARE)\b|\b(pg_[a-z0-9_]*|lo_[a-z0-9_]*|dblink|set_config|current_setting|current_database|version|to_json|to_jsonb|row_to_json|json_agg|jsonb_agg|json_build_object|jsonb_build_object|hstore)\s*\("
    if re.search(forbidden, sql, re.IGNORECASE):
        raise HTTPException(status_code=403, detail="Instruction ou fonction interdite dans l'explorateur.")
    secret_columns = r"\b(pass_word|password|secret|token|signature|head_signature|api_key|otp)\b"
    if re.search(secret_columns, sql, re.IGNORECASE):
        raise HTTPException(status_code=403, detail="Les colonnes sensibles sont masquées.")
    if re.search(r"\b(information_schema|pg_catalog|sqlite_master|pg_toast)\b", sql, re.IGNORECASE):
        raise HTTPException(status_code=403, detail="Les catalogues système ne sont pas accessibles depuis cet explorateur.")
    from_prefix = re.split(r"\b(?:WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT|OFFSET|UNION|INTERSECT|EXCEPT)\b", sql, maxsplit=1, flags=re.IGNORECASE)[0]
    from_clause = re.search(r"\bFROM\b(.*)$", from_prefix, re.IGNORECASE | re.DOTALL)
    if from_clause and "," in from_clause.group(1):
        raise HTTPException(status_code=403, detail="Utilisez des JOIN explicites ; les jointures implicites par virgule sont interdites.")
    allowed_tables = set(Base.metadata.tables.keys()) - {"otp_codes", "access_codes"}
    referenced = re.findall(r'\b(?:FROM|JOIN)\s+["]?([A-Za-z_][A-Za-z0-9_]*)["]?', sql, re.IGNORECASE)
    if not referenced or any(name not in allowed_tables for name in referenced):
        raise HTTPException(status_code=403, detail="La requête doit cibler uniquement les tables applicatives autorisées.")
    return sql


@app.post("/api/crypt-console/db/query")
def crypt_console_db_query(payload: dict, db: Session = Depends(get_db), _admin: dict = Depends(require_crypt_admin)):
    sql = validate_crypt_read_query(str(payload.get("query") or ""))
    query_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    try:
        # Le premier ordre SQL de la transaction active le mode lecture seule.
        db.execute(text("SET TRANSACTION READ ONLY"))
        db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
        result = db.execute(text("SELECT * FROM (" + sql + ") AS crypt_readonly_result LIMIT 101"))
        columns = list(result.keys())
        rows = result.mappings().fetchmany(101)
        truncated = len(rows) > 100
        rows = rows[:100]
        data = []
        hidden_columns = {"pass_word", "password", "secret", "token", "signature", "head_signature", "seal", "armoiries", "api_key", "otp"}
        for row in rows:
            item = {}
            for key in columns:
                if str(key).lower() in hidden_columns:
                    continue
                value = row.get(key)
                if isinstance(value, datetime):
                    value = value.isoformat() + "Z"
                elif isinstance(value, (bytes, bytearray)):
                    value = "[binaire masqué]"
                elif hasattr(value, "isoformat") and callable(value.isoformat):
                    value = value.isoformat()
                item[str(key)] = value
            data.append(item)
        db.rollback()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Erreur SQL : " + str(exc)[:350])
    audit_db = SessionLocal()
    try:
        _crypt_audit(audit_db, "database_read_query", None, {"query_sha256": query_hash, "rows": len(data)})
        audit_db.commit()
    finally:
        audit_db.close()
    return {
        "status": True,
        "columns": [column for column in columns if str(column).lower() not in hidden_columns],
        "rows": data,
        "truncated": truncated,
        "row_limit": 100,
    }


@app.post("/api/telemetry/events")
def crypt_telemetry_event(payload: dict, request: Request, db: Session = Depends(get_db)):
    remote = request.client.host if request.client else "unknown"
    limiter_key = "crypt-telemetry:" + remote
    if CRYPT_TELEMETRY_LIMITER.is_blocked(limiter_key):
        raise HTTPException(status_code=429, detail="Trop d'événements envoyés.")
    CRYPT_TELEMETRY_LIMITER.record(limiter_key)
    app_name = str(request.headers.get("x-crypt-app") or payload.get("app") or "unknown").lower()
    if app_name not in ("primeink", "classink", "suivi", "manager"):
        raise HTTPException(status_code=422, detail="Application inconnue.")
    device_id = str(request.headers.get("x-crypt-device") or payload.get("device_id") or "")[:100]
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", device_id):
        raise HTTPException(status_code=422, detail="Identifiant de session appareil invalide.")
    authorization = request.headers.get("authorization", "")
    claims = verify_token(authorization[7:].strip()) if authorization.lower().startswith("bearer ") else None
    claims = claims or {}
    school_id = str(claims.get("school_id") or request.headers.get("x-crypt-school") or "")[:100] or None
    account = str(claims.get("email") or school_id or "")[:180] or None
    message = clean_text(payload.get("message"), 700)
    feature = clean_text(payload.get("feature"), 160)
    event_type = str(payload.get("event_type") or "feature").lower()
    if event_type not in ("feature", "client_error", "sync_error", "login_error"):
        event_type = "feature"
    device = _crypt_touch_public_device(db, request, app_name, device_id)
    school_id = school_id or (device.school_id if device else None)
    account = account or (device.account_identifier if device else None)
    db.add(CryptTelemetryEvent(
        device_id=device_id, app=app_name, school_id=school_id, account_identifier=account,
        event_type=event_type, feature=feature, message=message,
        details=payload.get("details") if isinstance(payload.get("details"), dict) else {},
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return {"status": True}


@app.post("/api/telemetry/complaints")
def crypt_telemetry_complaint(payload: dict, request: Request, db: Session = Depends(get_db)):
    remote = request.client.host if request.client else "unknown"
    limiter_key = "crypt-ticket:" + remote
    if CRYPT_TICKET_LIMITER.is_blocked(limiter_key):
        raise HTTPException(status_code=429, detail="Trop de réclamations depuis cette connexion. Réessayez plus tard.")
    app_name = str(request.headers.get("x-crypt-app") or payload.get("app") or "unknown").lower()
    if app_name not in ("primeink", "classink", "suivi", "manager"):
        raise HTTPException(status_code=422, detail="Application inconnue.")
    message = clean_text(payload.get("message"), 5000)
    subject = clean_text(payload.get("subject"), 180)
    if len(message) < 10 or not subject:
        raise HTTPException(status_code=422, detail="Ajoutez un sujet et un message suffisamment détaillé.")
    CRYPT_TICKET_LIMITER.record(limiter_key)
    device_id = str(request.headers.get("x-crypt-device") or payload.get("device_id") or "")[:100]
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,100}", device_id):
        device_id = None
    device = _crypt_touch_public_device(db, request, app_name, device_id) if device_id else None
    claims = {}
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        claims = verify_token(authorization[7:].strip()) or {}
    ticket = CryptSupportTicket(
        app=app_name, device_id=device_id,
        school_id=str(claims.get("school_id") or request.headers.get("x-crypt-school") or (device.school_id if device else "") or "")[:100] or None,
        account_identifier=str(claims.get("email") or claims.get("school_id") or (device.account_identifier if device else "") or "")[:180] or None,
        contact=clean_text(payload.get("contact"), 180),
        category=clean_text(payload.get("category") or "support", 60),
        subject=subject, message=message, status="open",
    )
    db.add(ticket)
    db.add(CryptTelemetryEvent(
        device_id=device_id, app=app_name, school_id=ticket.school_id,
        account_identifier=ticket.account_identifier, event_type="complaint",
        feature=ticket.category, message=ticket.subject, details={"ticket_id": ticket.id},
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return {"status": True, "ticket_id": ticket.id, "message": "Réclamation transmise à CRYPT."}


@app.post("/api/llink/assist")
async def classink_llink_assist(payload: dict, request: Request):
    """Relais Llink limité à VELORA ; aucune donnée scolaire n'est ajoutée au contexte."""
    app_name = str(request.headers.get("x-crypt-app") or "").strip().lower()
    if app_name not in ("suivi", "manager", "crypt"):
        raise HTTPException(status_code=403, detail="Application non autorisée pour Llink.")

    authorization = request.headers.get("authorization", "")
    claims = verify_token(authorization[7:].strip()) if authorization.lower().startswith("bearer ") else None
    claims = claims or {}
    if app_name == "manager" and claims.get("role") != "school":
        raise HTTPException(status_code=401, detail="Connectez-vous à Manager avant d'utiliser Llink.")
    if app_name == "crypt" and claims.get("role") != "crypt_admin":
        raise HTTPException(status_code=403, detail="Session CRYPT administrateur requise.")

    remote = request.client.host if request.client else "unknown"
    limiter_key = "llink-assist:" + remote
    if CRYPT_LLINK_LIMITER.is_blocked(limiter_key):
        raise HTTPException(status_code=429, detail="Limite Llink atteinte. Réessayez dans quelques minutes.")

    message = str(payload.get("message") or "").strip()
    if not message or len(message) > 5000:
        raise HTTPException(status_code=422, detail="Votre question doit contenir entre 1 et 5 000 caractères.")

    history = []
    raw_history = payload.get("history")
    if isinstance(raw_history, list):
        for turn in raw_history[-8:]:
            if not isinstance(turn, dict):
                continue
            role = str(turn.get("role") or "").strip().lower()
            text_value = turn.get("text")
            if role not in ("user", "assistant") or not isinstance(text_value, str):
                continue
            text_value = text_value.strip()[-2500:]
            if text_value:
                history.append({"role": role, "text": text_value})
    if sum(len(item["text"]) for item in history) > 12000:
        history = history[-4:]

    context_by_app = {
        "suivi": "Tu es Llink, l'assistant d'aide de Class-Ink Suivi, destiné aux parents. Explique simplement l'utilisation de l'application et les notions générales. Tu ne reçois aucun dossier d'élève, aucune note et aucune identité. Ne prétends jamais consulter le compte du parent. Réponds en français sauf si la personne écrit dans une autre langue.",
        "manager": "Tu es Llink, l'assistant d'aide de ClassInk Manager, destiné aux responsables d'établissement. Explique la consultation des classes, élèves, indicateurs, accès Suivi et horaires. Tu ne reçois aucune donnée de l'école ni aucun dossier. Ne prétends pas voir l'écran ou exécuter une action.",
        "crypt": "Tu es Llink, l'assistant du centre de contrôle CRYPT. Aide à comprendre les fonctions de supervision et à diagnostiquer des problèmes généraux. Tu ne reçois ni les données du tableau de bord, ni les journaux, ni les réclamations, ni le contenu de la base. N'invente aucune observation.",
    }
    request_payload = {
        "message": message,
        "history": history,
        "model": "velora",
        "level": "velora",
        "effort": "normal",
        "mode": "chat",
        "source": "classink_" + app_name,
        "preferences": context_by_app[app_name] + " Les données scolaires et personnelles ne doivent jamais être demandées ni supposées. Ne demande pas à l'utilisateur de coller de notes, noms, identifiants ou informations confidentielles.",
        "data_collection_consent": False,
    }
    CRYPT_LLINK_LIMITER.record(limiter_key)

    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=12.0, read=90.0, write=20.0, pool=12.0))
    url = os.getenv("LLINK_API_BASE_URL", "https://llink-usz9.onrender.com").rstrip("/") + "/api/chat"
    try:
        upstream = await client.send(
            client.build_request(
                "POST", url, json=request_payload,
                headers={"Accept": "text/event-stream", "Cache-Control": "no-cache"},
            ),
            stream=True,
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=503, detail="Le serveur Llink est injoignable. Réessayez.") from exc

    if upstream.status_code >= 400:
        status_code = upstream.status_code if upstream.status_code in (401, 403, 422, 429, 503) else 502
        raw_error = (await upstream.aread()).decode("utf-8", "replace")[:500]
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=status_code, detail="Llink : " + (raw_error or "service indisponible"))

    async def relay_llink_stream():
        try:
            async for chunk in upstream.aiter_raw():
                if chunk:
                    yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        relay_llink_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )



@app.get("/api/cloud/{school_id}/status")
def cloud_school_status(school_id: str, db: Session = Depends(get_db)):
    """État public minimal d'une école, consulté par PrimeInk : suspendue ou licence expirée ?"""
    st = licence_state(db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first())
    return {"locked": st["locked"], "reason": st["reason"], "licence_date": st["licence_date"]}


@app.post("/api/admin/codes/verify")
def verify_and_cycle_code(data: CodeVerifySchema, background_tasks: BackgroundTasks, request: Request, db: Session = Depends(get_db)):
    """Vérifie, détruit, génère un nouveau et renvoie expiration ou échec."""
    client_ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                 or (request.client.host if request.client else "inconnu"))
    limiter_key = "code:" + client_ip
    if CODE_LIMITER.is_blocked(limiter_key):
        return JSONResponse(status_code=429, content={"status": False, "message": "Trop de codes incorrects. Réessayez dans 15 minutes."})
    code_record = db.query(AccessCode).filter(AccessCode.code == data.code).first()
    if not code_record:
        CODE_LIMITER.record(limiter_key)
        return {"status": False, "code": "0000"}
    CODE_LIMITER.reset(limiter_key)
    
    db.delete(code_record)
    new_code = generate_20_char_code()
    db.add(AccessCode(code=new_code))

    # Renouvellement de la licence : durée configurable via la variable d'environnement LICENCE_DAYS
    try:
        licence_days = int(os.getenv("LICENCE_DAYS", "365"))
    except ValueError:
        licence_days = 365
    expiration_date = (datetime.utcnow() + timedelta(days=licence_days)).strftime("%Y-%m-%d")
    target_school_id = data.school_id or data.identifier
    if target_school_id:
        school_row = db.query(SchoolInformation).filter(SchoolInformation.school_id == target_school_id).first()
        if school_row:
            school_row.licence_date = expiration_date
    db.commit()
    
    background_tasks.add_task(send_email_mock, ADMIN_EMAIL, "Renouvellement Code ClassNet", f"Utilisateur {data.identifier or data.school_id} a consommé un code. Nouveau code actif : {new_code}")
    
    return {
        "status": True, 
        "message": "Code valide et renouvelé.",
        "expiration_date": expiration_date
    }

# ---------------------------------------------------------
# 8. SYNCHRONISATION ET MISE À JOUR (APP & PRIMENET)
# ---------------------------------------------------------
def restore_classink_teacher_state(
    snapshot_data: Any,
    legacy_state: dict,
    teacher: Any,
    entered_password: str,
) -> dict:
    """Restaure l'instantané complet ; retombe sur l'ancien format si aucun instantané n'existe."""
    if not isinstance(snapshot_data, dict):
        return legacy_state
    restored = dict(snapshot_data)
    user = dict(restored.get("user") or {})
    user.update({
        "id": getattr(teacher, "teacher_code", None) or f"prof_{teacher.id}",
        "name": teacher.full_name,
        "email": teacher.email,
        "password": entered_password,
        "school": teacher.school_name or "Indépendant",
        "isLoggedIn": True,
    })
    restored["user"] = user
    restored["pendingCommits"] = 0
    return restored


def build_classink_teacher_snapshot(payload: dict, teacher_email: str) -> dict:
    """Construit une copie JSON sûre de l'état ClassInk, sans mot de passe ni compteurs obsolètes."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="La sauvegarde ClassInk doit être un objet JSON.")
    try:
        snapshot = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="La sauvegarde contient des données non JSON ou des nombres invalides.") from exc

    user = snapshot.get("user")
    if not isinstance(user, dict):
        raise HTTPException(status_code=422, detail="Le profil de l'enseignant manque dans la sauvegarde.")
    if str(user.get("email") or "").strip().lower() != teacher_email:
        raise HTTPException(status_code=403, detail="Le profil de la sauvegarde ne correspond pas à la session.")

    user.pop("password", None)
    user["email"] = teacher_email
    user["isLoggedIn"] = True
    snapshot["user"] = user
    snapshot["pendingCommits"] = 0

    serialized = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(serialized.encode("utf-8")) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="La sauvegarde ClassInk dépasse la taille maximale de 8 Mo.")
    return snapshot



@app.post("/api/sync/classnet-app")
def sync_classnet_app(payload: dict, db: Session = Depends(get_db), claims: dict = Depends(require_teacher)):
    """
    Synchronise la DB ClassNet App avec contrôles complets des données :
    - Vérification des noms d'élèves et appartenance aux classes
    - Validation des noms de cours et des maxima (maxScore)
    - Contrôle des notes (intervalle 0 <= note <= max_score)
    """
    user_data = payload.get("user", {})
    email = str(user_data.get("email") or "").strip().lower()  # même normalisation que la connexion
    if email != claims.get("email"):
        raise HTTPException(status_code=403, detail="Ce jeton ne correspond pas à l'enseignant indiqué dans les données.")
    
    teacher = db.query(Teacher).filter(Teacher.email == email).first()
    if not teacher:
        return {"status": False, "message": "Enseignant non trouvé."}
    blocked = teacher_school_block(db, teacher)
    if blocked:
        raise HTTPException(status_code=403, detail=blocked)

    snapshot = build_classink_teacher_snapshot(payload, teacher.email)

    students_dict = payload.get("students", {})
    courses_dict = payload.get("courses", {})
    evaluations_dict = payload.get("evaluations", {})
    grades_dict = payload.get("grades", {})

    # Signature de l'enseignant (photo prise dans ClassInk)
    if "signature" in user_data and clean_image(user_data.get("signature")):
        teacher.signature = clean_image(user_data.get("signature"))

    # Conduite : seulement pour la classe dont il se déclare titulaire, et acceptée si l'école confirme qu'il l'est
    conduct_report = None
    declared_class = str(user_data.get("titulaireOf") or "").strip()
    conduct_payload = payload.get("conduct") if isinstance(payload.get("conduct"), dict) else {}
    if declared_class and isinstance(conduct_payload.get(declared_class), dict):
        conduct_report = store_conduct(db, teacher, declared_class, students_dict.get(declared_class, []), conduct_payload[declared_class])

    # --- 1. Catalogue scolaire officiel et périmètre du professeur ---
    verified_school_id = teacher.school_id if teacher_has_verified_school_access(teacher) else None
    official_classes_by_name = {}
    assigned_courses_by_class = {}
    official_course_max = {}
    teacher_allowed_classes = set()

    if verified_school_id:
        official_classes = db.query(ClasseInformation).filter(
            ClasseInformation.school_id == verified_school_id
        ).all()
        official_classes_by_name = {
            normalize_name(item.class_name): item for item in official_classes
        }
        for official_class in official_classes:
            if teacher_is_titulaire(teacher, official_class):
                teacher_allowed_classes.add(str(official_class.class_name))
        official_courses = db.query(CourseInformation).filter(
            CourseInformation.school_id == verified_school_id
        ).all()
        classes_by_id = {str(item.id): item for item in official_classes}
        for course in official_courses:
            if not teacher_matches_catalog_name(teacher, course.titulaire_name):
                continue
            official_class = classes_by_id.get(str(course.class_id))
            if not official_class:
                continue
            class_name = str(official_class.class_name)
            teacher_allowed_classes.add(class_name)
            assigned_courses_by_class.setdefault(class_name, set()).add(str(course.course_name))
            official_course_max[(class_name, str(course.course_name))] = float(course.max_per or 40)

    # --- 2. Indexation des élèves réellement présents dans le catalogue autorisé ---
    student_lookup = {}  # { ClassInk student_id: {name, class, official_student_id} }
    for class_name, st_list in students_dict.items():
        if verified_school_id:
            official_class = official_classes_by_name.get(normalize_name(class_name))
            if not official_class or class_name not in teacher_allowed_classes:
                continue
        else:
            official_class = None
        for st in (st_list or []):
            if not isinstance(st, dict):
                continue
            st_id = str(st.get("id") or "").strip()
            st_name = str(st.get("name") or "").strip()
            if not st_id or not st_name:
                continue
            official_student_id = None
            if verified_school_id:
                official_student_id = resolve_student(
                    db, verified_school_id, official_class, teacher.email, st_id, st_name
                )
                if not official_student_id:
                    continue
            student_lookup[st_id] = {
                "name": st_name, "class": str(official_class.class_name) if official_class else class_name,
                "official_student_id": official_student_id,
            }

    # --- 3. Indexation des évaluations, limitée aux cours attribués ---
    eval_lookup = {}  # { eval_id: {name, max, course, period, class} }
    for class_name, periods in evaluations_dict.items():
        if verified_school_id and class_name not in teacher_allowed_classes:
            continue
        for period_key, ev_list in (periods or {}).items():
            if period_key not in PERIODS:
                continue
            for ev in (ev_list or []):
                if not isinstance(ev, dict):
                    continue
                ev_id = str(ev.get("id") or "").strip()
                ev_course = str(ev.get("course") or "").strip()
                ev_max = ev.get("max")
                if verified_school_id:
                    if ev_course not in assigned_courses_by_class.get(class_name, set()):
                        continue
                    catalog_max = official_course_max.get((class_name, ev_course))
                    if catalog_max is None:
                        continue
                else:
                    valid_courses = courses_dict.get(class_name, [])
                    if ev_course not in valid_courses:
                        continue
                    catalog_max = None

                if not ev_id or not isinstance(ev_max, (int, float)) or isinstance(ev_max, bool) or ev_max <= 0:
                    continue
                if catalog_max is not None and float(ev_max) > catalog_max:
                    continue
                eval_lookup[ev_id] = {
                    "name": str(ev.get("name") or "Évaluation")[:160],
                    "max": float(ev_max),
                    "course": ev_course,
                    "period": period_key,
                    "class": class_name,
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
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not (0 <= score <= eval_info["max"]):
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
    school_id = teacher.school_id if teacher_has_verified_school_access(teacher) else None

    # On ne génère le format PrimeNet que si l'enseignant a été autorisé par la liste officielle de l'école
    if school_id and str(school_id).lower() != "indépendant":
        # Construction des listes et dictionnaires de base
        classes_list = (
            [str(class_name) for class_name in students_dict.keys()
             if str(class_name) in teacher_allowed_classes
             and normalize_name(class_name) in official_classes_by_name]
            if verified_school_id else list(students_dict.keys())
        )
        effective_courses_dict = (
            {class_name: sorted(assigned_courses_by_class.get(class_name, set())) for class_name in classes_list}
            if verified_school_id else courses_dict
        )
        presences_dict = payload.get("presences", {})
        if isinstance(presences_dict, dict):
            dates_presence = list(presences_dict.keys())
        elif isinstance(presences_dict, list):
            # Si c'est une liste, on récupère les dates ou on laisse vide si la liste ne contient pas de clés
            dates_presence = [
                item.get("date") if isinstance(item, dict) else item 
                for item in presences_dict
            ]
        else:
            dates_presence = []
        
        # Formatage des élèves : { "classe": ["Nom Post-nom", ...] }
        formatted_students = {}
        for student_info in student_lookup.values():
            formatted_students.setdefault(student_info["class"], []).append(student_info["name"])
            
        # Construction de la moyenne classe selon ta structure
        moyenne_classe = {}
        for c_name in classes_list:
            moyenne_classe[c_name] = {}
            # On récupère les cours de cette classe
            c_courses = effective_courses_dict.get(c_name, [])
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
                    
                    if s_info and e_info and s_info["class"] == c_name and e_info["course"] == course and isinstance(score, (int, float)) and not isinstance(score, bool) and 0 <= score <= e_info["max"]:
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
            "cours": effective_courses_dict,
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

    # État complet : conserve les cours/classes/élèves sans aucune note.
    snapshot_row = db.query(ClassInkTeacherSnapshot).filter_by(teacher_email=teacher.email).first()
    if snapshot_row:
        snapshot_row.payload_data = snapshot
    else:
        db.add(ClassInkTeacherSnapshot(teacher_email=teacher.email, payload_data=snapshot))

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
        "warnings_or_errors": errors,
        "conduct": conduct_report
    }


# ---------------------------------------------------------
# ÉTAPE 4 : réglages PrimeInk conservés sur le serveur, signatures, conduite vérifiée
# ---------------------------------------------------------
IMAGE_DATAURL_RE = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=]+$")
MAX_IMAGE_CHARS = 450000
BULLETIN_MODELS = ("classink", "ige010", "repechage")
SIGNATURE_SOURCES = ("titulaire", "responsable", "default", "none")
PN_EXTRA_KEYS = ("signatureBook", "repechage", "printSettings", "behaviour", "managerSchedule")
PERIOD_TO_KEY = {"P1": "p1", "P2": "p2", "EX1": "ex1", "P3": "p3", "P4": "p4", "EX2": "ex2"}


def clean_image(value):
    """Renvoie l'image si c'est une image raster (data URL) valide et assez légère, sinon None."""
    if isinstance(value, str) and len(value) <= MAX_IMAGE_CHARS and IMAGE_DATAURL_RE.match(value):
        return value
    return None


def clean_text(value, max_len=200):
    return str(value).strip()[:max_len] if value is not None else ""


def clean_pn_extra(extra):
    """Ne garde que les réglages PrimeInk connus, avec des tailles raisonnables."""
    out = {}
    if not isinstance(extra, dict):
        return out
    book = extra.get("signatureBook")
    if isinstance(book, dict):
        out["signatureBook"] = {clean_text(k, 120): v for k, v in list(book.items())[:300] if clean_image(v)}
    for key in ("repechage", "behaviour"):
        value = extra.get(key)
        if isinstance(value, dict) and len(json.dumps(value)) < 2000000:
            out[key] = value
    settings = extra.get("printSettings")
    if isinstance(settings, dict):
        out["printSettings"] = {"paper": "A3" if settings.get("paper") == "A3" else "A4"}
    manager_schedule = extra.get("managerSchedule")
    if isinstance(manager_schedule, dict):
        try:
            if len(json.dumps(manager_schedule, ensure_ascii=False)) <= 1500000:
                out["managerSchedule"] = manager_schedule
        except (TypeError, ValueError):
            pass
    return out


def merge_pn_extra(school, extra):
    """Fusionne les réglages reçus avec ceux déjà enregistrés (les valeurs reçues l'emportent)."""
    if not extra:
        return
    merged = dict(school.pn_extra or {})
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    school.pn_extra = merged


def apply_school_settings(school, data, allow_clear=False):
    """Applique l'identité du bulletin. allow_clear=True : une valeur vide efface (sceau, signature, armoiries)."""
    if not isinstance(data, dict):
        return
    for attr, key, size in (("province", "province", 120), ("head_name", "headName", 160), ("school_year", "schoolYear", 30)):
        if key in data:
            setattr(school, attr, clean_text(data.get(key), size))
    for attr, key in (("seal", "seal"), ("head_signature", "headSignature"), ("armoiries", "armoiries")):
        if key not in data:
            continue
        value = data.get(key)
        if value in (None, ""):
            if allow_clear:
                setattr(school, attr, None)
        elif clean_image(value):
            setattr(school, attr, clean_image(value))


def school_state_extras(db, school):
    """Réglages à renvoyer à PrimeInk : identité du bulletin, carnets de signatures, conduite vérifiée."""
    extra = dict(school.pn_extra or {})
    behaviour = {k: v for k, v in (extra.get("behaviour") or {}).items()}
    for sid, kinds in build_verified_behaviour(db, school.school_id).items():
        entry = behaviour.setdefault(sid, {"app": {}, "cond": {}})
        for kind, periods in kinds.items():
            entry.setdefault(kind, {}).update(periods)
    out = {}
    for key in ("signatureBook", "repechage", "printSettings"):
        if key in extra:
            out[key] = extra[key]
    if behaviour:
        out["behaviour"] = behaviour
    if isinstance(extra.get("managerSchedule"), dict):
        out["managerSchedule"] = extra["managerSchedule"]
    return out


def teacher_has_verified_school_access(teacher) -> bool:
    """Vrai uniquement après rattachement du compte dans la liste officielle PrimeInk."""
    return bool(
        teacher
        and getattr(teacher, "school_id", None)
        and getattr(teacher, "school_access_verified", False)
    )


def teacher_name_identity_key(value: str) -> str:
    """Clé de nom enseignant tolérant titres de civilité et ordre des noms."""
    tokens = normalize_name(value or "").split()
    titles = {"prof", "professeur", "monsieur", "madame", "mme", "mr", "m", "dr", "docteur"}
    while tokens and tokens[0] in titles:
        tokens.pop(0)
    return " ".join(sorted(tokens))


def teacher_is_titulaire(teacher, classe) -> bool:
    """Vrai si nom complet, code unique ou e-mail du titulaire correspondent au compte."""
    titulaire_raw = str(classe.titulaire_name or "").strip()
    titulaire = normalize_name(titulaire_raw)
    if not titulaire:
        return False
    teacher_name = normalize_name(teacher.full_name or "")
    teacher_key = teacher_name_identity_key(teacher.full_name or "")
    titular_key = teacher_name_identity_key(titulaire_raw)
    if teacher_name and teacher_key and teacher_key == titular_key:
        return True
    candidates = {
        normalize_name(teacher.teacher_code or ""),
        normalize_name(teacher.email or ""),
    }
    candidates.discard("")
    return titulaire in candidates



def validate_school_class_catalog(payload: dict, expected_school_id: str) -> dict:
    """Valide le JSON de préparation avant toute écriture dans les tables de l'école."""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Le JSON de préparation doit être un objet.")
    school_data = payload.get("school") if isinstance(payload.get("school"), dict) else {}
    supplied_school_id = str(school_data.get("id") or school_data.get("school_id") or "").strip()
    if supplied_school_id != str(expected_school_id):
        raise HTTPException(status_code=403, detail="Ce jeton ne correspond pas à l'école indiquée dans les données.")

    raw_classes = payload.get("classes")
    raw_students = payload.get("students")
    raw_courses = payload.get("courses")
    raw_teachers = payload.get("teachers", [])
    if not isinstance(raw_classes, list) or not isinstance(raw_students, list) or not isinstance(raw_courses, list):
        raise HTTPException(status_code=422, detail="Les listes classes, students et courses sont obligatoires.")
    if not isinstance(raw_teachers, list):
        raise HTTPException(status_code=422, detail="La liste teachers doit être un tableau.")
    limits = ((raw_classes, 500, "classes"), (raw_students, 20000, "élèves"),
              (raw_courses, 12000, "cours"), (raw_teachers, 1500, "enseignants"))
    for values, limit, label in limits:
        if len(values) > limit:
            raise HTTPException(status_code=413, detail="Le nombre de " + label + " dépasse la limite autorisée.")

    classes, class_ids = [], set()
    for index, row in enumerate(raw_classes):
        if not isinstance(row, dict):
            raise HTTPException(status_code=422, detail=f"Classe invalide à la ligne {index + 1}.")
        class_id = clean_text(row.get("id"), 120)
        class_name = clean_text(row.get("name"), 160)
        if not class_id or not class_name:
            raise HTTPException(status_code=422, detail=f"Identifiant ou nom de classe manquant à la ligne {index + 1}.")
        if class_id in class_ids:
            raise HTTPException(status_code=422, detail="Identifiant de classe en double : " + class_id)
        class_ids.add(class_id)
        categories = row.get("categories") if isinstance(row.get("categories"), list) else []
        classes.append({
            "id": class_id,
            "name": class_name,
            "titulaire": clean_text(row.get("titulaire"), 160),
            "categories": [clean_text(value, 120) for value in categories[:100] if clean_text(value, 120)],
            "bulletinModel": row.get("bulletinModel") if row.get("bulletinModel") in BULLETIN_MODELS else None,
            "bulletinTitle": clean_text(row.get("bulletinTitle"), 160),
            "signatureSource": row.get("signatureSource") if row.get("signatureSource") in SIGNATURE_SOURCES else None,
        })

    students, student_ids = [], set()
    for index, row in enumerate(raw_students):
        if not isinstance(row, dict):
            raise HTTPException(status_code=422, detail=f"Élève invalide à la ligne {index + 1}.")
        student_id = clean_text(row.get("id"), 120)
        class_id = clean_text(row.get("classId"), 120)
        name = clean_text(row.get("name"), 120)
        postname = clean_text(row.get("postname"), 120)
        prename = clean_text(row.get("prename"), 120)
        if not student_id or class_id not in class_ids:
            raise HTTPException(status_code=422, detail=f"Identifiant ou classe inconnue pour l'élève à la ligne {index + 1}.")
        if not any((name, postname, prename)):
            raise HTTPException(status_code=422, detail=f"Nom d'élève manquant à la ligne {index + 1}.")
        if student_id in student_ids:
            raise HTTPException(status_code=422, detail="Identifiant d'élève en double : " + student_id)
        student_ids.add(student_id)
        sex = str(row.get("sexe") or "").strip().upper()
        students.append({
            "id": student_id, "classId": class_id, "name": name, "postname": postname,
            "prename": prename, "sexe": sex if sex in ("M", "F") else "",
            "bornDate": clean_text(row.get("bornDate"), 30),
            "bornWhere": clean_text(row.get("bornWhere"), 120),
            "permi": clean_text(row.get("permi"), 100),
        })

    courses, course_ids = [], set()
    for index, row in enumerate(raw_courses):
        if not isinstance(row, dict):
            raise HTTPException(status_code=422, detail=f"Cours invalide à la ligne {index + 1}.")
        course_id = clean_text(row.get("id"), 120)
        class_id = clean_text(row.get("classId"), 120)
        course_name = clean_text(row.get("name"), 160)
        if not course_id or class_id not in class_ids or not course_name:
            raise HTTPException(status_code=422, detail=f"Identifiant, classe ou nom de cours invalide à la ligne {index + 1}.")
        if course_id in course_ids:
            raise HTTPException(status_code=422, detail="Identifiant de cours en double : " + course_id)
        course_ids.add(course_id)
        try:
            max_per = float(row.get("maxPer", 40) or 40)
        except (TypeError, ValueError):
            max_per = 40.0
        if not (0 < max_per <= 100000):
            max_per = 40.0
        courses.append({
            "id": course_id, "classId": class_id, "name": course_name, "maxPer": max_per,
            "category": clean_text(row.get("category"), 120) or "Général",
            "titulaire": clean_text(row.get("titulaire"), 160),
            "subCategory": clean_text(row.get("subCategory"), 160),
        })

    teachers = []
    seen_teacher_ids = set()
    for row in raw_teachers:
        if not isinstance(row, dict):
            continue
        unique_id = clean_text(row.get("uniqueId"), 100)
        name = clean_text(row.get("name"), 160)
        if not name:
            continue
        if unique_id and unique_id in seen_teacher_ids:
            continue
        if unique_id:
            seen_teacher_ids.add(unique_id)
        teachers.append({
            "uniqueId": unique_id, "name": name,
            "subject": clean_text(row.get("subject"), 120),
            "status": clean_text(row.get("status"), 40) or "Actif",
        })

    clean_payload = {
        "school": {
            "id": str(expected_school_id),
            "name": clean_text(school_data.get("name"), 200),
            "code": clean_text(school_data.get("code"), 80),
            "city": clean_text(school_data.get("city"), 120),
            "commune": clean_text(school_data.get("commune"), 120),
        },
        "classes": classes, "students": students, "courses": courses, "teachers": teachers,
    }
    if len(json.dumps(clean_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 8 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Les informations des classes dépassent la taille maximale de 8 Mo.")
    return clean_payload


def teacher_matches_school_roster(teacher, roster_entry: dict) -> bool:
    """Le code unique communiqué à l'administration identifie le compte à rattacher."""
    unique_id = clean_text(roster_entry.get("uniqueId"), 100)
    return bool(
        unique_id
        and normalize_name(getattr(teacher, "teacher_code", "") or "") == normalize_name(unique_id)
    )


SUIVI_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

def generate_suivi_student_code() -> str:
    """Code parent lisible et non séquentiel : 75 bits d'entropie."""
    raw = "".join(secrets.choice(SUIVI_CODE_ALPHABET) for _ in range(15))
    return "CI-" + raw[:5] + "-" + raw[5:10] + "-" + raw[10:]


def ensure_suivi_student_code(db: Session, student: SchoolStudentInformation) -> str:
    """Attribue une seule fois un identifiant parent à l'élève et évite les collisions."""
    if student.suivi_code:
        return student.suivi_code
    for _ in range(12):
        code = generate_suivi_student_code()
        exists = db.query(SchoolStudentInformation.id).filter(
            SchoolStudentInformation.suivi_code == code
        ).first()
        if not exists:
            student.suivi_code = code
            return code
    raise HTTPException(status_code=503, detail="Impossible de générer un identifiant parent unique. Réessayez.")


def store_school_class_catalog(db: Session, school: SchoolInformation, catalog: dict) -> dict:
    """Met à jour les classes, élèves et cours sans toucher aux notes ni aux données locales des enseignants."""
    school_data = catalog["school"]
    for attr, key in (("name_school", "name"), ("code", "code"), ("city", "city"), ("commune", "commune")):
        if school_data.get(key):
            setattr(school, attr, school_data[key])

    class_rows = {}
    for item in catalog["classes"]:
        row = db.query(ClasseInformation).filter(ClasseInformation.id == item["id"]).first()
        if row and row.school_id != school.school_id:
            raise HTTPException(status_code=409, detail="L'identifiant de classe " + item["id"] + " appartient déjà à une autre école.")
        if not row:
            row = ClasseInformation(id=item["id"], school_id=school.school_id, class_name=item["name"])
            db.add(row)
        row.school_id = school.school_id
        row.class_name = item["name"]
        row.titulaire_name = item["titulaire"]
        row.domaines = item["categories"]
        if item["bulletinModel"]:
            row.bulletin_model = item["bulletinModel"]
        if item["bulletinTitle"]:
            row.bulletin_title = item["bulletinTitle"]
        if item["signatureSource"]:
            row.signature_source = item["signatureSource"]
        class_rows[item["id"]] = row
    db.flush()

    for item in catalog["students"]:
        row = db.query(SchoolStudentInformation).filter(
            SchoolStudentInformation.id == item["id"]
        ).first()
        if row and row.school_id != school.school_id:
            raise HTTPException(status_code=409, detail="L'identifiant d'élève " + item["id"] + " appartient déjà à une autre école.")
        if not row:
            row = SchoolStudentInformation(id=item["id"], school_id=school.school_id)
            db.add(row)
        permanent = item["permi"] or item["id"]
        collision = db.query(SchoolStudentInformation).filter(
            SchoolStudentInformation.student_n_permanent == permanent,
            SchoolStudentInformation.id != item["id"]
        ).first()
        if collision:
            permanent = (permanent[:70] + "_" + item["id"][:28])[:100]
        row.school_id = school.school_id
        row.class_id = item["classId"]
        row.student_name = item["name"] or ""
        row.student_post_name = item["postname"] or ""
        row.student_pre_name = item["prename"] or ""
        row.student_sexe = item["sexe"] or ""
        row.student_born_date = item["bornDate"] or ""
        row.student_born_place = item["bornWhere"] or ""
        row.student_n_permanent = permanent
        ensure_suivi_student_code(db, row)
    db.flush()

    for item in catalog["courses"]:
        row = db.query(CourseInformation).filter(CourseInformation.id == item["id"]).first()
        if row and row.school_id != school.school_id:
            raise HTTPException(status_code=409, detail="L'identifiant de cours " + item["id"] + " appartient déjà à une autre école.")
        if not row:
            row = CourseInformation(id=item["id"], school_id=school.school_id, course_name=item["name"])
            db.add(row)
        row.school_id = school.school_id
        row.class_id = item["classId"]
        row.course_name = item["name"]
        row.max_per = item["maxPer"]
        row.category = item["category"]
        row.titulaire_name = item["titulaire"]
        row.sub_category = item["subCategory"]
    db.flush()

    linked_teachers = set()
    for item in catalog["teachers"]:
        # Le code doit être celui du compte authentifié, communiqué à l'administration.
        # Une correspondance de nom seule n'est jamais suffisante pour ouvrir l'accès aux élèves.
        if not item["uniqueId"]:
            continue
        teacher = db.query(Teacher).filter(Teacher.teacher_code == item["uniqueId"]).first()
        if (
            not teacher
            or teacher.school_id not in (None, school.school_id)
            or not teacher_matches_school_roster(teacher, item)
        ):
            continue
        # Le nom réel du compte vient de Teacher, pas du libellé de pré-inscription PrimeInk.
        # Le bouton « Ajouter un enseignant » peut encore afficher un nom provisoire.
        teacher.school_id = school.school_id
        teacher.school_name = school.name_school
        teacher.school_access_verified = True
        if item["subject"] and item["subject"] != "Matière à attribuer":
            teacher.subject = item["subject"]
        if item["status"]:
            teacher.status = item["status"]
        linked_teachers.add(teacher.email)

    db.commit()
    return {
        "classes": len(catalog["classes"]),
        "students": len(catalog["students"]),
        "courses": len(catalog["courses"]),
        "linkedTeachers": len(linked_teachers),
    }


def teacher_matches_catalog_name(teacher, value: str) -> bool:
    """Compare un titulaire de cours au compte enseignant (nom, code unique ou e-mail)."""
    raw = str(value or "").strip()
    if not raw:
        return False
    normalized = normalize_name(raw)
    candidates = {
        normalize_name(getattr(teacher, "full_name", "") or ""),
        normalize_name(getattr(teacher, "teacher_code", "") or ""),
        normalize_name(getattr(teacher, "email", "") or ""),
    }
    if normalized in candidates:
        return True
    return teacher_name_identity_key(raw) == teacher_name_identity_key(getattr(teacher, "full_name", "") or "")


def compose_teacher_classroom_context(teacher, school, classes, students, courses) -> dict:
    """Construit le catalogue de l'école ; le statut de titulaire reste propre à chaque classe."""
    classes_by_id = {str(item.id): str(item.class_name) for item in classes}
    students_by_class, courses_by_class, course_max = {}, {}, {}
    for item in classes:
        students_by_class.setdefault(item.class_name, [])
        courses_by_class.setdefault(item.class_name, [])
    for item in students:
        class_name = classes_by_id.get(str(item.class_id))
        if not class_name:
            continue
        full_name = " ".join(part for part in (
            item.student_name or "", item.student_post_name or "", item.student_pre_name or ""
        ) if part).strip()
        students_by_class.setdefault(class_name, []).append({
            "id": item.id, "name": full_name,
            "postname": item.student_post_name or "", "prename": item.student_pre_name or "",
            "sexe": item.student_sexe or "", "classId": item.class_id,
        })
    for item in courses:
        class_name = classes_by_id.get(str(item.class_id))
        if not class_name:
            continue
        values = courses_by_class.setdefault(class_name, [])
        if item.course_name not in values:
            values.append(item.course_name)
        course_max[f"{class_name}_{item.course_name}"] = float(item.max_per or 40)

    # L'enseignant autorisé reçoit le catalogue des classes de son école.
    # En revanche, le droit de modifier la conduite n'est accordé que dans ses classes
    # de titulariat officielles, vérifiées par teacher_is_titulaire et store_conduct.
    titulaire_classes = [
        item.class_name for item in classes
        if teacher_is_titulaire(teacher, item)
    ]
    classroom_rows = []
    for item in classes:
        classroom_rows.append({
            "id": item.id,
            "name": item.class_name,
            "titulaire": item.titulaire_name or "",
            "isTitulaire": teacher_is_titulaire(teacher, item),
            "students": students_by_class.get(item.class_name, []),
            "courses": courses_by_class.get(item.class_name, []),
        })
    return {
        "schoolId": school.school_id if school else "",
        "schoolName": school.name_school if school else (teacher.school_name or ""),
        "classes": [item.class_name for item in classes],
        "students": students_by_class,
        "courses": courses_by_class,
        "courseMax": course_max,
        "titulaireClasses": titulaire_classes,
        "classrooms": classroom_rows,
    }


def apply_teacher_classroom_context(state: dict, context: dict) -> dict:
    """Fusionne le catalogue autorisé de l'école avec l'espace local sans effacer les notes."""
    state = dict(state or {})
    # Un enseignant indépendant/non autorisé garde son espace local, sans titulaire déclaré.
    if not context.get("schoolId"):
        user = dict(state.get("user") or {})
        user["schoolId"] = ""
        user["titulaireOf"] = ""
        user["titulaireClasses"] = []
        state["user"] = user
        state["titulaireClasses"] = []
        state["classrooms"] = []
        state["schoolId"] = ""
        return state

    state["classes"] = list(dict.fromkeys(
        [str(value) for value in (state.get("classes") or [])] +
        [str(value) for value in (context.get("classes") or [])]
    ))
    for key in ("courses", "students"):
        merged = dict(state.get(key) or {})
        for class_name, rows in (context.get(key) or {}).items():
            existing = list(merged.get(class_name) or [])
            seen = {str((item or {}).get("id")) if isinstance(item, dict) else str(item) for item in existing}
            for item in rows:
                item_id = str(item.get("id")) if isinstance(item, dict) else str(item)
                if item_id not in seen:
                    existing.append(item)
                    seen.add(item_id)
            merged[class_name] = existing
        state[key] = merged
    state["courseMax"] = {**(context.get("courseMax") or {}), **(state.get("courseMax") or {})}
    evaluations = dict(state.get("evaluations") or {})
    for class_name in context.get("classes") or []:
        evaluations.setdefault(class_name, {"P1": [], "P2": [], "EX1": [], "P3": [], "P4": [], "EX2": []})
    state["evaluations"] = evaluations
    state["titulaireClasses"] = list(context.get("titulaireClasses") or [])
    state["classrooms"] = list(context.get("classrooms") or [])
    state["schoolId"] = context.get("schoolId") or ""
    user = dict(state.get("user") or {})
    if context.get("schoolName"):
        user["school"] = context["schoolName"]
    user["schoolId"] = context.get("schoolId") or ""
    # Le rôle est fourni par le registre de l'école, jamais par une auto-déclaration locale.
    user["titulaireOf"] = (context.get("titulaireClasses") or [""])[0]
    user["titulaireClasses"] = list(context.get("titulaireClasses") or [])
    state["user"] = user
    return state

def build_teacher_classroom_context(db: Session, teacher) -> dict:
    if not teacher_has_verified_school_access(teacher):
        return compose_teacher_classroom_context(teacher, None, [], [], [])
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == teacher.school_id).first()
    if not school:
        return compose_teacher_classroom_context(teacher, None, [], [], [])
    classes = db.query(ClasseInformation).filter(
        ClasseInformation.school_id == school.school_id
    ).order_by(ClasseInformation.class_name.asc()).all()
    students = db.query(SchoolStudentInformation).filter(
        SchoolStudentInformation.school_id == school.school_id
    ).all()
    courses = db.query(CourseInformation).filter(
        CourseInformation.school_id == school.school_id
    ).all()
    return compose_teacher_classroom_context(teacher, school, classes, students, courses)


@app.post("/api/primenet/prepare-classes")
def prepare_classroom_information(
    payload: dict,
    db: Session = Depends(get_db),
    claims: dict = Depends(require_active_school),
):
    """PrimeInk publie le catalogue complet des classes, élèves, cours et titulaires."""
    school_id = str(claims.get("school_id") or "")
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="École introuvable.")
    try:
        catalog = validate_school_class_catalog(payload, school_id)
        counts = store_school_class_catalog(db, school, catalog)
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail="Impossible de préparer les informations des classes.") from exc
    return {
        "status": True,
        "message": (
            f"Informations publiées : {counts['classes']} classes, {counts['students']} élèves, "
            f"{counts['courses']} cours. {counts['linkedTeachers']} compte(s) enseignant(s) associé(s)."
        ),
        "counts": counts,
    }



@app.get("/api/teacher/classes")
def get_teacher_classroom_information(
    db: Session = Depends(get_db),
    claims: dict = Depends(require_teacher),
):
    teacher = db.query(Teacher).filter(Teacher.email == claims.get("email")).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Compte enseignant introuvable.")
    if not teacher_has_verified_school_access(teacher):
        raise HTTPException(status_code=403, detail="Votre compte n'est pas encore autorisé par l'école. Communiquez votre identifiant ClassInk à l'administration pour qu'elle l'ajoute dans PrimeInk.")
    blocked = teacher_school_block(db, teacher)
    if blocked:
        raise HTTPException(status_code=403, detail=blocked)
    context = build_teacher_classroom_context(db, teacher)
    return {"status": True, "data": context}

def is_suivi_results_allowed(db: Session, school_id: str, student: SchoolStudentInformation) -> bool:
    """L'accès parent exige le feu vert individuel ET l'autorisation des règles scolaires applicables."""
    if not bool(student.suivi_results_allowed):
        return False
    rules = db.query(GradeAccessRule).filter(
        GradeAccessRule.school_id == school_id,
        GradeAccessRule.audience == "suivi",
    ).all()
    return all(
        resolve_grade_access(rules, student.class_id, period, "suivi", default=True)
        for period in PERIODS
    )


def build_suivi_results_payload(db: Session, school: SchoolInformation, student: SchoolStudentInformation) -> dict:
    """Construit le suivi parent depuis les notes PrimeInk et les évaluations ClassInk."""
    classe = db.query(ClasseInformation).filter(
        ClasseInformation.id == student.class_id,
        ClasseInformation.school_id == school.school_id,
    ).first()
    courses = db.query(CourseInformation).filter(
        CourseInformation.school_id == school.school_id,
        CourseInformation.class_id == student.class_id,
    ).order_by(CourseInformation.course_name.asc()).all()

    # Notes semestrielles / périodes enregistrées par PrimeInk.
    official_rows = db.query(TeacherEvaluation).filter(
        TeacherEvaluation.school_id == school.school_id,
        TeacherEvaluation.student_n_permanent == (student.student_n_permanent or student.id),
    ).all()
    official_by_course = {str(row.course_id): row for row in official_rows}

    # Les notes détaillées ClassInk sont indexées par l'identifiant local transmis par l'enseignant.
    linked_ids = [student.id]
    links = db.query(StudentLink).filter(
        StudentLink.school_id == school.school_id,
        StudentLink.student_id == student.id,
    ).all()
    linked_ids.extend(link.classink_student_id for link in links if link.classink_student_id)
    teacher_emails = [
        email for (email,) in db.query(Teacher.email).filter(
            Teacher.school_id == school.school_id
        ).all()
    ]
    detailed_rows = []
    if linked_ids and teacher_emails:
        detailed_rows = db.query(StudentGrade).filter(
            StudentGrade.student_id.in_(list(set(linked_ids))),
            StudentGrade.teacher_email.in_(teacher_emails),
        ).order_by(StudentGrade.updated_at.asc()).all()

    details_by_course = {}
    for row in detailed_rows:
        key = normalize_name(row.course_name)
        details_by_course.setdefault(key, []).append({
            "period": row.period,
            "title": row.eval_name,
            "date": row.updated_at.isoformat() if row.updated_at else "",
            "max": float(row.max_score),
            "obtained": float(row.score),
            "content": "",
        })

    result_courses = []
    for course in courses:
        official = official_by_course.get(str(course.id))
        scores = {}
        if official:
            for period, attr in (
                ("P1", "p1"), ("P2", "p2"), ("EX1", "ex1"),
                ("P3", "p3"), ("P4", "p4"), ("EX2", "ex2"),
            ):
                value = getattr(official, attr, None)
                if value is not None:
                    scores[period] = float(value)
        # Les résultats par évaluation sont aussi affichés quand les notes viennent de ClassInk.
        evals = details_by_course.get(normalize_name(course.course_name), [])
        result_courses.append({
            "name": course.course_name,
            "category": course.category or "",
            "subCategory": course.sub_category or "",
            "maxPer": float(course.max_per or 0),
            "scores": scores,
            "evaluations": evals,
        })

    behaviour = {"app": {}, "cond": {}}
    conduct_rows = db.query(StudentConduct).filter(
        StudentConduct.school_id == school.school_id,
        StudentConduct.student_id == student.id,
    ).all()
    for row in conduct_rows:
        if row.kind in ("app", "cond"):
            behaviour[row.kind][row.period] = row.value

    return {
        "courses": result_courses,
        "totals": {},
        "behaviour": behaviour,
    }


@app.get("/api/suivi/schools")
def suivi_search_schools(q: str = "", db: Session = Depends(get_db)):
    """Recherche publique limitée des écoles inscrites à Class-Ink Suivi."""
    query = str(q or "").strip()
    if len(query) < 2:
        return {"status": True, "data": []}
    if len(query) > 100:
        raise HTTPException(status_code=422, detail="Recherche trop longue.")
    pattern = "%" + query.replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db.query(SchoolInformation).filter(
        SchoolInformation.is_locked.is_(False),
        (
            SchoolInformation.name_school.ilike(pattern)
            | SchoolInformation.city.ilike(pattern)
            | SchoolInformation.school_id.ilike(pattern)
            | SchoolInformation.code.ilike(pattern)
        ),
    ).order_by(SchoolInformation.name_school.asc()).limit(20).all()
    visible = [row for row in rows if not licence_state(row)["locked"]]
    return {
        "status": True,
        "data": [
            {"id": row.school_id, "name": row.name_school, "city": row.city or ""}
            for row in visible
        ],
    }


@app.post("/api/suivi/student")
def suivi_get_student(payload: dict, request: Request, db: Session = Depends(get_db)):
    """
    Vérifie le code parent et renvoie le dossier d'un seul élève.
    Les notes ne sont sérialisées que si l'école a explicitement autorisé leur affichage.
    """
    school_id = str(payload.get("school_id") or "").strip()
    code = str(payload.get("student_code") or "").strip().upper()
    if not school_id or not code or len(code) > 40:
        raise HTTPException(status_code=422, detail="École ou identifiant élève manquant.")

    remote = request.client.host if request.client else "unknown"
    rate_key = "suivi:" + remote + ":" + school_id
    if SUIVI_LOOKUP_LIMITER.is_blocked(rate_key):
        raise HTTPException(status_code=429, detail="Trop d'essais. Réessayez dans 15 minutes.")

    school = db.query(SchoolInformation).filter(
        SchoolInformation.school_id == school_id
    ).first()
    if not school or licence_state(school)["locked"]:
        SUIVI_LOOKUP_LIMITER.record(rate_key)
        raise HTTPException(status_code=404, detail="École ou élève introuvable.")

    student = db.query(SchoolStudentInformation).filter(
        SchoolStudentInformation.school_id == school_id,
        func.upper(SchoolStudentInformation.suivi_code) == code,
    ).first()
    if not student:
        SUIVI_LOOKUP_LIMITER.record(rate_key)
        # Même réponse pour un code inconnu et une école non disponible : pas d'énumération.
        raise HTTPException(status_code=404, detail="École ou élève introuvable.")

    SUIVI_LOOKUP_LIMITER.reset(rate_key)
    if not student.suivi_code:
        ensure_suivi_student_code(db, student)
        db.commit()

    results_allowed = is_suivi_results_allowed(db, school_id, student)

    classe = db.query(ClasseInformation).filter(
        ClasseInformation.id == student.class_id,
        ClasseInformation.school_id == school_id,
    ).first()

    if results_allowed:
        results = build_suivi_results_payload(db, school, student)
        access_reason = ""
    else:
        # Ne pas inclure les notes, évaluations, rangs ou conduite lorsqu'ils sont bloqués.
        results = None
        access_reason = "school"

    return {
        "status": True,
        "data": {
            "student": {
                "name": " ".join(part for part in (
                    student.student_name or "",
                    student.student_post_name or "",
                    student.student_pre_name or "",
                ) if part).strip(),
                "className": classe.class_name if classe else "",
                "year": school.school_year or "",
                "code": student.suivi_code,
            },
            "school": {
                "id": school.school_id,
                "name": school.name_school,
                "city": school.city or "",
                "phone": school.num_tel or "",
                "payInfo": "",
            },
            "fees": {
                "configured": False,
                "currency": "USD",
                "total": None,
                "paid": None,
                "minRequired": None,
                "tranches": [],
                "payments": [],
            },
            "results": results,
            "resultsAccess": {
                "allowed": results_allowed,
                "reason": access_reason,
                "message": (
                    "" if results_allowed else
                    "L'établissement n'a pas encore autorisé l'affichage des résultats de cet élève."
                ),
            },
            "communications": [],
            "teacherNotes": [],
            "updatedAt": datetime.utcnow().isoformat() + "Z",
        },
    }


@app.get("/api/suivi/school/classes/{class_id}/students")
def suivi_school_class_students(
    class_id: str,
    db: Session = Depends(get_db),
    claims: dict = Depends(require_active_school),
):
    """Liste sécurisée des élèves d'une classe, avec leur code parent et l'état d'accès."""
    school_id = str(claims.get("school_id") or "")
    classe = db.query(ClasseInformation).filter(
        ClasseInformation.id == class_id,
        ClasseInformation.school_id == school_id,
    ).first()
    if not classe:
        raise HTTPException(status_code=404, detail="Classe introuvable dans cette école.")

    rows = db.query(SchoolStudentInformation).filter(
        SchoolStudentInformation.school_id == school_id,
        SchoolStudentInformation.class_id == class_id,
    ).order_by(
        SchoolStudentInformation.student_name.asc(),
        SchoolStudentInformation.student_post_name.asc(),
        SchoolStudentInformation.student_pre_name.asc(),
    ).all()

    students = []
    for row in rows:
        code = ensure_suivi_student_code(db, row)
        students.append({
            "id": row.id,
            "code": code,
            "name": " ".join(part for part in (
                row.student_name or "", row.student_post_name or "", row.student_pre_name or ""
            ) if part).strip(),
            "classId": row.class_id,
            "resultsAllowed": is_suivi_results_allowed(db, school_id, row),
            "individualAccessAllowed": bool(row.suivi_results_allowed),
        })
    db.commit()
    return {
        "status": True,
        "data": {
            "classId": classe.id,
            "className": classe.class_name,
            "students": students,
        },
    }


@app.put("/api/suivi/school/students/{student_id}/access")
def suivi_school_set_student_access(
    student_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    claims: dict = Depends(require_active_school),
):
    """L'administration de l'école seule peut bloquer ou autoriser les résultats d'un élève."""
    if not isinstance(payload.get("is_allowed"), bool):
        raise HTTPException(status_code=422, detail="is_allowed doit être un booléen.")
    school_id = str(claims.get("school_id") or "")
    student = db.query(SchoolStudentInformation).filter(
        SchoolStudentInformation.id == student_id,
        SchoolStudentInformation.school_id == school_id,
    ).first()
    if not student:
        raise HTTPException(status_code=404, detail="Élève introuvable dans cette école.")

    ensure_suivi_student_code(db, student)
    student.suivi_results_allowed = payload["is_allowed"]
    student.suivi_results_updated_at = datetime.utcnow()
    student.suivi_results_updated_by = school_id
    db.commit()
    effective_allowed = is_suivi_results_allowed(db, school_id, student)
    return {
        "status": True,
        "message": (
            "Les résultats sont maintenant visibles dans Class-Ink Suivi."
            if effective_allowed else
            "Le choix individuel est enregistré, mais une règle scolaire bloque encore l'affichage des résultats."
            if student.suivi_results_allowed else
            "Les résultats sont maintenant bloqués dans Class-Ink Suivi."
        ),
        "data": {
            "studentId": student.id,
            "studentCode": student.suivi_code,
            "resultsAllowed": effective_allowed,
            "individualAccessAllowed": bool(student.suivi_results_allowed),
        },
    }


def find_class_by_name(db, school_id, class_name):
    wanted = normalize_name(class_name)
    for c in db.query(ClasseInformation).filter(ClasseInformation.school_id == school_id).all():
        if normalize_name(c.class_name) == wanted:
            return c
    return None


def resolve_student(db, school_id, classe, teacher_email, classink_student_id, classink_student_name):
    """Retrouve l'élève de l'école correspondant à un élève de ClassInk (lien enregistré, sinon rapprochement par nom)."""
    link = db.query(StudentLink).filter(
        StudentLink.school_id == school_id, StudentLink.teacher_email == teacher_email,
        StudentLink.classink_student_id == classink_student_id).first()
    if link:
        # Une ancienne correspondance ne doit jamais permettre d'écrire la conduite
        # sur un élève d'une autre classe, même si le professeur est titulaire ici.
        linked_student = db.query(SchoolStudentInformation).filter(
            SchoolStudentInformation.id == link.student_id,
            SchoolStudentInformation.school_id == school_id,
            SchoolStudentInformation.class_id == classe.id,
        ).first()
        return linked_student.id if linked_student else None

    # Dans certains imports, l'id ClassInk est identique à l'id PrimeInk.
    direct_student = db.query(SchoolStudentInformation).filter(
        SchoolStudentInformation.id == classink_student_id,
        SchoolStudentInformation.school_id == school_id,
        SchoolStudentInformation.class_id == classe.id,
    ).first()
    if direct_student:
        return direct_student.id

    wanted = name_tokens_key(classink_student_name)
    if not wanted:
        return None
    matches = []
    for s in db.query(SchoolStudentInformation).filter(
            SchoolStudentInformation.school_id == school_id, SchoolStudentInformation.class_id == classe.id).all():
        full = name_tokens_key(" ".join(x for x in (s.student_name, s.student_post_name, s.student_pre_name) if x))
        short = name_tokens_key(" ".join(x for x in (s.student_name, s.student_post_name) if x))
        if wanted in (full, short):
            matches.append(s)
    if len(matches) == 1:
        db.add(StudentLink(school_id=school_id, teacher_email=teacher_email, classink_student_id=classink_student_id,
                           student_id=matches[0].id, match_method="name"))
        return matches[0].id
    return None


def store_conduct(db, teacher, class_name, students, conduct_by_student):
    """Enregistre la conduite d'une classe, SEULEMENT si l'enseignant est le titulaire enregistré de cette classe."""
    report = {"accepted": 0, "rejected": []}
    if not teacher_has_verified_school_access(teacher):
        report["rejected"].append({"reason": "Compte enseignant non autorisé par l'école. Communiquez votre identifiant ClassInk à l'administration."})
        return report
    classe = find_class_by_name(db, teacher.school_id, class_name)
    if not classe:
        report["rejected"].append({"reason": "Classe introuvable dans PrimeInk : « " + clean_text(class_name, 80) + " »."})
        return report
    if not teacher_is_titulaire(teacher, classe):
        report["rejected"].append({"reason": "Vous n'êtes pas le titulaire enregistré de cette classe."})
        return report
    names = {str(s.get("id")): str(s.get("name", "")) for s in (students or []) if isinstance(s, dict)}
    for classink_id, kinds in (conduct_by_student or {}).items():
        if not isinstance(kinds, dict):
            continue
        student_id = resolve_student(db, teacher.school_id, classe, teacher.email, str(classink_id), names.get(str(classink_id), ""))
        if not student_id:
            report["rejected"].append({"studentId": str(classink_id), "reason": "Élève introuvable dans PrimeInk."})
            continue
        for kind in CONDUCT_KINDS:
            for period, value in (kinds.get(kind) or {}).items():
                if period not in PERIODS or validate_conduct_value(value):
                    report["rejected"].append({"studentId": str(classink_id), "reason": "Valeur ou période invalide (" + clean_text(period, 10) + ")."})
                    continue
                clean_value = value.strip().upper()[:CONDUCT_MAX_LEN]
                row = db.query(StudentConduct).filter(
                    StudentConduct.school_id == teacher.school_id, StudentConduct.student_id == student_id,
                    StudentConduct.kind == kind, StudentConduct.period == period).first()
                if row:
                    row.value = clean_value
                    row.updated_by = teacher.email
                else:
                    db.add(StudentConduct(school_id=teacher.school_id, student_id=student_id, kind=kind, period=period,
                                          value=clean_value, updated_by=teacher.email))
                report["accepted"] += 1
    db.commit()
    return report


def build_verified_behaviour(db, school_id):
    """Conduite déjà vérifiée (venant des titulaires), au format PrimeInk : { élève: { app: { p1: 'E' }, cond: {...} } }."""
    out = {}
    for row in db.query(StudentConduct).filter(StudentConduct.school_id == school_id).all():
        key = PERIOD_TO_KEY.get(row.period)
        if key and row.kind in CONDUCT_KINDS:
            out.setdefault(row.student_id, {"app": {}, "cond": {}})[row.kind][key] = row.value
    return out


def build_signature_map(db, school_id):
    """Signatures des enseignants de l'école, par identifiant PrimeInk (teacher_code)."""
    out = {}
    for t in db.query(Teacher).filter(Teacher.school_id == school_id).all():
        img = clean_image(t.signature)
        if img:
            out[t.teacher_code or f"prof_{t.id}"] = img
    return out


@app.put("/api/school/settings")
def update_school_settings(payload: dict, db: Session = Depends(get_db), claims: dict = Depends(require_active_school)):
    """Identité du bulletin : province, année scolaire, chef d'établissement, sceau, signature, armoiries."""
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == claims.get("school_id")).first()
    if not school:
        return {"status": False, "message": "École introuvable."}
    for key in ("seal", "headSignature", "armoiries"):
        if payload.get(key) not in (None, "") and not clean_image(payload.get(key)):
            return {"status": False, "message": "Image invalide ou trop lourde (" + key + ")."}
    apply_school_settings(school, payload, allow_clear=True)
    merge_pn_extra(school, clean_pn_extra(payload))
    db.commit()
    return {"status": True, "message": "Réglages de l'école enregistrés."}


@app.put("/api/classes/{class_id}/bulletin")
def update_class_bulletin(class_id: str, payload: dict, db: Session = Depends(get_db), claims: dict = Depends(require_active_school)):
    """Type de bulletin d'une classe, intitulé et choix de la signature."""
    classe = db.query(ClasseInformation).filter(ClasseInformation.id == class_id, ClasseInformation.school_id == claims.get("school_id")).first()
    if not classe:
        raise HTTPException(status_code=404, detail="Classe introuvable.")
    model = payload.get("bulletinModel")
    if model is not None:
        if model not in BULLETIN_MODELS:
            return {"status": False, "message": "Type de bulletin inconnu."}
        classe.bulletin_model = model
    source = payload.get("signatureSource")
    if source is not None:
        if source not in SIGNATURE_SOURCES:
            return {"status": False, "message": "Choix de signature inconnu."}
        classe.signature_source = source
    if "bulletinTitle" in payload:
        classe.bulletin_title = clean_text(payload.get("bulletinTitle"), 160)
    db.commit()
    return {"status": True, "message": "Bulletin de la classe enregistré."}


@app.put("/api/teacher/signature")
def update_teacher_signature(payload: dict, db: Session = Depends(get_db), claims: dict = Depends(require_teacher)):
    """Photo de signature de l'enseignant (prise dans ClassInk). signature = null pour la supprimer."""
    teacher = db.query(Teacher).filter(Teacher.email == claims.get("email")).first()
    if not teacher:
        return {"status": False, "message": "Enseignant non trouvé."}
    value = payload.get("signature")
    if value in (None, ""):
        teacher.signature = None
    elif clean_image(value):
        teacher.signature = clean_image(value)
    else:
        return {"status": False, "message": "Image invalide ou trop lourde."}
    db.commit()
    return {"status": True, "message": "Signature enregistrée."}


@app.post("/api/conduct/submit")
def submit_conduct(payload: dict, db: Session = Depends(get_db), claims: dict = Depends(require_teacher)):
    """Conduite d'une classe pour une période. Refusée si l'enseignant n'est pas le titulaire de la classe."""
    teacher = db.query(Teacher).filter(Teacher.email == claims.get("email")).first()
    if not teacher:
        return {"status": False, "message": "Enseignant non trouvé."}
    if str(payload.get("teacherEmail") or "").strip().lower() != claims.get("email"):
        raise HTTPException(status_code=403, detail="Ce jeton ne correspond pas à l'enseignant indiqué.")
    blocked = teacher_school_block(db, teacher)
    if blocked:
        raise HTTPException(status_code=403, detail=blocked)
    period = payload.get("period")
    entries = payload.get("entries")
    if period not in PERIODS or not isinstance(entries, list):
        return {"status": False, "message": "Période ou liste d'élèves invalide."}
    conduct, students = {}, []
    for e in entries[:2000]:
        if not isinstance(e, dict) or not e.get("studentId"):
            continue
        students.append({"id": e["studentId"], "name": e.get("studentName", "")})
        kinds = {"app": {}, "cond": {}}
        for kind, field in (("app", "app"), ("cond", "cond")):
            if str(e.get(field) or "").strip():
                kinds[kind][period] = e.get(field)
        conduct[str(e["studentId"])] = kinds
    report = store_conduct(db, teacher, str(payload.get("className") or ""), students, conduct)
    return {"status": report["accepted"] > 0 or not report["rejected"], "accepted": report["accepted"],
            "rejected": report["rejected"], "message": "" if report["accepted"] else "Aucune conduite acceptée."}


@app.post("/api/sync/primenet")
def sync_primenet(payload: dict, db: Session = Depends(get_db), claims: dict = Depends(require_active_school)):
    """Reçoit la DB PrimeNet (ClassNet P) et met à jour l'ensemble des données de l'école."""
    school_data = payload.get("school", {})
    # Récupère 'id' ou 'school_id' pour éviter les incompatibilités de nom de clé
    school_id = school_data.get("id") or school_data.get("school_id")
    if school_id != claims.get("school_id"):
        raise HTTPException(status_code=403, detail="Ce jeton ne correspond pas à l'école indiquée dans les données.")
    
    school = db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first()
    if not school:
        return {"status": False, "message": f"École non trouvée (ID transmis : {school_id})."}

    # 1. Mise à jour des informations générales
    if school_data.get("name"): school.name_school = school_data.get("name")
    if school_data.get("code"): school.code = school_data.get("code")
    if school_data.get("city"): school.city = school_data.get("city")
    if school_data.get("commune"): school.commune = school_data.get("commune")
    apply_school_settings(school, school_data)

    # 2. Synchronisation des classes
    classes_payload = payload.get("classes", [])
    for c in classes_payload:
        c_id = c.get("id")
        if not c_id: continue
        
        classe_obj = db.query(ClasseInformation).filter(
            ClasseInformation.id == c_id, 
            ClasseInformation.school_id == school_id
        ).first()
        
        if not classe_obj:
            classe_obj = ClasseInformation(id=c_id, school_id=school_id)
            db.add(classe_obj)
            
        classe_obj.class_name = c.get("name", "")
        classe_obj.titulaire_name = c.get("titulaire", "")
        classe_obj.domaines = c.get("categories", [])
        if c.get("bulletinModel") in BULLETIN_MODELS: classe_obj.bulletin_model = c.get("bulletinModel")
        if "bulletinTitle" in c: classe_obj.bulletin_title = clean_text(c.get("bulletinTitle"), 160)
        if c.get("signatureSource") in SIGNATURE_SOURCES: classe_obj.signature_source = c.get("signatureSource")

    # 3. Synchronisation des cours
    courses_payload = payload.get("courses", [])
    for crs in courses_payload:
        crs_id = crs.get("id")
        if not crs_id: continue
        
        course_obj = db.query(CourseInformation).filter(
            CourseInformation.id == crs_id, 
            CourseInformation.school_id == school_id
        ).first()
        
        if not course_obj:
            course_obj = CourseInformation(id=crs_id, school_id=school_id)
            db.add(course_obj)
            
        course_obj.course_name = crs.get("name", "")
        course_obj.max_per = float(crs.get("maxPer", 40.0))
        course_obj.category = crs.get("category", "")
        course_obj.titulaire_name = crs.get("titulaire", "")
        course_obj.class_id = crs.get("classId")
        course_obj.sub_category = clean_text(crs.get("subCategory"), 160)

    # 4. Synchronisation des élèves
# 4. Synchronisation des élèves (sécurisée contre les doublons de numéros permanents)
    students_payload = payload.get("students", [])
    seen_permanents = set()
    
    for st in students_payload:
        st_id = st.get("id")
        if not st_id: continue
        
        permi = str(st.get("permi") or "").strip() or st_id
        # Si le numéro permanent existe déjà dans ce lot de données, on le rend unique pour éviter la collision
        if permi in seen_permanents:
            permi = f"{permi}_{st_id}"
        seen_permanents.add(permi)

        # Recherche par ID d'élève ou par numéro permanent existant dans l'école
        student_obj = db.query(SchoolStudentInformation).filter(
            SchoolStudentInformation.school_id == school_id,
            (SchoolStudentInformation.id == st_id) | (SchoolStudentInformation.student_n_permanent == permi)
        ).first()
        
        if not student_obj:
            student_obj = SchoolStudentInformation(id=st_id, school_id=school_id)
            db.add(student_obj)
            
        student_obj.student_name = st.get("name", "")
        student_obj.student_post_name = st.get("postname", "")
        student_obj.student_pre_name = st.get("prename", "")
        student_obj.student_sexe = st.get("sexe", "M")
        student_obj.student_born_date = st.get("bornDate", "")
        student_obj.student_born_place = st.get("bornWhere", "")
        student_obj.student_n_permanent = permi
        student_obj.class_id = st.get("classId")
        ensure_suivi_student_code(db, student_obj)

    # 5. Rapprochement des enseignants
    teachers_payload = payload.get("teachers", [])
    for t in teachers_payload:
        t_code = t.get("uniqueId")
        if not t_code: continue
        
        teacher_obj = db.query(Teacher).filter(Teacher.teacher_code == t_code).first()
        
        if teacher_obj:
            teacher_obj.school_id = school_id
            teacher_obj.school_name = school.name_school
            teacher_obj.school_access_verified = True
            if t.get("subject") and t.get("subject") != "Matière à attribuer": teacher_obj.subject = t.get("subject")
            if t.get("status"): teacher_obj.status = t.get("status")

    # 6. Synchronisation des évaluations/notes
    grades_payload = payload.get("grades", {})
    for grade_key, eval_scores in grades_payload.items():
        if "_" not in grade_key: continue
        
        # Découpage robuste à partir du dernier '_'
        st_id, course_id = grade_key.rsplit("_", 1)
        
        student_obj = db.query(SchoolStudentInformation).filter(
            SchoolStudentInformation.id == st_id, 
            SchoolStudentInformation.school_id == school_id
        ).first()
        
        n_perm = student_obj.student_n_permanent if student_obj and student_obj.student_n_permanent else st_id

        eval_obj = db.query(TeacherEvaluation).filter(
            TeacherEvaluation.school_id == school_id,
            TeacherEvaluation.student_n_permanent == n_perm,
            TeacherEvaluation.course_id == course_id
        ).first()

        if not eval_obj:
            eval_obj = TeacherEvaluation(
                school_id=school_id,
                student_n_permanent=n_perm,
                course_id=course_id
            )
            db.add(eval_obj)

        if "p1" in eval_scores and eval_scores["p1"] is not None: eval_obj.p1 = float(eval_scores["p1"])
        if "p2" in eval_scores and eval_scores["p2"] is not None: eval_obj.p2 = float(eval_scores["p2"])
        if "ex1" in eval_scores and eval_scores["ex1"] is not None: eval_obj.ex1 = float(eval_scores["ex1"])
        if "p3" in eval_scores and eval_scores["p3"] is not None: eval_obj.p3 = float(eval_scores["p3"])
        if "p4" in eval_scores and eval_scores["p4"] is not None: eval_obj.p4 = float(eval_scores["p4"])
        if "ex2" in eval_scores and eval_scores["ex2"] is not None: eval_obj.ex2 = float(eval_scores["ex2"])

    merge_pn_extra(school, clean_pn_extra({k: payload.get(k) for k in PN_EXTRA_KEYS}))
    db.commit()
    return {"status": True, "message": "Synchronisation PrimeNet de l'école effectuée avec succès."}
# ---------------------------------------------------------
# 9. EXTRACTION DONNÉES ENSEIGNANT
# ---------------------------------------------------------
@app.get("/api/teachers/{identifier}")
def get_teacher_info(identifier: str, db: Session = Depends(get_db), claims: dict = Depends(get_bearer_claims)):
    """Renvoie toutes les informations concernant un enseignant via son identifiant (email)."""
    teacher = db.query(Teacher).filter(Teacher.email == identifier).first()
    if not teacher:
        return {"status": False, "message": "Enseignant introuvable."}

    is_self = claims.get("role") == "teacher" and claims.get("email") == teacher.email
    is_same_school = claims.get("role") == "school" and bool(teacher.school_id) and claims.get("school_id") == teacher.school_id
    if not (is_self or is_same_school):
        raise HTTPException(status_code=403, detail="Accès refusé à ce profil.")

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
def get_primenet_data(school_id: str, db: Session = Depends(get_db), claims: dict = Depends(require_active_school)):
    """
    Récupère et fusionne les données de tous les enseignants d'une école spécifique
    pour les envoyer à PrimeNet.
    """
    if school_id != claims.get("school_id"):
        raise HTTPException(status_code=403, detail="Accès refusé aux données d'une autre école.")

    # 1. Récupérer tous les payloads enregistrés pour cette école
    school_payloads = db.query(PrimeNetPayload).filter(PrimeNetPayload.school_id == school_id).all()
    
    signatures_map = build_signature_map(db, school_id)
    verified_behaviour = build_verified_behaviour(db, school_id)
    if not school_payloads and not signatures_map and not verified_behaviour:
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
        presences_data = data.get("presences")
        presences_dict = presences_data if isinstance(presences_data, dict) else {}
        
        for date_p, classes_dict in presences_dict.items():
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
    
    merged_data["signatures"] = signatures_map
    merged_data["conduct"] = verified_behaviour
    return {
        "status": True,
        "message": "Données PrimeNet récupérées avec succès.",
        "data": merged_data
    }


@app.post("/api/schedule/generate")
def generate_optimized_schedule(payload: ScheduleGenerationRequest, claims: dict = Depends(require_active_school)):
    """
    Endpoint FastAPI pour la génération d'horaires scolaires avec prise en charge
    des troncs communs, préférences et évitement des dernières heures.
    """
    engine = ScheduleEngine(config=payload.config, teachers=payload.teachers)
    return engine.generate()

@app.post("/api/mixage/generate")
def generate_exam_mixage(payload: dict, db: Session = Depends(get_db), claims: dict = Depends(require_active_school)):
    """
    Mixage des examens anti-triche (moteur ExamMixerEngine).
    Entrée : { school_id?, target_classes: [id de classe], rooms: [{id, nom, places, rangees, bancs}],
               supervisors: [{name}], exemplaires: n, students?: [{id, name, postname, classId}] }
    Les élèves sont ceux envoyés par PrimeInk ; à défaut, ceux de la base du serveur.
    """
    school_id = payload.get("school_id") or claims.get("school_id")
    if school_id != claims.get("school_id"):
        raise HTTPException(status_code=403, detail="Accès refusé aux données d'une autre école.")
    target_classes = [str(c) for c in (payload.get("target_classes") or [])][:100]
    rooms = [r for r in (payload.get("rooms") or []) if isinstance(r, dict)][:60]
    if not target_classes or not rooms:
        return {"success": False, "message": "Choisissez au moins une classe et une salle.", "seating_plans": []}

    sent = payload.get("students")
    if isinstance(sent, list) and sent:
        students_list = [
            {"id": str(s.get("id")), "name": str(s.get("name", "")), "postname": str(s.get("postname", "")), "classId": str(s.get("classId"))}
            for s in sent if isinstance(s, dict) and str(s.get("classId")) in target_classes
        ][:5000]
    else:
        db_students = db.query(SchoolStudentInformation).filter(
            SchoolStudentInformation.school_id == school_id,
            SchoolStudentInformation.class_id.in_(target_classes)
        ).all()
        students_list = [
            {"id": s.id, "name": s.student_name, "postname": s.student_post_name or "", "classId": s.class_id}
            for s in db_students
        ]
    if not students_list:
        return {"success": False, "message": "Aucun élève dans les classes choisies.", "seating_plans": []}

    mixer_result = ExamMixerEngine.mix_students_and_assign_seats(students_list, rooms)

    supervisors = [str(s.get("name", "")).strip() for s in (payload.get("supervisors") or []) if isinstance(s, dict) and str(s.get("name", "")).strip()]
    try:
        n_variants = max(1, min(int(payload.get("exemplaires", 1)), 5))
    except (TypeError, ValueError):
        n_variants = 1

    formatted_seating_plans = []
    seat_index = 0
    for room_index, room_res in enumerate(mixer_result.get("result", [])):
        benches_list = []
        for seat in room_res.get("seating_plan", []):
            benches_list.append({
                "bench_number": seat["banc"],
                "row": seat["rangee"],
                "student_left": {
                    "id": seat["student_exam_id"],
                    "name": seat["student_name"],
                    "class": seat["original_class"],
                    "variant_code": chr(65 + (seat_index % n_variants))   # A, B, C... : deux voisins n'ont pas le même sujet
                },
                "student_right": None
            })
            seat_index += 1
        formatted_seating_plans.append({
            "room_name": room_res["room_name"],
            "supervisor": supervisors[room_index % len(supervisors)] if supervisors else "Surveillant",
            "benches": benches_list
        })

    return {
        "success": True,
        "allocation_id": f"mix_{datetime.utcnow().strftime('%Y_%m_%d_%H%M')}",
        "seating_plans": formatted_seating_plans,
        "total_mixed": mixer_result.get("total_mixed", 0),
        "unassigned_students": mixer_result.get("unassigned_students", 0)
    }

# ==============================================================================================================================================================
from sqlalchemy.orm import Session

SEED_SCHOOL_IDS = ["63017630129000686"]
SEED_TEACHER_EMAIL = "prof.kabiya@gmail.com"
OLD_DEFAULT_SCHOOL_PASSWORD = "123456"
OLD_DEFAULT_TEACHER_PASSWORD = "profpassword123"


def lock_known_default_accounts(db: Session) -> int:
    """Verrouille les comptes de test qui utilisent encore l'ancien mot de passe public (123456 / profpassword123)."""
    locked = 0
    for school_id in SEED_SCHOOL_IDS:
        school = db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first()
        if school and verify_password(OLD_DEFAULT_SCHOOL_PASSWORD, school.pass_word):
            school.pass_word = hash_password(secrets.token_urlsafe(24))
            locked += 1
    teacher = db.query(Teacher).filter(Teacher.email == SEED_TEACHER_EMAIL).first()
    if teacher and verify_password(OLD_DEFAULT_TEACHER_PASSWORD, teacher.password):
        teacher.password = hash_password(secrets.token_urlsafe(24))
        locked += 1
    if locked:
        db.commit()
        print(f"🔒 [SEED] {locked} compte(s) de test verrouillé(s) : ils utilisaient l'ancien mot de passe public. "
              f"Pour les réutiliser : SEED_TEST_DATA=1 + SEED_SCHOOL_PASSWORD + SEED_TEACHER_PASSWORD.")
    return locked


def seed_initial_test_data():
    """
    Comptes de TEST (écoles fictives + enseignant fictif).
    - Désactivé par défaut. Pour l'activer : SEED_TEST_DATA=1 (jamais sur une base contenant de vraies écoles).
    - Mots de passe : SEED_SCHOOL_PASSWORD et SEED_TEACHER_PASSWORD. Absents, ils sont générés au hasard et affichés dans les logs.
    - Désactivé, les comptes de test encore protégés par l'ancien mot de passe public sont verrouillés.
    """
    db: Session = SessionLocal()
    try:
        if os.getenv("SEED_TEST_DATA", "0") != "1":
            lock_known_default_accounts(db)
            return

        env_school_pw = os.getenv("SEED_SCHOOL_PASSWORD")
        env_teacher_pw = os.getenv("SEED_TEACHER_PASSWORD")
        school_pw = env_school_pw or secrets.token_urlsafe(9)
        teacher_pw = env_teacher_pw or secrets.token_urlsafe(9)
        generated = []

        for index, school_id in enumerate(SEED_SCHOOL_IDS, start=1):
            school = db.query(SchoolInformation).filter(SchoolInformation.school_id == school_id).first()
            if not school:
                db.add(SchoolInformation(
                    school_id=school_id,
                    bulletin_seq_id=f"SEQ_TEST_00h1864",
                    code="630119",
                    name_school="ÉCOLE DE TEST CLASSNET",
                    city="BUKAVU",
                    commune="IBANDA",
                    name_responsable="Directeur Gabriel",
                    num_tel="+243900000000",
                    adresse_physique="Avenue du Test N°12",
                    email="ecole.test@classnet.cd",
                    pass_word="12345678"
                ))
                if not env_school_pw:
                    generated.append(f"école {school_id} : {school_pw}")
                print(f"🏫 [SEED] École fictive de test créée : {school_id}")
            elif env_school_pw:
                school.pass_word = hash_password(env_school_pw)

        teacher = db.query(Teacher).filter(Teacher.email == SEED_TEACHER_EMAIL).first()
        if not teacher:
            db.add(Teacher(
                full_name="Prof. Kabiya Test",
                email=SEED_TEACHER_EMAIL,
                school_name="ÉCOLE DE TEST CLASSNET",
                password="12345678",
                phone_number="+243810000000",
                age=32,
                teacher_code="PROF_TEST_bylh12",
                school_id="63017630129000686",
                status="Actif"
            ))
            if not env_teacher_pw:
                generated.append(f"enseignant {SEED_TEACHER_EMAIL} : {teacher_pw}")
            print("👨‍🏫 [SEED] Enseignant fictif de test créé.")
        elif env_teacher_pw:
            teacher.password = hash_password(env_teacher_pw)

        db.commit()
        for line in generated:
            print(f"🔑 [SEED] Mot de passe généré — {line}")
    except Exception as e:
        db.rollback()
        print(f"⚠️ [SEED] Erreur lors de la création des données de test : {e}")
    finally:
        db.close()


seed_initial_test_data()
# ==============================================================================================================================================================