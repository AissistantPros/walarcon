# aiagent.py
# -*- coding: utf-8 -*-
"""
aiagent – motor de decisión para la asistente telefónica
────────────────────────────────────────────────────────
• Modular, con modos (base/crear/editar/eliminar)
• Expone solo las tools necesarias según modo
• Compatible con prompts y flujos nuevos
• Mantiene doble pase, logs y helpers de tu versión original
"""

from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter
import time
from typing import Dict, List, Any, Optional, Tuple
from decouple import config
from openai import OpenAI
from selectevent import select_calendar_event_by_index
from weather_utils import get_cancun_weather
from groq import Groq
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat import ChatCompletionMessageToolCall
from openai.types.chat.chat_completion_message_tool_call import Function, ChatCompletionMessageToolCall

# ────────────────────── CONFIG LOGGING ────────────────────────────
LOG_LEVEL = logging.DEBUG # ⇢ INFO en prod.
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)5s | %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aiagent")

# ──────────────────────── OPENAI CLIENT ───────────────────────────
#try:
#    client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))
#except Exception as e:
#    logger.critical(f"No se pudo inicializar el cliente OpenAI. Verifica CHATGPT_SECRET_KEY: {e}")


# ──────────────────────── GROQ CLIENT ───────────────────────────

try:
    # Inicializa el cliente de Groq. Automáticamente usará la variable de entorno GROQ_API_KEY.
    client = Groq()
    logger.info("✅ Cliente de Groq inicializado correctamente.")
except Exception as e:
    logger.critical(f"No se pudo inicializar el cliente de Groq. Verifica la variable de entorno GROQ_API_KEY: {e}")


# ────────────────── IMPORTS DE TOOLS DE NEGOCIO ───────────────────
import buscarslot
from utils import search_calendar_event_by_phone
from consultarinfo import get_consultorio_data_from_cache
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event

# prompt dinámico (system)
from prompt import generate_openai_prompt

# ══════════════════ HELPERS ═══════════════════════════════════════
def _t(start: float) -> str:
    """Devuelve el tiempo transcurrido desde *start* en ms formateado."""
    return f"{(perf_counter() - start) * 1_000:6.1f} ms"

def merge_tool_calls(tool_calls_chunks):
    """Junta tool_calls fragmentadas en streaming usando su 'index' y concatena sus argumentos."""
    if not tool_calls_chunks:
        return None
    tool_calls_by_index = {}
    for tc in tool_calls_chunks:
        index = getattr(tc, "index", None)
        if index is None:
            index = len(tool_calls_by_index)
        if index not in tool_calls_by_index:
            tool_calls_by_index[index] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""}
            }
        if getattr(tc, "id", None):
            tool_calls_by_index[index]["id"] = tc.id
        if getattr(tc, "function", None) and getattr(tc.function, "name", None):
            tool_calls_by_index[index]["function"]["name"] = tc.function.name
        if getattr(tc, "function", None) and getattr(tc.function, "arguments", None):
            tool_calls_by_index[index]["function"]["arguments"] += tc.function.arguments
    merged = []
    for data in tool_calls_by_index.values():
        if data["id"] and data["function"]["name"]:
            merged.append(ChatCompletionMessageToolCall(
                id=data["id"],
                type="function",
                function=Function(
                    name=data["function"]["name"],
                    arguments=data["function"]["arguments"]
                )
            ))
    return merged

# ══════════════════ UNIFIED TOOLS DEFINITION (COMPLETO) ══════════════════════

