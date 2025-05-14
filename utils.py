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
# --- Constantes y Mapeos ---
# (Asegúrate que GOOGLE_... y otros settings globales estén accesibles si son necesarios aquí,
# aunque esta función trata de ser autocontenida en cuanto a lógica de fechas)

DAYS_EN_TO_ES = {
    "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
    "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sábado", "Sunday": "Domingo",
}
MONTHS_EN_TO_ES = {
    "January": "Enero", "February": "Febrero", "March": "Marzo", "April": "Abril",
    "May": "Mayo", "June": "Junio", "July": "Julio", "August": "Agosto",
    "September": "Septiembre", "October": "Octubre", "November": "Noviembre", "December": "Diciembre",
}
MESES_ES_A_NUM = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12
}
WEEKDAYS_ES_TO_NUM = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
}

# Horarios válidos de inicio de slots (debes tener esta lista igual que en buscarslot.py o importarla)
VALID_SLOT_START_TIMES = [ 
    "09:30", "10:15", "11:00", "11:45",
    "12:30", "13:15", "14:00"
]

# --- Funciones de Ayuda (si no las tienes ya importables) ---
def get_cancun_time():
    """Obtiene la hora actual en Cancún."""
    return datetime.now(pytz.timezone("America/Cancun"))






def format_date_nicely(target_date_obj: date, time_keyword: str = None, weekday_override: str = None) -> str:
    """
    Formatea una fecha a 'DíaDeLaSemana DD de Mes de AAAA' en español.
    Puede incluir la parte del día y un override para el día de la semana si hay conflicto.
    """
    try:
        day_name_en = target_date_obj.strftime("%A")
        day_name_str = DAYS_EN_TO_ES.get(day_name_en, day_name_en.capitalize())

        if weekday_override:
            day_name_str = weekday_override.capitalize()

        month_name_en = target_date_obj.strftime("%B")
        month_name_str = MONTHS_EN_TO_ES.get(month_name_en, month_name_en.capitalize())
        
        formatted = f"{day_name_str} {target_date_obj.day} de {month_name_str} de {target_date_obj.year}"

        if time_keyword == "mañana":
            formatted += ", por la mañana"
        elif time_keyword == "tarde":
            formatted += ", por la tarde"
        # Si se extrajo una hora específica y no es un slot exacto, podríamos añadir "alrededor de las..."
        # Pero por ahora, la descripción se basa en el slot ajustado.
        return formatted
    except Exception as e:
        logger.error(f"Error formateando fecha {target_date_obj}: {e}")
        return target_date_obj.strftime('%Y-%m-%d') # Fallback





def _adjust_time_to_next_valid_slot(requested_time_obj: dt_time, valid_starts: list) -> str | None:
    """
    Ajusta una hora solicitada al inicio del próximo slot válido.
    valid_starts: lista de strings "HH:MM" de inicios de slot válidos.
    """
    # Convertir valid_starts a objetos time
    valid_start_time_objs = []
    for vt in valid_starts:
        try:
            valid_start_time_objs.append(datetime.strptime(vt, "%H:%M").time())
        except ValueError:
            logger.warning(f"Hora de slot inválida en VALID_SLOT_START_TIMES: {vt}")
    
    valid_start_time_objs.sort() # Asegurar orden

    for valid_start in valid_start_time_objs:
        if requested_time_obj <= valid_start:
            return valid_start.strftime("%H:%M")
    
    # Si la hora solicitada es posterior a todos los slots válidos del día (ej. pide 3 PM y el último es 2 PM)
    # Podríamos devolver None para indicar que no hay slot válido ese día a partir de esa hora,
    # o el último slot si la intención es "lo más tarde posible pero no antes de X".
    # Por ahora, si es posterior a todos, no hay ajuste directo posible para ESE MISMO DÍA.
    return None









