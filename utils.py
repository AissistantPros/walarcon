# -*- coding: utf-8 -*-
#utils.py
"""
Módulo de utilidades para integración con Google APIs y manejo de tiempo.
"""

import os
import json
import logging
import threading
from datetime import datetime, timedelta, date
import pytz
from dotenv import load_dotenv
from decouple import config
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from dateutil.parser import parse
import locale

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






# =========================================
# NUEVA FUNCIÓN PARA FECHAS RELATIVAS
# =========================================

# Configurar locale a español para nombres de días/meses (puede requerir configuración en el servidor)
# Intentamos configurarlo, si falla, usamos nombres en inglés como fallback
try:
    # Intenta con una configuración común para español de México en Linux/macOS
    locale.setlocale(locale.LC_TIME, 'es_MX.UTF-8')
except locale.Error:
    try:
        # Intenta con una configuración común para español genérico en Linux/macOS
        locale.setlocale(locale.LC_TIME, 'es_ES.UTF-8')
    except locale.Error:
        try:
             # Intenta con la configuración de Windows
             locale.setlocale(locale.LC_TIME, 'Spanish_Mexico')
        except locale.Error:
            try:
                # Intenta con español genérico de Windows
                locale.setlocale(locale.LC_TIME, 'Spanish_Spain')
            except locale.Error:
                logger.warning("No se pudo configurar locale a español. Se usarán nombres de mes/día en inglés.")
                # Si todo falla, no hacemos nada, usará el default del sistema (probablemente inglés)

# --- Ayudante para formatear fechas ---
def format_date_nicely(target_date: date, relative_time: str = None) -> str:
    """Formatea una fecha a 'DíaDeLaSemana DD de Mes de AAAA' en español."""
    try:
        formatted = target_date.strftime("%A %d de %B de %Y").capitalize()
        if relative_time == "mañana":
            formatted += ", por la mañana"
        elif relative_time == "tarde":
            formatted += ", por la tarde"
        return formatted
    except Exception as e:
        logger.error(f"Error formateando fecha {target_date}: {e}")
        return target_date.strftime('%Y-%m-%d') # Fallback

# === Función Calculadora (Meses Simplificados) ===
def calculate_structured_date(relative_date: str = None, fixed_weekday: str = None, relative_time: str = None) -> dict:
    """
    Calcula una fecha objetivo basada en palabras clave relativas.
    VERSION MEJORADA: Maneja más casos y lógica de "próximo día". Meses simplificados.

    Args:
        relative_date (str, optional): 'hoy', 'mañana', ..., 'en un mes', 'en dos meses', etc.
        fixed_weekday (str, optional): 'lunes', 'martes', ..., 'domingo'.
        relative_time (str, optional): 'mañana' (am) o 'tarde' (pm).

    Returns:
        dict: {'calculated_date_str': 'YYYY-MM-DD', 'readable_description': ..., 'target_hour_pref': ...}
              o {'error': 'Mensaje de error'}
    """
    logger.info(f"📅 Calculando fecha estructurada: relative='{relative_date}', weekday='{fixed_weekday}', time='{relative_time}'")
    try:
        now = get_cancun_time()
        today = now.date()
        base_date = today

        # Palabras clave normalizadas
        relative_date_keyword = (relative_date or "hoy").lower().strip()
        fixed_weekday_keyword = (fixed_weekday or "").lower().strip()
        time_keyword = (relative_time or "").lower().strip()

        # Diccionario para días de la semana
        weekdays_es = {"lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6}
        target_weekday_num = weekdays_es.get(fixed_weekday_keyword) if fixed_weekday_keyword else None

        # --- 1. Calcular Base Date según relative_date ---
        is_relative_week = False

        if relative_date_keyword == "hoy":
            base_date = today
        elif relative_date_keyword == "mañana":
            base_date = today + timedelta(days=1)
        elif relative_date_keyword == "pasado mañana":
            base_date = today + timedelta(days=2)
        elif relative_date_keyword == "hoy en ocho":
            base_date = today + timedelta(days=7)
        elif relative_date_keyword == "de mañana en ocho":
             base_date = today + timedelta(days=8)
        elif relative_date_keyword == "en 15 dias":
             base_date = today + timedelta(days=15)
        # === INICIO: CAMBIO PARA MESES (SIMPLIFICADO) ===
        elif relative_date_keyword == "en un mes":
             base_date = today + timedelta(days=30) # Simplificado a 30 días
        elif relative_date_keyword == "en dos meses":
             base_date = today + timedelta(days=60) # Simplificado a 60 días
        elif relative_date_keyword == "en tres meses":
             base_date = today + timedelta(days=90) # Simplificado a 90 días
        # === FIN: CAMBIO PARA MESES ===
        elif "semana" in relative_date_keyword:
            is_relative_week = True
            days_until_monday = (0 - today.weekday() + 7) % 7
            if days_until_monday == 0:
                days_until_monday = 7
            base_date = today + timedelta(days=days_until_monday)
        elif relative_date_keyword not in ["hoy"]:
             if not fixed_weekday_keyword:
                  return {"error": f"No reconozco el término relativo '{relative_date}'. Intenta con 'hoy', 'mañana', 'próxima semana', etc."}

        target_date = base_date

        # --- 2. Ajustar por fixed_weekday si existe ---
        if target_weekday_num is not None:
            days_ahead = (target_weekday_num - base_date.weekday() + 7) % 7
            if days_ahead == 0 and (not is_relative_week or base_date.weekday() == target_weekday_num) :
                 days_ahead = 7
            target_date = base_date + timedelta(days=days_ahead)

        # --- 3. Validar que la fecha final no sea pasada ---
        if target_date < today:
             logger.warning(f"Cálculo final resultó en fecha pasada ({target_date}), usando 'today' ({today}) como fallback.")
             target_date = today

        # --- 4. Determinar preferencia de hora ---
        target_hour_pref = "09:30"
        if time_keyword == "tarde":
            target_hour_pref = "12:30"
        elif time_keyword == "mañana":
             target_hour_pref = "09:30"

        # --- 5. Formatear resultados ---
        calculated_date_str = target_date.strftime('%Y-%m-%d')
        readable_description = format_date_nicely(target_date, time_keyword or None)

        logger.info(f"✅ Fecha calculada: {calculated_date_str}, Desc: '{readable_description}', Hora Pref: {target_hour_pref}")

        return {
            "calculated_date_str": calculated_date_str,
            "readable_description": readable_description,
            "target_hour_pref": target_hour_pref
        }

    except Exception as e:
        logger.error(f"❌ Error calculando fecha estructurada: {str(e)}", exc_info=True)
        return {"error": "Ocurrió un error técnico al calcular la fecha."}
