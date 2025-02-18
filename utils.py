import os
import json
import asyncio
import logging
import threading  # ✅ Cambio crítico: Para manejar concurrencia
from datetime import datetime, timedelta
import pytz
from dotenv import load_dotenv
from decouple import config
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from dateutil.parser import parse

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ✅ Cambio crítico: Bloqueo para caché (evita race conditions)
cache_lock = threading.Lock()

# Variables de entorno y configuración
AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")
GOOGLE_SHEET_ID = config("GOOGLE_SHEET_ID")

# Caché optimizada
availability_cache = {
    "busy_slots": [],
    "last_updated": None
}

def initialize_google_calendar():
    try:
        logger.info("🔍 Inicializando Google Calendar...")
        credentials_info = {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key_id": config("GOOGLE_PRIVATE_KEY_ID"),
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "client_id": config("GOOGLE_CLIENT_ID"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": config("GOOGLE_CLIENT_CERT_URL")
        }
        credentials = Credentials.from_service_account_info(credentials_info, scopes=["https://www.googleapis.com/auth/calendar"])
        return build("calendar", "v3", credentials=credentials)
    except Exception as e:
        logger.error(f"❌ Error en initialize_google_calendar: {str(e)}")
        raise RuntimeError("Error de conexión con Google Calendar")

def cache_available_slots(days_ahead=30):
    """Actualiza la caché con bloqueo para evitar race conditions."""
    try:
        with cache_lock:  # ✅ Bloqueo aplicado
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
            logger.info(f"✅ Caché actualizada. Horarios ocupados: {len(busy_slots)}")

    except Exception as e:
        logger.error(f"❌ Error al precargar disponibilidad: {str(e)}")

def get_cached_availability():
    """Actualiza cada 15 minutos (antes era 1 hora)."""
    now = get_cancun_time()
    if (
        availability_cache["last_updated"] is None or
        (now - availability_cache["last_updated"]).seconds > 900  # ✅ Cambiado a 15 minutos
    ):
        logger.info("🔄 Actualizando caché de disponibilidad...")
        cache_available_slots()
    return availability_cache.get("busy_slots", [])

def get_cancun_time():
    cancun_tz = pytz.timezone("America/Cancun")
    return datetime.now(cancun_tz)

# ... (resto de funciones se mantienen igual, solo se muestran cambios críticos)