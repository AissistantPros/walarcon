# synthetic_responses.py
# ────────────────────────────────────────────────────────────────────────────────
import random
from typing import Dict, Any
from utils import convertir_hora_a_palabras  # ya existe

TEMPLATES: Dict[str, Dict[str, list[str]]] = {
    "process_appointment_request": {
        "SLOT_LIST": [
            "Para el {pretty_date}, tengo disponible: {available_pretty}. ¿Alguna le funciona?",
            "Ese día puedo ofrecerle {available_pretty}. ¿Cuál prefiere?",
            "El {pretty_date} me quedan libres {available_pretty}. ¿Le conviene alguno?",
            "Opciones para {pretty_date}: {available_pretty}. ¿Cuál tomamos?",
            "Tengo cupo {available_pretty} el {pretty_date}. ¿Le va bien?"
        ],
        "NO_SLOT": [
            "Lo siento, el {pretty_date} ya no hay espacios. ¿Busco otra fecha?",
            "Agenda llena ese día. ¿Le sugiero alternativa?",
            "No tengo huecos el {pretty_date}. ¿Reviso otro día?",
            "Sin disponibilidad ese día. ¿Le propongo una fecha distinta?",
            "Ese día está completo. ¿Buscamos otra opción?"
        ],
        "SLOT_FOUND_LATER": [
            "No había en {requested_date_iso}, pero el {suggested_date_iso} tengo {available_pretty}. ¿Le conviene?",
            "Alternativa: {suggested_date_iso} con horarios {available_pretty}. ¿Tomamos uno?",
            "Puedo ofrecerle {suggested_date_iso} a las {available_pretty}. ¿Le va bien?",
            "Encontré espacio el {suggested_date_iso}: {available_pretty}. ¿Reservo?",
            "Disponible el {suggested_date_iso} en {available_pretty}. ¿Confirmo?"
        ],
        "NEED_EXACT_DATE": [
            "¿Podría indicarme la fecha exacta que desea?",
            "Necesito la fecha concreta para revisar. ¿Cuál le interesa?",
            "Dígame el día específico y busco horario.",
            "Para ayudarle, indíqueme la fecha exacta, por favor.",
            "¿Qué día le gustaría exactamente?"
        ],
        "OUT_OF_RANGE": [
            "Solo puedo agendar dentro de 90 días. ¿Otra fecha?",
            "Esa fecha queda fuera de nuestro calendario. ¿Buscamos un día más cercano?",
            "Por ahora agendamos hasta 3 meses adelante. ¿Elige otra fecha?",
            "Agenda disponible solo 90 días adelante. ¿Qué otra fecha prefiere?",
            "Esa fecha está fuera de rango. ¿Otra que le convenga?"
        ]
    },

    "create_calendar_event": {
        "success": [
            "Cita confirmada. ¡Nos vemos entonces!",
            "Perfecto, quedó agendado. ¡Gracias!",
            "Registro exitoso. Le esperamos en consulta.",
            "Su cita se creó sin problema. ¡Hasta pronto!",
            "Listo, su cita quedó registrada."
        ],
        "error": [
            "Ocurrió un problema al crear la cita: {error}. ¿Reintento?",
            "Ups, no pude agendar: {error}. ¿Intentamos de nuevo?",
            "Error al registrar la cita ({error}). ¿Pruebo otra vez?",
            "No se pudo crear la cita: {error}. ¿Quiere que lo intente otra vez?",
            "Inconveniente ({error}). Dígame si reintentamos."
        ]
    },

    "search_calendar_event_by_phone": {
        "found": [
            "Tiene cita el {date} a las {time}. ¿Desea cambiar algo?",
            "Registro hallado: {date} {time}. ¿Modificamos algo?",
            "Su cita existente es el {date} a las {time}. ¿Todo correcto?",
            "Cita localizada: {date} {time}. Avíseme si requiere ajuste.",
            "Encontré su cita para el {date} a las {time}. ¿La editamos?"
        ],
        "not_found": [
            "No localicé citas con ese número. ¿Agendamos una nueva?",
            "Sin registros a su nombre. ¿Quiere programar cita?",
            "No veo citas asociadas. ¿Le ayudo a reservar?",
            "No aparece cita previa. ¿Desea agendar?",
            "Sin citas encontradas. ¿Le creo una?"
        ],
        "multiple": [
            "Hay {count} citas asociadas. ¿Sobre cuál desea información?",
            "Encontré varias citas ({count}). ¿A cuál se refiere?",
            "{count} registros hallados. Dígame fecha/hora para identificar la correcta.",
            "Son {count} citas. ¿Cuál de ellas revisamos?",
            "Múltiples citas detectadas ({count}). ¿Cuál desea modificar?"
        ]
    },

    "get_cancun_weather": {
        "default": [
            "Actualmente {description} en Cancún, {temperature} °C, se siente como {feels_like} °C.",
            "El clima en Cancún: {description}, {temperature} °C (sensación {feels_like} °C).",
            "Cancún ahora: {description}, temperatura {temperature} °C, sensación {feels_like} °C.",
            "Hace {description} con {temperature} °C en Cancún (se siente {feels_like} °C).",
            "Clima: {description}, {temperature} °C, sensación térmica {feels_like} °C."
        ]
    }
}


def _ensure_pretty(result: Dict[str, Any]) -> None:
    """
    Completa 'available_pretty' si falta, usando convertir_hora_a_palabras()
    sobre 'available_slots'.
    """
    if not result.get("available_pretty") and result.get("available_slots"):
        result["available_pretty"] = [
            convertir_hora_a_palabras(hhmm) for hhmm in result["available_slots"]
        ]
    # Convierte lista a string con " o "
    if isinstance(result.get("available_pretty"), list):
        result["available_pretty"] = " o ".join(result["available_pretty"])


def generate_synthetic_response(tool_name: str, result: Dict[str, Any]) -> str:
    """
    Selecciona plantilla según 'status' (o 'default'), valida campos y devuelve
    frase sintetizada.
    """
    templates_tool = TEMPLATES.get(tool_name)
    if not templates_tool:
        return "Listo."

    _ensure_pretty(result)

    status = result.get("status", "default")
    variants = templates_tool.get(status) or templates_tool.get("default")
    if not variants:
        # Fallback genérico
        variants = ["Perfecto."]

    template = random.choice(variants)
    try:
        return template.format(**result)
    except KeyError:
        # Si falta un campo, devuelve plantilla sin formatear
        return template
