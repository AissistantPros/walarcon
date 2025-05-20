# state_store.py
# Memoriza datos durante UNA llamada (se reinicia cuando Twilio abre un WS nuevo)
session_state = {
    "events_found": [],       # lista completa de citas encontradas
    "current_event_id": None  # la cita que el usuario confirmó
}
