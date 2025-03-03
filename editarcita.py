#editarcita.py
# -*- coding: utf-8 -*-
"""
Módulo para edición segura de citas con validación de horarios.
"""

import logging
import pytz
from datetime import datetime
from fastapi import APIRouter, HTTPException
from utils import (
    initialize_google_calendar,
    GOOGLE_CALENDAR_ID,
    search_calendar_event_by_phone,
    is_slot_available,
    get_cached_availability
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

def validate_and_convert_time(time_str: str):
    """Valida y convierte a datetime con zona horaria."""
    try:
        dt = datetime.fromisoformat(time_str)
        if not dt.tzinfo:
            raise ValueError("Falta zona horaria")
        return dt.astimezone(pytz.timezone("America/Cancun"))
    except ValueError as e:
        logger.error(f"❌ Formato de tiempo inválido: {str(e)}")
        raise HTTPException(status_code=400, detail="Formato inválido (usar ISO8601 con zona horaria)")

def edit_calendar_event(phone: str, original_start: str, new_start: str, new_end: str):
    try:
        # Validación inicial
        if len(phone) != 10 or not phone.isdigit():
            raise ValueError("Teléfono inválido")
        
        # Buscar evento existente
        events = search_calendar_event_by_phone(phone)
        if not events:
            raise ValueError("No se encontró la cita")
        
        if len(events) > 1:
            raise ValueError("Múltiples citas encontradas - Proporcione nombre")

        event = events[0]
        service = initialize_google_calendar()

        # Validar nuevo horario
        new_start_dt = validate_and_convert_time(new_start)
        new_end_dt = validate_and_convert_time(new_end)
        
        # Verificar disponibilidad
        busy_slots = get_cached_availability()
        if not is_slot_available(new_start_dt, new_end_dt, busy_slots):
            raise ValueError("Horario no disponible")

        # Actualizar evento
        updated_event = service.events().patch(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event["id"],
            body={
                "start": {"dateTime": new_start_dt.isoformat(), "timeZone": "America/Cancun"},
                "end": {"dateTime": new_end_dt.isoformat(), "timeZone": "America/Cancun"}
            }
        ).execute()

        return {
            "id": updated_event["id"],
            "start": updated_event["start"]["dateTime"],
            "end": updated_event["end"]["dateTime"]
        }

    except ValueError as ve:
        logger.error(f"❌ Error de validación: {str(ve)}")
        return {"error": str(ve)}
    except Exception as e:
        logger.error(f"❌ Error en Google Calendar: {str(e)}")
        return {"error": "CALENDAR_UNAVAILABLE"}

@router.put("/editar-cita")
async def api_edit_calendar_event(phone: str, new_start: str, new_end: str):
    try:
        # Validación básica
        if not new_start or not new_end:
            raise HTTPException(status_code=400, detail="Se requieren nuevos horarios")

        result = edit_calendar_event(phone, None, new_start, new_end)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        return {
            "message": "✅ Cita actualizada",
            "new_start": result["start"],
            "new_end": result["end"]
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"❌ Error crítico: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")
