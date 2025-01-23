from datetime import datetime, timedelta
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from utils import get_cancun_time
import pytz
from decouple import config

# **SECCIÓN 1: Configuración de Google Calendar**
# Aquí configuramos las credenciales necesarias para interactuar con Google Calendar.
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_PRIVATE_KEY = config("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")

def initialize_google_calendar():
    """
    Inicializa la conexión con la API de Google Calendar usando credenciales de servicio.

    Retorna:
        service: Cliente de Google Calendar listo para realizar consultas.
    """
    credentials = Credentials.from_service_account_info(
        {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=credentials)


# **SECCIÓN 2: Verificar disponibilidad de un rango de tiempo**
def check_availability(start_time, end_time):
    """
    Verifica si un rango de tiempo específico está disponible en el calendario de Google.

    Parámetros:
        start_time (datetime): Hora de inicio.
        end_time (datetime): Hora de fin.

    Retorna:
        bool: True si el rango está disponible, False si está ocupado.
    """
    service = initialize_google_calendar()
    events = service.freebusy().query(
        body={
            "timeMin": start_time.isoformat(),
            "timeMax": end_time.isoformat(),
            "timeZone": "America/Cancun",
            "items": [{"id": GOOGLE_CALENDAR_ID}]
        }
    ).execute()
    return len(events["calendars"][GOOGLE_CALENDAR_ID]["busy"]) == 0


# **SECCIÓN 3: Buscar el siguiente horario disponible**
def find_next_available_slot():
    """
    Encuentra el próximo horario disponible en el calendario siguiendo las reglas del consultorio.

    Reglas:
        - Los horarios válidos son:
          9:30 am, 10:15 am, 11:00 am, 11:45 am, 12:30 pm, 1:15 pm, 2:00 pm.
        - No se programan citas los domingos.
        - Si no hay disponibilidad para el día actual, pasa al siguiente día hábil.
        - Cada cita tiene una duración de 45 minutos.
        - Los horarios para el día actual solo se consideran si son al menos 4 horas posteriores a la hora actual.

    Retorna:
        dict: Información del próximo slot disponible con `start_time` y `end_time`, o None si no encuentra.
    """
    # Define los horarios de las citas
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

    # Día inicial
    day_offset = 0

    # Bucle infinito hasta encontrar un slot disponible
    while True:
        day = now + timedelta(days=day_offset)  # Avanzar al siguiente día si es necesario

        # Saltar los domingos (weekday() devuelve 6 para domingo)
        if day.weekday() == 6:
            day_offset += 1
            continue

        # Verificar cada slot del día
        for slot in slot_times:
            # Combinar la fecha con el horario del slot
            start_time_str = f"{day.strftime('%Y-%m-%d')} {slot['start']}:00"
            end_time_str = f"{day.strftime('%Y-%m-%d')} {slot['end']}:00"
            start_time = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))
            end_time = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))

            # Ignorar slots del pasado para el día actual
            if day_offset == 0 and start_time <= now + timedelta(hours=4):
                continue

            # Verificar disponibilidad del slot
            if check_availability(start_time, end_time):  # Llama a `check_availability`
                return {"start_time": start_time, "end_time": end_time}

        # Avanzar al siguiente día
        day_offset += 1
