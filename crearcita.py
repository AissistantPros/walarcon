from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config
import pytz

# **SECCIÓN 1: Configuración de Google Calendar**
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")  # ID del calendario
GOOGLE_PRIVATE_KEY = config("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")  # Llave privada
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")  # ID del proyecto de Google
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")  # Correo de la cuenta de servicio

def initialize_google_calendar():
    """
    Inicializa la conexión con la API de Google Calendar usando credenciales de servicio.

    Retorna:
        service: Cliente de Google Calendar listo para realizar operaciones.
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

# **SECCIÓN 2: Crear una cita en Google Calendar**
def create_calendar_event(name, phone, reason=None, start_time=None, end_time=None):
    """
    Crea un evento en Google Calendar para registrar una cita.

    Parámetros:
        name (str): Nombre del paciente (obligatorio).
        phone (str): Número de teléfono del paciente (obligatorio, debe ser un número de 10 dígitos).
        reason (str): Motivo de la consulta (opcional).
        start_time (datetime): Fecha y hora de inicio de la cita (obligatorio).
        end_time (datetime): Fecha y hora de fin de la cita (obligatorio).

    Retorna:
        dict: Detalles del evento creado, incluyendo su ID y horario.
    """
    # Validar campos obligatorios
    if not name or not phone:
        raise ValueError("Los campos 'name' y 'phone' son obligatorios.")
    if len(phone) != 10 or not phone.isdigit():
        raise ValueError("El campo 'phone' debe ser un número de 10 dígitos.")
    if not start_time or not end_time:
        raise ValueError("Los campos 'start_time' y 'end_time' son obligatorios.")

    # Inicializar Google Calendar API
    service = initialize_google_calendar()

    # Crear el evento
    event = {
        "summary": name,  # Título del evento (nombre del paciente)
        "description": f"Teléfono: {phone}\nMotivo: {reason or 'No especificado'}",  # Descripción del evento
        "start": {"dateTime": start_time.isoformat(), "timeZone": "America/Cancun"},
        "end": {"dateTime": end_time.isoformat(), "timeZone": "America/Cancun"},
    }

    # Enviar el evento a Google Calendar
    created_event = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()

    # Retornar los detalles del evento creado
    return {
        "id": created_event.get("id"),  # ID del evento
        "summary": created_event.get("summary"),  # Nombre del paciente
        "start": created_event["start"]["dateTime"],  # Hora de inicio
        "end": created_event["end"]["dateTime"],  # Hora de fin
        "description": created_event.get("description")  # Notas del evento
    }
