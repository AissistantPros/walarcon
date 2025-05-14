# buscarslot.py
import logging
from datetime import datetime, timedelta, time as dt_time, date
import pytz
import re # Asegúrate de importar re
from typing import Dict, Optional, Tuple, Union # Para type hints

# Tus otras importaciones de utils y demás (asegúrate que estén correctas)
from utils import (
    initialize_google_calendar,
    get_cancun_time,
    cache_lock,
    convert_utc_to_cancun,
    GOOGLE_CALENDAR_ID,
    # Las siguientes pueden venir de utils o estar definidas aquí si son específicas
    # format_date_nicely, # Asegúrate que esta función exista y esté importada
    # _adjust_time_to_next_valid_slot # Asegúrate que esta función exista y esté importada
)

logger = logging.getLogger(__name__)
# router = APIRouter() # Comentado si no se usa directamente aquí para un endpoint FastAPI

# --- Constantes y Mapeos (Movidos aquí o importados si son de utils) ---
DAYS_EN_TO_ES = {
    "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
    "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sábado", "Sunday": "Domingo",
}
MONTHS_EN_TO_ES = {
    "January": "Enero", "February": "Febrero", "March": "Marzo", "April": "Abril",
    "May": "Mayo", "June": "Junio", "July": "Julio", "August": "Agosto",
    "September": "Septiembre", "October": "Octubre", "November": "Noviembre", "December": "Diciembre",
}
MESES_ES_A_NUM = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12
}
WEEKDAYS_ES_TO_NUM = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6
}

VALID_SLOT_START_TIMES = [
    "09:30", "10:15", "11:00", "11:45",
    "12:30", "13:15", "14:00"
] # HH:MM

# Horarios de cita disponibles (start-end) en formato hh:mm
# Usaremos VALID_SLOT_START_TIMES para la lógica de ajuste,
# SLOT_TIMES para encontrar la duración/hora de fin.
SLOT_TIMES = [
    {"start": "09:30", "end": "10:15"},
    {"start": "10:15", "end": "11:00"},
    {"start": "11:00", "end": "11:45"},
    {"start": "11:45", "end": "12:30"},
    {"start": "12:30", "end": "13:15"},
    {"start": "13:15", "end": "14:00"},
    {"start": "14:00", "end": "14:45"},
]

MIN_ADVANCE_BOOKING_HOURS = 6 # Antelación mínima en horas
MORNING_CUTOFF_TIME_OBJ = dt_time(12, 0) # Slots ANTES de esta hora son "mañana"

# Caché de slots
free_slots_cache: Dict[str, list] = {}
last_cache_update: Optional[datetime] = None
CACHE_VALID_MINUTES = 15

# --- Funciones Auxiliares (Asegúrate que estén definidas o importadas) ---

def format_date_nicely(target_date_obj: date, time_keyword: Optional[str] = None, weekday_override: Optional[str] = None, specific_time_hhmm: Optional[str] = None) -> str:
    """
    Formatea una fecha a 'DíaDeLaSemana DD de Mes de AAAA' en español.
    Puede incluir la parte del día y un override para el día de la semana si hay conflicto.
    Si specific_time_hhmm se provee, lo añade.
    """
    try:
        day_name_en = target_date_obj.strftime("%A")
        day_name_str = DAYS_EN_TO_ES.get(day_name_en, day_name_en.capitalize())

        if weekday_override:
            day_name_str = weekday_override.capitalize()

        month_name_en = target_date_obj.strftime("%B")
        month_name_str = MONTHS_EN_TO_ES.get(month_name_en, month_name_en.capitalize())

        formatted = f"{day_name_str} {target_date_obj.day} de {month_name_str} de {target_date_obj.year}"

        if specific_time_hhmm:
            try:
                time_obj_for_format = datetime.strptime(specific_time_hhmm, "%H:%M").time()
                # Formato AM/PM, ej. "a las 09:30 a.m." o "a las 02:00 p.m."
                # Para español, es más común "a las 9:30" o "a las 14:00".
                # Vamos a usar un formato más directo HH:MM y luego la IA lo puede suavizar.
                # O podemos usar AM/PM directamente aquí si el prompt lo prefiere.
                # Ejemplo con AM/PM:
                formatted_time_ampm = time_obj_for_format.strftime("%I:%M %p").lower()
                if formatted_time_ampm.startswith("0"): # Quitar cero inicial para ej. "09:30 AM" -> "9:30 AM"
                    formatted_time_ampm = formatted_time_ampm[1:]
                formatted += f" a las {formatted_time_ampm}"
            except ValueError:
                logger.warning(f"Error formateando la hora específica {specific_time_hhmm}")
                formatted += f" a las {specific_time_hhmm}" # Fallback
        elif time_keyword == "mañana":
            formatted += ", por la mañana"
        elif time_keyword == "tarde":
            formatted += ", por la tarde"

        return formatted
    except Exception as e:
        logger.error(f"Error formateando fecha {target_date_obj}: {e}")
        return target_date_obj.strftime('%Y-%m-%d') # Fallback

