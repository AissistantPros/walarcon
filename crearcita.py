#crearcita.py
# -*- coding: utf-8 -*-
"""
Módulo para la creación de eventos en Google Calendar (Citas Médicas).
Incluye validaciones mejoradas y manejo de errores.
"""

import logging
from datetime import datetime
import pytz
from fastapi import APIRouter, HTTPException
from utils import initialize_google_calendar, GOOGLE_CALENDAR_ID, get_cancun_time


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)



def validate_iso_datetime(dt_str: str):
    """Valida formato ISO8601 con o sin zona horaria, y siempre regresa con tzinfo de Cancún."""
    try:
        # Primero intenta cargar con fromisoformat (soporta zona horaria)
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            # Si no tiene zona horaria, asumimos Cancún
            tz = pytz.timezone("America/Cancun")
            dt = tz.localize(dt)
        else:
            # Si trae zona, lo convertimos a Cancún por si las dudas
            dt = dt.astimezone(pytz.timezone("America/Cancun"))
        return dt
    except Exception as e:
        # Intenta parsear sin zona horaria explícita
        try:
            tz = pytz.timezone("America/Cancun")
            dt_naive = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S")
            dt = tz.localize(dt_naive)
            return dt
        except Exception as e2:
            # Ya ni cómo ayudarlo...
            logger.error(f"❌ Formato de fecha inválido: {str(e2)}")
            raise HTTPException(
                status_code=400,
                detail="Formato datetime inválido. Se esperaba ISO8601 (con o sin zona horaria, ej: 2025-07-08T10:15:00-05:00 o 2025-07-08T10:15:00)"
            )

def create_calendar_event(name: str, phone: str, reason: str, start_time: str, end_time: str):
    try:
        # Validación estricta de teléfono
        if len(phone) != 10 or not phone.isdigit():
            raise ValueError("Teléfono debe tener 10 dígitos numéricos")
        
        service = initialize_google_calendar()
        tz = pytz.timezone("America/Cancun")

        # Conversión y validación de tiempos
        start_dt = validate_iso_datetime(start_time)
        end_dt = validate_iso_datetime(end_time)

        # Verificar que la cita no sea en el pasado
        if start_dt < get_cancun_time():
            raise ValueError("No se pueden agendar citas en el pasado")
        
        if not name or not phone:
            raise ValueError("Faltan datos obligatorios para crear la cita.")


        event_body = {
            "summary": name,
            "description": f"📞 Teléfono: {phone}\n📝 Motivo: {reason or 'No especificado'}",
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Cancun"},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/Cancun"},
        }

        created_event = service.events().insert(
            calendarId=GOOGLE_CALENDAR_ID,
            body=event_body
        ).execute()

        return {
            "id": created_event["id"],
            "start": created_event["start"]["dateTime"],
            "end": created_event["end"]["dateTime"]
        }

    except ValueError as ve:
        logger.error(f"❌ Error de validación: {str(ve)}")
        if "Teléfono" in str(ve):
            return {
                "error": str(ve),
                "status": "invalid_phone"
            }
        return {
            "error": str(ve),
            "status": "validation_error"
        }

    except Exception as e:
        logger.error(f"❌ Error en Google Calendar: {str(e)}")
        return {"error": "CALENDAR_UNAVAILABLE"}


