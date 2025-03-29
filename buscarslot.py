# buscarslot.py
import logging
from datetime import datetime, timedelta
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

free_slots_cache = {}
last_cache_update = None
CACHE_VALID_MINUTES = 15

def load_free_slots_to_cache(days_ahead=90):
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

        for offset in range(days_ahead + 1):
            day_date = now + timedelta(days=offset)
            day_str = day_date.strftime("%Y-%m-%d")

            if day_date.weekday() == 6:
                free_slots_cache[day_str] = []
                continue

            day_busy_list = busy_by_day.get(day_str, [])
            free_slots = build_free_slots_for_day(day_date, day_busy_list)
            free_slots_cache[day_str] = free_slots

        last_cache_update = get_cancun_time()
        logger.info(f"✅ Slots libres precargados para los próximos {days_ahead} días.")

def build_free_slots_for_day(day_dt, busy_list):
    day_str = day_dt.strftime("%Y-%m-%d")
    free_list = []

    for slot in SLOT_TIMES:
        start_str = f"{day_str} {slot['start']}:00"
        end_str = f"{day_str} {slot['end']}:00"

        tz = pytz.timezone("America/Cancun")
        start_dt = tz.localize(datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S"))
        end_dt = tz.localize(datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S"))

        if all(
            not (start_dt < (b_end - timedelta(seconds=1)) and end_dt > b_start)
            for b_start, b_end in busy_list
        ):
            free_list.append(slot["start"])

    return sorted(free_list, key=lambda x: datetime.strptime(x, "%H:%M"))

def ensure_cache_is_fresh():
    global last_cache_update
    now = get_cancun_time()

    if not last_cache_update or (now - last_cache_update).total_seconds() / 60.0 > CACHE_VALID_MINUTES:
        load_free_slots_to_cache(days_ahead=90)

def adjust_to_valid_slot(requested_time, slot_times):
    req_time_obj = datetime.strptime(requested_time, "%H:%M").time()
    for s in slot_times:
        slot_start_obj = datetime.strptime(s["start"], "%H:%M").time()
        if slot_start_obj >= req_time_obj:
            return s["start"]
    return slot_times[-1]["start"]

def find_next_available_slot(target_date=None, target_hour=None, urgent=False):
    try:
        logger.info(f"📥 buscarslot.py recibió: target_date={target_date}, target_hour={target_hour}, urgent={urgent}")

        ensure_cache_is_fresh()
        now = get_cancun_time()

        if target_date:
            date_obj = datetime.strptime(target_date, "%Y-%m-%d")
            search_start = pytz.timezone("America/Cancun").localize(
                datetime(date_obj.year, date_obj.month, date_obj.day)
            )
            if search_start < now:
                return {"error": "No se puede agendar en el pasado."}
        elif urgent:
            search_start = now + timedelta(hours=4)
        else:
            search_start = pytz.timezone("America/Cancun").localize(
                datetime(now.year, now.month, now.day)
            )

        valid_target_hour = adjust_to_valid_slot(target_hour, SLOT_TIMES) if target_hour else None

        for offset in range(180):
            day_to_check = search_start + timedelta(days=offset)
            day_str = day_to_check.strftime("%Y-%m-%d")

            logger.info(f"🔎 Revisando día: {day_str}")

            if day_to_check.weekday() == 6:
                logger.info(f"⛔ Día {day_str} es domingo, se omite.")
                continue

            if day_str not in free_slots_cache:
                logger.info(f"⚠️ No hay datos en caché para {day_str}, se omite.")
                continue

            for free_slot_start in sorted(free_slots_cache[day_str], key=lambda x: datetime.strptime(x, "%H:%M")):
                start_dt = pytz.timezone("America/Cancun").localize(
                    datetime.strptime(f"{day_str} {free_slot_start}:00", "%Y-%m-%d %H:%M:%S")
                )

                if urgent and start_dt < now + timedelta(hours=4):
                    logger.info(f"⏩ Slot {free_slot_start} de {day_str} ignorado por urgencia.")
                    continue
                if start_dt < now:
                    continue

                if valid_target_hour:
                    fs_time_obj = datetime.strptime(free_slot_start, "%H:%M").time()
                    vt_time_obj = datetime.strptime(valid_target_hour, "%H:%M").time()
                    if fs_time_obj < vt_time_obj:
                        logger.info(f"⏩ Slot {free_slot_start} de {day_str} menor que target_hour {valid_target_hour}.")
                        continue

                end_str = next((s["end"] for s in SLOT_TIMES if s["start"] == free_slot_start), None)
                if not end_str:
                    continue

                end_dt = pytz.timezone("America/Cancun").localize(
                    datetime.strptime(f"{day_str} {end_str}:00", "%Y-%m-%d %H:%M:%S")
                )

                slot_found = {
                    "start_time": start_dt.isoformat(),
                    "end_time": end_dt.isoformat(),
                }
                logger.info(f"✅ buscarslot.py → slot encontrado: {slot_found}")
                return slot_found

        logger.error("❌ buscarslot.py no encontró ningún slot en 180 días.")
        return {"error": "No se encontraron horarios disponibles en los próximos 6 meses."}

    except Exception as e:
        logger.error(f"❌ Error inesperado al buscar horario: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}

@router.get("/buscar-disponibilidad")
async def get_next_available_slot_endpoint(target_date: str = None, target_hour: str = None, urgent: bool = False):
    slot = find_next_available_slot(target_date, target_hour, urgent)
    if "error" in slot:
        raise HTTPException(status_code=404, detail=slot["error"])
    return slot
