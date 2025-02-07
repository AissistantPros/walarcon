from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config
import logging
from datetime import datetime
import pytz



# 📌 Configuración de logging (¡aquí está el problema!)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)  # 🔹 Esto define correctamente logger


# Variables de configuración de Google Calendar
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_PRIVATE_KEY = config("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")

# ==================================================
# 🔹 Inicialización de Google Calendar
# ==================================================
def initialize_google_calendar():
    """
    Configura y conecta la API de Google Calendar usando credenciales de servicio.

    Retorna:
        object: Cliente autenticado de Google Calendar.
    """
    try:
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
    except Exception as e:
        logger.error(f"❌ Error al conectar con Google Calendar: {str(e)}")
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")

# ==================================================
# 🔹 Buscar citas por número de teléfono
# ==================================================
def search_calendar_event_by_phone(phone):
    """
    Busca todas las citas que coincidan con el número de teléfono en Google Calendar.

    Parámetros:
        phone (str): Número de teléfono del paciente.

    Retorna:
        list: Lista de eventos encontrados con detalles.
    """
    try:
        # Validar el número de teléfono
        if not phone or len(phone) != 10 or not phone.isdigit():
            raise ValueError("⚠️ El campo 'phone' debe ser un número de 10 dígitos.")

        # Inicializar Google Calendar API
        service = initialize_google_calendar()

        # Buscar eventos que contengan el número de teléfono
        events = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            q=phone,  # Buscar por teléfono en la descripción del evento
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        items = events.get("items", [])

        if not items:
            logger.warning(f"⚠️ No se encontró ninguna cita con el número {phone}.")
            return {"message": "No se encontraron citas con este número."}

        # Formatear los eventos encontrados
        citas = []
        for item in items:
            citas.append({
                "id": item["id"],
                "start": item["start"]["dateTime"],
                "end": item["end"]["dateTime"],
                "summary": item.get("summary", "Cita sin nombre"),
                "description": item.get("description", ""),
            })

        return {"citas": citas}

    except Exception as e:
        logger.error(f"❌ Error al buscar citas en Google Calendar: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}




















def get_cancun_time():
    """Obtiene la fecha y hora actual en la zona horaria de Cancún."""
    cancun_tz = pytz.timezone("America/Cancun")
    now = datetime.now(cancun_tz)
    return now

def get_iso_format():
    """Convierte la fecha y hora de Cancún al formato ISO 8601."""
    now = get_cancun_time()
    return now.isoformat()