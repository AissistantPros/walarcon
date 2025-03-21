# -*- coding: utf-8 -*-
#buscarslot.py
"""
Módulo para buscar el siguiente horario disponible en Google Calendar (usando caché).
Optimizado para manejar prioridades y fechas específicas.
"""

from datetime import datetime, timedelta
import pytz
import logging
from fastapi import APIRouter, HTTPException
from utils import (
    get_cancun_time,
    is_slot_available,
    get_cached_availability
)

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Crear el router para FastAPI
router = APIRouter()

# ==================================================
# 🔹 Configuración de horarios
# ==================================================
# Cada cita dura 45 minutos. Estos slots son de ejemplo.
SLOT_TIMES = [
    {"start": "09:30", "end": "10:15"},
    {"start": "10:15", "end": "11:00"},
    {"start": "11:00", "end": "11:45"},
    {"start": "11:45", "end": "12:30"},
    {"start": "12:30", "end": "13:15"},
    {"start": "13:15", "end": "14:00"},
    {"start": "14:00", "end": "14:45"},
]

# ==================================================
# 🔹 Búsqueda del Próximo Horario Disponible
# ==================================================
def find_next_available_slot(target_date=None, target_hour=None, urgent=False):
    """
    Busca el siguiente horario disponible, consultando la caché de slots ocupados.
    Si target_date no está vacío, busca a partir de esa fecha.
    Si target_hour se especifica, busca slots después de esa hora.
    Si urgent=True, evita las próximas 4 horas desde 'ahora'.

    Retorna:
      dict: {"start_time": <ISO>, "end_time": <ISO>} o {"error": "..."}
    """
    try:
        now = get_cancun_time()
        if target_date:
            # Construimos la fecha base (con la hora 00:00).
            # Ej: "2025-02-10"
            date_obj = datetime.strptime(target_date, "%Y-%m-%d")
            local_tz = pytz.timezone("America/Cancun")
            search_start = local_tz.localize(datetime(date_obj.year, date_obj.month, date_obj.day, 0, 0))
            # No permitir fecha en el pasado
            if search_start < now:
                return {"error": "No se puede agendar en el pasado."}
        elif urgent:
            search_start = now + timedelta(hours=4)
        else:
            search_start = now

        # Convertir target_hour a hora + slot
        if target_hour:
            target_hour = adjust_to_valid_slot(target_hour, SLOT_TIMES)

        # Buscar hasta 6 meses en adelante
        MAX_DAYS_LOOKAHEAD = 180
        for offset in range(MAX_DAYS_LOOKAHEAD):
            day_to_check = search_start + timedelta(days=offset)

            # Evitar domingos (weekday() == 6)
            if day_to_check.weekday() == 6:
                continue

            for slot in SLOT_TIMES:
                # Construimos la hora de inicio y fin en local time
                start_str = f"{day_to_check.strftime('%Y-%m-%d')} {slot['start']}:00"
                end_str = f"{day_to_check.strftime('%Y-%m-%d')} {slot['end']}:00"

                cancun_tz = pytz.timezone("America/Cancun")
                start_dt = cancun_tz.localize(datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S"))
                end_dt = cancun_tz.localize(datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S"))

                # Si es hoy y urgent, evitamos < now+4h
                if urgent and start_dt < now + timedelta(hours=4):
                    continue

                # Si target_hour está definido, skip slots menores
                if target_hour and slot["start"] < target_hour:
                    continue

                # Verificamos que el slot no sea pasado
                if start_dt < now:
                    continue

                # Verificamos que no haya overlap con busy_slots
                if is_slot_available(start_dt, end_dt):
                    return {
                        "start_time": start_dt.isoformat(),
                        "end_time": end_dt.isoformat()
                    }

        return {"error": "No se encontraron horarios disponibles en los próximos 6 meses."}

    except Exception as e:
        logger.error(f"❌ Error inesperado al buscar horario: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}

def adjust_to_valid_slot(requested_time, slot_times):
    """
    Ajusta una hora solicitada a un slot válido (por ejemplo, si piden "10:00",
    se busca el slot que comience >= 10:00).
    """
    req_time_obj = datetime.strptime(requested_time, "%H:%M").time()
    for s in slot_times:
        slot_start_obj = datetime.strptime(s["start"], "%H:%M").time()
        if slot_start_obj >= req_time_obj:
            return s["start"]
    return slot_times[-1]["start"]  # si no hay slot mayor, usar el último

# ==================================================
# 🔹 Endpoint para consultar disponibilidad
# ==================================================
@router.get("/buscar-disponibilidad")
async def get_next_available_slot(target_date: str = None, target_hour: str = None, urgent: bool = False):
    """
    Endpoint para buscar el próximo horario disponible en la agenda.
    
    Parámetros:
    - target_date (str, opcional): Fecha específica (YYYY-MM-DD).
    - target_hour (str, opcional): Hora preferida (HH:MM).
    - urgent (bool, opcional): Si True, omite las próximas 4 horas.

    Retorna:
    - Un dict con el horario disponible o un mensaje de error.
    """
    slot = find_next_available_slot(target_date, target_hour, urgent)
    if "error" in slot:
        raise HTTPException(status_code=404, detail=slot["error"])
    return slot
