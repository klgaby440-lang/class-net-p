import os
import secrets
import string
from datetime import datetime
from typing import Optional, Dict, Any, List
import httpx
from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey, Text, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, relationship

# ---------------------------------------------------------
# 1. CONFIGURATION ET BASE DE DONNÉES POSTGRESQL
# ---------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://classnet_user:password@localhost:5432/classnet")

# Correctif pour les URLs héritées de Render (postgres:// -> postgresql://)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Configuration de l'API WhatsApp (CallMeBot ou Webhook personnalisé)
WHATSAPP_PHONE = os.getenv("WHATSAPP_PHONE", "")  # Ex: "+243xxxxxxxxx"
WHATSAPP_API_KEY = os.getenv("WHATSAPP_API_KEY", "") # Clef API CallMeBot

# ---------------------------------------------------------
# 2. MODÈLES DE LA BASE DE DONNÉES (SQLAlchemy)
# ---------------------------------------------------------

class School(Base):
    __tablename__ = "schools"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    license_key = Column(String, unique=True, nullable=False)
    is_locked = Column(Boolean, default=False)
    expiration_date = Column(String, default="2026-12-31")
    created_at = Column(DateTime, default=datetime.utcnow)

    teachers = relationship("Teacher", back_populates="school")
    backups = relationship("DataBackup", back_populates="school")

class Teacher(Base):
    __tablename__ = "teachers"

    id = Column(Integer, primary_key=True, index=True)
    teacher_code = Column(String(50), unique=True, index=True, nullable=False) # Ex: TCH-8921
    full_name = Column(String, nullable=False)
    phone_number = Column(String, nullable=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True) # OPTIONNEL : Peut être None !

    school = relationship("School", back_populates="teachers")
    backups = relationship("DataBackup", back_populates="teacher")

class AccessCode(Base):
    __tablename__ = "access_codes"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(20), unique=True, index=True, nullable=False)
    is_used = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    used_at = Column(DateTime, nullable=True)

class DataBackup(Base):
    __tablename__ = "data_backups"

    id = Column(Integer, primary_key=True, index=True)
    teacher_id = Column(Integer, ForeignKey("teachers.id"), nullable=False)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True)
    payload = Column(JSON, nullable=False) # Contient toutes les notes, bulletins, matières
    created_at = Column(DateTime, default=datetime.utcnow)

    teacher = relationship("Teacher", back_populates="backups")
    school = relationship("School", back_populates="backups")

class AuditLog(Base):
    """Outil essentiel : Traçabilité complète des actions du serveur"""
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    action = Column(String, nullable=False)
    details = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

# Création automatique de toutes les tables PostgreSQL
Base.metadata.create_all(bind=engine)

# ---------------------------------------------------------
# 3. SCHÉMAS DE VALIDATION (Pydantic)
# ---------------------------------------------------------

class SchoolCreate(BaseModel):
    name: str
    license_key: str
    expiration_date: Optional[str] = "2026-12-31"

class TeacherRegister(BaseModel):
    teacher_code: str
    full_name: str
    phone_number: Optional[str] = None
    school_id: Optional[int] = None # Facultatif !

class BackupPayload(BaseModel):
    teacher_code: str
    school_name: Optional[str] = None
    data: Dict[str, Any] # Données des cahiers de notes / bulletins

class AccessCodeRedeem(BaseModel):
    code: str

# ---------------------------------------------------------
# 4. SERVICES AUXILIAIRES & ENVOI WHATSAPP
# ---------------------------------------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

async def send_whatsapp_notification(message: str):
    """Fonction asynchrone pour notifier Gabriel sur WhatsApp dès qu'un code est utilisé."""
    if not WHATSAPP_PHONE or not WHATSAPP_API_KEY:
        print(f"[WhatsApp Simulation] Alert: {message}")
        return

    # Utilisation du service gratuit CallMeBot pour WhatsApp
    url = f"https://api.callmebot.com/whatsapp.php?phone={WHATSAPP_PHONE}&text={message}&apikey={WHATSAPP_API_KEY}"
    async with httpx.AsyncClient() as client:
        try:
            await client.get(url, timeout=10.0)
        except Exception as e:
            print(f"Erreur d'envoi de la notification WhatsApp : {e}")

