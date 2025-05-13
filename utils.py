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

# --- Mapeos para traducción de fecha a español ---
# (Puedes colocar estos mapeos al inicio del archivo utils.py o justo antes de la función)
DAYS_EN_TO_ES = {
    "Monday": "Lunes",
    "Tuesday": "Martes",
    "Wednesday": "Miércoles",
    "Thursday": "Jueves",
    "Friday": "Viernes",
    "Saturday": "Sábado",
    "Sunday": "Domingo",
}

MONTHS_EN_TO_ES = {
    "January": "Enero",
    "February": "Febrero",
    "March": "Marzo",
    "April": "Abril",
    "May": "Mayo",
    "June": "Junio",
    "July": "Julio",
    "August": "Agosto",
    "September": "Septiembre",
    "October": "Octubre",
    "November": "Noviembre",
    "December": "Diciembre",
}

def format_date_nicely(target_date: date, relative_time: str = None, weekday_override: str = None) -> str:
    """
    Formatea una fecha a 'DíaDeLaSemana DD de Mes de AAAA' en español.
    Puede incluir la parte del día y un override para el día de la semana si hay conflicto.
    Utiliza mapeos internos para asegurar nombres en español independientemente del locale.
    """
    try:
        # Obtener el nombre del día en inglés usando strftime como clave
        day_name_en = target_date.strftime("%A")
        # Traducir usando el mapeo, con fallback al inglés capitalizado si no se encuentra (poco probable)
        day_name_str = DAYS_EN_TO_ES.get(day_name_en, day_name_en.capitalize())

        if weekday_override:  # Si hay un conflicto y queremos mostrar el día que el usuario dijo
            # Asumimos que weekday_override ya viene en español y capitalizado si es necesario,
            # o lo capitalizamos aquí. Si weekday_override viene de fixed_weekday_keyword,
            # ese keyword ya debería estar en español (ej. 'martes').
            day_name_str = weekday_override.capitalize()

        # Obtener el nombre del mes en inglés usando strftime como clave
        month_name_en = target_date.strftime("%B")
        # Traducir usando el mapeo, con fallback al inglés capitalizado
        month_name_str = MONTHS_EN_TO_ES.get(month_name_en, month_name_en.capitalize())
        
        formatted = f"{day_name_str} {target_date.day} de {month_name_str} de {target_date.year}"

        if relative_time == "mañana":
            formatted += ", por la mañana"
        elif relative_time == "tarde":
            formatted += ", por la tarde"
        return formatted
    except Exception as e:
        logger.error(f"Error formateando fecha {target_date}: {e}")
        # Fallback a formato ISO si todo lo demás falla
        return target_date.strftime('%Y-%m-%d')





