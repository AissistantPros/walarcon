# -*- coding: utf-8 -*-
"""
Módulo para buscar el siguiente horario disponible en Google Calendar.
Optimizado para manejar prioridades y fechas específicas.
"""

from datetime import datetime, timedelta
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from utils import get_cancun_time  # Ajuste importante para zona horaria correcta
import pytz
from decouple import config
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_PRIVATE_KEY = config("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")

def initialize_google_calendar():
    """
    Inicializa la conexión con Google Calendar usando credenciales de servicio.
    """
    try:
        credentials = Credentials.from_service_account_info({
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        }, scopes=["https://www.googleapis.com/auth/calendar"])
        return build("calendar", "v3", credentials=credentials)
    except Exception as e:
        logger.error(f"Error al conectar con Google Calendar: {str(e)}")
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")

def check_availability(start_time, end_time):
    """
    Verifica si un horario está disponible en Google Calendar.
    Retorna True si está libre, False si está ocupado.
    """
    try:
        service = initialize_google_calendar()
        events = service.freebusy().query(body={
            "timeMin": start_time.isoformat(),
            "timeMax": end_time.isoformat(),
            "timeZone": "America/Cancun",
            "items": [{"id": GOOGLE_CALENDAR_ID}]
        }).execute()
        return len(events["calendars"][GOOGLE_CALENDAR_ID]["busy"]) == 0
    except Exception as e:
        logger.error(f"Error al verificar disponibilidad en Google Calendar: {str(e)}")
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")

# ==================================================
# 🔹 Búsqueda del Próximo Horario Disponible
# ==================================================
def find_next_available_slot(target_date=None, target_hour=None, urgent=False):
    """
    Busca el siguiente horario disponible en Google Calendar.

    Parámetros:
      target_date (datetime, opcional): Fecha específica para buscar disponibilidad.
      target_hour (str, opcional): Hora preferida (HH:MM). 
        - Si no coincide exactamente con los slots, buscaremos el siguiente slot >= esa hora.
      urgent (bool, opcional): Si True, evita los próximos 4 horas y arranca búsqueda después.
    
    Retorna:
      dict: {"start_time": <ISO>, "end_time": <ISO>} con la cita encontrada
      o {"error": "..."} si no hay disponibilidad.
    """

    try:
        # [CAMBIO] Límite de días para evitar bucles infinitos
        MAX_DAYS_LOOKAHEAD = 180  # 6 meses

        slot_times = [
            {"start": "09:30", "end": "10:15"},
            {"start": "10:15", "end": "11:00"},
            {"start": "11:00", "end": "11:45"},
            {"start": "11:45", "end": "12:30"},
            {"start": "12:30", "end": "13:15"},
            {"start": "13:15", "end": "14:00"},
            {"start": "14:00", "end": "14:45"},
        ]

        now = get_cancun_time()
        if target_date:
            start_day = target_date
        elif urgent:
            start_day = now + timedelta(hours=4)  # Omite las próximas 4 horas
        else:
            start_day = now

        day_offset = 0

        while True:
            if day_offset > MAX_DAYS_LOOKAHEAD:
                logger.warning("Se ha excedido el límite de búsqueda de días.")
                return {"error": "No se encontró disponibilidad en los próximos 6 meses."}

            day = start_day + timedelta(days=day_offset)
            # Saltar domingos
            if day.weekday() == 6:
                day_offset += 1
                continue

            # [CAMBIO 2] Si el usuario pidió 'target_hour', 
            # buscaremos slots >= esa hora en el día.
            for slot in slot_times:
                # Convierto el slot start a un objeto time
                slot_time_obj = datetime.strptime(slot["start"], "%H:%M").time()
                if target_hour:
                    requested_time = datetime.strptime(target_hour, "%H:%M").time()
                    # Si el slot es antes que la hora solicitada, continuar
                    if slot_time_obj < requested_time:
                        continue

                # Armo los strings
                start_time_str = f"{day.strftime('%Y-%m-%d')} {slot['start']}:00"
                end_time_str = f"{day.strftime('%Y-%m-%d')} {slot['end']}:00"

                # Convierto a datetime con tz Cancún
                start_dt = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))
                end_dt   = datetime.strptime(end_time_str,   "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))

                # Si urgent y day_offset == 0, descartar slots que ocurran < ahora + 4h
                if urgent and day_offset == 0 and start_dt <= now + timedelta(hours=4):
                    continue

                # Revisar disponibilidad real en Calendar
                if check_availability(start_dt, end_dt):
                    logger.info(f"✅ Horario disponible encontrado: {start_dt} - {end_dt}")
                    return {
                        "start_time": start_dt.isoformat(),
                        "end_time": end_dt.isoformat()
                    }

            # Pasar al siguiente día
            day_offset += 1

    except ConnectionError as ce:
        logger.warning(f"⚠️ Error al buscar horario: {str(ce)}")
        raise
    except Exception as e:
        logger.error(f"❌ Error inesperado al buscar horario: {str(e)}")
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")

# ==================================================
# 🔹 Prueba Local
# ==================================================
if __name__ == "__main__":
    try:
        slot = find_next_available_slot()
        if isinstance(slot, dict) and "start_time" in slot:
            print(f"✅ Próximo horario disponible: {slot['start_time']} - {slot['end_time']}")
        else:
            print(f"❌ No se encontró horario: {slot}")
    except ConnectionError as ce:
        print(f"⚠️ Error de conexión: {str(ce)}")
    except Exception as e:
        print(f"❌ Error desconocido: {str(e)}")
