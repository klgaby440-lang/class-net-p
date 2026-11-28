"""Passerelle Intranet ClassInk -> PrimeInk.

Le service ne possède aucune base de données et n'écrit aucun fichier.
Les paquets en attente vivent uniquement en mémoire, un dernier paquet par
enseignant et établissement, jusqu'à l'accusé de réception de PrimeInk.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse


APP_NAME = "ClassInk Intranet Relay"
DEFAULT_PORT = 5001
MAX_PAYLOAD_BYTES = int(os.getenv("INTRANET_MAX_PAYLOAD_BYTES", str(8 * 1024 * 1024)))
MAX_PENDING_PER_SCHOOL = int(os.getenv("INTRANET_MAX_PENDING_PER_SCHOOL", "300"))
MAX_TOTAL_BUFFER_BYTES = int(os.getenv("INTRANET_MAX_TOTAL_BUFFER_BYTES", str(128 * 1024 * 1024)))
BUFFER_TTL_SECONDS = int(os.getenv("INTRANET_BUFFER_TTL_SECONDS", str(24 * 60 * 60)))

# Uniquement des données qui servent à mettre à jour PrimeInk.
# Les mots de passe, quiz, conversations et bases complètes sont interdits.
ALLOWED_DATA_FIELDS = {
    "teachers",
    "moyenne_classe",
    "studentAliases",
    "attendanceRecords",
    "conduct",
    "signatures",
}

_PENDING: dict[tuple[str, str], dict[str, Any]] = {}
_BUFFER_LOCK = threading.RLock()

app = FastAPI(
    title=APP_NAME,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

allowed_origins = [
    item.strip()
    for item in os.getenv("INTRANET_ALLOWED_ORIGINS", "*").split(",")
    if item.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins or ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _auth_secret() -> str:
    # La valeur est fournie à l'installation, jamais incluse dans le dépôt.
    return os.getenv("INTRANET_AUTH_SECRET") or os.getenv("AUTH_SECRET") or ""


def _claims_from_token(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Session requise. Connectez-vous à ClassInk.")

    secret = _auth_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="La passerelle n'est pas configurée : secret d'authentification absent.",
        )

    token = authorization[7:].strip()
    try:
        payload_part, supplied_signature = token.split(".", 1)
        expected_signature = _b64url(
            hmac.new(secret.encode("utf-8"), payload_part.encode("ascii"), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(supplied_signature, expected_signature):
            raise ValueError("signature")
        claims = json.loads(_b64url_decode(payload_part).decode("utf-8"))
        if not isinstance(claims, dict) or int(claims.get("exp", 0)) < int(time.time()):
            raise ValueError("expired")
        return claims
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Session invalide ou expirée. Reconnectez-vous.") from exc


def _require_role(authorization: str | None, role: str) -> dict[str, Any]:
    claims = _claims_from_token(authorization)
    if claims.get("role") != role:
        raise HTTPException(status_code=403, detail="Accès refusé pour ce type de compte.")
    school_id = str(claims.get("school_id") or "").strip()
    if not school_id:
        raise HTTPException(status_code=403, detail="Aucun établissement n'est associé à cette session.")
    return claims


async def _read_json(request: Request, max_bytes: int = MAX_PAYLOAD_BYTES) -> dict[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="Le paquet dépasse la taille maximale autorisée.")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Taille de requête invalide.") from exc

    raw = await request.body()
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail="Le paquet dépasse la taille maximale autorisée.")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Le paquet JSON est illisible.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Le paquet doit être un objet JSON.")
    return payload


def _purge_expired(now: float | None = None) -> None:
    current = time.time() if now is None else now
    expired = [
        key for key, item in _PENDING.items()
        if current - float(item.get("received_at", 0)) > BUFFER_TTL_SECONDS
    ]
    for key in expired:
        _PENDING.pop(key, None)


def _buffer_size() -> int:
    return sum(int(item.get("size_bytes", 0)) for item in _PENDING.values())


@app.middleware("http")
async def no_store_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, private, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/healthz")
def health_check():
    """Contrôle technique minimal ; aucune donnée scolaire n'est exposée."""
    return {"status": True, "service": APP_NAME, "storage": "volatile-memory-only"}


