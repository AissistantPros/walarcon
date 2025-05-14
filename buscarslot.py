# buscarslot.py
import logging
from datetime import datetime, timedelta, time, date
import pytz
from fastapi import APIRouter, HTTPException

from utils import (
    initialize_google_calendar,
    get_cancun_time,
    cache_lock,
    availability_cache,
    convert_utc_to_cancun,
    GOOGLE_CALENDAR_ID,
)

logger = logging.getLogger(__name__)
router = APIRouter()

# Horarios de cita disponibles (start-end) en formato hh:mm
SLOT_TIMES = [
    {"start": "09:30", "end": "10:15"},
    {"start": "10:15", "end": "11:00"},
    {"start": "11:00", "end": "11:45"},
    {"start": "11:45", "end": "12:30"},
    {"start": "12:30", "end": "13:15"},
    {"start": "13:15", "end": "14:00"},
    {"start": "14:00", "end": "14:45"},
]

# Constantes de corte para “mañana” y “tarde”
MORNING_CUTOFF = datetime.strptime("12:30", "%H:%M").time()  # Hasta antes de 12:30 = mañana
# Después de 12:30, se considera tarde

free_slots_cache = {}
last_cache_update = None
CACHE_VALID_MINUTES = 15

def load_free_slots_to_cache(days_ahead=90):
    """
    Carga en caché los slots libres para los próximos 'days_ahead' días.
    """
    global free_slots_cache, last_cache_update

    with cache_lock:
        logger.info("⏳ Cargando slots libres desde Google Calendar...")
        free_slots_cache.clear()

        service = initialize_google_calendar()
        now = get_cancun_time()
        time_min = now.isoformat()
        time_max = (now + timedelta(days=days_ahead)).isoformat()

        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "timeZone": "America/Cancun",
            "items": [{"id": GOOGLE_CALENDAR_ID}],
        }

        calendar_id = GOOGLE_CALENDAR_ID
        events_result = service.freebusy().query(body=body).execute()
        busy_slots = events_result["calendars"][calendar_id]["busy"]

        busy_by_day = {}
        for b in busy_slots:
            start_local = convert_utc_to_cancun(b["start"])
            end_local = convert_utc_to_cancun(b["end"])
            day_key = start_local.strftime("%Y-%m-%d")
            busy_by_day.setdefault(day_key, []).append((start_local, end_local))

        # Construye la lista de slots libres para cada día
        for offset in range(days_ahead + 1):
            day_date = now + timedelta(days=offset)
            day_str = day_date.strftime("%Y-%m-%d")

            # Si es domingo (weekday=6), no hay citas
            if day_date.weekday() == 6:
                free_slots_cache[day_str] = []
                continue

            day_busy_list = busy_by_day.get(day_str, [])
            free_slots = build_free_slots_for_day(day_date, day_busy_list)
            free_slots_cache[day_str] = free_slots

        last_cache_update = get_cancun_time()
        logger.info(f"✅ Slots libres precargados para los próximos {days_ahead} días.")










