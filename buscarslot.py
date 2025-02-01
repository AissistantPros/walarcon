# -*- coding: utf-8 -*-
"""
Módulo para buscar el siguiente horario disponible en Google Calendar.
Utiliza la API de Google Calendar para verificar disponibilidad.
"""





# ==================================================
# Parte 1 📌 Importaciones y Configuración
# ==================================================
from datetime import datetime, timedelta
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from utils import get_cancun_time  # Ajuste importante para zona horaria correcta
import pytz
from decouple import config
import logging

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variables de configuración de Google Calendar
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_PRIVATE_KEY = config("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")







# ==================================================
# Parte 2 🔹 Inicialización de Google Calendar
# ==================================================
def initialize_google_calendar():
    """
    Inicializa la conexión con Google Calendar usando credenciales de servicio.
    
    Retorna:
        service (obj): Cliente autenticado de Google Calendar.
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
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")  # Error manejado






# ==================================================
# Parte 3 🔹 Verificación de Disponibilidad
# ==================================================
def check_availability(start_time, end_time):
    """
    Verifica si un horario está disponible en Google Calendar.

    Parámetros:
        start_time (datetime): Hora de inicio de la cita.
        end_time (datetime): Hora de fin de la cita.

    Retorna:
        bool: True si el horario está libre, False si está ocupado.
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
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")  # Error manejado







# ==================================================
# Parte 4 🔹 Búsqueda del Próximo Horario Disponible
# ==================================================
def find_next_available_slot():
    """
    Busca el siguiente horario disponible en Google Calendar.
    
    Retorna:
        dict: Horario disponible con formato {"start_time": datetime, "end_time": datetime}.
    """
    try:
        # Definir horarios estándar de atención
        slot_times = [
            {"start": "09:30", "end": "10:15"},
            {"start": "10:15", "end": "11:00"},
            {"start": "11:00", "end": "11:45"},
            {"start": "11:45", "end": "12:30"},
            {"start": "12:30", "end": "13:15"},
            {"start": "13:15", "end": "14:00"},
            {"start": "14:00", "end": "14:45"},
        ]

        # Obtener la hora actual en Cancún
        now = get_cancun_time()

        # Iniciar la búsqueda desde el día actual en adelante
        day_offset = 0
        while True:
            day = now + timedelta(days=day_offset)

            # Saltar domingos
            if day.weekday() == 6:  
                day_offset += 1
                continue

            for slot in slot_times:
                # Formatear fechas de inicio y fin en formato ISO
                start_time_str = f"{day.strftime('%Y-%m-%d')} {slot['start']}:00"
                end_time_str = f"{day.strftime('%Y-%m-%d')} {slot['end']}:00"

                # Convertir a objetos datetime con zona horaria
                start_time = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))
                end_time = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))

                # Omitir horarios en las próximas 4 horas del mismo día
                if day_offset == 0 and start_time <= now + timedelta(hours=4):
                    continue

                # Verificar si el horario está disponible
                if check_availability(start_time, end_time):
                    logger.info(f"Horario disponible encontrado: {start_time} - {end_time}")
                    return {"start_time": start_time, "end_time": end_time}

            # Si no encuentra en el día actual, pasa al siguiente día
            day_offset += 1

    except ConnectionError as ce:
        # Error ya mapeado en `initialize_google_calendar()` y `check_availability()`
        logger.warning(f"Error al buscar horario: {str(ce)}")
        raise
    except Exception as e:
        logger.error(f"Error inesperado al buscar horario: {str(e)}")
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")  # Error general






# ==================================================
# Parte 5 🔹 Prueba Local del Módulo
# ==================================================
if __name__ == "__main__":
    """
    Prueba rápida para verificar el funcionamiento de la búsqueda de horarios.
    Se recomienda ejecutar este script directamente para depuración.
    """
    try:
        slot = find_next_available_slot()
        if slot:
            print(f"✅ Próximo horario disponible: {slot['start_time']} - {slot['end_time']}")
        else:
            print("❌ No se encontraron horarios disponibles.")
    except ConnectionError as ce:
        print(f"⚠️ Error de conexión: {str(ce)}")
    except Exception as e:
        print(f"❌ Error desconocido: {str(e)}")
