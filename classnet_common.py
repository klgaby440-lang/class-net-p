"""
classnet_common.py : définitions partagées ClassNet (étape 3, modèle de données commun).

Ce module ne dépend que de la bibliothèque standard. Il est importé par server_internet.py
(et, aux étapes suivantes, par server_intranet.py). Toute valeur autorisée, règle de validation
ou colonne du format d'échange Excel est définie ICI et nulle part ailleurs.

Entités communes (tables dans server_internet.py) :
  - bulletin_types      : types de bulletin d'une école (config JSON validée par validate_bulletin_config)
  - student_conduct     : Application / Conduite, une ligne par (élève, nature, période)
  - bulletin_signatures : signatures du bulletin (titulaire, élève, chef d'établissement, chef de centre)
  - grade_access_rules  : blocage / autorisation d'accès aux notes, appliqué côté serveur (étape 6)
  - student_links       : correspondance entre l'id d'élève ClassInk et l'élève de l'école
  - classe_informations : deux colonnes ajoutées (conduite_enabled, bulletin_type_id)
"""
import re
import unicodedata

# ---------------------------------------------------------------------------
# Périodes et totaux
# ---------------------------------------------------------------------------
# Seules ces six périodes sont saisies et stockées. Les totaux de semestre (TOTS1, TOTS2) et le
# total général (TG) sont calculés à l'affichage : on ne les stocke jamais.
PERIODS = ("P1", "P2", "EX1", "P3", "P4", "EX2")
DERIVED_TOTALS = {"TOTS1": ("P1", "P2", "EX1"), "TOTS2": ("P3", "P4", "EX2")}

# ---------------------------------------------------------------------------
# Conduite
# ---------------------------------------------------------------------------
CONDUCT_KINDS = ("app", "cond")      # app = Application, cond = Conduite (clés déjà utilisées par PrimeInk)
CONDUCT_MAX_LEN = 10                 # la valeur est libre (TB, B, ...) : l'échelle n'est pas imposée ici

# ---------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------
SIGNER_ROLES = ("titulaire", "eleve", "chef_etablissement", "chef_centre")

# ---------------------------------------------------------------------------
# Accès aux notes
# ---------------------------------------------------------------------------
ACCESS_AUDIENCES = ("teacher", "suivi")   # teacher = enseignants (ClassInk), suivi = Class-Ink Suivi
GRADE_ACCESS_DEFAULT = True               # sans règle applicable : accès autorisé (comportement actuel)

# ---------------------------------------------------------------------------
# Correspondance des élèves
# ---------------------------------------------------------------------------
MATCH_METHODS = ("permanent", "name", "manual")

# ---------------------------------------------------------------------------
# Format d'échange Excel v1 (ClassInk exporte, PrimeInk importe)
# ---------------------------------------------------------------------------
EXCHANGE_FORMAT_VERSION = 1
EXCHANGE_SHEETS = {"info": "INFO", "notes": "NOTES", "conduite": "CONDUITE"}   # CONDUITE est facultative
KEY_TYPES = ("permanent", "classink_id")   # nature de student_key

# Feuille INFO : deux colonnes « cle » / « valeur », une ligne par clé ci-dessous.
INFO_KEYS = ("format_version", "school_id", "class_name", "teacher_email", "exported_at")

# Feuille NOTES : une ligne par (élève, cours, période). score et max_score sont les SOMMES des
# évaluations de la période, exportées telles quelles : PrimeInk contrôle, il ne remet pas à l'échelle.
NOTES_COLUMNS = ("student_key", "key_type", "student_name", "class_name", "course_name",
                 "period", "score", "max_score", "nb_evaluations")

# Feuille CONDUITE : une ligne par (élève, nature, période).
CONDUITE_COLUMNS = ("student_key", "key_type", "student_name", "class_name", "kind", "period", "value")


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def sql_in(column: str, values, allow_null: bool = False) -> str:
    """Texte d'une contrainte CHECK : sql_in('period', PERIODS) -> "period IN ('P1', ...)"."""
    for v in values:
        if "'" in v:
            raise ValueError(f"Valeur interdite dans une contrainte SQL : {v!r}")
    clause = f"{column} IN ({', '.join(repr(v) for v in values)})"
    return f"{column} IS NULL OR {clause}" if allow_null else clause


