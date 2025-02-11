# -*- coding: utf-8 -*-
"""
Módulo para la creación de eventos en Google Calendar.
Permite agendar citas para los pacientes del consultorio del Dr. Wilfrido Alarcón.
"""

# ==================================================
# 📌 Importaciones y Configuración
# ==================================================
import logging
from logging import config
from utils import initialize_google_calendar, get_cancun_time, search_calendar_event_by_phone, GOOGLE_CALENDAR_ID
from buscarslot import check_availability  # Importar verificación de disponibilidad
from datetime import datetime
import pytz

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
        dict: Información del evento creado en Google Calendar.
    """
    try:
        service = initialize_google_calendar()

        # 📌 Convertir los horarios a la zona horaria de Cancún
        start_dt = datetime.fromisoformat(start_time).astimezone(pytz.timezone("America/Cancun"))
        end_dt = datetime.fromisoformat(end_time).astimezone(pytz.timezone("America/Cancun"))

        # 📌 Log para verificar qué datos está recibiendo la función
        logger.info(f"📩 Datos recibidos en `create_calendar_event`:\n"
                    f"  - Nombre: {name}\n"
                    f"  - Teléfono: {phone}\n"
                    f"  - Motivo: {reason}\n"
                    f"  - Inicio (ISO original): {start_time}\n"
                    f"  - Fin (ISO original): {end_time}\n"
                    f"  - Inicio (Cancún): {start_dt}\n"
                    f"  - Fin (Cancún): {end_dt}")

        # Validaciones básicas
        if not name.strip():
            logger.warning("⚠️ Error: El nombre del paciente no puede estar vacío.")
            raise ValueError("El nombre del paciente no puede estar vacío.")
        if not phone.strip().isdigit() or len(phone.strip()) != 10:
            logger.warning("⚠️ Error: El número de teléfono debe tener 10 dígitos numéricos.")
            raise ValueError("El número de teléfono debe tener 10 dígitos numéricos.")
        if not start_time or not end_time:
            logger.warning("⚠️ Error: Los valores de fecha y hora no pueden estar vacíos.")
            raise ValueError("Los valores de fecha y hora no pueden estar vacíos.")

        # 📌 Log antes de verificar disponibilidad
        logger.info(f"🔍 Verificando disponibilidad de {start_dt} a {end_dt}...")

        # Verificar disponibilidad antes de crear la cita
        if not check_availability(start_dt, end_dt):
            logger.warning(f"⚠️ No se puede agendar. El horario {start_dt} - {end_dt} ya está ocupado.")
            raise ValueError("El horario solicitado no está disponible. Intente otro horario.")

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

        logger.info(f"✅ Cita creada para {name} el {start_dt}")

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
