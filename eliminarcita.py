from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
from decouple import config

# **SECCIÓN 1: Configuración de Google Calendar**
GOOGLE_CALENDAR_ID = config("GOOGLE_CALENDAR_ID")
GOOGLE_PRIVATE_KEY = config("GOOGLE_PRIVATE_KEY").replace("\\n", "\n")
GOOGLE_PROJECT_ID = config("GOOGLE_PROJECT_ID")
GOOGLE_CLIENT_EMAIL = config("GOOGLE_CLIENT_EMAIL")

def initialize_google_calendar():
    """
    Inicializa la conexión con la API de Google Calendar usando credenciales de servicio.

    Retorna:
        service: Cliente de Google Calendar listo para realizar operaciones.
    """
    credentials = Credentials.from_service_account_info(
        {
            "type": "service_account",
            "project_id": GOOGLE_PROJECT_ID,
            "private_key": GOOGLE_PRIVATE_KEY,
            "client_email": GOOGLE_CLIENT_EMAIL,
            "token_uri": "https://oauth2.googleapis.com/token",
        },
        scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=credentials)


# **SECCIÓN 2: Eliminar un evento existente**
def delete_calendar_event(phone, patient_name=None):
    """
    Busca y elimina un evento en Google Calendar basado en el número de teléfono.
    Si se encuentran múltiples eventos con el mismo número, solicita confirmar con el nombre del paciente.

    Parámetros:
        phone (str): Número de teléfono del paciente para identificar la cita.
        patient_name (str): Nombre del paciente (opcional, para confirmar en caso de múltiples citas).

    Retorna:
        dict: Detalles del resultado de la operación o un mensaje indicando el estado.
    """
    # Validar el número de teléfono
    if not phone or len(phone) != 10 or not phone.isdigit():
        raise ValueError("El campo 'phone' debe ser un número de 10 dígitos.")

    # Inicializar Google Calendar API
    service = initialize_google_calendar()

    # Buscar eventos que coincidan con el número de teléfono
    events = service.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        q=phone,  # Buscar por teléfono en las notas del evento
        singleEvents=True
    ).execute()

    items = events.get("items", [])

    # Si no se encuentran eventos, informar al usuario
    if not items:
        return {"message": "No se encontraron citas con el número proporcionado."}

    # Si hay múltiples citas, confirmar con el nombre del paciente
    if len(items) > 1 and patient_name:
        # Buscar un evento que coincida con el nombre del paciente
        for event in items:
            if event["summary"].lower() == patient_name.lower():
                # Eliminar el evento encontrado
                service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event["id"]).execute()
                return {"message": f"El evento para {patient_name} ha sido eliminado con éxito."}

        # Si no se encuentra un evento con el nombre especificado
        return {
            "message": "Se encontraron múltiples citas con este número, pero ninguna coincide con el nombre proporcionado.",
            "options": [event["summary"] for event in items]  # Lista los nombres de los pacientes
        }

    # Si solo hay un evento o no se especificó el nombre, eliminar el primer evento encontrado
    event = items[0]
    service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event["id"]).execute()
    return {"message": f"El evento para {event['summary']} ha sido eliminado con éxito."}
