from datetime import datetime, timedelta
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from utils import get_cancun_time  # Cambio clave aquí
import pytz
from decouple import config

# Configuración de Google Calendar
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_PRIVATE_KEY = config("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")

def initialize_google_calendar():
    credentials = Credentials.from_service_account_info({
        "type": "service_account",
        "project_id": GOOGLE_PROJECT_ID,
        "private_key": GOOGLE_PRIVATE_KEY,
        "client_email": GOOGLE_CLIENT_EMAIL,
        "token_uri": "https://oauth2.googleapis.com/token",
    }, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=credentials)

def check_availability(start_time, end_time):
    service = initialize_google_calendar()
    events = service.freebusy().query(body={
        "timeMin": start_time.isoformat(),
        "timeMax": end_time.isoformat(),
        "timeZone": "America/Cancun",
        "items": [{"id": GOOGLE_CALENDAR_ID}]
    }).execute()
    return len(events["calendars"][GOOGLE_CALENDAR_ID]["busy"]) == 0

def find_next_available_slot():
    slot_times = [
        {"start": "09:30", "end": "10:15"},
        {"start": "10:15", "end": "11:00"},
        {"start": "11:00", "end": "11:45"},
        {"start": "11:45", "end": "12:30"},
        {"start": "12:30", "end": "13:15"},
        {"start": "13:15", "end": "14:00"},
        {"start": "14:00", "end": "14:45"},
    ]

    now = get_cancun_time()  # Usando función corregida

    day_offset = 0
    while True:
        day = now + timedelta(days=day_offset)
        if day.weekday() == 6:  # Domingo
            day_offset += 1
            continue

        for slot in slot_times:
            start_time_str = f"{day.strftime('%Y-%m-%d')} {slot['start']}:00"
            start_time = datetime.strptime(start_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))
            end_time = datetime.strptime(start_time_str.replace(slot['start'], slot['end']), "%Y-%m-%d %H:%M:%S").replace(tzinfo=pytz.timezone("America/Cancun"))

            if day_offset == 0 and start_time <= now + timedelta(hours=4):
                continue

            if check_availability(start_time, end_time):
                return {"start_time": start_time, "end_time": end_time}

        day_offset += 1