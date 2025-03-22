# -*- coding: utf-8 -*-
# buscarslot.py
"""
Módulo para buscar el siguiente horario disponible en Google Calendar (usando caché).
Optimizado para manejar prioridades y fechas específicas, precargando slots LIBRES
para los próximos 90 días y evitando llamadas constantes a Calendar.
"""

import logging
from datetime import datetime, timedelta
import pytz
from fastapi import APIRouter, HTTPException

# Importa tus utilidades de Calendar y hora local
from utils import (
    initialize_google_calendar,
    get_cancun_time,
    cache_lock,
    availability_cache,  # dejaremos la estructura antigua para no romper nada
    logger as utils_logger,
)

# =========================================
# Configuración de logging local
# =========================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# =========================================
# APIRouter para exponer endpoints
# =========================================
router = APIRouter()

# =========================================
# Slots de ejemplo (cada cita dura ~45 min)
# =========================================
SLOT_TIMES = [
    {"start": "09:30", "end": "10:15"},
    {"start": "10:15", "end": "11:00"},
    {"start": "11:00", "end": "11:45"},
    {"start": "11:45", "end": "12:30"},
    {"start": "12:30", "end": "13:15"},
    {"start": "13:15", "end": "14:00"},
    {"start": "14:00", "end": "14:45"},
]

# =========================================
# Caché de slots LIBRES
# Estructura:
#   free_slots_cache = {
#       "YYYY-MM-DD": [ "09:30", "10:15", ... ],
#       ...
#   }
# =========================================
free_slots_cache = {}
last_cache_update = None
CACHE_VALID_MINUTES = 15  # Ej: refresca cada 15 min en la misma llamada, si deseas

# =========================================
# 1) Precargar slots libres
# =========================================
def load_free_slots_to_cache(days_ahead=90):
    """
    Carga en caché los slots LIBRES de los próximos `days_ahead` días.
    Almacena el resultado en `free_slots_cache`.
    """
    global free_slots_cache, last_cache_update

    # Bloquea para evitar race conditions si se llama varias veces
    with cache_lock:
        logger.info("⏳ Cargando slots libres desde Google Calendar...")
        free_slots_cache.clear()

        # Inicializa servicio de Calendar
        service = initialize_google_calendar()
        now = get_cancun_time()
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days_ahead)).isoformat()

        # Consulta FREEBUSY para obtener "busy" (ocupados)
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "timeZone": "America/Cancun",
            "items": [{"id": service.calendarId}],
        }

        # A veces guardamos el ID del calendario en la clase service,
        # depende de tu initialize_google_calendar. Ajusta si difiere:
        calendar_id = service.calendarId

        events_result = service.freebusy().query(body=body).execute()
        busy_slots = events_result["calendars"][calendar_id]["busy"]

        # Construye un map de días: rangos ocupados en UTC
        busy_by_day = {}
        for b in busy_slots:
            start_utc = b["start"]
            end_utc = b["end"]
            # Convertir a datetime local Cancún para agrupar por día
            start_local = convert_utc_to_cancun(start_utc)
            end_local = convert_utc_to_cancun(end_utc)

            day_key = start_local.strftime("%Y-%m-%d")
            if day_key not in busy_by_day:
                busy_by_day[day_key] = []
            busy_by_day[day_key].append((start_local, end_local))

        # Para cada día en el rango, calcular slots libres
        for offset in range(days_ahead + 1):
            day_date = now + timedelta(days=offset)
            day_str = day_date.strftime("%Y-%m-%d")

            # Construye la lista de slots libres
            if day_date.weekday() == 6:  
                # Domingo => sin citas
                free_slots_cache[day_str] = []
                continue

            # Aplica la lógica: si no está en busy_by_day => todo el día está libre
            day_busy_list = busy_by_day.get(day_str, [])
            free_slots = build_free_slots_for_day(day_date, day_busy_list)
            free_slots_cache[day_str] = free_slots

        last_cache_update = get_cancun_time()
        logger.info(f"✅ Slots libres precargados para los próximos {days_ahead} días.")