@app.post("/api/intranet/sync")
async def receive_teacher_data(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Reçoit le paquet métier limité d'un enseignant et le garde en RAM."""
    claims = _require_role(authorization, "teacher")

    if str(claims.get("workspace_mode") or "").lower() == "independent":
        raise HTTPException(status_code=403, detail="Un espace enseignant indépendant ne peut pas transmettre à une école.")

    school_id = str(claims.get("school_id") or "").strip()
    teacher_email = str(claims.get("email") or "").strip().lower()
    if not teacher_email:
        raise HTTPException(status_code=403, detail="Identité de l'enseignant absente de la session.")

    payload = await _read_json(request)
    supplied_school = str(payload.get("school_id") or "").strip()
    supplied_email = str(payload.get("teacher_email") or "").strip().lower()

    if supplied_school != school_id:
        raise HTTPException(status_code=403, detail="Ce paquet ne correspond pas à l'établissement de la session.")
    if supplied_email != teacher_email:
        raise HTTPException(status_code=403, detail="Ce paquet ne correspond pas à l'enseignant connecté.")

    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="Les données à transmettre sont absentes.")

    unsupported = set(data.keys()) - ALLOWED_DATA_FIELDS
    if unsupported:
        raise HTTPException(
            status_code=422,
            detail="Le paquet contient des champs non autorisés : " + ", ".join(sorted(unsupported)),
        )

    try:
        serialized = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        size_bytes = len(serialized.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Le paquet contient des valeurs JSON invalides.") from exc

    if size_bytes > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Le paquet dépasse la taille maximale autorisée.")

    expected_types = {
        "teachers": list,
        "moyenne_classe": dict,
        "studentAliases": dict,
        "attendanceRecords": list,
        "conduct": dict,
        "signatures": dict,
    }
    for field, expected_type in expected_types.items():
        if field in data and not isinstance(data[field], expected_type):
            raise HTTPException(status_code=422, detail="Le champ " + field + " est invalide.")

    now = time.time()
    receipt_id = uuid.uuid4().hex
    key = (school_id, teacher_email)

    with _BUFFER_LOCK:
        _purge_expired(now)
        previous = _PENDING.get(key)
        projected_size = _buffer_size() - int(previous.get("size_bytes", 0) if previous else 0) + size_bytes
        teacher_count = sum(1 for pending_key in _PENDING if pending_key[0] == school_id)
        if previous is None and teacher_count >= MAX_PENDING_PER_SCHOOL:
            raise HTTPException(status_code=429, detail="La passerelle a atteint sa capacité temporaire pour cet établissement.")
        if projected_size > MAX_TOTAL_BUFFER_BYTES:
            raise HTTPException(status_code=507, detail="Mémoire temporaire saturée. PrimeInk doit récupérer les paquets en attente.")

        # Un nouvel envoi remplace l'ancien paquet du même enseignant.
        _PENDING[key] = {
            "receipt_id": receipt_id,
            "school_id": school_id,
            "teacher_email": teacher_email,
            "received_at": now,
            "size_bytes": size_bytes,
            "data": json.loads(serialized),
        }

    return {
        "status": True,
        "receipt_id": receipt_id,
        "message": "Paquet reçu par la passerelle. PrimeInk le récupérera sur le réseau local.",
        "storage": "volatile-memory-only",
    }


@app.get("/api/primenet/sync")
def send_pending_data_to_primeink(authorization: str | None = Header(default=None)):
    """PrimeInk lit les paquets en attente de son établissement sans les supprimer."""
    claims = _require_role(authorization, "school")
    school_id = str(claims["school_id"]).strip()

    with _BUFFER_LOCK:
        _purge_expired()
        entries = [
            {
                "receipt_id": item["receipt_id"],
                "teacher_email": item["teacher_email"],
                "received_at": int(item["received_at"]),
                "data": item["data"],
            }
            for key, item in _PENDING.items()
            if key[0] == school_id
        ]

    entries.sort(key=lambda item: (item["received_at"], item["teacher_email"]))
    return JSONResponse(
        content={"status": True, "data": {"submissions": entries}},
        headers={"Cache-Control": "no-store, private, max-age=0"},
    )


@app.post("/api/primenet/ack")
async def acknowledge_primeink_save(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Supprime du tampon RAM uniquement les paquets effectivement sauvegardés par PrimeInk."""
    claims = _require_role(authorization, "school")
    school_id = str(claims["school_id"]).strip()
    payload = await _read_json(request, max_bytes=64 * 1024)
    receipt_ids = payload.get("receipt_ids")

    if not isinstance(receipt_ids, list) or len(receipt_ids) > MAX_PENDING_PER_SCHOOL:
        raise HTTPException(status_code=422, detail="Liste d'accusés de réception invalide.")

    requested = {str(value).strip() for value in receipt_ids if str(value).strip()}
    removed = 0
    with _BUFFER_LOCK:
        _purge_expired()
        for key, item in list(_PENDING.items()):
            if key[0] == school_id and item.get("receipt_id") in requested:
                _PENDING.pop(key, None)
                removed += 1

    return {"status": True, "acknowledged": removed}


if __name__ == "__main__":
    import uvicorn

    certfile = os.getenv("INTRANET_SSL_CERTFILE")
    keyfile = os.getenv("INTRANET_SSL_KEYFILE")
    if bool(certfile) != bool(keyfile):
        raise RuntimeError("INTRANET_SSL_CERTFILE et INTRANET_SSL_KEYFILE doivent être renseignés ensemble.")

    uvicorn.run(
        "intranet_server:app",
        host=os.getenv("INTRANET_HOST", "0.0.0.0"),
        port=int(os.getenv("INTRANET_PORT", str(DEFAULT_PORT))),
        reload=False,
        access_log=False,
        log_level=os.getenv("INTRANET_LOG_LEVEL", "warning"),
        ssl_certfile=certfile,
        ssl_keyfile=keyfile,
    )
