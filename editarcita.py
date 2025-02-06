# -*- coding: utf-8 -*-
"""
Módulo para la edición de citas en Google Calendar.
Permite modificar horarios de citas ya existentes en la agenda del consultorio del Dr. Wilfrido Alarcón.
"""

# ==================================================
# 📌 Importaciones y Configuración
# ==================================================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config
import pytz
import logging
from datetime import timedelta
from utils import get_cancun_time  # Asegura el uso de la zona horaria correcta

# Importaciones de módulos internos
from crearcita import create_calendar_event
from buscarslot import check_availability  # Verificar disponibilidad de horarios

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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
# 🔹 Edición de un evento en Google Calendar
# ==================================================
def edit_calendar_event(phone, original_start_time, new_start_time=None, new_end_time=None):
    """
    Edita una cita existente en Google Calendar. Si no se encuentra el evento, se ofrece crear una nueva cita.

    Parámetros:
        phone (str): Número de teléfono del paciente para identificar el evento.
        original_start_time (datetime): Fecha y hora original del evento para filtrar resultados.
        new_start_time (datetime): Nueva fecha y hora de inicio (opcional).
        new_end_time (datetime): Nueva fecha y hora de fin (opcional).

    Retorna:
        dict: Detalles del evento modificado o un mensaje indicando que se creó una nueva cita.
    """
    try:
        # Validar el número de teléfono
        if not phone or len(phone) != 10 or not phone.isdigit():
            raise ValueError("⚠️ El campo 'phone' debe ser un número de 10 dígitos.")

        # Inicializar Google Calendar API
        service = initialize_google_calendar()

        # Buscar eventos que coincidan con el teléfono y la hora de inicio original
        events = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            q=phone,  # Buscar por teléfono en la descripción del evento
            timeMin=original_start_time.isoformat(),
            timeMax=(original_start_time + timedelta(minutes=45)).isoformat(),
            singleEvents=True
        ).execute()

        items = events.get("items", [])

        # Si no se encuentra un evento, ofrecer crear una nueva cita
        if not items:
            logger.warning(f"⚠️ No se encontró una cita con el número {phone}. Se ofrecerá crear una nueva.")
            return {"message": "No se encontró una cita con este número. Procedemos a crear una nueva."}

        # Si hay múltiples citas, confirmar con el nombre del paciente
        if len(items) > 1:
            nombres = [item["summary"] for item in items]
            logger.warning(f"⚠️ Se encontraron múltiples citas con el número {phone}.")
            return {
                "message": "Se encontraron múltiples citas con este número.",
                "options": nombres
            }

        # Editar el primer evento encontrado
        event = items[0]

        # Si no se proporciona nueva fecha/hora, conservar la original
        if not new_start_time or not new_end_time:
            new_start_time = event["start"]["dateTime"]
            new_end_time = event["end"]["dateTime"]

        # Convertir fechas al formato ISO 8601 con zona horaria de Cancún
        new_start_iso = new_start_time.astimezone(pytz.timezone("America/Cancun")).isoformat()
        new_end_iso = new_end_time.astimezone(pytz.timezone("America/Cancun")).isoformat()

        # Verificar disponibilidad para la nueva fecha/hora
        if not check_availability(new_start_time, new_end_time):
            logger.warning(f"⚠️ El horario solicitado ({new_start_iso} - {new_end_iso}) no está disponible.")
            return {"message": "El horario solicitado no está disponible. Intente otro horario."}

        # Actualizar la fecha/hora en el evento
        event["start"] = {"dateTime": new_start_iso, "timeZone": "America/Cancun"}
        event["end"] = {"dateTime": new_end_iso, "timeZone": "America/Cancun"}

        # Guardar los cambios en el evento
        updated_event = service.events().update(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event["id"],
            body=event
        ).execute()

        logger.info(f"✅ Cita editada para {event['summary']} el {new_start_iso}")

        # Retornar detalles del evento modificado
        return {
            "id": updated_event.get("id"),
            "start": updated_event["start"]["dateTime"],
            "end": updated_event["end"]["dateTime"],
            "summary": updated_event.get("summary"),
            "description": updated_event.get("description")
        }

    except ValueError as ve:
        logger.warning(f"⚠️ Error de validación: {str(ve)}")
        raise
    except Exception as e:
        logger.error(f"❌ Error al editar la cita en Google Calendar: {str(e)}")
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")

# ==================================================
# 🔹 Prueba Local del Módulo
# ==================================================
if __name__ == "__main__":
    """
    Prueba rápida para verificar la conexión y edición de citas en Google Calendar.
    """
    from datetime import datetime, timedelta

    try:
        # Obtener la hora actual en Cancún
        now = get_cancun_time()
        test_phone = "9981234567"
        test_original_time = now + timedelta(days=1, hours=3)
        test_new_time = now + timedelta(days=1, hours=5)

        result = edit_calendar_event(
            phone=test_phone,
            original_start_time=test_original_time,
            new_start_time=test_new_time,
            new_end_time=test_new_time + timedelta(minutes=45)
        )

        print("✅ Resultado de la edición de la cita:")
        print(result)

    except ConnectionError as ce:
        print(f"❌ Error de conexión con Google Calendar: {str(ce)}")
    except Exception as e:
        print(f"❌ Error desconocido: {str(e)}")