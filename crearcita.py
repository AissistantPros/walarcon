from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config
import pytz
from utils import get_iso_format  # Cambio clave aquí

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

def create_calendar_event(name, phone, reason, start_time, end_time):
    service = initialize_google_calendar()
    
    # Convertir a formato ISO con zona horaria
    start_iso = start_time.isoformat()
    end_iso = end_time.isoformat()

    event = {
        "summary": name,
        "description": f"Teléfono: {phone}\nMotivo: {reason or 'No especificado'}",
        "start": {"dateTime": start_iso, "timeZone": "America/Cancun"},
        "end": {"dateTime": end_iso, "timeZone": "America/Cancun"},
    }

    created_event = service.events().insert(
        calendarId=GOOGLE_CALENDAR_ID,
        body=event
    ).execute()

    return {
        "id": created_event["id"],
        "start": created_event["start"]["dateTime"],
        "end": created_event["end"]["dateTime"],
    }