def normalize_name(name: str) -> str:
    """Nom sans accents, en minuscules, espaces et ponctuation réduits. Sert à rapprocher les élèves."""
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def name_tokens_key(name: str) -> str:
    """Clé indépendante de l'ordre des mots : « Nsimwa Paul Junior » == « Junior Nsimwa Paul »."""
    return " ".join(sorted(normalize_name(name).split()))


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# Validations (chaque fonction renvoie la liste des erreurs ; liste vide = valide)
# ---------------------------------------------------------------------------
def validate_conduct_value(value) -> list:
    if not isinstance(value, str) or not value.strip():
        return ["La valeur de conduite est vide."]
    if len(value.strip()) > CONDUCT_MAX_LEN:
        return [f"La valeur de conduite dépasse {CONDUCT_MAX_LEN} caractères."]
    return []


def validate_bulletin_config(config) -> list:
    """config = {"periods": [...], "show_application": bool, "show_conduite": bool, "signatures": [...]}"""
    if not isinstance(config, dict):
        return ["La configuration du bulletin doit être un objet."]
    errors = []
    periods = config.get("periods", list(PERIODS))
    if not isinstance(periods, list) or not periods or any(p not in PERIODS for p in periods):
        errors.append(f"periods doit être une liste non vide parmi {list(PERIODS)}.")
    signatures = config.get("signatures", [])
    if not isinstance(signatures, list) or any(s not in SIGNER_ROLES for s in signatures):
        errors.append(f"signatures doit être une liste parmi {list(SIGNER_ROLES)}.")
    for flag in ("show_application", "show_conduite"):
        if flag in config and not isinstance(config[flag], bool):
            errors.append(f"{flag} doit être vrai ou faux.")
    return errors


def validate_info(info) -> list:
    if not isinstance(info, dict):
        return ["La feuille INFO est illisible."]
    errors = [f"INFO : clé manquante « {k} »." for k in INFO_KEYS if not str(info.get(k) or "").strip()]
    if str(info.get("format_version")) != str(EXCHANGE_FORMAT_VERSION):
        errors.append(f"Version de format non prise en charge : {info.get('format_version')!r} "
                      f"(attendu {EXCHANGE_FORMAT_VERSION}).")
    return errors


def _validate_key(row: dict) -> list:
    errors = []
    if not str(row.get("student_key") or "").strip():
        errors.append("student_key est vide.")
    if row.get("key_type") not in KEY_TYPES:
        errors.append(f"key_type doit être parmi {list(KEY_TYPES)}.")
    return errors


def validate_notes_row(row: dict) -> list:
    errors = _validate_key(row)
    if not str(row.get("course_name") or "").strip():
        errors.append("course_name est vide.")
    if row.get("period") not in PERIODS:
        errors.append(f"period doit être parmi {list(PERIODS)}.")
    score, max_score = row.get("score"), row.get("max_score")
    if not _is_number(max_score) or max_score <= 0:
        errors.append("max_score doit être un nombre supérieur à 0.")
    elif not _is_number(score) or not (0 <= score <= max_score):
        errors.append(f"score doit être un nombre compris entre 0 et {max_score}.")
    return errors


def validate_conduct_row(row: dict) -> list:
    errors = _validate_key(row)
    if row.get("kind") not in CONDUCT_KINDS:
        errors.append(f"kind doit être parmi {list(CONDUCT_KINDS)}.")
    if row.get("period") not in PERIODS:
        errors.append(f"period doit être parmi {list(PERIODS)}.")
    return errors + validate_conduct_value(row.get("value"))


# ---------------------------------------------------------------------------
# Accès aux notes : quelle règle s'applique ?
# ---------------------------------------------------------------------------
def _field(rule, name):
    return rule.get(name) if isinstance(rule, dict) else getattr(rule, name, None)


def resolve_grade_access(rules, class_id, period, audience, default: bool = GRADE_ACCESS_DEFAULT) -> bool:
    """
    Décide si `audience` peut accéder aux notes de `class_id` pour `period`.

    Une règle s'applique si son audience correspond et si sa classe / sa période est vide (= toutes)
    ou égale à celle demandée. La plus précise l'emporte : classe + période, puis classe seule,
    puis période seule, puis école entière. À précision égale, la plus récente (updated_at) gagne.
    Sans règle applicable, `default` est renvoyé.
    """
    best, best_key = None, None
    for rule in rules:
        if _field(rule, "audience") != audience:
            continue
        rule_class, rule_period = _field(rule, "class_id"), _field(rule, "period")
        if rule_class not in (None, class_id) or rule_period not in (None, period):
            continue
        specificity = (2 if rule_class is not None else 0) + (1 if rule_period is not None else 0)
        key = (specificity, str(_field(rule, "updated_at") or ""))
        if best_key is None or key > best_key:
            best, best_key = rule, key
    return default if best is None else bool(_field(best, "is_allowed"))
