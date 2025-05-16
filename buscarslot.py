# buscarslot.py – versión final completa
# -*- coding: utf-8 -*-
"""
Gestión de caché de slots libres y parsing de expresiones de fecha / hora.
Reemplaza íntegramente tu archivo original por este.
"""

import logging
import re
from datetime import datetime, timedelta, time as dt_time, date
from typing import Dict, Optional, Tuple, Union, List

import pytz

from utils import (
    initialize_google_calendar,
    get_cancun_time,
    cache_lock,
    convert_utc_to_cancun,
    GOOGLE_CALENDAR_ID,
)

logger = logging.getLogger(__name__)

# ──────────── CONSTANTES DE IDIOMA ─────────────────────────────────────────
DAYS_EN_TO_ES = {
    "Monday": "Lunes", "Tuesday": "Martes", "Wednesday": "Miércoles",
    "Thursday": "Jueves", "Friday": "Viernes", "Saturday": "Sábado",
    "Sunday": "Domingo",
}
MONTHS_EN_TO_ES = {
    "January": "Enero", "February": "Febrero", "March": "Marzo", "April": "Abril",
    "May": "Mayo", "June": "Junio", "July": "Julio", "August": "Agosto",
    "September": "Septiembre", "October": "Octubre", "November": "Noviembre", "December": "Diciembre",
}
MESES_ES_A_NUM = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}
WEEKDAYS_ES_TO_NUM = {
    "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2, "jueves": 3,
    "viernes": 4, "sábado": 5, "sabado": 5, "domingo": 6,
}

# ──────────── SINÓNIMOS Y NÚMEROS ──────────────────────────────────────────
SINONIMOS_HOY = {
    "hoy", "ahorita", "hoy mismo", "en el transcurso del día", "hoy mero",
}
SINONIMOS_SEMANA = {
    "esta semana", "en esta semana", "esta misma semana", "en esta misma semana", "para esta semana",
    "para esta misma semana",
}
SINONIMOS_MANANA = {
    "mañana", "mañana mismo", "para mañana",
}
URGENCIA_KWS = {
    "lo antes posible", "en cuanto se pueda", "lo más pronto posible", "lo mas pronto posible",
}
PALABRA_A_NUM: Dict[str, int] = {}
for i, w in enumerate([
        "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete", "ocho", "nueve", "diez",
        "once", "doce", "trece", "catorce", "quince", "dieciséis", "dieciseis", "diecisiete",
        "dieciocho", "diecinueve", "veinte", "veintiuno", "veintidós", "veintidos", "veintitrés",
        "veintitres", "veinticuatro", "veinticinco", "veintiséis", "veintiseis", "veintisiete",
        "veintiocho", "veintinueve", "treinta"], start=1):
    PALABRA_A_NUM[w] = i

# ──────────── CONFIGURACIÓN DE SLOTS ───────────────────────────────────────
VALID_SLOT_START_TIMES = {
    "09:30", "10:15", "11:00", "11:45", "12:30", "13:15", "14:00",
}
SLOT_TIMES: List[Dict[str, str]] = [
    {"start": "09:30", "end": "10:15"},
    {"start": "10:15", "end": "11:00"},
    {"start": "11:00", "end": "11:45"},
    {"start": "11:45", "end": "12:30"},
    {"start": "12:30", "end": "13:15"},
    {"start": "13:15", "end": "14:00"},
    {"start": "14:00", "end": "14:45"},
]
MORNING_CUTOFF_TIME_OBJ = dt_time(12, 0)
MIN_ADVANCE_BOOKING_HOURS = 6

# ──────────── CACHÉ ───────────────────────────────────────────────────────
free_slots_cache: Dict[str, List[str]] = {}
last_cache_update: Optional[datetime] = None
CACHE_VALID_MINUTES = 15

# ──────────── HELPERS ─────────────────────────────────────────────────────

def _word_to_int(token: str) -> int:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return PALABRA_A_NUM.get(token, 0)


def format_date_nicely(target_date_obj: date, time_keyword: Optional[str] = None,
                       weekday_override: Optional[str] = None,
                       specific_time_hhmm: Optional[str] = None) -> str:
    day_name_es = DAYS_EN_TO_ES.get(target_date_obj.strftime("%A"), "")
    if weekday_override:
        day_name_es = weekday_override.capitalize()
    month_es = MONTHS_EN_TO_ES.get(target_date_obj.strftime("%B"), "")
    text = f"{day_name_es} {target_date_obj.day} de {month_es}"
    if specific_time_hhmm:
        hhmm = datetime.strptime(specific_time_hhmm, "%H:%M").time()
        text += f" a las {hhmm.strftime('%I:%M %p').lower().lstrip('0')}"
    elif time_keyword == "mañana":
        text += ", por la mañana"
    elif time_keyword == "tarde":
        text += ", por la tarde"
    return text

