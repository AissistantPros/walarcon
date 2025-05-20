# eliminarcita.py
# -*- coding: utf-8 -*-
"""
Módulo para la eliminación de citas en Google Calendar.
La IA debe proveer el event_id de la cita a eliminar, obtenido previamente
mediante search_calendar_event_by_phone.
"""

import logging
import pytz # Para _validate_iso_datetime si se mantiene para alguna validación
from datetime import datetime
from tw_utils import session_state


from utils import (
    initialize_google_calendar,
    GOOGLE_CALENDAR_ID
    # search_calendar_event_by_phone, # Es llamado por la IA antes de llamar a esta función
)

logging.basicConfig(level=logging.INFO) # Ajusta el nivel según necesites
logger = logging.getLogger(__name__)

def _validate_iso_datetime_string_simple(dt_str: str) -> bool:
    """Valida si el string parece un ISO datetime. No convierte."""
    try:
        datetime.fromisoformat(dt_str.replace("Z", "+00:00")) # Reemplazar Z para compatibilidad
        return True
    except ValueError:
        return False

def delete_calendar_event(event_id: str, original_start_time_iso: str | None = None):
    """
    Elimina la cita especificada por event_id.
    original_start_time_iso es opcional y solo para confirmación o logging si se desea.

    Parámetros:
        event_id (str): El ID único del evento de Google Calendar a eliminar.
        original_start_time_iso (str, opcional): Hora de inicio original para logging o una capa extra de confirmación (no usada para la operación de borrado por ID).

    Retorna:
        Un diccionario con un mensaje de éxito o un diccionario con una clave "error".
    """
  # ─── Parche: si la IA mandó un ID vacío o de ejemplo, usamos el seleccionado ───
    if event_id in ("", "a1b2c3d4e5f6g7h8"):
        real_id = session_state.get("current_event_id")
        if real_id:
            event_id = real_id

            
    logger.info(f"Intentando eliminar evento ID: {event_id}"
                f"{f' (hora original confirmada: {original_start_time_iso})' if original_start_time_iso else ''}")

    if not event_id:
        logger.error("No se proporcionó event_id para eliminar la cita.")
        return {"error": "No se especificó el ID de la cita a eliminar."}

    # Validación opcional del formato de original_start_time_iso si se usa
    if original_start_time_iso and not _validate_iso_datetime_string_simple(original_start_time_iso):
        logger.warning(f"El formato de original_start_time_iso ('{original_start_time_iso}') parece inválido, pero se procederá con la eliminación por ID.")
        # No es un error fatal ya que el event_id es lo principal.

    try:
        service = initialize_google_calendar()
        
        # Opcional: Antes de borrar, podrías obtener el evento para loguear su 'summary'
        try:
            event_to_delete = service.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
            event_summary = event_to_delete.get('summary', '(cita sin título)')
            logger.info(f"Se procederá a eliminar la cita: '{event_summary}' (ID: {event_id})")
        except Exception as e_get:
            logger.warning(f"No se pudo obtener el evento {event_id} antes de eliminar (puede que ya no exista o ID incorrecto): {e_get}")
            # Si no se puede obtener, igual intentar borrar si la IA está segura del ID.
            # O podrías devolver un error aquí si consideras que es necesario confirmar que existe antes de borrar.
            # Por ahora, se procede a intentar el borrado.
            event_summary = "(no se pudo obtener resumen)"


        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_id).execute()
        
        logger.info(f"✅ Cita eliminada exitosamente. Evento ID: {event_id}, Resumen: {event_summary}")
        return {
            "message": f"La cita para '{event_summary}' ha sido eliminada con éxito.",
            "deleted_event_id": event_id
        }

    except Exception as e:
        logger.error(f"❌ Error en la función delete_calendar_event al intentar eliminar ID {event_id}: {str(e)}", exc_info=True)
        # Revisa si el error es porque el evento no existe (ej. 'HttpError 404')
        if hasattr(e, 'resp') and hasattr(e.resp, 'status') and e.resp.status == 404:
             logger.warning(f"El evento con ID {event_id} no fue encontrado. Es posible que ya haya sido eliminado.")
             return {"error": f"La cita con ID {event_id} no fue encontrada. Es posible que ya haya sido eliminada."}
        return {"error": f"Ocurrió un error en el servidor al intentar eliminar la cita: {str(e)}"}