def build_free_slots_for_day(day_dt, busy_list):
    """
    Devuelve la lista de horas (str) disponibles del día (ej: ["09:30","10:15",...])
    Recibe la lista de rangos ocupados para ese día en hora local.
    """
    day_str = day_dt.strftime("%Y-%m-%d")
    free_list = []

    for slot in SLOT_TIMES:
        # Arma start y end de este slot en local tz
        start_str = f"{day_str} {slot['start']}:00"
        end_str = f"{day_str} {slot['end']}:00"

        start_dt = pytz.timezone("America/Cancun").localize(datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S"))
        end_dt = pytz.timezone("America/Cancun").localize(datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S"))

        # Verifica si se solapa con algo en busy_list
        overlapped = False
        for (busy_start, busy_end) in busy_list:
            # si (start < busy_end) y (end > busy_start) => overlap
            if start_dt < busy_end and end_dt > busy_start:
                overlapped = True
                break

        if not overlapped:
            free_list.append(slot["start"])  # Ej: "09:30"

    return free_list


def ensure_cache_is_fresh():
    """
    Revisa si el caché de slots libres está vacío o desactualizado.
    Si está vacío o pasaron más de CACHE_VALID_MINUTES => recarga.
    """
    global last_cache_update

    now = get_cancun_time()
    if not last_cache_update:
        # Cargamos por primera vez
        load_free_slots_to_cache(days_ahead=90)
    else:
        # Ver si han pasado más de X minutos
        diff = (now - last_cache_update).total_seconds() / 60.0
        if diff > CACHE_VALID_MINUTES:
            load_free_slots_to_cache(days_ahead=90)


def convert_utc_to_cancun(utc_str):
    """
    Convierte string UTC (ISO8601) a datetime en tz Cancún.
    """
    utc_dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
    cancun_tz = pytz.timezone("America/Cancun")
    return utc_dt.astimezone(cancun_tz)

# =========================================
# 2) Ajustar a slot válido
# =========================================
def adjust_to_valid_slot(requested_time, slot_times):
    """
    Ajusta una hora solicitada a un slot válido (por ejemplo, si piden "10:00",
    se busca el slot que comience >= 10:00).
    Retorna el 'start' (str) del slot válido.
    """
    req_time_obj = datetime.strptime(requested_time, "%H:%M").time()
    for s in slot_times:
        slot_start_obj = datetime.strptime(s["start"], "%H:%M").time()
        if slot_start_obj >= req_time_obj:
            return s["start"]
    # Si no hay slot mayor, usar el último
    return slot_times[-1]["start"]


# =========================================
# 3) Función principal de búsqueda
# =========================================
def find_next_available_slot(target_date=None, target_hour=None, urgent=False):
    """
    Busca el siguiente horario disponible, consultando la caché de slots LIBRES.
    Si target_date no está vacío, busca a partir de esa fecha.
    Si target_hour se especifica, busca slots después de esa hora.
    Si urgent=True, omite las próximas 4 horas desde 'ahora'.

    Retorna:
      dict: {"start_time": <ISO>, "end_time": <ISO>} o {"error": "..."}
    """
    try:
        # 1) Aseguramos que el caché está listo
        ensure_cache_is_fresh()

        now = get_cancun_time()

        # 2) Definir fecha/hora inicial de búsqueda (search_start)
        if target_date:
            # Construimos la fecha base con hora 0
            date_obj = datetime.strptime(target_date, "%Y-%m-%d")
            local_tz = pytz.timezone("America/Cancun")
            search_start = local_tz.localize(datetime(date_obj.year, date_obj.month, date_obj.day, 0, 0))

            # No permitir fecha en el pasado
            if search_start < now:
                return {"error": "No se puede agendar en el pasado."}

        elif urgent:
            # Si es urgente => arrancar en now + 4 horas
            search_start = now + timedelta(hours=4)
        else:
            # Caso por default, buscar a partir de hoy (hora 0)
            local_tz = pytz.timezone("America/Cancun")
            search_start = local_tz.localize(datetime(now.year, now.month, now.day, 0, 0))

        # 3) Ajustar hora solicitada a slot válido (opcional)
        #    (p.ej. "09:00" => "09:30")
        valid_target_hour = None
        if target_hour:
            valid_target_hour = adjust_to_valid_slot(target_hour, SLOT_TIMES)  # Ej "09:30"

        # 4) Empezar la búsqueda hasta 180 días adelante (o 90, como gustes)
        MAX_DAYS_LOOKAHEAD = 180
        for offset in range(MAX_DAYS_LOOKAHEAD):
            day_to_check = search_start + timedelta(days=offset)

            # Salta si es domingo
            if day_to_check.weekday() == 6:
                continue

            # Si el día está en el pasado ( vs. "ahora" ), saltar
            # (p.ej. si search_start era hoy, offset=0, pero la hora actual es 11am,
            #  no queremos slots de 9:30)
            # => Manejo más abajo a nivel slot.

            day_str = day_to_check.strftime("%Y-%m-%d")
            if day_str not in free_slots_cache:
                # Significa que no hay info. O es muy lejano. Saltamos
                continue

            # Extraer la lista de slots libres (ej ["09:30","10:15",...])
            day_free_slots = free_slots_cache[day_str]
            if not day_free_slots:
                # Día sin huecos
                continue

            # Revisar slot por slot
            for free_slot_start in day_free_slots:
                # Construir datetime de inicio y fin
                start_str = f"{day_str} {free_slot_start}:00"
                start_dt = pytz.timezone("America/Cancun").localize(
                    datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S")
                )

                # Encuentra la definición en SLOT_TIMES (para obtener el end)
                end_str = None
                for st in SLOT_TIMES:
                    if st["start"] == free_slot_start:
                        end_str = st["end"]
                        break
                if not end_str:
                    continue

                end_dt = pytz.timezone("America/Cancun").localize(
                    datetime.strptime(f"{day_str} {end_str}:00", "%Y-%m-%d %H:%M:%S")
                )

                # Reglas:
                #   a) Si urgent => evitar start_dt < now+4h
                if urgent and start_dt < (now + timedelta(hours=4)):
                    continue

                #   b) Si start_dt < now => es pasado
                if start_dt < now:
                    continue

                #   c) Si hay valid_target_hour => solo slots >= esa hora
                if valid_target_hour:
                    # "09:30" => time(9,30)
                    # free_slot_start => "09:30" => lo comparamos con valid_target_hour
                    fs_time_obj = datetime.strptime(free_slot_start, "%H:%M").time()
                    vt_time_obj = datetime.strptime(valid_target_hour, "%H:%M").time()
                    if fs_time_obj < vt_time_obj:
                        # Aún muy temprano, skip
                        continue

                # Si llegamos aquí, es el primer slot que cumple:
                return {
                    "start_time": start_dt.isoformat(),
                    "end_time": end_dt.isoformat()
                }

        # Si terminamos el bucle y no encontramos nada
        return {"error": "No se encontraron horarios disponibles en los próximos 6 meses."}

    except Exception as e:
        logger.error(f"❌ Error inesperado al buscar horario: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}


# =========================================
# 4) Endpoint FastAPI (opcional)
# =========================================
@router.get("/buscar-disponibilidad")
async def get_next_available_slot(
    target_date: str = None,
    target_hour: str = None,
    urgent: bool = False
):
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

# =========================================
# (Opcional) Funciones Legacy para no romper
# =========================================
def is_slot_available(start_dt, end_dt):
    """
    LEGACY. Se dejaba para checar un slot contra busy_slots.
    Ahora usamos `free_slots_cache`, pero lo mantenemos para compatibilidad.
    Retorna True si ese horario está dentro de los slots libres del día.
    """
    day_str = start_dt.strftime("%Y-%m-%d")
    if day_str not in free_slots_cache:
        # forzamos recarga si no hay nada
        ensure_cache_is_fresh()
    if day_str not in free_slots_cache:
        return False  # sin info = no?
    # Para saber si está disponible, confirmamos que la hora de start_dt
    # esté en la lista de free_slots_cache[day_str] y coincida con la misma
    # end. Esta es una forma simplificada.
    start_hhmm = start_dt.strftime("%H:%M")
    for slot in SLOT_TIMES:
        if slot["start"] == start_hhmm:
            # check if slot['end'] coincide con end_dt
            slot_end_str = slot["end"]
            proposed_end_str = end_dt.strftime("%H:%M")
            if slot_end_str == proposed_end_str:
                # está. Falta ver si de verdad está en free_slots_cache
                if start_hhmm in free_slots_cache[day_str]:
                    return True
    return False
