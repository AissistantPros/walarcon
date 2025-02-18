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
from buscarslot import find_next_available_slot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

def validate_iso_datetime(dt_str: str):
    """Valida formato ISO8601 con zona horaria."""
    try:
        dt = datetime.fromisoformat(dt_str)
        if not dt.tzinfo:
            raise ValueError("Falta zona horaria")
        return dt.astimezone(pytz.timezone("America/Cancun"))
    except ValueError as e:
        logger.error(f"❌ Formato de fecha inválido: {str(e)}")
        raise HTTPException(status_code=400, detail="Formato datetime inválido (usar ISO8601 con zona horaria)")

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
        return {"error": str(ve)}
    except Exception as e:
        logger.error(f"❌ Error en Google Calendar: {str(e)}")
        return {"error": "CALENDAR_UNAVAILABLE"}

@router.post("/crear-cita")
async def api_create_calendar_event(name: str, phone: str, reason: str = None, target_date: str = None, target_hour: str = None):
    try:
        # Validación de parámetros
        if not name.strip():
            raise HTTPException(status_code=400, detail="El nombre no puede estar vacío")
        
        # Buscar slot disponible con validación
        slot = find_next_available_slot(target_date, target_hour)
        if "error" in slot:
            raise HTTPException(status_code=409, detail=slot["error"])

        # Crear evento
        event_data = create_calendar_event(
            name=name,
            phone=phone,
            reason=reason,
            start_time=slot["start_time"],
            end_time=slot["end_time"]
        )

        if "error" in event_data:
            raise HTTPException(status_code=500, detail=event_data["error"])

        return {
            "message": "✅ Cita creada exitosamente",
            "event_id": event_data["id"],
            "start": event_data["start"],
            "end": event_data["end"]
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"❌ Error crítico: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")