# -*- coding: utf-8 -*-
"""
Módulo para la eliminación de citas en Google Calendar.
Permite buscar y eliminar eventos en la agenda del consultorio del Dr. Wilfrido Alarcón.
"""

# ==================================================
# 📌 Importaciones y Configuración
# ==================================================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config
import logging
import pytz
from utils import get_cancun_time  # Asegura el uso de la zona horaria correcta

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
# 🔹 Eliminación de un evento en Google Calendar
# ==================================================
def delete_calendar_event(phone, patient_name=None):
    """
    Busca y elimina un evento en Google Calendar basado en el número de teléfono.
    Si se encuentran múltiples eventos con el mismo número, solicita confirmar con el nombre del paciente.

    Parámetros:
        phone (str): Número de teléfono del paciente para identificar la cita.
        patient_name (str): Nombre del paciente (opcional, para confirmar en caso de múltiples citas).

    Retorna:
        dict: Detalles del resultado de la operación o un mensaje indicando el estado.
    """
    try:
        # Validar el número de teléfono
        if not phone or len(phone) != 10 or not phone.isdigit():
            raise ValueError("⚠️ El campo 'phone' debe ser un número de 10 dígitos.")

        # Inicializar Google Calendar API
        service = initialize_google_calendar()

        # Buscar eventos que coincidan con el número de teléfono
        events = service.events().list(
            calendarId=GOOGLE_CALENDAR_ID,
            q=phone,  # Buscar por teléfono en la descripción del evento
            singleEvents=True
        ).execute()

        items = events.get("items", [])

        # Si no se encuentran eventos, informar al usuario
        if not items:
            logger.warning(f"⚠️ No se encontraron citas para el número: {phone}")
            return {"message": "No se encontraron citas con el número proporcionado."}

        # Si hay múltiples citas y se proporciona el nombre del paciente
        if len(items) > 1 and patient_name:
            for event in items:
                if event["summary"].lower() == patient_name.lower():
                    # Eliminar el evento encontrado
                    service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event["id"]).execute()
                    logger.info(f"✅ Cita eliminada para {patient_name}")
                    return {"message": f"El evento para {patient_name} ha sido eliminado con éxito."}

            # Si no se encuentra un evento con el nombre especificado
            nombres = [event["summary"] for event in items]
            logger.warning(f"⚠️ Se encontraron varias citas con el número {phone}, pero ninguna coincide con {patient_name}.")
            return {
                "message": "Se encontraron múltiples citas con este número, pero ninguna coincide con el nombre proporcionado.",
                "options": nombres  # Lista los nombres de los pacientes
            }

        # Si solo hay un evento o no se especificó el nombre, eliminar el primer evento encontrado
        event = items[0]
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event["id"]).execute()
        logger.info(f"✅ Cita eliminada para {event['summary']}")

        return {"message": f"El evento para {event['summary']} ha sido eliminado con éxito."}

    except ValueError as ve:
        logger.warning(f"⚠️ Error de validación: {str(ve)}")
        raise
    except Exception as e:
        logger.error(f"❌ Error al eliminar cita en Google Calendar: {str(e)}")
        raise ConnectionError("GOOGLE_CALENDAR_UNAVAILABLE")

# ==================================================
# 🔹 Prueba Local del Módulo
# ==================================================
if __name__ == "__main__":
    """
    Prueba rápida para verificar la conexión y eliminación de citas en Google Calendar.
    """
    try:
        test_phone = "9981234567"
        test_name = "Juan Pérez"
        result = delete_calendar_event(phone=test_phone, patient_name=test_name)

        print("✅ Resultado de la eliminación de la cita:")
        print(result)

    except ConnectionError as ce:
        print(f"❌ Error de conexión con Google Calendar: {str(ce)}")
    except Exception as e:
        print(f"❌ Error desconocido: {str(e)}")