# (Tus tools completas tal cual tienes, solo agrupa por modo abajo)
TOOLS_BASE = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información general del consultorio como dirección, horarios de atención general, servicios principales, o políticas de cancelación. No usar para verificar disponibilidad de citas."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cancun_weather",
            "description": "Obtener el estado del tiempo actual en Cancún, como temperatura, descripción (soleado, nublado, lluvia), y sensación térmica. Útil si el usuario pregunta específicamente por el clima."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detecta la intención del usuario cuando no está claro si quiere agendar en un horario 'más tarde' (more_late) o 'más temprano' (more_early) de la hora que le propusimos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intention": {
                        "type": "string",
                        "enum": ["more_late", "more_early"],
                        "description": "La intención detectada del usuario."
                    }
                },
                "required": ["intention"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_mode",
            "description": (
                "Cambia el modo de operación del asistente. "
                "Úsala cuando detectes una intención clara del usuario de agendar, editar o eliminar una cita. "
                "Solo cambia el modo si la intención es evidente. Si hay duda, primero pregunta al usuario."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["crear", "editar", "eliminar", "None"],
                        "description": (
                            "'crear' para agendar cita nueva, "
                            "'editar' para modificar, "
                            "'eliminar' para cancelar cita, "
                            "'None' para modo informativo/general."
                        ),
                    }
                },
                "required": ["mode"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": "Cierra la llamada de manera definitiva. Úsala cuando ya se haya despedido al paciente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo del cierre. Ej: 'user_request', 'task_completed', 'assistant_farewell'."
                    }
                },
                "required": ["reason"]
            }
        }
    },
]
TOOLS_CREAR = TOOLS_BASE + [
    {
        "type": "function",
        "function": {
            "name": "process_appointment_request",
            "description": (
                "Procesa la solicitud de agendamiento o consulta de disponibilidad de citas. "
                "Interpreta la petición de fecha/hora del usuario (ej. 'próxima semana', 'el 15 a las 10', etc.)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_query_for_date_time": {"type": "string"},
                    "day_param": {"type": "integer"},
                    "month_param": {"type": ["string", "integer"]},
                    "year_param": {"type": "integer"},
                    "fixed_weekday_param": {"type": "string"},
                    "explicit_time_preference_param": {"type": "string", "enum": ["mañana", "tarde", "mediodia"]},
                    "is_urgent_param": {"type": "boolean"},
                    "more_late_param": {"type": "boolean"},
                    "more_early_param": {"type": "boolean"}
                },
                "required": ["user_query_for_date_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear una nueva cita médica en el calendario después de que el usuario haya confirmado todo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "phone": {"type": "string"},
                    "reason": {"type": "string"},
                    "start_time": {"type": "string", "format": "date-time"},
                    "end_time": {"type": "string", "format": "date-time"}
                },
                "required": ["name", "phone", "start_time", "end_time"]
            }
        }
    }
]
TOOLS_EDITAR = TOOLS_BASE + [
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas existentes de un paciente por su número de teléfono."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_calendar_event",
            "description": "Modificar una cita existente en el calendario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "new_start_time_iso": {"type": "string", "format": "date-time"},
                    "new_end_time_iso": {"type": "string", "format": "date-time"},
                    "new_name": {"type": "string"},
                    "new_reason": {"type": "string"},
                    "new_phone_for_description": {"type": "string"}
                },
                "required": ["event_id", "new_start_time_iso", "new_end_time_iso"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_appointment_request",
            "description": "Verifica nuevos slots para citas editadas."
        }
    }
]
TOOLS_ELIMINAR = TOOLS_BASE + [
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas existentes de un paciente por su número de teléfono."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Eliminar/Cancelar una cita existente del calendario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "original_start_time_iso": {"type": "string", "format": "date-time"}
                },
                "required": ["event_id", "original_start_time_iso"]
            }
        }
    }
]

TOOLS_BY_MODE = {
    None: TOOLS_BASE,
    "crear": TOOLS_CREAR,
    "editar": TOOLS_EDITAR,
    "eliminar": TOOLS_ELIMINAR,
}

# ══════════════════ TOOL EXECUTOR ═════════════════════════════════
def handle_tool_execution(tc: Any) -> Dict[str, Any]:
    fn_name = tc.function.name
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        logger.error(f"Error al decodificar argumentos JSON para {fn_name}: {tc.function.arguments}")
        return {"error": f"Argumentos inválidos para {fn_name}"}

    # --- Detecta required_params, aunque no todos los tools lo tienen definido ---
    all_tools = TOOLS_CREAR + TOOLS_EDITAR + TOOLS_ELIMINAR + TOOLS_BASE
    required_params = next(
        (tool["function"].get("parameters", {}).get("required", [])
         for tool in all_tools if tool["function"]["name"] == fn_name),
        []
    )
    missing = [p for p in required_params if p not in args]
    if missing:
        logger.error(f"Faltan parámetros requeridos para {fn_name}: {', '.join(missing)}")
        return {"error": f"Missing required parameters: {', '.join(missing)}"}

    logger.debug("🛠️ Ejecutando herramienta: %s con args: %s", fn_name, args)
    try:
        if fn_name == "read_sheet_data":
            return {"data_consultorio": get_consultorio_data_from_cache()}
        elif fn_name == "get_cancun_weather":
            return get_cancun_weather()
        elif fn_name == "process_appointment_request":
            return buscarslot.process_appointment_request(**args)
        elif fn_name == "create_calendar_event":
            phone = args.get("phone", "")
            if not (isinstance(phone, str) and phone.isdigit() and len(phone) == 10):
                logger.warning(f"Teléfono inválido '{phone}' para crear evento. La IA debería haberlo validado.")
                return {"error": "Teléfono inválido proporcionado para crear la cita. Debe tener 10 dígitos."}
            return create_calendar_event(**args)
        elif fn_name == "edit_calendar_event":
            return edit_calendar_event(**args)
        elif fn_name == "delete_calendar_event":
            return delete_calendar_event(**args)
        elif fn_name == "search_calendar_event_by_phone":
            return {"search_results": search_calendar_event_by_phone(**args)}
        elif fn_name == "detect_intent":
            return {"intent_detected": args.get("intention")}
        elif fn_name == "end_call":
            return {"call_ended_reason": args.get("reason", "unknown")}
        elif fn_name == "set_mode":
            modo = args.get("mode")
            logger.info(f"🔁 Tool set_mode llamada: cambiando modo a '{modo}'")
            return {"new_mode": modo}
        else:
            logger.warning(f"Función {fn_name} no reconocida en handle_tool_execution.")
            return {"error": f"Función desconocida: {fn_name}"}
    except Exception as e:
        logger.exception("Error crítico durante la ejecución de la herramienta %s", fn_name)
        return {"error": f"Error interno al ejecutar {fn_name}: {str(e)}"}










