# editarcita.py
# -*- coding: utf-8 -*-
"""
Módulo para edición segura de citas con validación de horarios, agregando la lógica
para calcular new_end automáticamente si no se provee, con base a 45 minutos.
"""

import logging
import pytz
from datetime import datetime, timedelta
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
        return dt
    except ValueError as e:
        logger.error(f"❌ Formato de tiempo inválido: {str(e)}")
        raise HTTPException(status_code=400, detail="Formato inválido (usar ISO8601 con zona horaria)")

def _parse_reason_from_description(description: str):
    """Extrae 'Motivo: XYZ' de la descripción, si existe."""
    if not description:
        return None
    lines = description.split("\n")
    for line in lines:
        if "motivo:" in line.lower():
            return line.split("motivo:")[-1].strip()
    return None

def _convert_events_list(events: list):
    """
    Convierte la lista de 'events' de Google Calendar en un formato simple
    para que la IA o el usuario elijan cuál editar.
    """
    simplified = []
    for evt in events:
        name = evt.get("summary", "(sin nombre)")
        start_dt = evt["start"]["dateTime"]
        end_dt = evt["end"]["dateTime"]
        description = evt.get("description", "")
        reason = _parse_reason_from_description(description)

        simplified.append({
            "id": evt["id"],
            "name": name,
            "start": start_dt,
            "end": end_dt,
            "reason": reason
        })
    return simplified

def edit_calendar_event(phone: str, original_start: str, new_start: str, new_end: str = None):
    """
    Edita una cita existente.
    - phone: número de teléfono (usado para buscar la(s) cita(s)).
    - original_start: fecha-hora ISO con zona horaria de la cita a editar.
    - new_start: fecha-hora ISO con zona horaria para la nueva cita.
    - new_end: opcional. Si no se provee, se calcula automáticamente sumando 45 min a 'new_start'.

    1. Busca citas con 'phone'.
    2. Si hay múltiples y no se especifica 'original_start', se devuelven todas
       para que la IA decida cuál editar.
    3. Si se especifica 'original_start', se intenta localizar esa cita exacta.
    4. Se patchan solo 'start' y 'end', manteniendo 'summary' (nombre) y 'description' (motivo, phone) intactos.
    """

    try:
        if len(phone) != 10 or not phone.isdigit():
            raise ValueError("Teléfono inválido (debe tener 10 dígitos)")

        # Buscar evento(s)
        events = search_calendar_event_by_phone(phone)
        if not events:
            return {"error": "No se encontró ninguna cita con ese número"}

        # Si no hay 'original_start', y hay > 1, devolvemos la lista de citas para que la IA pregunte
        if not original_start:
            if len(events) > 1:
                return {
                    "multiple_events": True,
                    "events_found": _convert_events_list(events),
                    "message": "Se encontraron múltiples citas con este número. Indique cuál desea editar."
                }
            else:
                target_evt = events[0]
        else:
            # Validamos la fecha/hora original
            original_dt = validate_and_convert_time(original_start)
            target_evt = None
            for e in events:
                start_str = e["start"]["dateTime"]
                start_dt = validate_and_convert_time(start_str)
                # Convertimos a tz Cancún para comparar
                # asumiendo que start_str ya es '2025-04-22T09:30:00-05:00'
                start_dt = start_dt.astimezone(pytz.timezone("America/Cancun"))
                original_dt = original_dt.astimezone(pytz.timezone("America/Cancun"))

                # Usamos un umbral de ~1 min para considerar "igual"
                if abs((start_dt - original_dt).total_seconds()) < 60:
                    target_evt = e
                    break

            if not target_evt:
                return {
                    "multiple_events": True,
                    "events_found": _convert_events_list(events),
                    "message": (
                        "No se encontró una cita con ese horario exacto. "
                        "Por favor seleccione cuál de la lista desea editar."
                    )
                }

        # A partir de aquí, 'target_evt' es la cita a editar
        # Convertimos new_start en datetime
        new_start_dt = validate_and_convert_time(new_start)
        new_start_dt = new_start_dt.astimezone(pytz.timezone("America/Cancun"))

        # Si no especifican new_end, sumamos 45 min
        if not new_end:
            new_end_dt = new_start_dt + timedelta(minutes=45)
        else:
            new_end_dt = validate_and_convert_time(new_end)
            new_end_dt = new_end_dt.astimezone(pytz.timezone("America/Cancun"))

        # Verificar disponibilidad
        busy_slots = get_cached_availability()
        if not is_slot_available(new_start_dt, new_end_dt, busy_slots):
            return {"error": "Horario no disponible"}

        service = initialize_google_calendar()
        updated_event = service.events().patch(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=target_evt["id"],
            body={
                "start": {"dateTime": new_start_dt.isoformat(), "timeZone": "America/Cancun"},
                "end": {"dateTime": new_end_dt.isoformat(), "timeZone": "America/Cancun"}
            }
        ).execute()

        logger.info(f"✅ Cita editada exitosamente: {updated_event['id']}")
        reason_in_event = _parse_reason_from_description(target_evt.get("description", ""))

        return {
            "id": updated_event["id"],
            "start": updated_event["start"]["dateTime"],
            "end": updated_event["end"]["dateTime"],
            "name": target_evt.get("summary", "(sin nombre)"),
            "reason": reason_in_event,
        }

    except ValueError as ve:
        logger.error(f"❌ Error de validación en edit_calendar_event: {str(ve)}")
        return {"error": str(ve)}
    except Exception as e:
        logger.error(f"❌ Error en Google Calendar: {str(e)}")
        return {"error": "CALENDAR_UNAVAILABLE"}


@router.put("/editar-cita")
async def api_edit_calendar_event(
    phone: str,
    original_start: str = None,
    new_start: str = None,
    new_end: str = None
):
    """
    Endpoint para editar una cita.
    - phone: número de WhatsApp de 10 dígitos (obligatorio).
    - original_start: fecha-hora ISO con zona horaria de la cita original (opcional).
    - new_start: nueva fecha-hora ISO con zona horaria (opcional).
    - new_end: nueva fecha-hora ISO con zona horaria (opcional).
      Si no se pasa, el sistema asume 45min (new_start_dt + 45min).

    Si hay múltiples citas y no proporcionas original_start, se devolverá
    un objeto con multiple_events=True y la lista de eventos encontrados.
    """
    try:
        if not new_start:
            raise HTTPException(
                status_code=400,
                detail="Se requiere new_start en formato ISO8601 con zona horaria."
            )

        result = edit_calendar_event(phone, original_start, new_start, new_end)

        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])

        if "multiple_events" in result and result["multiple_events"]:
            return result

        return {
            "message": "✅ Cita actualizada",
            "edited_event": result
        }

    except HTTPException as he:
        raise he
    except Exception as e:
        logger.error(f"❌ Error crítico en endpoint /editar-cita: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")
