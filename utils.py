# -*- coding: utf-8 -*-
#utils.py
"""
Módulo de utilidades para integración con Google APIs y manejo de tiempo.
"""

import os
import json
import logging
import threading
from datetime import datetime, timedelta, date # Asegúrate que 'date' está aquí
import pytz
from dotenv import load_dotenv
from decouple import config
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
# from dateutil.parser import parse # No se usa directamente en este archivo ahora
import locale # No se usa directamente en este archivo ahora
import re
from typing import Dict, Optional, List, Any # Añadido Any y List
from state_store import session_state


      


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

# -----------------------------------------------------------------------------
# FUNCIONES DE UTILIDAD PARA FORMATEO DE FECHA Y HORA (Centralizadas aquí)
# -----------------------------------------------------------------------------

NUMEROS_A_PALABRAS: Dict[int, str] = {
    0: "en punto", 1: "una", 2: "dos", 3: "tres", 4: "cuatro", 5: "cinco",
    6: "seis", 7: "siete", 8: "ocho", 9: "nueve", 10: "diez",
    11: "once", 12: "doce", 13: "trece", 14: "catorce", 15: "quince",
    16: "dieciséis", 17: "diecisiete", 18: "dieciocho", 19: "diecinueve",
    20: "veinte", 21: "veintiuno", 22: "veintidós", 23: "veintitrés", 24: "veinticuatro",
    25: "veinticinco", 26: "veintiséis", 27: "veintisiete", 28: "veintiocho", 29: "veintinueve",
    30: "treinta", 40: "cuarenta", 45: "cuarenta y cinco", 50: "cincuenta",
    # Para minutos específicos no listados, la función puede construir "treinta y uno", "cuarenta y dos", etc.
    # O puedes añadir más aquí si prefieres, ej: 31: "treinta y una" (si es la hora)
}