# ──────────── PARSERS ─────────────────────────────────────────────────────


MEDIODIA_KWS = {"mediodia", "medio día", "mediodía", "hora de la comida"}

def parse_time_of_day(q: str) -> Optional[str]:
    q_l = q.lower()
    if re.search(r"\b(por|en|a)\s+la\s+mañana\b", q_l) or "tempranito" in q_l or "mañanita" in q_l:
        return "mañana"
    if re.search(r"\b(por|en|a)\s+la\s+tarde\b", q_l) or "tardecita" in q_l:
        return "tarde"
    if any(k in q_l for k in MEDIODIA_KWS):
        return "mediodia"
    if re.search(r"\bnoche\b|\bmadrugada\b", q_l):
        return "fuera_horario"
    return None


def parse_relative_date(q: str, today: date) -> Optional[date]:
    """
    Devuelve un objeto datetime.date o None si no se reconoce la frase.
    Interpreta expresiones relativas, “de hoy en ocho”, días de la semana,
    ‘fin de semana’, y ahora también “esta semana”.
    """
    q_l = q.lower().strip()

    # — hoy / mañana / pasado —
    if q_l in SINONIMOS_HOY:
        return today
    if q_l in SINONIMOS_MANANA:
        return today + timedelta(days=1)
    if "pasado mañana" in q_l:
        return today + timedelta(days=2)

    # — esta semana / en esta semana —
    #   ⇒ devolvemos el propio 'today' para que process_appointment_request
    #      sepa que la intención es la semana actual (sin día fijo aún).
    if any(phrase in q_l for phrase in SINONIMOS_SEMANA):
        return today  # marcador de “semana actual”

    # — de hoy/mañana en N —
    if m := re.search(r"\b(hoy|mañana)\s+en\s+(\d+|\w+)", q_l):
        base = 0 if m.group(1) == "hoy" else 1          # “mañana” = hoy+1
        n = _word_to_int(m.group(2))                    # número capturado (1-30)
        if n:
            if n == 8:                                  # caso especial de costumbre
                return today + timedelta(days=base + 7)
            return today + timedelta(days=base + n)

    # — en N días / semanas / meses —
    if m := re.search(r"\ben\s+(\d+|\w+)\s+d[ií]as?\b", q_l):
        n = _word_to_int(m.group(1))
        if n:
            return today + timedelta(days=n)
    if m := re.search(r"\ben\s+(\d+|\w+)\s+semanas?\b", q_l):
        n = _word_to_int(m.group(1))
        if n:
            return today + timedelta(days=n * 7)
    if m := re.search(r"\ben\s+(\d+|\w+)\s+mes(es)?\b", q_l):
        n = _word_to_int(m.group(1))
        if n:
            from dateutil.relativedelta import relativedelta as rd
            return today + rd(months=n)

    # — próxima / siguiente semana —
    if any(p in q_l for p in ["próxima semana", "la semana que viene", "la semana que entra",
                              "para la otra semana", "la siguiente semana"]):
        days_until_monday = (7 - today.weekday()) % 7 or 7
        return today + timedelta(days=days_until_monday)

    # — fin de semana (sábado) —
    if "fin de semana" in q_l:
        days_until_sat = (5 - today.weekday()) % 7 or 7
        return today + timedelta(days=days_until_sat)

    return None


# ──────────── CACHÉ DE SLOTS ───────────────────────────────────────────────
def load_free_slots_to_cache(days_ahead: int = 90) -> None:
    """Precarga en memoria los slots libres de los próximos *days_ahead* días."""
    global free_slots_cache, last_cache_update
    with cache_lock:
        logger.info("⏳ Cargando slots libres desde Google Calendar…")
        free_slots_cache.clear()

        service = initialize_google_calendar()
        now = get_cancun_time()
        body = {
            "timeMin": now.isoformat(),
            "timeMax": (now + timedelta(days=days_ahead)).isoformat(),
            "timeZone": "America/Cancun",
            "items": [{"id": GOOGLE_CALENDAR_ID}],
        }

        try:
            result = service.freebusy().query(body=body).execute()
            busy_raw = result["calendars"][GOOGLE_CALENDAR_ID]["busy"]
        except Exception as e:
            logger.error(f"Error freebusy: {e}")
            last_cache_update = get_cancun_time()
            return

        busy_by_day: Dict[str, List[Tuple[datetime, datetime]]] = {}
        for b in busy_raw:
            start_l = convert_utc_to_cancun(b["start"])
            end_l = convert_utc_to_cancun(b["end"])
            key = start_l.strftime("%Y-%m-%d")
            busy_by_day.setdefault(key, []).append((start_l, end_l))

        for offset in range(days_ahead + 1):
            d = now.date() + timedelta(days=offset)
            key = d.strftime("%Y-%m-%d")

            # Domingo sin citas
            if d.weekday() == 6:
                free_slots_cache[key] = []
                continue

            busy_intervals = busy_by_day.get(key, [])
            free_slots_cache[key] = _build_free_slots_for_day(d, busy_intervals)

        last_cache_update = get_cancun_time()
        logger.info(f"✅ Slots libres precargados ({days_ahead} días)")


