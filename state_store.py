# state_store.py
# ────────────────────────────────────────────────────────────────────────────────
# Guarda información VOLÁTIL (en memoria) durante UNA llamada telefónica.
# Cada sesión (Call SID) tiene su propio sub-diccionario para no mezclar datos.
# Estructura por sesión:
# {
#     "events_found":   list[dict]   ← citas halladas por search_calendar_event…
#     "current_event_id": str|None   ← ID de cita seleccionada para editar/eliminar
#     "events":          list[dict]   ← telemetría de latencia
# }
# ────────────────────────────────────────────────────────────────────────────────

import time
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# Diccionario global en memoria (vive mientras el proceso corra)
session_state: Dict[str, Dict[str, Any]] = {}


# ╭─ Helpers internos ──────────────────────────────────────────────────────────╮
def _ensure_session(session_id: str) -> Dict[str, Any]:
    """Crea la estructura básica para una sesión si aún no existe."""
    if session_id not in session_state:
        session_state[session_id] = {
            "events_found": [],      # usado por search_calendar_event_by_phone
            "current_event_id": None,
            "events": []             # telemetría de latencia
        }
    return session_state[session_id]


# ╭─ Telemetría ────────────────────────────────────────────────────────────────╮
def emit_latency_event(session_id: str,
                       event_name: str,
                       metadata: Optional[dict] = None) -> None:
    """
    Registra un evento dentro de session_state[session_id]["events"].

    Example:
        emit_latency_event("CA123", "tts_start", {"text": "Hola"})
    """
    sess = _ensure_session(session_id)
    sess["events"].append({
        "event": event_name,
        "timestamp": time.perf_counter(),
        "metadata": metadata or {}
    })
    logger.debug(f"[LATENCY] {event_name} | session={session_id} | meta={metadata or {}}")


# ╭─ API de soporte para herramientas existentes ───────────────────────────────╮
def set_current_event(session_id: str, event_id: str) -> None:
    _ensure_session(session_id)["current_event_id"] = event_id


def get_current_event(session_id: str) -> Optional[str]:
    return _ensure_session(session_id)["current_event_id"]


def add_found_event(session_id: str, event: dict) -> None:
    _ensure_session(session_id)["events_found"].append(event)


def get_found_events(session_id: str) -> List[dict]:
    return _ensure_session(session_id)["events_found"]
