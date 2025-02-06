# -*- coding: utf-8 -*-
"""
Módulo para la creación de eventos en Google Calendar.
Permite agendar citas para los pacientes del consultorio del Dr. Wilfrido Alarcón.
"""

# ==================================================
# 📌 Importaciones y Configuración
# ==================================================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config
import pytz
import logging
from utils import get_cancun_time
from buscarslot import check_availability  # Importar verificación de disponibilidad

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
        credentials = Credentials.from_service_account_info({
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        }, scopes=["https://www.googleapis.com/auth/calendar"])
        return build("calendar", "v3", credentials=credentials)
    except Exception as e:
        logger.error(f"❌ Error al conectar con Google Calendar: {str(e)}")
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")

# ==================================================
# 🔹 Creación de un evento en Google Calendar
# ==================================================
def create_calendar_event(name, phone, reason, start_time, end_time):
    """
    Crea un evento en Google Calendar para agendar una cita.

    Parámetros:
        name (str): Nombre del paciente.
        phone (str): Número de teléfono del paciente.
        reason (str): Motivo de la cita (opcional).
        start_time (datetime): Hora de inicio de la cita.
        end_time (datetime): Hora de fin de la cita.

    Retorna:
        dict: Información del evento creado en Google Calendar.
    """
    try:
        service = initialize_google_calendar()

        # Validaciones básicas
        if not name.strip():
            raise ValueError("⚠️ El nombre del paciente no puede estar vacío.")
        if not phone.strip().isdigit() or len(phone.strip()) != 10:
            raise ValueError("⚠️ El número de teléfono debe tener 10 dígitos numéricos.")
        if not start_time or not end_time:
            raise ValueError("⚠️ Los valores de fecha y hora no pueden estar vacíos.")

        # Convertir a formato ISO 8601 con zona horaria de Cancún
        start_iso = start_time.astimezone(pytz.timezone("America/Cancun")).isoformat()
        end_iso = end_time.astimezone(pytz.timezone("America/Cancun")).isoformat()

        # Verificar disponibilidad antes de crear la cita
        if not check_availability(start_time, end_time):
            logger.warning(f"⚠️ No se puede agendar. El horario {start_iso} - {end_iso} ya está ocupado.")
            raise ValueError("El horario solicitado no está disponible. Intente otro horario.")

        # Crear el evento en Google Calendar
        event = {
            "summary": name,
            "description": f"📌 Teléfono: {phone}\n📝 Motivo: {reason or 'No especificado'}",
            "start": {"dateTime": start_iso, "timeZone": "America/Cancun"},
            "end": {"dateTime": end_iso, "timeZone": "America/Cancun"},
        }

        created_event = service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=event
        ).execute()

        logger.info(f"✅ Cita creada para {name} el {start_iso}")

        return {
            "id": created_event["id"],
            "start": created_event["start"]["dateTime"],
            "end": created_event["end"]["dateTime"],
        }

    except ValueError as ve:
        logger.warning(f"⚠️ Error de validación: {str(ve)}")
        raise
    except Exception as e:
        logger.error(f"❌ Error al crear la cita en Google Calendar: {str(e)}")
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")

# ==================================================
# 🔹 Prueba Local del Módulo
# ==================================================
if __name__ == "__main__":
    """
    Prueba rápida para verificar la conexión y creación de citas en Google Calendar.
    """
    from datetime import datetime, timedelta

    try:
        # Obtener la hora actual en Cancún
        now = get_cancun_time()
        start_test = now + timedelta(days=1, hours=3)
        end_test = start_test + timedelta(minutes=45)

        test_event = create_calendar_event(
            name="Juan Pérez",
            phone="9981234567",
            reason="Chequeo general",
            start_time=start_test,
            end_time=end_test
        )

        print("✅ Cita creada exitosamente:")
        print(test_event)

    except ConnectionError as ce:
        print(f"❌ Error de conexión con Google Calendar: {str(ce)}")
    except Exception as e:
        print(f"❌ Error desconocido: {str(e)}")