# ══════════════════ CORE – UNIFIED RESPONSE GENERATION ═════════════

async def generate_openai_response_main(
    history: List[Dict],
    *,
    modo: Optional[str] = None,
    pending_question: Optional[str] = None,
    model: str = "llama-3.1-70b-versatile", # Asegúrate de que el modelo esté aquí
) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Orquesta la lógica de doble pase para ser compatible con Llama 3 en Groq.
    """
    logger.info("▶️ Iniciando generate_openai_response_main para Llama 3...")

    # Variables para tracking de estado
    current_mode = modo
    current_pending = pending_question

    try:
        # --- PASO 1: El LLM decide qué herramienta usar ---
        messages_for_pass_1 = generate_openai_prompt(
            list(history),
            modo=current_mode,
            pending_question=current_pending,
        )
        
        tools_to_use = TOOLS_BY_MODE.get(current_mode, TOOLS_BASE)
        logger.info(f"PASO 1: Llamando a Groq para decisión de herramienta (Modo: {current_mode})")

        response_pass_1 = await client.chat.completions.create(
            model=model,
            messages=messages_for_pass_1,
            tools=tools_to_use,
            tool_choice="auto",
            temperature=0.1,
        )

        response_message = response_pass_1.choices[0].message
        tool_calls = response_message.tool_calls

        # Si el modelo NO decide usar una herramienta, responde directamente.
        if not tool_calls:
            logger.info("✅ Groq respondió directamente sin usar herramientas.")
            final_response = response_message.content or "No he podido generar una respuesta."
            return (final_response, current_mode, current_pending)

        logger.info(f"✅ Groq decidió usar {len(tool_calls)} herramienta(s).")

        # --- PASO 2: Ejecutamos la herramienta y llamamos de nuevo para síntesis ---
        
        # Añadir la respuesta original del asistente (con la decisión de la tool) al historial.
        messages_for_pass_2 = list(history)
        messages_for_pass_2.append(response_message.model_dump())

        for tool_call in tool_calls:
            result = handle_tool_execution(tool_call)
            
            # Actualizamos el estado local basado en el resultado de la tool
            if tool_call.function.name == "set_mode" and "new_mode" in result:
                current_mode = result.get("new_mode")
                logger.info(f"✨ MODO ACTUALIZADO para 2º pase a: '{current_mode}'")
                # ... (lógica para establecer pending_question)

            if result.get("call_ended_reason"):
                return ("__END_CALL__", current_mode, current_pending)
            
            # Añadimos el resultado de la herramienta al historial para el segundo pase.
            messages_for_pass_2.append(
                {
                    "tool_call_id": tool_call.id,
                    "role": "tool",
                    "name": tool_call.function.name,
                    "content": json.dumps(result),
                }
            )

        logger.info("PASO 2: Llamando a Groq para sintetizar la respuesta final.")
        
        # Hacemos la segunda llamada para que genere la respuesta al usuario.
        response_pass_2 = await client.chat.completions.create(
            model=model,
            messages=messages_for_pass_2,
            # No pasamos las tools en el segundo pase, solo queremos una respuesta de texto.
        )
        
        final_response = response_pass_2.choices[0].message.content or "Entendido."
        logger.info("💬 Respuesta final sintetizada por Groq: '%s'", final_response)
        
        return (final_response, current_mode, current_pending)

    except Exception as e:
        logger.exception("generate_openai_response_main falló")
        return ("Lo siento, estoy experimentando un problema técnico.", modo, pending_question)
