from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config
import logging
from datetime import datetime
import pytz
import os
from dotenv import load_dotenv
import json

# Cargar variables del .env
load_dotenv()

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cargar variables de Google
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")


def initialize_google_calendar():
    try:
        logger.info("🔍 Inicializando Google Calendar...")
        
        # Crear diccionario de credenciales
        credentials_info = {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key_id": config("GOOGLE_PRIVATE_KEY_ID"),
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "client_id": config("GOOGLE_CLIENT_ID"),
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "client_x509_cert_url": config("GOOGLE_CLIENT_CERT_URL")
        }

        credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=["https://www.googleapis.com/auth/calendar"]
        )

        return build("calendar", "v3", credentials=credentials)
    
    except Exception as e:
        logger.error(f"❌ Error en initialize_google_calendar: {str(e)}")
        raise RuntimeError("Error de conexión con Google Calendar")

# ==================================================
# 🔹 Buscar citas por número de teléfono
# ==================================================
def search_calendar_event_by_phone(phone):
    """
    Busca todas las citas que coincidan con el número de teléfono en Google Calendar.

    Parámetros:
        phone (str): Número de teléfono del paciente.

    Retorna:
        dict: Datos de la cita encontrada.
    """
    try:
        # Validar el número de teléfono
        if not phone or len(phone) < 10 or not phone.isdigit():
            raise ValueError("⚠️ El campo 'phone' debe ser un número de al menos 10 dígitos.")

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
            return {"error": "No se encontraron citas con este número."}

        # Tomar solo la primera cita encontrada
        event = items[0]

        # Extraer nombre del paciente del resumen de la cita
        patient_name = event.get("summary", "Nombre no disponible")

        # Convertir fecha y hora a formato legible
        start_time = event["start"].get("dateTime", "").split("T")
        end_time = event["end"].get("dateTime", "").split("T")

        if len(start_time) < 2 or len(end_time) < 2:
            return {"error": "No se pudo extraer la fecha y hora correctamente."}

        start_date = start_time[0]  # Fecha en formato YYYY-MM-DD
        start_hour = start_time[1][:5]  # Solo HH:MM

        return {
            "id": event["id"],
            "name": patient_name,
            "date": start_date,
            "time": start_hour
        }

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