def _build_free_slots_for_day(
    day_date_obj: date, busy_intervals: List[Tuple[datetime, datetime]]
) -> List[str]:
    """Devuelve lista de HH:MM libres para *day_date_obj*."""
    day_str = day_date_obj.strftime("%Y-%m-%d")
    tz = pytz.timezone("America/Cancun")
    free: List[str] = []

    for slot in SLOT_TIMES:
        s_str = f"{day_str} {slot['start']}:00"
        e_str = f"{day_str} {slot['end']}:00"
        s_dt = tz.localize(datetime.strptime(s_str, "%Y-%m-%d %H:%M:%S"))
        e_dt = tz.localize(datetime.strptime(e_str, "%Y-%m-%d %H:%M:%S"))

        if not any(s_dt < (b_end - timedelta(seconds=1)) and e_dt > b_start for b_start, b_end in busy_intervals):
            free.append(slot["start"])

    return free


def ensure_cache_is_fresh() -> None:
    """Recarga la caché si lleva más de CACHE_VALID_MINUTES sin actualizarse."""
    global last_cache_update
    if (
        last_cache_update is None
        or (get_cancun_time() - last_cache_update).total_seconds() > CACHE_VALID_MINUTES * 60
    ):
        load_free_slots_to_cache()



def _slots_for_franja(slots_del_dia: list[str], franja: str) -> list[str]:
    """Devuelve los slots del día que caen en la franja (‘mañana’ / ‘tarde’)."""
    if franja == "mañana":
        return [s for s in slots_del_dia if s < "11:45"]
    if franja == "tarde":
        return [s for s in slots_del_dia if s >= "11:45"]
    return slots_del_dia                     # fallback “cualquiera”

def _pretty_hhmm(hhmm: str) -> str:
    """Formatea '11:45' ➜ 'once cuarenta y cinco de la mañana/tarde'."""
    t = datetime.strptime(hhmm, "%H:%M")
    suf = "de la mañana" if t.hour < 12 else "de la tarde"
    return t.strftime("%I:%M").lstrip("0") + f" {suf}"

















# ──────────── FUNCIÓN PRINCIPAL PARA LA IA ──────────────────────────────────


