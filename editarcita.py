# editarcita.py
# -*- coding: utf-8 -*-
"""
Módulo para edición de citas.
La verificación de disponibilidad del nuevo slot se asume que fue hecha
previamente por process_appointment_request.
"""

import logging
import pytz # Para manejo de zonas horarias si es necesario internamente
import re # Para parsear la descripción
from datetime import datetime, timedelta # timedelta podría no ser necesario si new_end_time_iso siempre se provee


# Importaciones de utils deben ser correctas
from utils import (
    initialize_google_calendar,
    GOOGLE_CALENDAR_ID

)

logging.basicConfig(level=logging.INFO) # Ajusta el nivel según necesites
logger = logging.getLogger(__name__)

def _parse_field_from_description(description: str, field_name: str, is_phone: bool = False) -> str | None:
    """
    Extrae un campo específico de la descripción.
    Ejemplo: field_name="Motivo" o field_name="Teléfono".
    """
    if not description:
        return None
    # Usar re.IGNORECASE para ser más robusto
    # Para teléfono, busca dígitos y algunos caracteres comunes, limpiándolos después.
    pattern_str = rf"{field_name}:\s*(.*)"
    match = re.search(pattern_str, description, re.IGNORECASE | re.MULTILINE)
    if match:
        value = match.group(1).strip()
        if is_phone:
            # Limpiar caracteres no numéricos si es un teléfono
            return re.sub(r"[^\d]", "", value)
        return value
    return None


def edit_calendar_event(
    event_id: str,
    new_start_time_iso: str,
    new_end_time_iso: str,
    new_name: str | None = None,
    new_reason: str | None = None,
    new_phone_for_description: str | None = None 
):
    """
    Edita una cita existente utilizando su event_id.
    La disponibilidad del nuevo horario (new_start_time_iso, new_end_time_iso)
    DEBE haber sido verificada y confirmada previamente por la IA usando 'process_appointment_request'.

    Parámetros:
        event_id (str): El ID único del evento de Google Calendar a modificar.
        new_start_time_iso (str): Nueva hora de inicio en formato ISO8601 con offset.
        new_end_time_iso (str): Nueva hora de fin en formato ISO8601 con offset.
        new_name (str, opcional): Nuevo nombre del paciente (summary del evento).
        new_reason (str, opcional): Nuevo motivo para la descripción.
        new_phone_for_description (str, opcional): Nuevo teléfono para la descripción.

    Retorna:
        Un diccionario con los detalles del evento actualizado o un diccionario con una clave "error".
    """
    logger.info(f"Intentando editar evento ID: {event_id} para nuevo horario: {new_start_time_iso}")
    try:
        service = initialize_google_calendar()

        # 1. Obtener el evento original para acceder a su summary y description actuales
        try:
            original_event = service.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        except Exception as e_get:
            logger.error(f"Error al obtener el evento original ({event_id}) para editar: {e_get}")
            return {"error": f"No se pudo encontrar la cita original con ID {event_id} para modificar."}

        # 2. Validar formato de los nuevos tiempos (básico, `process_appointment_request` hizo el trabajo duro)
        try:
            datetime.fromisoformat(new_start_time_iso)
            datetime.fromisoformat(new_end_time_iso)
        except ValueError:
            logger.error(f"Formato ISO inválido para new_start_time_iso ('{new_start_time_iso}') o new_end_time_iso ('{new_end_time_iso}').")
            return {"error": "El nuevo formato de hora para la cita es inválido."}

        # 3. Preparar el cuerpo del evento para la actualización (patch)
        updated_body = {
            "start": {"dateTime": new_start_time_iso, "timeZone": "America/Cancun"}, # Google Calendar maneja la zona horaria
            "end": {"dateTime": new_end_time_iso, "timeZone": "America/Cancun"}
        }

        # Actualizar summary (nombre) si se provee uno nuevo
        if new_name:
            updated_body["summary"] = new_name
        else:
            updated_body["summary"] = original_event.get("summary", "Cita") # Mantener original si no hay nuevo

        # Reconstruir la descripción si se actualiza el motivo o el teléfono
        original_description = original_event.get("description", "")
        
        # Extraer teléfono y motivo actuales de la descripción original
        current_phone_in_desc = _parse_field_from_description(original_description, "Teléfono", is_phone=True)
        current_reason_in_desc = _parse_field_from_description(original_description, "Motivo")

        # Usar los nuevos valores si se proveen, si no, los actuales de la descripción
        phone_to_write = new_phone_for_description if new_phone_for_description else current_phone_in_desc
        reason_to_write = new_reason if new_reason else current_reason_in_desc
        
        new_description_parts = []
        if phone_to_write:
            new_description_parts.append(f"📞 Teléfono: {phone_to_write}")
        if reason_to_write:
            new_description_parts.append(f"📝 Motivo: {reason_to_write}")
        
        if new_description_parts: # Si hay teléfono o motivo para escribir
            updated_body["description"] = "\n".join(new_description_parts)
        elif original_description: # Si no hay nuevos pero había descripción original
            updated_body["description"] = original_description
        # Si no hay nuevos y no había descripción original, no se añade campo description.

        # 4. Realizar la actualización (patch)
        updated_event = service.events().patch(
            calendarId=GOOGLE_CALENDAR_ID,
            eventId=event_id,
            body=updated_body
        ).execute()

        logger.info(f"✅ Cita editada exitosamente. Evento ID: {updated_event.get('id')}")
        
        # Devolver la información clave del evento actualizado
        return {
            "id": updated_event.get("id"),
            "name": updated_event.get("summary"),
            "start_time_iso": updated_event.get("start", {}).get("dateTime"),
            "end_time_iso": updated_event.get("end", {}).get("dateTime"),
            "reason": _parse_field_from_description(updated_event.get("description", ""), "Motivo"),
            "phone_in_description": _parse_field_from_description(updated_event.get("description", ""), "Teléfono", is_phone=True),
            "message": "Cita actualizada exitosamente."
        }

    except Exception as e:
        logger.error(f"❌ Error general en la función edit_calendar_event: {str(e)}", exc_info=True)
        return {"error": f"Ocurrió un error en el servidor al intentar editar la cita: {str(e)}"}