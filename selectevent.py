# selectevent.py


def select_calendar_event_by_index(selected_index: int) -> dict:
    """
    Guarda en sesión cuál de las citas halladas (events_found)
    es la que el paciente quiere modificar o cancelar.
    selected_index: 0 para la primera cita listada, 1 para la segunda, etc.
    """
    from state_store import session_state

    events = session_state.get("events_found", [])
    if 0 <= selected_index < len(events):
        session_state["current_event_id"] = events[selected_index]["event_id"]
        return {"message": "ID actualizado", "event_id": session_state["current_event_id"]}
    return {"error": "Índice fuera de rango"}