def convertir_hora_a_palabras(hhmm_str: str) -> str:
    """
    Convierte una cadena de hora "HH:MM" a un formato en palabras más natural
    para TTS, incluyendo "de la mañana", "de la tarde", etc.
    Ej: "09:30" -> "nueve treinta de la mañana"
        "12:15" -> "doce quince del mediodía"
        "14:00" -> "dos en punto de la tarde"
    """
    try:
        h, m = map(int, hhmm_str.split(':'))

        sufijo_horario = "de la mañana" # Valor por defecto
        display_h = h

        if h == 0: # Medianoche
            display_h = 12
            sufijo_horario = "de la madrugada" # o "de la noche"
        elif h == 12: # Mediodía
            sufijo_horario = "del mediodía"
        elif 13 <= h <= 17: # Tarde (1 PM a 5 PM)
            display_h = h - 12
            sufijo_horario = "de la tarde"
        elif h >= 18: # Tarde-Noche (6 PM en adelante)
            display_h = h - 12
            sufijo_horario = "de la noche" # o "de la tarde-noche"
        # Horas AM (1-11) ya tienen display_h = h y sufijo_horario "de la mañana"

        hora_palabra = NUMEROS_A_PALABRAS.get(display_h, str(display_h))
        # Caso especial para "una" de la mañana/tarde en lugar de "uno"
        if display_h == 1 and (sufijo_horario == "de la mañana" or sufijo_horario == "de la tarde" or sufijo_horario == "de la madrugada"):
            hora_palabra = "una"


        minuto_palabra = ""
        if m == 0:
            minuto_palabra = "en punto"
        elif m == 15:
            minuto_palabra = "quince"
        elif m == 30:
            # Preferir "y media" excepto para las 12:30 ("doce y treinta")
            minuto_palabra = "y media" if display_h != 12 else "treinta"
        elif m == 45:
            minuto_palabra = "cuarenta y cinco"
        elif m < 30 : # Para minutos como 01-29 no cubiertos
            if m in NUMEROS_A_PALABRAS:
                minuto_palabra = NUMEROS_A_PALABRAS[m]
            elif m > 20: # veintiuno, veintidós...
                 minuto_palabra = f"veinti{NUMEROS_A_PALABRAS.get(m % 10, str(m % 10))}" if m % 10 != 0 else "veinte" # ej. veintidós
                 if m % 10 == 1 and m != 21: minuto_palabra = f"veintiún" # veintiún (antes de sustantivo) - no aplica aquí directamente
                 elif m == 21 : minuto_palabra = "veintiuno"

            else: # Para 1-19 no especiales
                minuto_palabra = str(m) # Fallback
        elif m > 30: # Para minutos como 31-59 no cubiertos
            decena = (m // 10) * 10
            unidad = m % 10
            if unidad == 0: # cuarenta, cincuenta
                minuto_palabra = NUMEROS_A_PALABRAS.get(decena, str(decena))
            else: # treinta y uno, cuarenta y dos...
                palabra_decena = NUMEROS_A_PALABRAS.get(decena, str(decena))
                palabra_unidad = NUMEROS_A_PALABRAS.get(unidad, str(unidad))
                minuto_palabra = f"{palabra_decena} y {palabra_unidad}"
        else: # Fallback si algo se escapa
            minuto_palabra = str(m)

        # Ajustes finales para frases comunes
        if minuto_palabra == "en punto":
            return f"las {hora_palabra} {minuto_palabra} {sufijo_horario}"
        if minuto_palabra == "y media":
             return f"las {hora_palabra} y media {sufijo_horario}"

        return f"las {hora_palabra} {minuto_palabra} {sufijo_horario}"

    except Exception as e:
        logger.error(f"Error convirtiendo hora '{hhmm_str}' a palabras: {e}")
        # Fallback a una representación numérica simple si falla la conversión a palabras
        try:
            t_obj = datetime.strptime(hhmm_str, "%H:%M")
            suf = "de la mañana" if t_obj.hour < 12 else "de la tarde"
            if t_obj.hour == 12: suf = "del mediodía"
            elif t_obj.hour == 0: suf = "de la madrugada"
            
            hora_fmt = t_obj.strftime("%I").lstrip("0")
            min_fmt = t_obj.strftime("%M")
            return f"las {hora_fmt}:{min_fmt} {suf}"
        except: # Fallback absoluto
            return hhmm_str


DAYS_EN_TO_ES = {
    "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
    "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sábado",
    "Sunday": "Domingo",
}
MONTHS_EN_TO_ES = {
    "January": "Enero", "February": "Febrero", "March": "Marzo", "April": "Abril",
    "May": "Mayo", "June": "Junio", "July": "Julio", "August": "Agosto",
    "September": "Septiembre", "October": "Octubre", "November": "Noviembre", "December": "Diciembre",
}

def format_date_nicely(target_date_obj: date, time_keyword: Optional[str] = None,
                       weekday_override: Optional[str] = None,
                       specific_time_hhmm: Optional[str] = None) -> str:
    """
    Formatea una fecha y opcionalmente una hora en una cadena amigable para el usuario.
    Usa convertir_hora_a_palabras para la parte de la hora.
    """
    day_name_es = DAYS_EN_TO_ES.get(target_date_obj.strftime("%A"), target_date_obj.strftime("%A"))
    if weekday_override:
        day_name_es = weekday_override.capitalize()
    month_es = MONTHS_EN_TO_ES.get(target_date_obj.strftime("%B"), target_date_obj.strftime("%B"))
    text = f"{day_name_es} {target_date_obj.day} de {month_es}"

    if specific_time_hhmm: # specific_time_hhmm es una cadena como "09:30"
        try:
            hora_en_palabras = convertir_hora_a_palabras(specific_time_hhmm)
            text += f" a {hora_en_palabras}" # "a las nueve treinta de la mañana"
        except Exception as e:
            logger.warning(f"Fallback en format_date_nicely para la hora '{specific_time_hhmm}': {e}")
            # Fallback si convertir_hora_a_palabras tiene problemas, aunque no debería si está bien probada
            text += f" a las {specific_time_hhmm}"
    elif time_keyword == "mañana":
        text += ", por la mañana"
    elif time_keyword == "tarde":
        text += ", por la tarde"
    return text




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





# -----------------------------------------------------------------------------
# FUNCIÓN search_calendar_event_by_phone (MODIFICADA)
# -----------------------------------------------------------------------------
def search_calendar_event_by_phone(phone: str) -> List[Dict[str, Any]]:
    """
    Busca citas por número de teléfono y devuelve una lista de diccionarios
    con una estructura clara para la IA.
    """
    logger.info(f"Iniciando búsqueda de citas para el teléfono: {phone}")
    try:
        service = initialize_google_calendar()
        # Google Calendar API espera la hora en UTC para timeMin
        # Usamos la fecha actual de Cancún, convertida a inicio del día en UTC para no perder eventos del día
        now_cancun = get_cancun_time()
        start_of_today_cancun = now_cancun.replace(hour=0, minute=0, second=0, microsecond=0)
        time_min_utc_iso = start_of_today_cancun.astimezone(pytz.utc).isoformat()

        logger.debug(f"Buscando eventos en Google Calendar para el teléfono: {phone} desde {time_min_utc_iso} (UTC).")

        events_result = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            q=phone,
            timeMin=time_min_utc_iso, # Solo citas desde el inicio del día de hoy (en UTC) hacia adelante
            singleEvents=True,
            orderBy="startTime"
        ).execute()
        
        items = events_result.get("items", [])
        logger.info(f"Google Calendar API encontró {len(items)} eventos crudos para el teléfono {phone}.")

        parsed_events: List[Dict[str, Any]] = []
        for evt_idx, evt in enumerate(items):
            logger.debug(f"Procesando evento crudo #{evt_idx + 1}: ID {evt.get('id')}, Summary: {evt.get('summary')}")
            summary = evt.get("summary", "Paciente Desconocido")
            description = evt.get("description", "")
            
            motive = None
            phone_in_desc = None
            
            lines = description.split("\n")
            for line in lines:
                line_lower = line.lower()
                # Búsqueda más robusta para teléfono y motivo
                if re.search(r"tel[eé]fono\s*:", line_lower):
                    phone_in_desc = re.sub(r"[^\d\s\+\-\(\)]", "", line.split(":", 1)[-1]).strip() # Limpia un poco más
                    phone_in_desc = re.sub(r"\s+", "", phone_in_desc) # Quita espacios internos
                if re.search(r"motivo\s*:", line_lower):
                    motive = line.split(":", 1)[-1].strip()

            start_utc_str = evt.get("start", {}).get("dateTime")
            # end_utc_str = evt.get("end", {}).get("dateTime") # No se usa en el dict de salida actualmente

            start_cancun_dt_obj: Optional[datetime] = None
            start_cancun_pretty_str: str = "Fecha/hora no disponible"
            start_cancun_iso_for_tool_str: Optional[str] = None

            if start_utc_str:
                try:
                    start_cancun_dt_obj = convert_utc_to_cancun(start_utc_str)
                    start_cancun_iso_for_tool_str = start_cancun_dt_obj.isoformat()
                    start_cancun_pretty_str = format_date_nicely(
                        start_cancun_dt_obj.date(), 
                        specific_time_hhmm=start_cancun_dt_obj.strftime("%H:%M")
                    )
                except Exception as e_conv:
                    logger.error(f"Error convirtiendo/formateando fecha para evento ID {evt.get('id')}, start_utc_str '{start_utc_str}': {e_conv}")
            else:
                logger.warning(f"Evento ID {evt.get('id')} no tiene start.dateTime.")


            cita_parseada = {
                "event_id": evt.get("id"), # ID real de Google Calendar
                "patient_name": summary,   # Nombre del paciente (del campo summary de Google)
                "start_time_iso_utc": start_utc_str, # Hora de inicio original en UTC
                "start_time_cancun_iso": start_cancun_iso_for_tool_str, # Hora de inicio en Cancún ISO (para herramientas)
                "start_time_cancun_pretty": start_cancun_pretty_str, # Hora de inicio formateada (para leer al usuario)
                "appointment_reason": motive if motive else "No especificado", # Motivo extraído
                "phone_in_description": phone_in_desc # Teléfono de la descripción
            }
            # Guarda el ID real para que la tool de borrado lo use si GPT manda un placeholder
            session_state["last_event_found"] = cita_parseada["event_id"]


            parsed_events.append(cita_parseada)
            logger.debug(f"Evento parseado y añadido: {cita_parseada}")

            
            # Guardar la lista completa y un ID por defecto en la memoria de la llamada
            session_state["events_found"] = parsed_events
            if parsed_events:
                session_state["current_event_id"] = parsed_events[0]["event_id"]  # la primera por defecto
           

        if not parsed_events:
            logger.info(f"No se encontraron citas parseables para el teléfono {phone} que cumplan los criterios.")
        else:
            logger.info(f"Se parsearon {len(parsed_events)} citas para el teléfono {phone}.")
            
        return parsed_events

    except Exception as e:
        logger.error(f"❌ Error general en search_calendar_event_by_phone para el teléfono {phone}: {str(e)}", exc_info=True)
        return [] # Devolver lista vacía en caso de error mayor