def _adjust_time_to_next_valid_slot(requested_time_obj: dt_time, valid_starts: list) -> Optional[str]:
    """
    Ajusta una hora solicitada al inicio del próximo slot válido.
    valid_starts: lista de strings "HH:MM" de inicios de slot válidos.
    Retorna "HH:MM" o None si no hay ajuste posible.
    """
    valid_start_time_objs = []
    for vt_str in valid_starts:
        try:
            valid_start_time_objs.append(datetime.strptime(vt_str, "%H:%M").time())
        except ValueError:
            logger.warning(f"Hora de slot inválida en VALID_SLOT_START_TIMES: {vt_str}")
    valid_start_time_objs.sort()

    for valid_start_time_obj_item in valid_start_time_objs:
        if requested_time_obj <= valid_start_time_obj_item:
            return valid_start_time_obj_item.strftime("%H:%M")
    return None


# --- Lógica de Caché de Slots (como la tenías, adaptada) ---
def load_free_slots_to_cache(days_ahead=90):
    global free_slots_cache, last_cache_update
    with cache_lock:
        logger.info("⏳ Cargando slots libres desde Google Calendar...")
        free_slots_cache.clear()
        service = initialize_google_calendar() # Asume que esta función existe y está importada
        now_cancun_dt = get_cancun_time() # Asume que esta función existe
        time_min_iso = now_cancun_dt.isoformat()
        time_max_iso = (now_cancun_dt + timedelta(days=days_ahead)).isoformat()

        body = {
            "timeMin": time_min_iso,
            "timeMax": time_max_iso,
            "timeZone": "America/Cancun",
            "items": [{"id": GOOGLE_CALENDAR_ID}], # Asume GOOGLE_CALENDAR_ID importado/definido
        }
        try:
            events_result = service.freebusy().query(body=body).execute()
            # GOOGLE_CALENDAR_ID debe ser el ID correcto de tu calendario principal
            busy_slots_list = events_result["calendars"][GOOGLE_CALENDAR_ID]["busy"]
        except Exception as e_cal:
            logger.error(f"Error obteniendo free/busy de Google Calendar: {e_cal}")
            # Podríamos reintentar o simplemente dejar la caché vacía y que falle la búsqueda de slots.
            # Por ahora, si falla, la caché quedará vacía y las búsquedas no encontrarán nada.
            last_cache_update = get_cancun_time() # Actualizar timestamp incluso si falla para no reintentar inmediatamente
            return


        busy_by_day_dict: Dict[str, list] = {}
        for b_slot in busy_slots_list:
            # convert_utc_to_cancun debe existir y estar importada/definida
            start_local_dt = convert_utc_to_cancun(b_slot["start"])
            end_local_dt = convert_utc_to_cancun(b_slot["end"])
            day_key_str = start_local_dt.strftime("%Y-%m-%d")
            busy_by_day_dict.setdefault(day_key_str, []).append((start_local_dt, end_local_dt))

        for offset in range(days_ahead + 1):
            current_day_date_obj = now_cancun_dt.date() + timedelta(days=offset)
            current_day_str_key = current_day_date_obj.strftime("%Y-%m-%d")

            if current_day_date_obj.weekday() == 6:  # Domingo
                free_slots_cache[current_day_str_key] = []
                continue

            day_busy_intervals = busy_by_day_dict.get(current_day_str_key, [])
            free_slots_for_day = build_free_slots_for_day(current_day_date_obj, day_busy_intervals)
            free_slots_cache[current_day_str_key] = free_slots_for_day
        
        last_cache_update = get_cancun_time()
        logger.info(f"✅ Slots libres precargados para los próximos {days_ahead} días.")