def build_free_slots_for_day(day_dt, busy_list):
    """
    Para un día 'day_dt', construye la lista de horas de inicio disponibles.
    """
    day_str = day_dt.strftime("%Y-%m-%d")
    free_list = []

    for slot in SLOT_TIMES:
        start_str = f"{day_str} {slot['start']}:00"
        end_str = f"{day_str} {slot['end']}:00"

        tz = pytz.timezone("America/Cancun")
        start_dt = tz.localize(datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S"))
        end_dt = tz.localize(datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S"))

        # Comprobamos que NO se solape con ninguno de los busy
        if all(
            not (start_dt < (b_end - timedelta(seconds=1)) and end_dt > b_start)
            for b_start, b_end in busy_list
        ):
            free_list.append(slot["start"])

    # Ordenamos de menor a mayor hora
    return sorted(free_list, key=lambda x: datetime.strptime(x, "%H:%M"))









def ensure_cache_is_fresh():
    """
    Si la caché no se ha actualizado en los últimos 15 minutos, la recarga.
    """
    global last_cache_update
    now = get_cancun_time()

    if not last_cache_update or (now - last_cache_update).total_seconds() / 60.0 > CACHE_VALID_MINUTES:
        load_free_slots_to_cache(days_ahead=90)








def adjust_to_valid_slot(requested_time, slot_times):
    """
    Dado 'requested_time' (ej. '12:30'), encuentra en SLOT_TIMES el primer slot >= esa hora.
    """
    req_time_obj = datetime.strptime(requested_time, "%H:%M").time()
    for s in slot_times:
        slot_start_obj = datetime.strptime(s["start"], "%H:%M").time()
        if slot_start_obj >= req_time_obj:
            return s["start"]
    return slot_times[-1]["start"]











def find_next_available_slot(
    target_date: str = None,
    target_hour: str = None, # Usado como preferencia suave si no hay filtros estrictos
    urgent: bool = False,
    search_range_end_date: str = None, # Para "esta semana", formato 'YYYY-MM-DD'
    time_of_day_strict: str = None, # NUEVO: "mañana" o "tarde"
    specific_time_strict: str = None # NUEVO: "HH:MM"
):
    """
    Busca el próximo horario disponible aplicando filtros estrictos si se proporcionan.
    """
    try:
        logger.info(
            f"📥 find_next_available_slot INICIADO con: target_date={target_date}, target_hour={target_hour}, "
            f"urgent={urgent}, search_range_end_date={search_range_end_date}, "
            f"time_of_day_strict='{time_of_day_strict}', specific_time_strict='{specific_time_strict}'"
        )

        ensure_cache_is_fresh() # ¡MUY IMPORTANTE!
        now_cancun = get_cancun_time()
        cancun_tz = pytz.timezone("America/Cancun")

        # 1) Determinar punto de arranque para la fecha (search_start_date_obj)
        search_start_date_obj = None
        if target_date:
            try:
                parsed_date = datetime.strptime(target_date, "%Y-%m-%d").date()
                # Si la fecha objetivo es hoy o futura, usarla. Si es pasada, empezar desde hoy.
                search_start_date_obj = max(parsed_date, now_cancun.date())
            except ValueError:
                logger.error(f"Formato de target_date inválido: {target_date}. Se buscará desde hoy.")
                search_start_date_obj = now_cancun.date()
        else: # Si no hay target_date (puede ser urgent, o solo preferencia de mañana/tarde sin día)
            search_start_date_obj = now_cancun.date()

        # Determinar la fecha final de la búsqueda si se especifica search_range_end_date
        search_end_limit_obj = None
        if search_range_end_date:
            try:
                search_end_limit_obj = datetime.strptime(search_range_end_date, "%Y-%m-%d").date()
                if search_end_limit_obj < search_start_date_obj:
                    logger.warning(f"search_range_end_date {search_range_end_date} es anterior a la fecha de inicio. Se ignorará.")
                    search_end_limit_obj = None
            except ValueError:
                logger.error(f"Formato de search_range_end_date inválido: {search_range_end_date}. Se ignorará.")
        
        # Convertir specific_time_strict a objeto time para comparaciones
        specific_time_obj_strict_filter = None
        if specific_time_strict:
            try:
                specific_time_obj_strict_filter = datetime.strptime(specific_time_strict, "%H:%M").time()
            except ValueError:
                logger.warning(f"Formato incorrecto para specific_time_strict: '{specific_time_strict}'. Se ignorará el filtro de hora específica.")

        # Lógica de tu sección 2 original (chequeo rápido para target_date y target_hour tipo mañana/tarde)
        # Esto puede ser un chequeo rápido antes del bucle principal si target_date y target_hour (interpretado como mañana/tarde) se dan.
        # Nota: los nuevos filtros time_of_day_strict y specific_time_strict son más generales.
        if target_date and not specific_time_strict and not time_of_day_strict and target_hour in ("09:30", "12:30"):
            day_str_check = search_start_date_obj.strftime("%Y-%m-%d")
            if day_str_check not in free_slots_cache or not free_slots_cache.get(day_str_check):
                logger.info(f"No hay slots pre-calculados en caché para {day_str_check} o está vacío.")
                if target_hour == "09:30": # Representa "mañana"
                    return {"error": "NO_MORNING_AVAILABLE", "date": day_str_check, "detail": f"No se encontraron slots por la mañana para {day_str_check} (caché vacía)."}
                else: # Representa "tarde"
                    return {"error": "NO_TARDE_AVAILABLE", "date": day_str_check, "detail": f"No se encontraron slots por la tarde para {day_str_check} (caché vacía)."}

            available_slots_for_day = sorted(free_slots_cache[day_str_check], key=lambda x: datetime.strptime(x, "%H:%M").time())
            
            temp_time_of_day_strict = "mañana" if target_hour == "09:30" else "tarde"
            filtered_slots_for_quick_check = []

            for slot_start_hour_str in available_slots_for_day:
                slot_time_obj = datetime.strptime(slot_start_hour_str, "%H:%M").time()
                passes_quick_filter = False
                if temp_time_of_day_strict == "mañana":
                    if slot_time_obj < MORNING_CUTOFF: # MORNING_CUTOFF es 12:30
                        passes_quick_filter = True
                elif temp_time_of_day_strict == "tarde":
                    if slot_time_obj >= MORNING_CUTOFF: # MORNING_CUTOFF es 12:30
                        passes_quick_filter = True
                
                if passes_quick_filter:
                     # Verificar que el slot no haya pasado hoy
                    slot_dt_check = cancun_tz.localize(datetime.combine(search_start_date_obj, slot_time_obj))
                    if urgent and slot_dt_check < now_cancun + timedelta(hours=4):
                        continue
                    if slot_dt_check < now_cancun:
                        continue
                    filtered_slots_for_quick_check.append(slot_start_hour_str)
            
            if not filtered_slots_for_quick_check:
                logger.info(f"Quick check: No slots en la franja '{temp_time_of_day_strict}' para {day_str_check}.")
                if temp_time_of_day_strict == "mañana":
                    return {"error": "NO_MORNING_AVAILABLE", "date": day_str_check, "detail": f"No se encontraron slots por la mañana para {day_str_check}."}
                else:
                    return {"error": "NO_TARDE_AVAILABLE", "date": day_str_check, "detail": f"No se encontraron slots por la tarde para {day_str_check}."}
            # Si encontramos slots en este chequeo rápido, la lógica principal de abajo los considerará.

        # 3) Lógica normal de búsqueda (adaptada de tu original)
        # valid_target_hour ya no se usa tanto con los filtros estrictos, pero puede servir para ordenar
        # Mantengo tu adjust_to_valid_slot si lo usas para ordenar o como fallback.
        soft_preference_hour_str = adjust_to_valid_slot(target_hour, SLOT_TIMES) if target_hour and not (specific_time_strict or time_of_day_strict) else None
        soft_preference_time_obj = datetime.strptime(soft_preference_hour_str, "%H:%M").time() if soft_preference_hour_str else None


        for offset in range(180): # Buscar hasta 180 días en el futuro
            current_search_day = search_start_date_obj + timedelta(days=offset)

            if search_end_limit_obj and current_search_day > search_end_limit_obj:
                logger.info(f"Se alcanzó el límite de búsqueda (search_range_end_date): {search_end_limit_obj.strftime('%Y-%m-%d')}")
                break

            if current_search_day.weekday() == 6:  # 6 es Domingo
                continue

            day_str_key = current_search_day.strftime("%Y-%m-%d")
            if day_str_key not in free_slots_cache or not free_slots_cache.get(day_str_key):
                # logger.debug(f"No hay slots pre-calculados en caché para {day_str_key} o está vacío.")
                continue # No hay slots libres definidos para este día en la caché

            # Obtener los slots libres ya filtrados por ocupación de Google Calendar
            # Estos son strings "HH:MM"
            day_available_start_times = sorted(
                free_slots_cache[day_str_key],
                key=lambda x: datetime.strptime(x, "%H:%M").time()
            )

            slots_meeting_criteria_for_day = []

            for slot_start_hour_str in day_available_start_times:
                try:
                    slot_start_time_obj = datetime.strptime(slot_start_hour_str, "%H:%M").time()
                except ValueError:
                    logger.warning(f"Formato de hora inválido en free_slots_cache para {day_str_key}: {slot_start_hour_str}")
                    continue
                
                current_slot_start_dt_local = cancun_tz.localize(datetime.combine(current_search_day, slot_start_time_obj))

                # Omitir slots pasados (redundante si search_start_date_obj ya es >= hoy, pero seguro)
                if current_slot_start_dt_local < now_cancun:
                    continue
                
                # Omitir si es urgent y está muy próximo
                if urgent and current_slot_start_dt_local < now_cancun + timedelta(hours=4):
                    continue

                # ---- INICIO DE LA NUEVA LÓGICA DE FILTRADO ESTRICTO ----
                passes_strict_filter = True
                if specific_time_obj_strict_filter: # Prioridad 1: Hora exacta
                    if current_slot_start_dt_local.time() != specific_time_obj_strict_filter:
                        passes_strict_filter = False
                
                elif time_of_day_strict: # Prioridad 2: Mañana o Tarde
                    slot_hour_val = current_slot_start_dt_local.hour
                    if time_of_day_strict == "mañana": # Antes de las 12:00 PM
                        if slot_hour_val >= 12:
                            passes_strict_filter = False
                    elif time_of_day_strict == "tarde": # Desde las 12:00 PM en adelante
                        if slot_hour_val < 12:
                            passes_strict_filter = False
                
                if not passes_strict_filter:
                    continue # No pasó el filtro estricto, al siguiente slot del día
                # ---- FIN DE LA NUEVA LÓGICA DE FILTRADO ESTRICTO ----

                # Si llegó aquí, el slot pasó los filtros estrictos y está en free_slots_cache (o sea, está libre)
                slots_meeting_criteria_for_day.append(slot_start_hour_str)

            # Si encontramos slots que cumplen los criterios estrictos para este día
            if slots_meeting_criteria_for_day:
                chosen_start_hour_str = None
                # Si había una preferencia de target_hour (y no filtros estrictos que ya la definieran),
                # intentamos escoger el más cercano a esa hora.
                if soft_preference_time_obj and not specific_time_strict and not time_of_day_strict:
                    # Ordenar por proximidad a la soft_preference_time_obj
                    slots_meeting_criteria_for_day.sort(
                        key=lambda s_h_str: abs(
                            datetime.combine(date.min, datetime.strptime(s_h_str, "%H:%M").time()) -
                            datetime.combine(date.min, soft_preference_time_obj)
                        )
                    )
                    chosen_start_hour_str = slots_meeting_criteria_for_day[0]
                else:
                    # Si hay filtros estrictos, o no hay soft_preference_hour, tomar el primero del día que cumpla
                    chosen_start_hour_str = slots_meeting_criteria_for_day[0]

                # Construir los datetimes finales
                final_start_time_obj = datetime.strptime(chosen_start_hour_str, "%H:%M").time()
                final_start_dt_local = cancun_tz.localize(datetime.combine(current_search_day, final_start_time_obj))
                
                # Encontrar la hora de fin del slot desde SLOT_TIMES
                end_hour_str = next((s_def["end"] for s_def in SLOT_TIMES if s_def["start"] == chosen_start_hour_str), None)
                if not end_hour_str:
                    logger.error(f"No se encontró hora de fin para el slot {chosen_start_hour_str} en SLOT_TIMES.")
                    # Fallback a 45 minutos si no se encuentra, o puedes retornar error
                    final_end_dt_local = final_start_dt_local + timedelta(minutes=45) 
                else:
                    final_end_time_obj = datetime.strptime(end_hour_str, "%H:%M").time()
                    final_end_dt_local = cancun_tz.localize(datetime.combine(current_search_day, final_end_time_obj))

                logger.info(f"✅ Slot encontrado y filtrado: {final_start_dt_local.isoformat()}")
                return {
                    "start_time": final_start_dt_local.isoformat(),
                    "end_time": final_end_dt_local.isoformat(),
                    "error": None
                }
        
        # Si el bucle de días termina sin encontrar nada
        error_message_detail = "No se encontraron horarios disponibles"
        if specific_time_strict:
            error_message_detail += f" exactamente a las {specific_time_strict}"
        elif time_of_day_strict:
            error_message_detail += f" por la {time_of_day_strict}"
        if search_range_end_date:
            error_message_detail += f" hasta el {search_range_end_date}"
        if target_date: # Si se buscaba en una fecha específica con filtros
            error_message_detail += f" para el {datetime.strptime(target_date,'%Y-%m-%d').strftime('%d de %B de %Y')}"
        error_message_detail += "."
        
        logger.info(f"🚫 {error_message_detail}")

        # Devolver un error específico si no se encontró nada en la franja solicitada para un target_date
        # Estos errores NO_MORNING_AVAILABLE y NO_TARDE_AVAILABLE son los que tu prompt espera.
        if target_date: # Solo si se especificó una fecha de inicio para la búsqueda con estos filtros
            if time_of_day_strict == "mañana":
                return {"error": "NO_MORNING_AVAILABLE", "date": target_date, "detail": error_message_detail}
            if time_of_day_strict == "tarde":
                return {"error": "NO_TARDE_AVAILABLE", "date": target_date, "detail": error_message_detail}
            if specific_time_strict: # Si se pidió hora específica y no se encontró en target_date
                 return {"error": "NO_SPECIFIC_TIME_AVAILABLE", "date": target_date, "time": specific_time_strict, "detail": error_message_detail}


        return {"error": "NO_SLOTS_FOUND_WITH_CRITERIA", "detail": error_message_detail}

    except Exception as e:
        logger.error(f"❌ Error inesperado en find_next_available_slot: {str(e)}", exc_info=True)
        # Devolver un error genérico si falla algo internamente
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE", "detail": "Error interno del servidor al buscar horario."}

