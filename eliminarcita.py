# eliminarcita.py
#  -*- coding: utf-8 -*-
"""
Módulo para la eliminación de citas en Google Calendar.
Permite buscar y eliminar eventos en la agenda del consultorio.
Admite original_start_time para elegir la cita exacta a borrar.
"""

import logging
from fastapi import APIRouter, HTTPException
from datetime import datetime
import pytz
from utils import (
    initialize_google_calendar,
    GOOGLE_CALENDAR_ID,
    search_calendar_event_by_phone
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

def _validate_iso_datetime(dt_str):
    """
    Valida y convierte un string ISO8601 con zona horaria
    a un objeto datetime (zona Cancún).
    """
    try:
        dt = datetime.fromisoformat(dt_str)
        if not dt.tzinfo:
            raise ValueError("Falta zona horaria")
        return dt.astimezone(pytz.timezone("America/Cancun"))
    except Exception as e:
        raise ValueError(f"Fecha/hora inválida: {dt_str}")

def delete_calendar_event(phone: str, original_start_time: str = None, patient_name: str = None):
    """
    Elimina la cita que coincida con 'phone' y 'original_start_time'.
    Si no se especifica 'original_start_time' y hay múltiples citas, se devuelven en 'multiple_events'.
    """
    try:
        if not phone or len(phone) != 10 or not phone.isdigit():
            return {"error": "El número de teléfono debe ser de 10 dígitos."}

        service = initialize_google_calendar()
        events = search_calendar_event_by_phone(phone)
        if not events:
            return {"error": "No se encontraron citas con ese número."}

        # Si user no especifica 'original_start_time' y hay más de 1 => devolvemos multiple
        if not original_start_time:
            if len(events) > 1:
                return {
                    "multiple_events": True,
                    "events_found": _convert_events_list(events),
                    "message": "Se encontraron múltiples citas con este número. Indique cuál eliminar."
                }
            else:
                # Solo 1
                evt_to_delete = events[0]
        else:
            # localizamos la cita con original_start_time
            target_dt = _validate_iso_datetime(original_start_time)
            evt_to_delete = None

            for e in events:
                start_str = e["start"]["dateTime"]  # "2025-04-22T09:30:00-05:00"
                start_dt = _validate_iso_datetime(start_str)

                # si coincide ±1 min
                if abs((start_dt - target_dt).total_seconds()) < 60:
                    evt_to_delete = e
                    break

            if not evt_to_delete:
                # No coincidió => devolvemos la lista
                return {
                    "multiple_events": True,
                    "events_found": _convert_events_list(events),
                    "message": "No se encontró cita con ese horario exacto. Seleccione cuál desea eliminar."
                }

        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=evt_to_delete["id"]).execute()
        logger.info(f"✅ Cita eliminada para {evt_to_delete.get('summary','(sin nombre)')}")

        return {
            "message": f"La cita '{evt_to_delete.get('summary','(sin nombre)')}' fue eliminada con éxito."
        }

    except ValueError as ve:
        logger.error(f"❌ Error al eliminar cita: {str(ve)}")
        return {"error": str(ve)}
    except Exception as e:
        logger.error(f"❌ Error en Google Calendar: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}

def _convert_events_list(events):
    """
    Convierte la lista de 'events' a un formato simple.
    """
    simplified = []
    for evt in events:
        simplified.append({
            "id": evt["id"],
            "name": evt.get("summary", "(sin nombre)"),
            "start": evt["start"]["dateTime"],
            "end": evt["end"]["dateTime"]
        })
    return simplified


@router.delete("/eliminar-cita")
async def api_delete_calendar_event(phone: str, original_start_time: str = None, patient_name: str = None):
    """
    Endpoint para eliminar una cita en Google Calendar.
    Parámetros:
    - phone (str): Número de teléfono (10 dígitos).
    - original_start_time (str, opcional): Fecha-hora ISO con zona horaria de la cita a eliminar.
    - patient_name (str, opcional): Nombre del paciente (si se quiere filtrar).
    
    Retorna dict con confirmación de eliminación o lista de citas si hay múltiples.
    """
    try:
        result = delete_calendar_event(phone, original_start_time, patient_name)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"❌ Error al eliminar cita en endpoint /eliminar-cita: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")
