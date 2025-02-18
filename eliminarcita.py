# -*- coding: utf-8 -*-
"""
Módulo para la eliminación de citas en Google Calendar.
Permite buscar y eliminar eventos en la agenda del consultorio del Dr. Wilfrido Alarcón.
"""

import logging
from fastapi import APIRouter, HTTPException
from utils import (
    initialize_google_calendar,
    GOOGLE_CALENDAR_ID,
    search_calendar_event_by_phone
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

def delete_calendar_event(phone, patient_name=None):
    """
    Elimina la(s) cita(s) que coincidan con el número de teléfono.
    - Si hay múltiples, se filtra por 'patient_name' si se proporciona.
    - Retorna dict con mensaje de éxito o error.
    """
    try:
        if not phone or len(phone) != 10 or not phone.isdigit():
            return {"error": "El número de teléfono debe ser de 10 dígitos."}

        service = initialize_google_calendar()
        events = search_calendar_event_by_phone(phone)
        if not events:
            logger.warning(f"⚠️ No se encontraron citas para el número: {phone}")
            return {"message": "No se encontraron citas con el número proporcionado."}

        # Si hay múltiples y se provee patient_name, filtramos
        if patient_name:
            filtered = []
            for evt in events:
                if evt["name"].lower() == patient_name.lower():
                    filtered.append(evt)
            if not filtered:
                logger.warning(f"⚠️ Ninguna cita coincide con el nombre {patient_name} para el número {phone}.")
                return {"message": f"No se encontró una cita con el nombre {patient_name}."}
            events = filtered

        if len(events) > 1:
            # Aún hay múltiples citas
            return {
                "message": "Se encontraron múltiples citas con este número. Proporcione el nombre del paciente.",
                "options": [e["name"] for e in events]
            }

        # Ya tenemos una única cita
        event_to_delete = events[0]
        service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event_to_delete["id"]).execute()
        logger.info(f"✅ Cita eliminada para {event_to_delete['name']}")

        return {"message": f"El evento para {event_to_delete['name']} ha sido eliminado con éxito."}

    except Exception as e:
        logger.error(f"❌ Error al eliminar cita en Google Calendar: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}

@router.delete("/eliminar-cita")
async def api_delete_calendar_event(phone: str, patient_name: str = None):
    """
    Endpoint para eliminar una cita en Google Calendar.

    Parámetros:
    - phone (str): Número de teléfono (10 dígitos).
    - patient_name (str, opcional): Nombre del paciente si hay múltiples citas.

    Retorna:
    - Dict con confirmación de eliminación o mensaje de error.
    """
    try:
        if not phone.isdigit() or len(phone) != 10:
            raise HTTPException(status_code=400, detail="El número de teléfono debe tener 10 dígitos.")

        result = delete_calendar_event(phone, patient_name)
        if "error" in result:
            raise HTTPException(status_code=500, detail=result["error"])

        return result

    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"❌ Error en el endpoint de eliminación de cita: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor.")
