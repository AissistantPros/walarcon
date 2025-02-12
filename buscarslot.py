from datetime import datetime, timedelta
from decouple import config
from utils import get_cancun_time, initialize_google_calendar, GOOGLE_CALENDAR_ID
import pytz
import logging
from dotenv import load_dotenv
load_dotenv()  # ✅ Carga las variables

# Configurar logging
logger = logging.getLogger(__name__)

# ==================================================
# 🔹 Verificación de Disponibilidad
# ==================================================

def check_availability(start_time, end_time):
    """
    Verifica si un horario está disponible en Google Calendar.
    """
    try:
        service = initialize_google_calendar()
        logger.info(f"🔍 Verificando disponibilidad de {start_time} a {end_time} en Google Calendar...")
        
        events = service.freebusy().query(body={
            "timeMin": start_time.isoformat(),
            "timeMax": end_time.isoformat(),
            "timeZone": "America/Cancun",
            "items": [{"id": GOOGLE_CALENDAR_ID}]
        }).execute()
        
        is_available = len(events["calendars"][GOOGLE_CALENDAR_ID]["busy"]) == 0
        logger.info(f"✅ Disponibilidad: {'Disponible' if is_available else 'Ocupado'}")
        return is_available
    
    except Exception as e:
        logger.error(f"❌ Error al verificar disponibilidad en Google Calendar: {str(e)}")
        return False  # Evitar que la IA falle, devolviendo 'ocupado' por defecto

# ==================================================
# 🔹 Búsqueda del Próximo Horario Disponible
# ==================================================

def find_next_available_slot(target_date=None, target_hour=None, urgent=False):
    """
    Busca el siguiente horario disponible en Google Calendar.
    """
    try:
        logger.info("🔎 Iniciando búsqueda de disponibilidad de cita...")
        
        slot_times = [
            {"start": "09:30", "end": "10:15"},
            {"start": "10:15", "end": "11:00"},
            {"start": "11:00", "end": "11:45"},
            {"start": "11:45", "end": "12:30"},
            {"start": "12:30", "end": "13:15"},
            {"start": "13:15", "end": "14:00"},
            {"start": "14:00", "end": "14:45"},
        ]

        now = get_cancun_time()
        logger.info(f"🕒 Hora actual en Cancún: {now}")
        
        day_offset = 0
        start_day = now
        
        if target_date:
            logger.info(f"🎯 Se especificó fecha objetivo: {target_date}")
            start_day = target_date
        else:
            start_day = now + timedelta(hours=4)  # Siempre sumar 4 horas antes de iniciar
            logger.info(f"⏩ Ajustando inicio de búsqueda a: {start_day}")

        if start_day.hour >= 15:
            logger.info("🌙 Fuera del horario laboral tras ajustar 4 horas, avanzando al siguiente día hábil.")
            day_offset += 1
        
        tz = pytz.timezone("America/Cancun")  # 🔹 Definir zona horaria

        while True:
            day = start_day + timedelta(days=day_offset)

            if day.weekday() == 6:
                logger.info("📅 Se omite el domingo y se pasa al siguiente día.")
                day_offset += 1
                continue

            for slot in slot_times:
                logger.info(f"🧐 Revisando slot válido: {slot['start']} - {slot['end']}")

                if target_hour and slot["start"] != target_hour:
                    continue

                naive_start = datetime.combine(day.date(), datetime.strptime(slot["start"], "%H:%M").time())
                start_time = tz.localize(naive_start)
                naive_end = datetime.combine(day.date(), datetime.strptime(slot["end"], "%H:%M").time())
                end_time = tz.localize(naive_end)
                
                start_time = start_time.astimezone(pytz.FixedOffset(-300))
                end_time = end_time.astimezone(pytz.FixedOffset(-300))
                
                logger.info(f"🔍 Evaluando slot: {start_time} - {end_time}")

                if urgent and start_time <= now + timedelta(hours=4):
                    logger.info(f"⚠️ Omitiendo {start_time}, ya que está dentro de las próximas 4 horas (modo urgente).")
                    continue

                if start_time < now:
                    logger.info(f"⚠️ Omitiendo {start_time}, ya que está en el pasado.")
                    continue

                if check_availability(start_time, end_time):
                    logger.info(f"✅ Horario disponible encontrado: {start_time} - {end_time}")
                    return {
                        "start_time": start_time.isoformat(),
                        "end_time": end_time.isoformat()
                    }
                else:
                    logger.info(f"❌ Horario {start_time} - {end_time} no disponible, probando el siguiente...")

            day_offset += 1
    
    except Exception as e:
        logger.error(f"❌ Error inesperado en find_next_available_slot: {str(e)}")
        return {"error": "GOOGLE_CALENDAR_UNAVAILABLE"}
