import os
import json
import asyncio
import logging
from datetime import datetime
import pytz
from dotenv import load_dotenv
from decouple import config
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

from audio_utils import generate_audio_with_eleven_labs

# Cargar variables del .env
load_dotenv()

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"

# Cargar variables de Google
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")
GOOGLE_PRIVATE_KEY = os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n")

def initialize_google_calendar():
    """
    Inicializa y retorna el servicio de Google Calendar.
    """
    try:
        logger.info("🔍 Inicializando Google Calendar...")
        
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
# 🔹 Buscar citas por número de teléfono y nombre
# ==================================================

def search_calendar_event_by_phone(phone, name=None):
    """
    Busca citas futuras que coincidan con el número de teléfono en Google Calendar.
    Si hay múltiples coincidencias, puede filtrar por nombre del paciente.
    """
    try:
        if not phone or len(phone) < 10 or not phone.isdigit():
            raise ValueError("⚠️ El campo 'phone' debe ser un número de al menos 10 dígitos.")

        service = initialize_google_calendar()
        now = datetime.utcnow().isoformat() + 'Z'

        events = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            q=phone,
            timeMin=now,
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        items = events.get("items", [])
        if not items:
            logger.warning(f"⚠️ No se encontró ninguna cita con el número {phone}.")
            return {"error": "No se encontraron citas futuras con este número."}

        if len(items) == 1:
            event = items[0]
        else:
            # Si hay múltiples citas, filtrar por nombre si se proporciona
            if name:
                filtered_events = [evt for evt in items if evt.get("summary", "").lower() == name.lower()]
                if not filtered_events:
                    return {"error": "No se encontraron citas con ese nombre y número."}
                event = filtered_events[0]
            else:
                return {"error": "Hay múltiples citas con este número. Proporcione el nombre del paciente."}

        patient_name = event.get("summary", "Nombre no disponible")
        start_time = event["start"].get("dateTime", "").split("T")
        end_time = event["end"].get("dateTime", "").split("T")

        if len(start_time) < 2 or len(end_time) < 2:
            return {"error": "No se pudo extraer la fecha y hora correctamente."}

        start_date = start_time[0]  # YYYY-MM-DD
        start_hour = start_time[1][:5]  # HH:MM

        return {
            "id": event["id"],
            "name": patient_name,
            "date": start_date,
            "time": start_hour,
            "message": f"Su cita está programada para el {start_date} a las {start_hour} a nombre de {patient_name}."
        }

    except Exception as e:
        logger.error(f"❌ Error al buscar citas en Google Calendar: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}

# ==================================================
# 🔹 Utilidades de Tiempo
# ==================================================

def get_cancun_time():
    """
    Obtiene la fecha y hora actual en la zona horaria de Cancún.
    """
    cancun_tz = pytz.timezone("America/Cancun")
    now = datetime.now(cancun_tz)
    return now

def get_iso_format():
    """
    Retorna la fecha-hora actual de Cancún en formato ISO 8601.
    """
    now = get_cancun_time()
    return now.isoformat()

# ==================================================
# 🔹 Función para terminar la llamada (end_call)
# ==================================================

async def end_call(response, reason="", conversation_history=None):
    """
    Permite que la IA termine la llamada de manera natural según la razón.
    """
    farewell_messages = {
        "silence": "Lo siento, no puedo escuchar. Terminaré la llamada. Que tenga buen día.",
        "user_request": "Fue un placer atenderle, que tenga un excelente día.",
        "spam": "Hola colega, este número es solo para información y citas del Dr. Wilfrido Alarcón. Hasta luego.",
        "time_limit": "Qué pena, tengo que terminar la llamada. Si puedo ayudar en algo más, por favor, marque nuevamente."
    }

    message = farewell_messages.get(reason, "Gracias por llamar. Hasta luego.")
    logger.info(f"[end_call] Motivo de finalización => {reason}")

    try:
        # Generar el audio con ElevenLabs
        audio_buffer = await generate_audio_with_eleven_labs(message)
        if audio_buffer:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio_buffer.getvalue())
            response.play("/audio-response")
        else:
            response.say(message)

        # Esperar un poco si la llamada termina a petición del usuario
        if reason == "user_request":
            await asyncio.sleep(5)

        # Colgar la llamada
        response.hangup()

        # Limpiar historial si se proporcionó
        if conversation_history is not None:
            conversation_history.clear()

    except Exception as e:
        logger.error(f"❌ Error en end_call: {str(e)}")
        # En caso de error, forzar el hangup para no dejar la llamada colgada
        response.hangup()

    return str(response)