def process_appointment_request(
    user_query_for_date_time: str,
    day_param: Optional[int] = None,
    month_param: Optional[Union[str, int]] = None,
    year_param: Optional[int] = None,
    fixed_weekday_param: Optional[str] = None,
    explicit_time_preference_param: Optional[str] = None,
    is_urgent_param: bool = False,
    more_late_param: bool = False, 
    more_early_param: bool = False
) -> Dict:
    """
    Devuelve un diccionario para la IA con:
      status: SLOT_FOUND | SLOT_FOUND_LATER | NO_SLOT | NEED_EXACT_DATE | OUT_OF_RANGE
      pretty: texto legible
      start_iso / end_iso
      requested_date_iso / suggested_date_iso
      requested_time_kw
      is_urgent
    """
    ensure_cache_is_fresh()
    now = get_cancun_time()
    today = now.date()

    # —— urgencia implícita ——
    if not is_urgent_param and any(k in user_query_for_date_time.lower() for k in URGENCIA_KWS):
        is_urgent_param = True

    # —— fecha objetivo ——
    target_date: Optional[date] = None

    # “el 19” sin mes
    if day_param and month_param is None and year_param is None:
        month_param = today.month if day_param >= today.day else (today.month % 12 + 1)

    if day_param and month_param:
        m_num = MESES_ES_A_NUM.get(str(month_param).lower(), month_param)
        year_val = year_param or today.year
        try:
            target_date = date(int(year_val), int(m_num), int(day_param))
        except ValueError:
            pass
    elif fixed_weekday_param:
        wd = WEEKDAYS_ES_TO_NUM.get(fixed_weekday_param.lower())
        if wd is not None:
            offset = (wd - today.weekday()) % 7 or 7
            target_date = today + timedelta(days=offset)
    else:
        target_date = parse_relative_date(user_query_for_date_time, today)

    if is_urgent_param and target_date is None:
        target_date = today

    if target_date is None:
        return {"status": "NEED_EXACT_DATE", "message": "fecha_ambigua"}

    # —— franja horaria ——
    time_kw = explicit_time_preference_param or parse_time_of_day(user_query_for_date_time)
    if time_kw == "fuera_horario":
        return {"status": "OUT_OF_RANGE", "message": "horario_fuera_de_rango"}

    requested_date_iso = target_date.isoformat()

    def _filter_by_kw(slots: List[str]) -> List[str]:
        if time_kw == "mañana":
            return [s for s in slots if datetime.strptime(s, "%H:%M").time() < dt_time(11, 45)]
        if time_kw == "tarde":
            return [s for s in slots if datetime.strptime(s, "%H:%M").time() >= dt_time(11, 45)]
        if time_kw == "mediodia":
            return [s for s in slots if dt_time(11, 0) <= datetime.strptime(s, "%H:%M").time() <= dt_time(13, 15)]
        return slots




    # —— búsqueda de slot —— (máx 120 días) ────────────────────────────────
    is_this_week = any(p in user_query_for_date_time.lower() for p in SINONIMOS_SEMANA)
    days_until_saturday = (5 - today.weekday()) % 7  # 0=Lun … 5=Sáb
    is_today_request = target_date == today and "hoy" in user_query_for_date_time.lower()

    for day_offset in range(0, 120):
        chk_date = target_date + timedelta(days=day_offset)
        if chk_date.weekday() == 6:       # domingo
            continue

        day_key = chk_date.strftime("%Y-%m-%d")
        free_slots = free_slots_cache.get(day_key, []).copy()

        # franja pedida (mañana / tarde / mediodía, etc.)
        free_slots = _filter_by_kw(free_slots)

        # ── regla “6 h antes” + cierre diario ──────────────────────────────
        if chk_date == today:
            future_dt = now + timedelta(hours=MIN_ADVANCE_BOOKING_HOURS)
            if future_dt.date() != today or now.time() >= dt_time(14, 0):
                free_slots = []
            else:
                limit = future_dt.time()
                free_slots = [
                    s for s in free_slots
                    if datetime.strptime(s, "%H:%M").time() >= limit
                ]

        # ─ regla “más tarde / más temprano” ───────────────────────────────
        if free_slots and (more_late_param or more_early_param):
            if more_late_param:
                free_slots = free_slots[1:5]       # siguientes 4
            if more_early_param:
                free_slots = free_slots[-5:-1]     # anteriores 4
            if not free_slots:
                return {
                    "status": "NO_MORE_LATE" if more_late_param else "NO_MORE_EARLY",
                    "message": "sin_disponibilidad",
                }

        # ─ Filtrar por franja explícita (“tarde”, “mañana”…) —─────────────
        if explicit_time_preference_param:
            free_slots = _slots_for_franja(free_slots, explicit_time_preference_param)

        # ─ Si tras todos los filtros no quedó nada, seguimos con el día ↓──
        if not free_slots:
            continue   # ⬅️  ANTES devolvía NO_SLOT_FRANJA

        # ─ Si la consulta era “esta semana” y el hueco es > sábado, avisa ──
        if is_this_week and day_offset > days_until_saturday:
            available = free_slots[:4]
            pretty_list = [_pretty_hhmm(h) for h in available]
            return {
                "status": "SLOT_FOUND_LATER",
                "requested_date_iso": requested_date_iso,
                "suggested_date_iso": chk_date.isoformat(),
                "available_slots": available,
                "available_pretty": pretty_list,
            }
        # ─ Si la consulta era “hoy” y el hueco cae otro día, avisa ─────────
        if is_today_request and day_offset > 0:
            available = free_slots[:4]
            pretty_list = [_pretty_hhmm(h) for h in available]
            return {
                "status": "SLOT_FOUND_LATER",
                "requested_date_iso": requested_date_iso,
                "suggested_date_iso": chk_date.isoformat(),
                "available_slots": available,
                "available_pretty": pretty_list,
            }

        # ─ Devolver los horarios del día hallado ──────────────────────────
        available = free_slots[:4]
        pretty_list = [_pretty_hhmm(h) for h in available]
        return {
            "status": "SLOT_LIST",
            "date_iso": chk_date.isoformat(),
            "available_slots": available,
            "available_pretty": pretty_list,
        }

    # ─ Si no se encontró nada en 120 días ─────────────────────────────────
    return {
        "status": "NO_SLOT",
        "message": "sin_disponibilidad",
        "requested_date_iso": requested_date_iso,
        "requested_time_kw": time_kw,
        "is_urgent": is_urgent_param,
    }