# === Función Calculadora Principal ===
def calculate_structured_date(
    text_input: str = None,
    day: int = None,
    month: (str | int) = None,
    year: int = None,
    fixed_weekday: str = None,
    relative_time: str = None # "mañana" o "tarde" que la IA puede extraer como parámetro
) -> dict:
    logger.info(
        f"📅 calculate_structured_date INICIADO con: text_input='{text_input}', day={day}, "
        f"month='{month}', year={year}, fixed_weekday='{fixed_weekday}', relative_time='{relative_time}'"
    )
    
    now_cancun = get_cancun_time()
    today = now_cancun.date()

    calculated_date_obj: date | None = None
    weekday_conflict_note_val: str | None = None
    error_val: str | None = None
    requires_confirmation_val: bool = True # Default a True
    search_range_end_date_val: str | None = None
    extracted_specific_time_val: str | None = None # Formato "HH:MM"
    adjusted_specific_time_val: str | None = None # Hora ajustada a slot válido "HH:MM"
    
    time_keyword_from_input: str = (relative_time or "").lower().strip() # "mañana" o "tarde"

    text_input_original: str = (text_input or "").strip()
    text_input_lower: str = text_input_original.lower()
    fixed_weekday_keyword_lower: str = (fixed_weekday or "").lower().strip()

    # 1. Extracción y Ajuste de Hora Específica del text_input
    time_match = re.search(r"(?:a la(?:s)?\s*)?(\b\d{1,2}(?::\d{2})?\b)\s*(am|pm|hrs?\.?)?", text_input_lower, re.IGNORECASE)
    if time_match:
        hour_str = time_match.group(1)
        am_pm_modifier = (time_match.group(2) or "").lower()
        h_parts = hour_str.split(':')
        try:
            h = int(h_parts[0])
            m = int(h_parts[1]) if len(h_parts) > 1 else 0

            if "pm" in am_pm_modifier and h != 12: h += 12 # 12pm es 12:00, 1pm es 13:00
            elif "am" in am_pm_modifier and h == 12: h = 0 # 12am es 00:00

            if 0 <= h <= 23 and 0 <= m <= 59:
                extracted_specific_time_val = f"{h:02d}:{m:02d}"
                logger.info(f"Hora específica extraída de text_input: {extracted_specific_time_val}")
                
                # Ajustar la hora extraída al slot válido más cercano (posterior o igual)
                requested_time_obj_for_adjust = datetime.strptime(extracted_specific_time_val, "%H:%M").time()
                adjusted_time_str = _adjust_time_to_next_valid_slot(requested_time_obj_for_adjust, VALID_SLOT_START_TIMES)
                
                if adjusted_time_str:
                    adjusted_specific_time_val = adjusted_time_str
                    logger.info(f"Hora extraída '{extracted_specific_time_val}' ajustada a slot válido: '{adjusted_specific_time_val}'")
                    if not time_keyword_from_input: # Inferir mañana/tarde del slot ajustado si no se dio
                        adjusted_h = int(adjusted_specific_time_val.split(':')[0])
                        time_keyword_from_input = "tarde" if adjusted_h >= 12 else "mañana"
                else:
                    logger.warning(f"No se pudo ajustar la hora '{extracted_specific_time_val}' a un slot válido de inicio para ese mismo día (podría ser muy tarde).")
                    # Si no se pudo ajustar (ej. pide 10 PM), no debería usarse como filtro estricto,
                    # pero podríamos mantener extracted_specific_time_val para informar al usuario.
                    # Por ahora, si no se ajusta, no se pasa `specific_time_strict`.
                    # error_val = f"La hora {extracted_specific_time_val} no es un horario de inicio de cita válido o es muy tarde."
                    # No ponemos error aquí aún, dejemos que la búsqueda de slot falle si es necesario.
                    pass # extracted_specific_time_val queda, pero adjusted_specific_time_val es None
        except ValueError:
            logger.warning(f"No se pudo parsear la hora extraída: {hour_str}")
            extracted_specific_time_val = None # Resetear si el parseo falló

    # 2. Inferir time_keyword_from_input (mañana/tarde) si no se dio y no se extrajo hora específica
    if not time_keyword_from_input and not adjusted_specific_time_val: # Solo si no se pudo inferir de una hora ajustada
        if "mañana" in text_input_lower and "pasado mañana" not in text_input_lower and "mañana en 8" not in text_input_lower:
            time_keyword_from_input = "mañana"
        elif "tarde" in text_input_lower:
            time_keyword_from_input = "tarde"

    # 3. Lógica de Fecha (D/M/Y y luego frases relativas)
    # (Asegúrate que MESES_ES_A_NUM y WEEKDAYS_ES_TO_NUM estén definidos globalmente o aquí)
    current_year_val = today.year
    
    if day is not None and month is not None : # Prioridad si se dan día y mes
        calculated_year_val = year or current_year_val
        calculated_month_num_val = None
        if isinstance(month, int) and 1 <= month <= 12: calculated_month_num_val = month
        elif isinstance(month, str):
            if month.isdigit() and 1 <= int(month) <= 12: calculated_month_num_val = int(month)
            elif month.lower() in MESES_ES_A_NUM: calculated_month_num_val = MESES_ES_A_NUM[month.lower()]

        if calculated_month_num_val:
            try:
                temp_date = date(calculated_year_val, calculated_month_num_val, day)
                if temp_date < today and year is None: # Si es pasada Y el usuario no fijó el año
                    logger.info(f"Fecha D/M ({day}/{month}) interpretada como pasada ({temp_date}), usando próximo año.")
                    temp_date = date(calculated_year_val + 1, calculated_month_num_val, day)
                
                calculated_date_obj = temp_date
                requires_confirmation_val = False # Fecha específica clara no requiere confirmación
                
                if fixed_weekday_keyword_lower and fixed_weekday_keyword_lower in WEEKDAYS_ES_TO_NUM:
                    # ... (lógica de weekday_conflict_note_val como la tenías) ...
                    # Ejemplo:
                    target_weekday_num = WEEKDAYS_ES_TO_NUM[fixed_weekday_keyword_lower]
                    actual_weekday_num = calculated_date_obj.weekday()
                    if actual_weekday_num != target_weekday_num:
                        actual_day_name_for_note = format_date_nicely(calculated_date_obj).split(' ')[0]
                        month_name_for_note = format_date_nicely(calculated_date_obj).split(' de ')[1].capitalize()
                        weekday_conflict_note_val = (
                            f"Mencionó {fixed_weekday_keyword_lower.capitalize()}, pero el "
                            f"{calculated_date_obj.day} de {month_name_for_note} de {calculated_date_obj.year} "
                            f"es {actual_day_name_for_note}."
                        )
                        requires_confirmation_val = True # Conflicto sí requiere confirmación
            except ValueError:
                error_val = f"La fecha {day}/{month}/{calculated_year_val} no es válida."
        else:
            error_val = f"El mes '{month}' no es válido."

    # Si no se pudo por D/M/Y, o si solo se dio día (ej. "el 16"), usar text_input
    if calculated_date_obj is None and error_val is None:
        requires_confirmation_val = True # Default para frases relativas
        is_this_week_search_flag = False
        
        normalized_text_input = text_input_lower
        # Normalizaciones específicas (tus claves preferidas)
        if "hoy en 8" in text_input_lower or "de hoy en 8" in text_input_lower or "en 8 dias" in text_input_lower or "en ocho dias" in text_input_lower:
            normalized_text_input = "hoy_en_ocho_mx"
        elif "mañana en 8" in text_input_lower or "de mañana en 8" in text_input_lower or "de manana en 8" in text_input_lower:
            normalized_text_input = "manana_en_ocho_mx"
        elif "en 15 dias" in text_input_lower or "en quince dias" in text_input_lower:
            normalized_text_input = "en_quince_dias_mx"
        elif "esta semana" in text_input_lower:
            is_this_week_search_flag = True
            normalized_text_input = "esta semana"
        elif "próxima semana" in text_input_lower or "siguiente semana" in text_input_lower or "semana que viene" in text_input_lower or "semana que entra" in text_input_lower:
            normalized_text_input = "proxima semana"
        elif day is not None and not month and not year: # Si la IA pasó solo 'day' (ej. "el 16")
            normalized_text_input = f"dia_especifico_{day}" # Crear clave única para este caso

        if not normalized_text_input and fixed_weekday_keyword_lower:
            normalized_text_input = "hoy" # Base para calcular próximo día de semana

        if normalized_text_input == "hoy":
            calculated_date_obj = today
            requires_confirmation_val = False
        elif normalized_text_input == "mañana":
            calculated_date_obj = today + timedelta(days=1)
            requires_confirmation_val = False
        elif normalized_text_input == "pasado mañana":
            calculated_date_obj = today + timedelta(days=2)
            requires_confirmation_val = False
        elif normalized_text_input == "hoy_en_ocho_mx":
            calculated_date_obj = today + timedelta(days=7)
        elif normalized_text_input == "manana_en_ocho_mx":
            calculated_date_obj = (today + timedelta(days=1)) + timedelta(days=7)
        elif normalized_text_input == "en_quince_dias_mx":
            calculated_date_obj = today + timedelta(days=14)
        elif normalized_text_input == "en un mes": calculated_date_obj = today + timedelta(days=30)
        elif normalized_text_input == "en dos meses": calculated_date_obj = today + timedelta(days=60)
        elif normalized_text_input == "en tres meses": calculated_date_obj = today + timedelta(days=90)
        elif normalized_text_input == "esta semana":
            calculated_date_obj = today
            days_to_saturday = 5 - today.weekday() # Lunes=0, Sabado=5
            if days_to_saturday >= 0:
                search_range_end_date_val = (today + timedelta(days=days_to_saturday)).strftime('%Y-%m-%d')
            requires_confirmation_val = False # Intención directa de búsqueda
        elif normalized_text_input == "proxima semana":
            days_to_next_monday = (0 - today.weekday() + 7) % 7
            if days_to_next_monday == 0 and today.weekday() == 0: days_to_next_monday = 7
            calculated_date_obj = today + timedelta(days=days_to_next_monday)
        elif normalized_text_input.startswith("dia_especifico_"):
            try:
                specific_day = int(normalized_text_input.split("_")[-1])
                # Asumir mes actual si solo se da el día. Si el día ya pasó este mes, asumir próximo mes.
                calculated_month_for_day = today.month
                calculated_year_for_day = today.year
                if specific_day < today.day:
                    calculated_month_for_day +=1
                    if calculated_month_for_day > 12:
                        calculated_month_for_day = 1
                        calculated_year_for_day +=1
                calculated_date_obj = date(calculated_year_for_day, calculated_month_for_day, specific_day)
                requires_confirmation_val = False # Tratar como fecha específica
            except ValueError:
                error_val = f"El día '{specific_day}' no es válido para el mes calculado."

        # Ajustar por fixed_weekday si se usó text_input y se calculó una base
        if calculated_date_obj and fixed_weekday_keyword_lower and fixed_weekday_keyword_lower in WEEKDAYS_ES_TO_NUM:
            target_weekday_num = WEEKDAYS_ES_TO_NUM[fixed_weekday_keyword_lower]
            days_ahead = (target_weekday_num - calculated_date_obj.weekday() + 7) % 7
            # Si ya es ese día de la semana Y NO es un cálculo de "próxima semana X", avanzar una semana.
            if days_ahead == 0 and not normalized_text_input.startswith("proxima"):
                 days_ahead = 7
            calculated_date_obj += timedelta(days=days_ahead)
            requires_confirmation_val = True # Ajuste por día de semana requiere confirmación

        if not calculated_date_obj and not error_val:
            error_val = f"No logro reconocer la frase '{text_input_original}'. Intente con 'hoy', 'mañana', 'próxima semana', o una fecha específica."

    # 4. Validación final: Fecha no pasada
    if calculated_date_obj and calculated_date_obj < today and error_val is None:
        error_val = f"La fecha {format_date_nicely(calculated_date_obj)} es pasada. Por favor, indique una fecha futura."
        calculated_date_obj = None # Invalidar

    # 5. Preparar diccionario de respuesta
    if error_val:
        logger.warning(f"Error en calculate_structured_date: {error_val}")
        return {"error": error_val}

    if not calculated_date_obj:
        logger.error("calculated_date_obj es None sin error_val seteado al final.")
        return {"error": "No se pudo determinar una fecha válida."}

    target_hour_pref_val = "09:30" # Default
    if time_keyword_from_input == "tarde": target_hour_pref_val = "12:30"
    elif time_keyword_from_input == "mañana": target_hour_pref_val = "09:30"
    
    # Si se ajustó una hora específica a un slot válido, esa es la preferencia más fuerte
    if adjusted_specific_time_val:
        target_hour_pref_val = adjusted_specific_time_val
        # Si se ajustó la hora, usualmente no se requiere confirmación de la fecha si esta era clara
        if not requires_confirmation_val: # Mantiene False si ya era False
            pass 
        else: # Si era True por ej. por "próxima semana a las 10", se vuelve False por la hora específica.
             requires_confirmation_val = False 
    elif extracted_specific_time_val and not adjusted_specific_time_val:
        # Si se extrajo hora pero no se pudo ajustar (ej. "a las 10 PM"),
        # no la usamos para target_hour_pref, pero la IA podría mencionarlo.
        # La IA NO pasará esta hora como specific_time_strict a find_next_available_slot.
        # No cambiamos target_hour_pref_val aquí, se queda con mañana/tarde o default.
        logger.info(f"Se extrajo hora '{extracted_specific_time_val}' pero no se ajustó a slot; se usará preferencia mañana/tarde.")


    readable_description_val = format_date_nicely(
        calculated_date_obj,
        time_keyword_from_input, # Para "por la mañana/tarde"
        weekday_override=fixed_weekday_keyword_lower if weekday_conflict_note_val else None
    )
    # Si se ajustó una hora, incorporar la hora ajustada en la descripción
    if adjusted_specific_time_val:
         readable_description_val += f" a las {datetime.strptime(adjusted_specific_time_val, '%H:%M').strftime('%I:%M %p').lower()}"


    if is_this_week_search_flag and not adjusted_specific_time_val and not extracted_specific_time_val : # Solo si es "esta semana" sin hora específica
        readable_description_val = "para buscar disponibilidad esta semana"
        if time_keyword_from_input == "mañana": readable_description_val += ", por la mañana"
        elif time_keyword_from_input == "tarde": readable_description_val += ", por la tarde"


    calculated_date_str_val = calculated_date_obj.strftime('%Y-%m-%d')

    response = {
        "calculated_date_str": calculated_date_str_val,
        "readable_description": readable_description_val,
        "target_hour_pref": target_hour_pref_val,
        "relative_time_keyword": time_keyword_from_input if time_keyword_from_input else None,
        "extracted_specific_time": adjusted_specific_time_val, # DEVOLVEMOS LA AJUSTADA (o None si no se pudo ajustar)
        "search_range_end_date": search_range_end_date_val,
        "requires_confirmation": requires_confirmation_val,
        "weekday_conflict_note": weekday_conflict_note_val,
        "error": None # Si llegamos aquí, no hay error
    }
    
    final_response = {k: v for k, v in response.items() if v is not None}
    logger.info(f"✅ calculate_structured_date RETORNANDO: {final_response}")
    return final_response