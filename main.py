import os
import secrets
import string
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import httpx
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, status, Header, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, EmailStr
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON, Float, text, func, UniqueConstraint, CheckConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship
import random
import re
from classnet_common import (
    PERIODS, CONDUCT_KINDS, CONDUCT_MAX_LEN, SIGNER_ROLES, ACCESS_AUDIENCES, MATCH_METHODS, sql_in,
    validate_conduct_value, normalize_name, name_tokens_key,
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
    
    conn.execute(text("ALTER TABLE otp_codes ADD COLUMN IF NOT EXISTS attempts INTEGER DEFAULT 0;"))
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

    # 4. Création du compte enseignant
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
    db.delete(otp_record)  # Correction : 'otp_record' au lieu de 'record'
    db.commit()

    return {"status": True, "message": "Compte enseignant créé avec succès !"}

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
        students_data.append({
            "id": s.id,
            "name": s.student_name,
            "postname": s.student_post_name,
            "prename": s.student_pre_name,
            "sexe": s.student_sexe,
            "bornDate": s.student_born_date or "",
            "bornWhere": s.student_born_place or "",
            "permi": s.student_n_permanent or "",
            "classId": s.class_id
        })
        if s.student_n_permanent:
            student_id_map[s.student_n_permanent] = s.id

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
            "commune": school.commune
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
    """PrimeInk envoie { school_id, password } : même logique que /api/auth/school/login."""
    return login_school(LoginSchema(identifier=data.school_id, password=data.password), db)

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
    school_id = payload.get("school_id") or getattr(teacher, "school_id", None)
    
    # On ne génère le format PrimeNet que si l'enseignant appartient à une école
    if school_id and str(school_id).lower() != "indépendant":
        # Construction des listes et dictionnaires de base
        classes_list = list(students_dict.keys())
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
PN_EXTRA_KEYS = ("signatureBook", "repechage", "printSettings", "behaviour")
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
    return out


def teacher_is_titulaire(teacher, classe) -> bool:
    """Vrai si l'enseignant est le titulaire enregistré de la classe (nom, code ou e-mail)."""
    titulaire = normalize_name(classe.titulaire_name or "")
    if not titulaire:
        return False
    candidates = {normalize_name(teacher.full_name or ""), normalize_name(teacher.teacher_code or ""), normalize_name(teacher.email or "")}
    candidates.discard("")
    return titulaire in candidates


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
        return link.student_id
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
    if not teacher.school_id:
        report["rejected"].append({"reason": "Enseignant non rattaché à une école."})
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

    # 5. Rapprochement des enseignants
    teachers_payload = payload.get("teachers", [])
    for t in teachers_payload:
        t_code = t.get("uniqueId")
        if not t_code: continue
        
        teacher_obj = db.query(Teacher).filter(
            (Teacher.teacher_code == t_code) | (Teacher.full_name == t.get("name"))
        ).first()
        
        if teacher_obj:
            teacher_obj.school_id = school_id
            if t.get("subject"): teacher_obj.subject = t.get("subject")
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
SEED_TEACHER_EMAIL = "prof.kabila@gmail.com"
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
                    bulletin_seq_id=f"SEQ_TEST_00{h1864}",
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
                full_name="Prof. Kabila Test",
                email=SEED_TEACHER_EMAIL,
                school_name="ÉCOLE DE TEST CLASSNET",
                password=hash_password(teacher_pw),
                phone_number="+243810000000",
                age=32,
                teacher_code="PROF_TEST_001",
                school_id=SEED_SCHOOL_IDS[-1],
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