def generate_20_char_code() -> str:
    """Génère un code d'accès sécurisé de exactement 20 caractères majuscules/chiffres."""
    alphabet = string.ascii_uppercase + string.digits
    return ''.join(secrets.choice(alphabet) for _ in range(20))

# ---------------------------------------------------------
# 5. INITIALISATION DE L'APPLICATION FASTAPI
# ---------------------------------------------------------

app = FastAPI(
    title="CRYPT Cloud Server & ClassNet Ecosystem API",
    version="2.0.0",
    description="Backend centralisé pour la gestion des écoles, des enseignants et de la synchronisation ClassNet."
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------
# 6. ENDPOINTS
# ---------------------------------------------------------

@app.get("/")
def root():
    return {"status": "online", "system": "CRYPT Core Engine", "version": "2.0.0"}

@app.get("/health")
def health_check(db: Session = Depends(get_db)):
    """Outil essentiel : Vérification de la santé de la BD PostgreSQL."""
    try:
        db.execute("SELECT 1")
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database unreachable: {str(e)}")

# --- COMPATIBILITÉ PRIMENET & ÉCOLES ---

@app.get("/api/cloud/{school_name}/status")
def check_school_lockdown(school_name: str, db: Session = Depends(get_db)):
    """Compatibilité exacte avec la fonction checkCryptLockdown() de PrimeNet."""
    school = db.query(School).filter(School.name == school_name).first()
    if not school:
        # Si l'école n'est pas encore enregistrée dans le cloud, statut par défaut non bloqué
        return {"locked": False, "expiration": "N/A", "school": school_name}
    
    return {
        "locked": school.is_locked,
        "expiration": school.expiration_date,
        "school": school.name
    }

@app.post("/api/schools/register")
def register_school(school_data: SchoolCreate, db: Session = Depends(get_db)):
    """Enregistre une nouvelle école dans PrimeNet Cloud."""
    existing = db.query(School).filter(School.name == school_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Une école avec ce nom existe déjà.")
    
    new_school = School(
        name=school_data.name,
        license_key=school_data.license_key,
        expiration_date=school_data.expiration_date
    )
    db.add(new_school)
    db.commit()
    db.refresh(new_school)
    return {"message": "École créée avec succès", "school": new_school.name, "id": new_school.id}

@app.post("/api/schools/{school_id}/toggle-lock")
def toggle_school_lock(school_id: int, locked: bool, db: Session = Depends(get_db)):
    """Bloque ou débloque une école à distance depuis PrimeNet."""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="École non trouvée.")
    
    school.is_locked = locked
    db.commit()
    return {"message": f"Statut de verrouillage mis à jour pour {school.name}", "locked": school.is_locked}

# --- GESTION DES ENSEIGNANTS (AUTONOMES OU ÉCOLE) ---

@app.post("/api/teachers/register")
def register_teacher(teacher_data: TeacherRegister, db: Session = Depends(get_db)):
    """Inscrit un enseignant. Peut être lié à une école (school_id) ou être indépendant (school_id = None)."""
    existing = db.query(Teacher).filter(Teacher.teacher_code == teacher_data.teacher_code).first()
    if existing:
        raise HTTPException(status_code=400, detail="Cet identifiant enseignant est déjà utilisé.")
    
    if teacher_data.school_id:
        school = db.query(School).filter(School.id == teacher_data.school_id).first()
        if not school:
            raise HTTPException(status_code=404, detail="L'école spécifiée n'existe pas.")

    new_teacher = Teacher(
        teacher_code=teacher_data.teacher_code,
        full_name=teacher_data.full_name,
        phone_number=teacher_data.phone_number,
        school_id=teacher_data.school_id
    )
    db.add(new_teacher)
    db.commit()
    db.refresh(new_teacher)
    return {
        "message": "Enseignant enregistré",
        "teacher_code": new_teacher.teacher_code,
        "is_standalone": new_teacher.school_id is None
    }

@app.get("/api/teachers/{teacher_code}/profile")
def get_teacher_profile(teacher_code: str, db: Session = Depends(get_db)):
    """Connexion de l'enseignant depuis ClassNet App avec son identifiant unique."""
    teacher = db.query(Teacher).filter(Teacher.teacher_code == teacher_code).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Identifiant enseignant invalide.")
    
    school_info = None
    if teacher.school:
        school_info = {
            "school_id": teacher.school.id,
            "school_name": teacher.school.name,
            "is_locked": teacher.school.is_locked
        }

    return {
        "teacher_id": teacher.id,
        "teacher_code": teacher.teacher_code,
        "full_name": teacher.full_name,
        "has_school": teacher.school_id is not None,
        "school": school_info
    }

# --- SAUVEGARDE ET SYNCHRONISATION DES DONNÉES (ClassNet App) ---

@app.post("/api/sync/backup")
def backup_teacher_data(payload: BackupPayload, db: Session = Depends(get_db)):
    """Sauvegarde les notes et cahiers de cours reçus de ClassNet App."""
    teacher = db.query(Teacher).filter(Teacher.teacher_code == payload.teacher_code).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Enseignant non identifié.")
    
    school_id = teacher.school_id
    
    # Création du registre de sauvegarde
    backup = DataBackup(
        teacher_id=teacher.id,
        school_id=school_id,
        payload=payload.data
    )
    db.add(backup)
    
    # Audit Log
    log = AuditLog(
        action="DATA_BACKUP",
        details=f"Sauvegarde effectuée pour l'enseignant {teacher.teacher_code}"
    )
    db.add(log)
    
    db.commit()
    return {"status": "success", "backup_id": backup.id, "timestamp": backup.created_at}

# --- GESTION DES CODES D'ACCÈS À 20 CARACTÈRES & ALERTE WHATSAPP ---

@app.post("/api/access-codes/generate")
def generate_access_code(count: int = 1, db: Session = Depends(get_db)):
    """Génère un ou plusieurs codes d'accès uniques à 20 caractères."""
    generated_codes = []
    for _ in range(count):
        code_str = generate_20_char_code()
        # Assurer l'unicité
        while db.query(AccessCode).filter(AccessCode.code == code_str).first():
            code_str = generate_20_char_code()
            
        new_code = AccessCode(code=code_str)
        db.add(new_code)
        generated_codes.append(code_str)
    
    db.commit()
    return {"generated_codes": generated_codes, "total": len(generated_codes)}

@app.post("/api/access-codes/use")
def use_access_code(payload: AccessCodeRedeem, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Vérifie, consomme et SUPPRIME un code à 20 caractères, puis alerte Gabriel par WhatsApp."""
    access_code = db.query(AccessCode).filter(AccessCode.code == payload.code).first()
    
    if not access_code:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Code d'accès invalide ou déjà utilisé."
        )
    
    if access_code.is_used:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Ce code d'accès a déjà été consommé."
        )

    # Marquer le code et le SUPPRIMER de la base de données
    code_value = access_code.code
    db.delete(access_code)
    
    # Traçabilité
    log = AuditLog(
        action="CODE_CONSUMED",
        details=f"Le code à 20 caractères {code_value} a été consommé et supprimé."
    )
    db.add(log)
    db.commit()

    # Envoi de la notification WhatsApp en tâche de fond (évite de ralentir la réponse API)
    message_text = f"🚨 *CRYPT ALERT* 🚨%0ALe code d'accès {code_value} vient d'être utilisé et supprimé de la base de données !%0AHeure: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    background_tasks.add_task(send_whatsapp_notification, message_text)

    return {
        "status": "success",
        "message": f"Code {code_value} consommé et supprimé avec succès. Alerte transmise.",
        "code_deleted": True
    }
