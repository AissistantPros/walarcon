# -*- coding: utf-8 -*-
#utils.py
"""
Módulo de utilidades para integración con Google APIs y manejo de tiempo.
"""

import os
import json
import logging
import threading
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from decouple import config
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from dateutil.parser import parse
import dateparser

# Cargar variables de entorno
load_dotenv()

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------
# 🔐 Variables de Entorno (NO modificar nombres)
# ------------------------------------------
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_SHEET_ID = config("GOOGLE_SHEET_ID")  # ✅ Nombre exacto
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_PRIVATE_KEY_ID = config("GOOGLE_PRIVATE_KEY_ID")
GOOGLE_CLIENT_ID = config("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_CERT_URL = config("GOOGLE_CLIENT_CERT_URL")

# ------------------------------------------
# 🔄 Caché de Disponibilidad (Thread-safe)
# ------------------------------------------
cache_lock = threading.Lock()
availability_cache = {
    "busy_slots": [],
    "last_updated": None
}

def initialize_google_calendar():
    """Inicializa el servicio de Google Calendar."""
    try:
        logger.info("🔍 Inicializando Google Calendar...")
        credentials_info = {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key_id": GOOGLE_PRIVATE_KEY_ID,
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "client_id": GOOGLE_CLIENT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": GOOGLE_CLIENT_CERT_URL
        }
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        return build("calendar", "v3", credentials=credentials)
    except Exception as e:
        logger.error(f"❌ Error en Google Calendar: {str(e)}")
        raise

def initialize_google_sheets():
    """Inicializa el servicio de Google Sheets."""
    try:
        logger.info("🔍 Inicializando Google Sheets...")
        credentials_info = {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key_id": GOOGLE_PRIVATE_KEY_ID,
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "client_id": GOOGLE_CLIENT_ID,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": GOOGLE_CLIENT_CERT_URL
        }
        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        service = build("sheets", "v4", credentials=credentials)
        service.sheet_id = GOOGLE_SHEET_ID  # Adjuntamos el ID para uso futuro
        return service
    except Exception as e:
        logger.error(f"❌ Error en Google Sheets: {str(e)}")
        raise

def cache_available_slots(days_ahead=30):
    """Precarga los slots ocupados en caché."""
    try:
        with cache_lock:
            service = initialize_google_calendar()
            now = get_cancun_time()
            time_min = now.isoformat()
            time_max = (now + timedelta(days=days_ahead)).isoformat()

            body = {
                "timeMin": time_min,
                "timeMax": time_max,
                "timeZone": "America/Cancun",
                "items": [{"id": GOOGLE_CALENDAR_ID}]
            }

            events_result = service.freebusy().query(body=body).execute()
            busy_slots = events_result["calendars"][GOOGLE_CALENDAR_ID]["busy"]

            availability_cache["busy_slots"] = busy_slots
            availability_cache["last_updated"] = now
            logger.info(f"✅ Caché actualizada. Slots ocupados: {len(busy_slots)}")
    except Exception as e:
        logger.error(f"❌ Error al actualizar caché: {str(e)}")

def get_cached_availability():
    """Obtiene slots ocupados desde la caché (actualiza cada 15 min)."""
    now = get_cancun_time()
    if not availability_cache["last_updated"] or (now - availability_cache["last_updated"]).seconds > 900:
        cache_available_slots()
    return availability_cache["busy_slots"]

def search_calendar_event_by_phone(phone: str):
    """Busca citas por número de teléfono."""
    try:
        service = initialize_google_calendar()
        now_utc = datetime.utcnow().isoformat() + 'Z'
        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            q=phone,
            timeMin=now_utc,
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        items = events_result.get("items", [])

        parsed_events = []
        for evt in items:
            # summary => nombre del paciente
            summary = evt.get("summary", "Desconocido")
            description = evt.get("description", "")
            
            # Variables por defecto
            motive = None
            phone_in_desc = None
            
            # Parse de la descripción
            # Ej: "📞 Teléfono: 9982137477\n📝 Motivo: Dolor en el pecho"
            # Lo puedes hacer con splits:
            lines = description.split("\n")
            for line in lines:
                line_lower = line.lower()
                if "teléfono:" in line_lower:
                    phone_in_desc = line.split("Teléfono:")[-1].strip()
                if "motivo:" in line_lower:
                    motive = line.split("Motivo:")[-1].strip()
            
            # Reemplazar en la data "name", "reason", "phone"
            evt["name"] = summary
            evt["reason"] = motive
            evt["phone"] = phone_in_desc if phone_in_desc else phone
            
            parsed_events.append(evt)

        return parsed_events

    except Exception as e:
        logger.error(f"❌ Error buscando citas: {str(e)}")
        return []

def get_cancun_time():
    """Obtiene la hora actual en Cancún."""
    return datetime.now(pytz.timezone("America/Cancun"))

def is_slot_available(start_dt: datetime, end_dt: datetime, busy_slots=None):
    """Verifica si un slot está disponible (sin solaparse con 'busy_slots')."""
    if busy_slots is None:
        busy_slots = get_cached_availability()
    for slot in busy_slots:
        slot_start = parse(slot["start"]).astimezone(pytz.UTC)
        slot_end = parse(slot["end"]).astimezone(pytz.UTC)
        if start_dt < slot_end and end_dt > slot_start:
            return False
    return True



def convert_utc_to_cancun(utc_str):
    """Convierte un string UTC (ISO8601) a datetime en zona horaria de Cancún."""
    from datetime import datetime
    import pytz

    utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    cancun_tz = pytz.timezone("America/Cancun")
    return utc_dt.astimezone(cancun_tz)


def parse_relative_date(expression: str) -> str:
    """
    Convierte expresiones de fecha en español a YYYY-MM-DD (zona Cancún).
    Ej.: “próximo martes”, “martes de la próxima semana”, “dentro de 10 días”.
    """
    base = get_cancun_time()

    # --- normalizaciones mínimas ------------------------------------
    expr = expression.lower().strip()

    # 1) quita “de la / del / de” que suele colarse
    expr = expr.replace(" de la ", " ").replace(" del ", " ").replace(" de ", " ")

    # 2) reemplaza “próxima semana” por “la próxima semana”
    #    (dateparser entiende mejor con el artículo)
    expr = expr.replace("próxima semana", "la próxima semana")\
               .replace("siguiente semana", "la próxima semana")

    # 3) si la frase empieza con día sin artículo, anteponemos “el”
    if expr.split()[0] in [
        "lunes","martes","miércoles","miercoles","jueves","viernes","sábado","sabado","domingo"
    ] and not expr.startswith("el "):
        expr = "el " + expr

    # ----------------------------------------------------------------
    dt = dateparser.parse(
        expr,
        languages=["es"],
        settings={
            "RELATIVE_BASE": base,
            "TIMEZONE": "America/Cancun",
            "RETURN_AS_TIMEZONE_AWARE": True,
        },
    )
    if not dt:
        raise ValueError(f"No se pudo parsear la fecha: {expression}")
    return dt.date().isoformat()
