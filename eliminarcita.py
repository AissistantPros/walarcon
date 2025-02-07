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
from utils import get_cancun_time, search_calendar_event_by_phone  # Asegura el uso de la zona horaria correcta

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

        # Buscar la cita con el número de teléfono
        events = search_calendar_event_by_phone(phone)

        # Si no se encuentran eventos, informar al usuario
        if not events:
            logger.warning(f"⚠️ No se encontraron citas para el número: {phone}")
            return {"message": "No se encontraron citas con el número proporcionado."}

        # Si hay múltiples citas, confirmar el nombre del paciente
        if len(events) > 1:
            if not patient_name:
                nombres = [event["summary"] for event in events]
                logger.warning(f"⚠️ Se encontraron varias citas con el número {phone}.")
                return {
                    "message": "Se encontraron múltiples citas con este número. ¿Podría proporcionar el nombre del paciente?",
                    "options": nombres  # Lista los nombres de los pacientes
                }

            # Buscar la cita que coincida con el nombre proporcionado
            event_to_delete = next((event for event in events if event["summary"].lower() == patient_name.lower()), None)

            if not event_to_delete:
                logger.warning(f"⚠️ Ninguna cita coincide con el nombre {patient_name} para el número {phone}.")
                return {
                    "message": f"No se encontró una cita con el nombre {patient_name}. Verifique el nombre y vuelva a intentarlo."
                }
        else:
            # Si solo hay una cita, eliminarla directamente
            event_to_delete = events[0]

        # Inicializar Google Calendar API
        service = initialize_google_calendar()

        # Eliminar el evento encontrado
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_to_delete["id"]).execute()
        logger.info(f"✅ Cita eliminada para {event_to_delete['summary']}")

        return {"message": f"El evento para {event_to_delete['summary']} ha sido eliminado con éxito."}

    except ValueError as ve:
        logger.warning(f"⚠️ Error de validación: {str(ve)}")
        return {"error": str(ve)}
    except Exception as e:
        logger.error(f"❌ Error al eliminar cita en Google Calendar: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}

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