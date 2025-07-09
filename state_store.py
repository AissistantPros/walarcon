# state_store.py
# Memoriza datos durante UNA llamada (se reinicia cuando Twilio abre un WS nuevo)
import time
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

session_state = {
    "events_found": [],       # lista completa de citas encontradas
    "current_event_id": None  # la cita que el usuario confirmó
}

def emit_latency_event(session_id: str, event_name: str, metadata: Optional[dict] = None) -> None:
    """
    Registra un evento de latencia para análisis posterior.
    
    Args:
        session_id: ID de la sesión (call_sid)
        event_name: Nombre del evento (chunk_received, parse_start, etc.)
        metadata: Datos adicionales del evento
    """
    if session_id not in session_state:
        session_state[session_id] = {"events": []}
    
    if "events" not in session_state[session_id]:
        session_state[session_id]["events"] = []
    
    session_state[session_id]["events"].append({
        "event": event_name,
        "timestamp": time.perf_counter(),
        "metadata": metadata or {}
    })
    
    logger.debug(f"[LATENCY] {event_name} | session={session_id} | meta={metadata or {}}")