def build_free_slots_for_day(day_date_obj: date, busy_intervals_list: list) -> list:
    day_str = day_date_obj.strftime("%Y-%m-%d")
    free_list_for_day = []
    cancun_tz = pytz.timezone("America/Cancun")

    for slot_def in SLOT_TIMES: # SLOT_TIMES tiene start y end "HH:MM"
        slot_start_str = f"{day_str} {slot_def['start']}:00"
        slot_end_str = f"{day_str} {slot_def['end']}:00"
        
        try:
            slot_start_dt_local = cancun_tz.localize(datetime.strptime(slot_start_str, "%Y-%m-%d %H:%M:%S"))
            slot_end_dt_local = cancun_tz.localize(datetime.strptime(slot_end_str, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            logger.error(f"Error parseando slot time {slot_def} para {day_str}")
            continue

        is_overlapping = False
        for busy_start_dt, busy_end_dt in busy_intervals_list:
            # Chequeo de solapamiento:
            # (SlotStart < BusyEnd) and (SlotEnd > BusyStart)
            if slot_start_dt_local < (busy_end_dt - timedelta(seconds=1)) and slot_end_dt_local > busy_start_dt:
                is_overlapping = True
                break
        
        if not is_overlapping:
            free_list_for_day.append(slot_def["start"]) # Guardar solo la hora de inicio "HH:MM"

    return sorted(free_list_for_day, key=lambda x_time: datetime.strptime(x_time, "%H:%M").time())

def ensure_cache_is_fresh():
    global last_cache_update
    now_cancun_dt = get_cancun_time()
    if not last_cache_update or (now_cancun_dt - last_cache_update).total_seconds() / 60 > CACHE_VALID_MINUTES:
        load_free_slots_to_cache()


# --- "Súper Herramienta" ---
def process_appointment_request(
    user_query_for_date_time: str,
    day_param: Optional[int] = None,
    month_param: Optional[Union[str, int]] = None,
    year_param: Optional[int] = None,
    fixed_weekday_param: Optional[str] = None,
    explicit_time_preference_param: Optional[str] = None, # "mañana" o "tarde"
    is_urgent_param: bool = False
) -> Dict:
    """
    Procesa la solicitud de cita, interpretando fecha/hora y buscando disponibilidad.
    """
    logger.info(
        f"⚙️ process_appointment_request INICIADO con: query='{user_query_for_date_time}', "
        f"day={day_param}, month='{month_param}', year={year_param}, weekday='{fixed_weekday_param}', "
        f"time_pref='{explicit_time_preference_param}', urgent={is_urgent_param}"
    )

    # Variables para la salida de Fase 1 / entrada de Fase 2
    interpreted_date_obj: Optional[date] = None
    filter_time_of_day_strict: Optional[str] = None  # "mañana" o "tarde"
    filter_specific_time_strict: Optional[str] = None # "HH:MM" ajustado a slot
    filter_search_range_end_date_str: Optional[str] = None # 'YYYY-MM-DD'
    
    # --- FASE 1: Interpretación de Fecha/Hora ---
    # (Adaptación de la lógica de la antigua calculate_structured_date)
    
    now_cancun_dt = get_cancun_time()
    today_date_obj = now_cancun_dt.date()
    
    # Normalizar inputs de texto
    user_query_norm = (user_query_for_date_time or "").lower().strip()
    fixed_weekday_norm = (fixed_weekday_param or "").lower().strip()
    time_pref_norm = (explicit_time_preference_param or "").lower().strip()

    # 1.A. Extracción y Ajuste de Hora Específica del user_query_for_date_time
    extracted_raw_time_hhmm: Optional[str] = None
    time_match = re.search(r"(?:a la(?:s)?\s*)?(\b\d{1,2}(?::\d{2})?\b)\s*(am|pm|hrs?\.?)?", user_query_norm, re.IGNORECASE)
    if time_match:
        hour_str_match = time_match.group(1)
        am_pm_modifier_match = (time_match.group(2) or "").lower()
        h_parts_match = hour_str_match.split(':')
        try:
            h_val = int(h_parts_match[0])
            m_val = int(h_parts_match[1]) if len(h_parts_match) > 1 else 0

            if "pm" in am_pm_modifier_match and h_val != 12: h_val += 12
            elif "am" in am_pm_modifier_match and h_val == 12: h_val = 0

            if 0 <= h_val <= 23 and 0 <= m_val <= 59:
                extracted_raw_time_hhmm = f"{h_val:02d}:{m_val:02d}"
                logger.info(f"Hora cruda extraída de query: {extracted_raw_time_hhmm}")
                
                # Ajustar la hora extraída al slot válido
                requested_time_obj_for_adjust = datetime.strptime(extracted_raw_time_hhmm, "%H:%M").time()
                adjusted_hhmm_slot = _adjust_time_to_next_valid_slot(requested_time_obj_for_adjust, VALID_SLOT_START_TIMES)
                
                if adjusted_hhmm_slot:
                    filter_specific_time_strict = adjusted_hhmm_slot
                    logger.info(f"Hora extraída '{extracted_raw_time_hhmm}' ajustada a slot válido: '{filter_specific_time_strict}'")
                    # Inferir "mañana" o "tarde" del slot ajustado si no se proveyó explícitamente
                    if not time_pref_norm:
                        adjusted_h_val = int(filter_specific_time_strict.split(':')[0])
                        time_pref_norm = "tarde" if adjusted_h_val >= MORNING_CUTOFF_TIME_OBJ.hour else "mañana"
                else:
                    msg = (f"Lo siento, la hora {extracted_raw_time_hhmm} no corresponde a un horario de atención válido "
                           f"o es demasiado tarde en el día. Los horarios de inicio de cita son: {', '.join(VALID_SLOT_START_TIMES)}.")
                    logger.warning(f"Fase 1: {msg}")
                    return {"status": "INVALID_TIME_REQUESTED", "message_to_user": msg, "slot_details": None}
            else: # Hora inválida como 25:00
                 extracted_raw_time_hhmm = None # Resetear si el parseo h,m fue inválido
        except ValueError: # Fallo en int(h_parts[0]) por ejemplo
            logger.warning(f"No se pudo parsear la hora extraída de query: {hour_str_match}")
            extracted_raw_time_hhmm = None

    # 1.B. Inferir "mañana"/"tarde" general si no se extrajo hora específica ni se proveyó explicit_time_preference_param
    if not filter_specific_time_strict and not time_pref_norm:
        if "mañana" in user_query_norm and "pasado mañana" not in user_query_norm: # Evitar "pasado mañana"
            time_pref_norm = "mañana"
        elif "tarde" in user_query_norm:
            time_pref_norm = "tarde"
    
    # Asignar a filter_time_of_day_strict si corresponde (y no hay hora específica)
    if time_pref_norm and not filter_specific_time_strict:
        filter_time_of_day_strict = time_pref_norm

    # 1.C. Procesar Fecha D/M/Y (si se proveen day_param, month_param)
    current_year = today_date_obj.year
    temp_calculated_date_obj: Optional[date] = None

    if day_param is not None and month_param is not None:
        year_to_use = year_param or current_year
        month_num_to_use: Optional[int] = None
        
        if isinstance(month_param, int):
            if 1 <= month_param <= 12: month_num_to_use = month_param
        elif isinstance(month_param, str):
            if month_param.isdigit() and 1 <= int(month_param) <= 12:
                month_num_to_use = int(month_param)
            elif month_param.lower() in MESES_ES_A_NUM:
                month_num_to_use = MESES_ES_A_NUM[month_param.lower()]
        
        if month_num_to_use:
            try:
                prospective_date = date(year_to_use, month_num_to_use, day_param)
                # Si la fecha es pasada Y el año no fue fijado por el usuario, intentar con el año siguiente
                if prospective_date < today_date_obj and year_param is None:
                    logger.info(f"Fecha D/M/Y ({day_param}/{month_num_to_use}/{year_to_use}) es pasada, intentando año siguiente.")
                    prospective_date = date(year_to_use + 1, month_num_to_use, day_param)
                temp_calculated_date_obj = prospective_date
                
                # Conflicto con fixed_weekday_param
                if fixed_weekday_norm and fixed_weekday_norm in WEEKDAYS_ES_TO_NUM:
                    target_weekday_num = WEEKDAYS_ES_TO_NUM[fixed_weekday_norm]
                    actual_weekday_num = temp_calculated_date_obj.weekday()
                    if actual_weekday_num != target_weekday_num:
                        actual_day_name_nice = format_date_nicely(temp_calculated_date_obj).split(' ')[0] # "Lunes"
                        month_name_nice = format_date_nicely(temp_calculated_date_obj).split(' de ')[1].capitalize() # "Mayo"
                        
                        conf_msg = (f"Mencionó {fixed_weekday_norm.capitalize()}, pero el {temp_calculated_date_obj.day} de {month_name_nice} "
                                    f"de {temp_calculated_date_obj.year} es {actual_day_name_nice}. ¿Podría aclarar la fecha que desea, por favor?")
                        logger.warning(f"Fase 1: Conflicto de día de semana. {conf_msg}")
                        return {"status": "NEEDS_CLARIFICATION", "message_to_user": conf_msg, "clarification_type": "weekday_conflict", "slot_details": None}
            except ValueError: # Ej. 30 de febrero
                err_msg = f"La fecha que indicó ({day_param} de {month_param} de {year_to_use}) no parece ser válida."
                logger.warning(f"Fase 1: {err_msg}")
                return {"status": "DATE_PARSE_ERROR", "message_to_user": err_msg, "slot_details": None}
        else:
            err_msg = f"El mes '{month_param}' no es válido."
            logger.warning(f"Fase 1: {err_msg}")
            return {"status": "DATE_PARSE_ERROR", "message_to_user": err_msg, "slot_details": None}

    # 1.D. Procesar Frases Relativas (si no se construyó fecha por D/M/Y)
    if temp_calculated_date_obj is None:
        query_for_relative = user_query_norm # Usar la normalizada
        # Simplificaciones para match
        if "hoy en 8" in query_for_relative or "de hoy en 8" in query_for_relative or "en 8 dias" in query_for_relative or "en ocho dias" in query_for_relative:
            query_for_relative = "hoy_mas_7_dias"
        elif "en 15 dias" in query_for_relative or "en quince dias" in query_for_relative:
            query_for_relative = "hoy_mas_14_dias"
        
        if "hoy" == query_for_relative: temp_calculated_date_obj = today_date_obj
        elif "mañana" == query_for_relative and "pasado mañana" not in user_query_norm : temp_calculated_date_obj = today_date_obj + timedelta(days=1)
        elif "pasado mañana" == query_for_relative: temp_calculated_date_obj = today_date_obj + timedelta(days=2)
        elif "hoy_mas_7_dias" == query_for_relative: temp_calculated_date_obj = today_date_obj + timedelta(days=7)
        elif "hoy_mas_14_dias" == query_for_relative: temp_calculated_date_obj = today_date_obj + timedelta(days=14)
        elif "esta semana" in query_for_relative:
            temp_calculated_date_obj = today_date_obj # Búsqueda empieza hoy
            # Sábado es weekday 5 (Lunes=0)
            days_until_saturday = (5 - today_date_obj.weekday() + 7) % 7
            filter_search_range_end_date_str = (today_date_obj + timedelta(days=days_until_saturday)).strftime('%Y-%m-%d')
        elif "próxima semana" in query_for_relative or "siguiente semana" in query_for_relative:
            days_until_next_monday = (0 - today_date_obj.weekday() + 7) % 7
            if days_until_next_monday == 0: days_until_next_monday = 7 # Si hoy es Lunes, el próximo Lunes
            temp_calculated_date_obj = today_date_obj + timedelta(days=days_until_next_monday)
        
        # Procesar "en X meses"
        month_match = re.search(r"en\s+(\d+|un|dos|tres)\s+mes(es)?", query_for_relative)
        if month_match and not temp_calculated_date_obj: # Solo si no se ha calculado otra cosa
            num_months_str = month_match.group(1)
            num_months = 0
            if num_months_str.isdigit(): num_months = int(num_months_str)
            elif num_months_str == "un": num_months = 1
            elif num_months_str == "dos": num_months = 2
            elif num_months_str == "tres": num_months = 3
            
            if num_months > 0:
                # Aproximación simple: N * 30 días. Podría ser más preciso con dateutil.relativedelta
                temp_calculated_date_obj = today_date_obj + timedelta(days=num_months * 30)

        # Si solo se da un día numérico (ej. "el 16") y no se usó day_param antes
        # day_param se usa para D/M/Y. Aquí es si SOLO se dice "el 16".
        day_num_match = re.search(r"\b(el|día)\s+(\d{1,2})\b", query_for_relative)
        if day_num_match and not temp_calculated_date_obj and not day_param:
            day_val_from_query = int(day_num_match.group(2))
            try:
                month_to_try = today_date_obj.month
                year_to_try = today_date_obj.year
                if day_val_from_query < today_date_obj.day: # Si el día ya pasó este mes
                    month_to_try += 1
                    if month_to_try > 12:
                        month_to_try = 1
                        year_to_try += 1
                temp_calculated_date_obj = date(year_to_try, month_to_try, day_val_from_query)
            except ValueError:
                err_msg = f"El día {day_val_from_query} no es válido para el mes que calculé. ¿Podría ser más específico?"
                logger.warning(f"Fase 1: {err_msg}")
                return {"status": "DATE_PARSE_ERROR", "message_to_user": err_msg, "slot_details": None}

        # Ajustar fecha calculada si se provee fixed_weekday_param y se calculó una fecha base
        if temp_calculated_date_obj and fixed_weekday_norm and fixed_weekday_norm in WEEKDAYS_ES_TO_NUM:
            target_weekday_num = WEEKDAYS_ES_TO_NUM[fixed_weekday_norm]
            current_weekday_num = temp_calculated_date_obj.weekday()
            days_to_advance = (target_weekday_num - current_weekday_num + 7) % 7
            # Si ya es ese día de la semana Y NO es un cálculo de "próxima semana Lunes", avanzar una semana.
            # Esto es para casos como "Lunes" (se asume próximo Lunes) vs "Próxima semana Lunes".
            if days_to_advance == 0 and not ("próxima semana" in query_for_relative or "siguiente semana" in query_for_relative):
                 if temp_calculated_date_obj.weekday() == WEEKDAYS_ES_TO_NUM[fixed_weekday_norm] and temp_calculated_date_obj <= today_date_obj : # si es hoy y es el dia de la semana, o es un dia pasado que cae en ese dia de la semana
                    days_to_advance = 7


            temp_calculated_date_obj += timedelta(days=days_to_advance)
    
    # 1.E. Validación Final de Fecha Pasada
    if temp_calculated_date_obj and temp_calculated_date_obj < today_date_obj:
        err_msg = f"La fecha {format_date_nicely(temp_calculated_date_obj)} es en el pasado. Por favor, elija una fecha futura."
        logger.warning(f"Fase 1: {err_msg}")
        return {"status": "DATE_PARSE_ERROR", "message_to_user": err_msg, "slot_details": None}

    # Si después de todo, no hay fecha calculada, es un error.
    if not temp_calculated_date_obj:
        err_msg = (f"No pude entender la fecha de su solicitud ('{user_query_for_date_time}'). "
                   "Intente con 'hoy', 'mañana', 'próximo lunes', o una fecha como 'el 15 de mayo'.")
        logger.warning(f"Fase 1: {err_msg}")
        return {"status": "DATE_PARSE_ERROR", "message_to_user": err_msg, "slot_details": None}

    interpreted_date_obj = temp_calculated_date_obj
    
    logger.info(f"✓ Fase 1 completada: interpreted_date_obj={interpreted_date_obj}, "
                f"filter_time_of_day_strict='{filter_time_of_day_strict}', "
                f"filter_specific_time_strict='{filter_specific_time_strict}', "
                f"filter_search_range_end_date_str='{filter_search_range_end_date_str}'")

    # --- FASE 2: Búsqueda de Disponibilidad ---
    # (Lógica adaptada de la antigua find_next_available_slot)
    
    try:
        ensure_cache_is_fresh()
        now_cancun_with_time = get_cancun_time() # Necesitamos la hora para antelación
        cancun_tz = pytz.timezone("America/Cancun")

        # Determinar fecha de inicio del bucle de búsqueda
        # interpreted_date_obj ya debería ser >= today_date_obj por validaciones previas
        search_loop_start_date_obj = interpreted_date_obj
        if is_urgent_param:
            search_loop_start_date_obj = today_date_obj # Si es urgente, empezar hoy ignorando la fecha interpretada
            # Si es urgente, también ignoramos filtros de día/hora específicos que el usuario pudo haber mencionado antes.
            # La urgencia tiene prioridad.
            filter_time_of_day_strict = None
            filter_specific_time_strict = None
            filter_search_range_end_date_str = None # No limitar rango si es urgente
            logger.info("Búsqueda urgente: Se ignorarán filtros de fecha/hora y se buscará desde hoy.")


        search_loop_end_date_obj: Optional[date] = None
        if filter_search_range_end_date_str:
            try:
                search_loop_end_date_obj = datetime.strptime(filter_search_range_end_date_str, "%Y-%m-%d").date()
            except ValueError:
                logger.warning(f"Error parseando filter_search_range_end_date_str: {filter_search_range_end_date_str}. Se ignorará.")
        
        # Límite máximo de búsqueda
        max_search_date_obj = today_date_obj + timedelta(days=180)

        current_search_day_obj = search_loop_start_date_obj
        days_iterated = 0

        while current_search_day_obj <= max_search_date_obj:
            days_iterated += 1
            if days_iterated > 182: # Failsafe para evitar bucles infinitos
                logger.error("Fase 2: Límite de iteración de días alcanzado (182). Abortando búsqueda.")
                break

            if search_loop_end_date_obj and current_search_day_obj > search_loop_end_date_obj:
                logger.info(f"Fase 2: Límite de rango de búsqueda ({search_loop_end_date_obj}) alcanzado.")
                break
            
            if current_search_day_obj.weekday() == 6: # Domingo (6)
                current_search_day_obj += timedelta(days=1)
                continue

            day_key_cache = current_search_day_obj.strftime("%Y-%m-%d")
            
            # Los slots en free_slots_cache son strings "HH:MM" de inicio
            available_start_times_for_day: list[str] = free_slots_cache.get(day_key_cache, [])

            if not available_start_times_for_day:
                current_search_day_obj += timedelta(days=1)
                continue

            for slot_start_hhmm_str in available_start_times_for_day:
                try:
                    slot_start_time_obj = datetime.strptime(slot_start_hhmm_str, "%H:%M").time()
                except ValueError:
                    logger.warning(f"Fase 2: Hora de slot inválida '{slot_start_hhmm_str}' en caché para {day_key_cache}")
                    continue
                
                # Combinar con la fecha actual del bucle para crear un datetime localizado
                slot_start_dt_local = cancun_tz.localize(datetime.combine(current_search_day_obj, slot_start_time_obj))

                # Aplicar Antelación Mínima
                if current_search_day_obj == today_date_obj:
                    min_booking_time_dt = now_cancun_with_time + timedelta(hours=MIN_ADVANCE_BOOKING_HOURS)
                    if slot_start_dt_local < min_booking_time_dt:
                        continue # Slot demasiado pronto hoy

                # Aplicar Filtros Estrictos
                passes_filters = True
                if filter_specific_time_strict: # Filtro más prioritario
                    if slot_start_hhmm_str != filter_specific_time_strict:
                        passes_filters = False
                elif filter_time_of_day_strict:
                    slot_hour = slot_start_time_obj.hour
                    if filter_time_of_day_strict == "mañana": # Antes de MORNING_CUTOFF_TIME_OBJ (ej. 12:00 PM)
                        if slot_start_time_obj >= MORNING_CUTOFF_TIME_OBJ:
                            passes_filters = False
                    elif filter_time_of_day_strict == "tarde": # MORNING_CUTOFF_TIME_OBJ o después
                         if slot_start_time_obj < MORNING_CUTOFF_TIME_OBJ:
                            passes_filters = False
                
                if not passes_filters:
                    continue

                # ¡Slot encontrado!
                # Encontrar la hora de fin del slot desde SLOT_TIMES
                slot_definition = next((s_def for s_def in SLOT_TIMES if s_def["start"] == slot_start_hhmm_str), None)
                if not slot_definition or not slot_definition.get("end"):
                    logger.error(f"Fase 2: No se encontró definición de fin para slot {slot_start_hhmm_str}. Usando duración por defecto.")
                    # Fallback: asumir duración de 45 minutos si no se encuentra (o la primera de VALID_SLOT_START_TIMES)
                    # Esto debería ser robusto, pero idealmente SLOT_TIMES siempre tiene el "end"
                    slot_end_dt_local = slot_start_dt_local + timedelta(minutes=45)
                else:
                    slot_end_time_obj = datetime.strptime(slot_definition["end"], "%H:%M").time()
                    slot_end_dt_local = cancun_tz.localize(datetime.combine(current_search_day_obj, slot_end_time_obj))

                readable_desc = format_date_nicely(current_search_day_obj, specific_time_hhmm=slot_start_hhmm_str)
                
                logger.info(f"✓ Fase 2: Slot encontrado: {readable_desc}")
                return {
                    "status": "SLOT_FOUND",
                    "message_to_user": None, # La IA construirá la frase
                    "slot_details": {
                        "start_time_iso": slot_start_dt_local.isoformat(),
                        "end_time_iso": slot_end_dt_local.isoformat(),
                        "readable_slot_description": readable_desc
                    }
                }
            
            # Si no se encontró slot en este día que cumpla criterios, pasar al siguiente
            current_search_day_obj += timedelta(days=1)

        # Si el bucle termina sin encontrar slot
        desc_busqueda = ""
        if is_urgent_param:
            desc_busqueda = "lo más pronto posible"
        else:
            # Construir una descripción de lo que se buscó para el mensaje de "no disponible"
            temp_date_for_desc = interpreted_date_obj # Ya validado que no es None aquí
            temp_time_keyword_for_desc = filter_time_of_day_strict
            temp_specific_time_for_desc = filter_specific_time_strict
            
            if filter_search_range_end_date_str and not temp_specific_time_for_desc and not temp_time_keyword_for_desc:
                 desc_busqueda = f"esta semana ({format_date_nicely(temp_date_for_desc)} al {format_date_nicely(search_loop_end_date_obj if search_loop_end_date_obj else temp_date_for_desc)})"
            elif temp_specific_time_for_desc:
                desc_busqueda = format_date_nicely(temp_date_for_desc, specific_time_hhmm=temp_specific_time_for_desc)
            elif temp_time_keyword_for_desc:
                desc_busqueda = format_date_nicely(temp_date_for_desc, time_keyword=temp_time_keyword_for_desc)
            else:
                desc_busqueda = format_date_nicely(temp_date_for_desc)


        no_slot_msg = f"Una disculpa, no encontré disponibilidad para {desc_busqueda}. ¿Le gustaría intentar con otra fecha?"
        logger.info(f"Fase 2: No se encontró slot disponible. {no_slot_msg}")
        return {
            "status": "NO_SLOT_AVAILABLE",
            "message_to_user": no_slot_msg,
            "slot_details": None
        }

    except Exception as e_fase2:
        logger.error(f"❌ Error inesperado en Fase 2 (Búsqueda de Disponibilidad): {str(e_fase2)}", exc_info=True)
        return {
            "status": "INTERNAL_ERROR",
            "message_to_user": "Lo siento, tuve un problema técnico al buscar disponibilidad. Por favor, intente más tarde.",
            "slot_details": None
        }


