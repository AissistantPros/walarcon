# -*- coding: utf-8 -*-
#utils.py
"""
Módulo de utilidades para integración con Google APIs y manejo de tiempo.
"""

import os
import json
import logging
import threading
from datetime import datetime, timedelta, date, time as dt_time
import pytz
from dotenv import load_dotenv
from decouple import config
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from dateutil.parser import parse
import locale
import re

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



def convert_utc_to_cancun(utc_str):
    """Convierte un string UTC (ISO8601) a datetime en zona horaria de Cancún."""
    from datetime import datetime
    import pytz

    utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    cancun_tz = pytz.timezone("America/Cancun")
    return utc_dt.astimezone(cancun_tz)














