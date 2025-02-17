# -*- coding: utf-8 -*-
"""
Módulo para buscar el siguiente horario disponible en Google Calendar.
Optimizado para manejar prioridades y fechas específicas.
"""

from datetime import datetime, timedelta
import pytz
import logging
from utils import get_cancun_time, initialize_google_calendar, GOOGLE_CALENDAR_ID

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================================================
# 🔹 Configuración de horarios
# ==================================================
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
    Busca el siguiente horario disponible en Google Calendar.

    Parámetros:
      target_date (str, opcional): Fecha específica para buscar disponibilidad (YYYY-MM-DD).
      target_hour (str, opcional): Hora preferida (HH:MM). 
        - Si no coincide con los slots válidos, se ajustará al más cercano.
      urgent (bool, opcional): Si True, evita los próximos 4 horas y busca después.
    
    Retorna:
      dict: {"start_time": <ISO 8601>, "end_time": <ISO 8601>} con la cita encontrada
      o {"error": "..."} si no hay disponibilidad.
    """

    try:
        # Inicializar Google Calendar
        service = initialize_google_calendar()

        # Configurar límites de búsqueda (6 meses adelante, 2 semanas atrás **pero nunca antes de hoy**)
        MAX_DAYS_LOOKAHEAD = 180  # 6 meses adelante
        MAX_DAYS_BACKWARD = 14    # 2 semanas atrás **solo si sigue en el futuro**

        # Obtener la hora actual en Cancún
        now = get_cancun_time()

        # Determinar fecha base para la búsqueda
        if target_date:
            search_start = datetime.strptime(target_date, "%Y-%m-%d").replace(tzinfo=pytz.timezone("America/Cancun"))
            if search_start < now:
                return {"error": "No se puede agendar en el pasado"}
        elif urgent:
            search_start = now + timedelta(hours=4)  # Evita las próximas 4 horas
        else:
            search_start = now

        # Ajustar target_hour si no es un slot válido
        if target_hour:
            target_hour = adjust_to_valid_slot(target_hour, SLOT_TIMES)

        # Buscar en fechas **hacia atrás y hacia adelante**
        for offset in range(-MAX_DAYS_BACKWARD, MAX_DAYS_LOOKAHEAD):
            search_day = search_start + timedelta(days=offset)

            # No buscar en domingos
            if search_day.weekday() == 6:
                continue

            # No buscar en fechas anteriores a hoy
            if search_day.date() < now.date():
                continue

            for slot in SLOT_TIMES:
                start_time_str = f"{search_day.strftime('%Y-%m-%d')} {slot['start']}:00"
                end_time_str = f"{search_day.strftime('%Y-%m-%d')} {slot['end']}:00"

                start_dt = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))
                end_dt = datetime.strptime(end_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))

                # Si es hoy y hay restricción de urgencia, evitar horarios antes de `now + 4h`
                if search_day.date() == now.date() and urgent and start_dt < now + timedelta(hours=4):
                    continue

                # Si target_hour está definido, buscar esa hora exacta en los días cercanos
                if target_hour and slot["start"] < target_hour:
                    continue

                # Verificar disponibilidad real en Google Calendar
                events = service.freebusy().query(body={
                    "timeMin": start_dt.isoformat(),
                    "timeMax": end_dt.isoformat(),
                    "timeZone": "America/Cancun",
                    "items": [{"id": GOOGLE_CALENDAR_ID}]
                }).execute()

                if not events["calendars"][GOOGLE_CALENDAR_ID]["busy"]:
                    return {
                        "start_time": start_dt.isoformat(),
                        "end_time": end_dt.isoformat()
                    }

        return {"error": "No se encontraron horarios disponibles en los próximos 6 meses."}

    except Exception as e:
        logger.error(f"❌ Error inesperado al buscar horario: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}

# ==================================================
# 🔹 Ajuste de Horario a Slot Válido
# ==================================================
def adjust_to_valid_slot(requested_time, slot_times):
    """
    Ajusta una hora solicitada a un slot válido.

    Parámetros:
        requested_time (str): Hora solicitada por el usuario en formato HH:MM.
        slot_times (list): Lista de horarios válidos.

    Retorna:
        str: La hora ajustada al slot más cercano.
    """
    requested_time_obj = datetime.strptime(requested_time, "%H:%M").time()

    for slot in slot_times:
        slot_time = datetime.strptime(slot["start"], "%H:%M").time()
        if slot_time >= requested_time_obj:
            return slot["start"]

    # Si la hora solicitada es después del último slot, devolver el último slot
    return slot_times[-1]["start"]

# ==================================================
# 🔹 Prueba Local del Módulo
# ==================================================
if __name__ == "__main__":
    """
    Prueba rápida para verificar el funcionamiento de la búsqueda de horarios.
    Se recomienda ejecutar este script directamente para depuración.
    """
    try:
        slot = find_next_available_slot()
        if isinstance(slot, dict) and "start_time" in slot:
            print(f"✅ Próximo horario disponible: {slot['start_time']} - {slot['end_time']}")
        else:
            print(f"❌ No se encontró horario: {slot}")
    except Exception as e:
        print(f"❌ Error desconocido: {str(e)}")