# === Función Calculadora REFORZADA ===
def calculate_structured_date(
    text_input: str = None,
    day: int = None,
    month: (str | int) = None,
    year: int = None,
    fixed_weekday: str = None,
    relative_time: str = None
) -> dict:
    """
    Calcula una fecha objetivo basada en componentes de fecha o texto relativo.
    Prioriza componentes numéricos (day, month, year) si se proporcionan.
    Maneja discrepancias entre 'fixed_weekday' y la fecha numérica.

    Args:
        text_input (str, optional): Frase relativa como 'hoy', 'mañana', 'próxima semana'.
                                     Usado si day, month, year no son suficientes.
        day (int, optional): Número del día (1-31).
        month (str|int, optional): Nombre del mes ('enero', 'febrero') o número (1-12).
        year (int, optional): Año (ej. 2025).
        fixed_weekday (str, optional): 'lunes', 'martes', ..., 'domingo'.
        relative_time (str, optional): 'mañana' (am) o 'tarde' (pm).

    Returns:
        dict: {'calculated_date_str': 'YYYY-MM-DD',
               'readable_description': 'Martes 20 de mayo de 2025, por la tarde',
               'target_hour_pref': 'HH:MM',
               'weekday_conflict_note': 'Nota sobre discrepancia de día (opcional)'}
              o {'error': 'Mensaje de error'}
    """
    logger.info(f"📅 Calculando fecha: text='{text_input}', d={day},m={month},y={year}, wd='{fixed_weekday}', rt='{relative_time}'")
    try:
        now = get_cancun_time()
        today = now.date()
        
        # Normalizar inputs
        text_input_keyword = (text_input or "").lower().strip()
        fixed_weekday_keyword = (fixed_weekday or "").lower().strip()
        time_keyword = (relative_time or "").lower().strip()

        # Mapeo de meses y días
        meses_es_a_num = {
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
            "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12
        }
        weekdays_es_to_num = {"lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6}
        num_to_weekdays_es = {v: k.capitalize() for k, v in weekdays_es_to_num.items() if k not in ["miercoles", "sabado"]} # Para notas

        target_date = None
        weekday_conflict_note = None
        
        # --- Prioridad 1: Usar day, month, year si están completos ---
        current_year = today.year
        calculated_year = year or current_year
        
        calculated_month_num = None
        if isinstance(month, int):
            calculated_month_num = month
        elif isinstance(month, str) and month.lower() in meses_es_a_num:
            calculated_month_num = meses_es_a_num[month.lower()]
        elif month is None and day is not None: # Si solo se da día, asumir mes actual
            calculated_month_num = today.month
            if year is None: # Y año actual si no se dio año
                 calculated_year = today.year

        if day is not None and calculated_month_num is not None:
            try:
                # Si el año es menor que el actual, y el mes ya pasó o es el actual y el día ya pasó, asumir el próximo año
                if calculated_year < current_year or \
                   (calculated_year == current_year and calculated_month_num < today.month) or \
                   (calculated_year == current_year and calculated_month_num == today.month and day < today.day):
                    if year is None: # Solo incrementa el año si no fue fijado explícitamente por el usuario
                        calculated_year += 1
                        logger.info(f"Fecha {day}/{calculated_month_num}/{year or current_year} interpretada como del próximo año: {calculated_year}")

                target_date = date(calculated_year, calculated_month_num, day)

                # Validar discrepancia con fixed_weekday
                if fixed_weekday_keyword and fixed_weekday_keyword in weekdays_es_to_num:
                    target_weekday_num_from_keyword = weekdays_es_to_num[fixed_weekday_keyword]
                    actual_weekday_num_of_date = target_date.weekday()
                    if actual_weekday_num_of_date != target_weekday_num_from_keyword:
                        actual_day_name = target_date.strftime("%A").capitalize() # Usará locale (inglés si falla)
                        try: # Intentar obtener nombre en español para la nota
                            actual_day_name = format_date_nicely(target_date).split(' ')[0]
                        except: pass
                        
                        weekday_conflict_note = (
                            f"Mencionó {fixed_weekday_keyword.capitalize()}, pero el "
                            f"{target_date.day} de {target_date.strftime('%B').capitalize()} de {target_date.year} "
                            f"es {actual_day_name}."
                        )
                        # Por ahora, priorizamos la fecha numérica. La IA usará esta nota.
                        logger.warning(f"Discrepancia de día: {weekday_conflict_note}")

            except ValueError: # Día inválido para el mes/año (ej. 30 de Feb)
                return {"error": f"La fecha {day}/{month}/{calculated_year} no es válida."}
        
        # --- Prioridad 2: Usar text_input para frases relativas si no se formó una fecha numérica ---
        if target_date is None:
            base_date_for_relative = today
            is_relative_week = False

            if not text_input_keyword and fixed_weekday_keyword: # Solo día de la semana
                 text_input_keyword = "hoy" # Para que la lógica de 'próximo día' funcione desde hoy

            if text_input_keyword == "hoy":
                target_date = today
            elif text_input_keyword == "mañana":
                target_date = today + timedelta(days=1)
            elif text_input_keyword == "pasado mañana":
                target_date = today + timedelta(days=2)
            elif text_input_keyword == "hoy en ocho":
                target_date = today + timedelta(days=7)
            elif text_input_keyword == "de mañana en ocho":
                target_date = today + timedelta(days=8)
            elif text_input_keyword == "en 15 dias":
                target_date = today + timedelta(days=15)
            elif text_input_keyword == "en un mes":
                target_date = today + timedelta(days=30)
            elif text_input_keyword == "en dos meses":
                target_date = today + timedelta(days=60)
            elif text_input_keyword == "en tres meses":
                target_date = today + timedelta(days=90)
            elif "semana" in text_input_keyword: # "proxima semana", "siguiente semana", etc.
                is_relative_week = True
                days_until_monday = (0 - today.weekday() + 7) % 7
                if days_until_monday == 0: days_until_monday = 7
                base_date_for_relative = today + timedelta(days=days_until_monday)
                target_date = base_date_for_relative # Asignar a target_date
            elif not text_input_keyword and not fixed_weekday_keyword: # No se dio nada útil
                return {"error": "No especificó una fecha o término relativo que pueda entender."}
            elif not text_input_keyword and fixed_weekday_keyword: # Solo se dio un dia de semana, base es hoy
                 target_date = today
            else: # Frase no reconocida en text_input
                return {"error": f"No reconozco el término relativo '{text_input}'. Intenta con 'hoy', 'mañana', 'próxima semana', o una fecha específica."}

            # Ajustar por fixed_weekday si se usó text_input y se dio fixed_weekday
            # (ej. text_input="proxima semana", fixed_weekday="martes")
            if fixed_weekday_keyword and fixed_weekday_keyword in weekdays_es_to_num:
                # Usamos target_date (que ya podría ser el lunes de la prox sem) como base
                current_base_for_weekday = target_date 
                target_weekday_num_from_keyword = weekdays_es_to_num[fixed_weekday_keyword]
                days_ahead = (target_weekday_num_from_keyword - current_base_for_weekday.weekday() + 7) % 7
                
                # Si el día calculado es el mismo que la base Y NO era una referencia a "próxima semana"
                # O si es una referencia a "próxima semana" y el día base ya es el día deseado (ej. lunes de prox sem, y se pide lunes)
                if days_ahead == 0:
                    # Para "el [día]" o "próximo [día]", si cae hoy, queremos el de la sig. semana.
                    # Para "[día] de la próxima semana", si el cálculo ya dio ese día, no sumar 7.
                    if not is_relative_week or (is_relative_week and current_base_for_weekday.weekday() == target_weekday_num_from_keyword):
                         days_ahead = 7
                target_date = current_base_for_weekday + timedelta(days=days_ahead)

        # --- Validación Final: Asegurar que la fecha no sea pasada ---
        if target_date < today:
            # Si después de todos los cálculos la fecha es pasada (ej. "el lunes" y hoy es viernes)
            # y no era una fecha numérica específica que ya se ajustó para el futuro.
            # Intentamos avanzar 7 días como última medida si es un día de semana.
            if fixed_weekday_keyword and not (day and month): # Si no venía de una fecha D/M/A explícita
                 logger.warning(f"Fecha calculada ({target_date}) era pasada, intentando +7 días.")
                 target_date += timedelta(days=7)
                 if target_date < today: # Aún pasada, error.
                      return {"error": f"La fecha calculada ({target_date.strftime('%Y-%m-%d')}) sigue siendo en el pasado."}
            else: # Si era fecha D/M/A o no se puede ajustar más
                 return {"error": f"La fecha calculada ({target_date.strftime('%Y-%m-%d')}) es en el pasado."}


        # --- Determinar preferencia de hora ---
        target_hour_pref = "09:30" # Default
        if time_keyword == "tarde":
            target_hour_pref = "12:30"
        elif time_keyword == "mañana":
            target_hour_pref = "09:30"

        # --- Formatear resultados ---
        calculated_date_str = target_date.strftime('%Y-%m-%d')
        # Pasamos el fixed_weekday_keyword original para que la descripción use el día que dijo el usuario si hay conflicto
        readable_description = format_date_nicely(target_date, time_keyword or None, 
                                                 weekday_override=fixed_weekday_keyword if weekday_conflict_note else None)

        logger.info(f"✅ Fecha calculada: {calculated_date_str}, Desc: '{readable_description}', Hora Pref: {target_hour_pref}, Conflicto: {weekday_conflict_note}")

        response = {
            "calculated_date_str": calculated_date_str,
            "readable_description": readable_description,
            "target_hour_pref": target_hour_pref
        }
        if weekday_conflict_note:
            response["weekday_conflict_note"] = weekday_conflict_note
        
        return response

    except Exception as e:
        logger.error(f"❌ Error calculando fecha estructurada: {str(e)}", exc_info=True)
        return {"error": "Ocurrió un error técnico al calcular la fecha."}

# --- Fin de la función ---