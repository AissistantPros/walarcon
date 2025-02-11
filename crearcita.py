import logging
from utils import initialize_google_calendar, GOOGLE_CALENDAR_ID
from datetime import datetime
import pytz
from decouple import config

# Configuración de logging
logger = logging.getLogger(__name__)

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
        start_time (str): Hora de inicio de la cita (formato ISO 8601).
        end_time (str): Hora de fin de la cita (formato ISO 8601).

    Retorna:
        dict: Información del evento creado en Google Calendar o mensaje de error.
    """
    try:
        service = initialize_google_calendar()

        # 📌 Convertir los horarios a la zona horaria de Cancún
        start_dt = datetime.fromisoformat(start_time).astimezone(pytz.timezone("America/Cancun"))
        end_dt = datetime.fromisoformat(end_time).astimezone(pytz.timezone("America/Cancun"))

        logger.info(f"📩 Intentando crear cita con los siguientes datos:")
        logger.info(f"  - Nombre: {name}")
        logger.info(f"  - Teléfono: {phone}")
        logger.info(f"  - Motivo: {reason}")
        logger.info(f"  - Inicio (ISO): {start_time}")
        logger.info(f"  - Fin (ISO): {end_time}")

        # Crear el evento en Google Calendar
        event = {
            "summary": name,
            "description": f"📌 Teléfono: {phone}\n📝 Motivo: {reason or 'No especificado'}",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Cancun"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/Cancun"},
        }

        created_event = service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=event
        ).execute()

        if "id" not in created_event:
            logger.error("❌ Error: No se recibió un ID de evento después de la inserción en Google Calendar.")
            return {"error": "La cita no pudo guardarse en el calendario."}

        logger.info(f"✅ Cita creada con éxito: {created_event['id']}")

        return {
            "id": created_event["id"],
            "start": created_event["start"]["dateTime"],
            "end": created_event["end"]["dateTime"]
        }

    except Exception as e:
        logger.error(f"❌ Error al crear la cita en Google Calendar: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}