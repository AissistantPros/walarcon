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
from typing import Dict, List, Any
from decouple import config
from openai import OpenAI
from selectevent import select_calendar_event_by_index
from weather_utils import get_cancun_weather
#streaming gpt-4.1-mini
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
try:
    client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))
except Exception as e:
    logger.critical(f"No se pudo inicializar el cliente OpenAI. Verifica CHATGPT_SECRET_KEY: {e}")

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
    modo: str | None = None,
    pending_question: str | None = None,      # ← NUEVO parámetro
    model: str = "gpt-4.1-mini",
):
    """
    Llama dos veces a GPT:

    • Pase 1  (modelo “inteligente”) decide herramientas y lógica.
    • Pase 2  (modelo “rápido”) genera la respuesta final
      con todo el historial + resultados de tools.

    Parámetros:
        history ............ historial completo de la llamada
        modo ............... modo actual (None → BASE)
        pending_question ... si la IA tiene una pregunta pendiente que
                             no debe volver a formular
        model .............. modelo para el primer pase
    """
    start_gpt_time = time.perf_counter()
    logger.info("⏱️ [LATENCIA-2] GPT llamada iniciada")

    try:
        # ── PROMPT PARA PRIMER PASE ─────────────────────────────────────────
        full_conversation_history = generate_openai_prompt(
            list(history),
            modo=modo,
            pending_question=pending_question,          # ← NUEVO
        )

        # ---------- LOG del prompt (se mantiene igual) ----------
        logger.info("=" * 50)
        logger.info("📋 PROMPT COMPLETO PARA GPT:")
        for i, msg in enumerate(full_conversation_history):
            short = (msg.get("content", "")[:200] + "…") if len(msg.get("content", "")) > 200 else msg.get("content", "")
            logger.info("  [%d] %s: %s", i, msg.get("role", ""), short)
        logger.info("📏 Total mensajes: %d", len(full_conversation_history))
        logger.info("📏 Caracteres totales: %d", sum(len(str(m)) for m in full_conversation_history))
        logger.info("=" * 50)

        if not client:
            logger.error("Cliente OpenAI no inicializado.")
            yield "Lo siento, estoy teniendo problemas técnicos para conectarme."
            return

        # ---------- Selección de tools según modo ----------
        tools_to_use = TOOLS_BY_MODE.get(modo, TOOLS_BASE)
        logger.info("🔧 TOOLS para modo '%s': %s", modo, [t['function']['name'] for t in tools_to_use])

        # ── PRIMERA LLAMADA (STREAM) ────────────────────────────────────────
        stream_response = client.chat.completions.create(
            model=model,
            messages=full_conversation_history,
            tools=tools_to_use,
            tool_choice="auto",
            max_tokens=100,
            temperature=0.1,
            timeout=15,
            stream=True,
        )

        full_content = ""
        tool_calls_chunks: list[Any] = []
        first_chunk = True

        for chunk in stream_response:
            # texto
            if chunk.choices[0].delta.content:
                txt = chunk.choices[0].delta.content
                full_content += txt
                if first_chunk:
                    logger.info("⏱️ [LATENCIA-2-FIRST] GPT primer chunk: %.1f ms",
                                (time.perf_counter() - start_gpt_time) * 1000)
                    first_chunk = False
                yield txt
            # tools
            if chunk.choices[0].delta.tool_calls is not None:
                for tc in chunk.choices[0].delta.tool_calls:
                    if not hasattr(tc, "index"):
                        tc.index = len(tool_calls_chunks)
                    tool_calls_chunks.append(tc)

        logger.info("💬 GPT RESPUESTA PASE 1: '%s'", full_content)

        # Si no hubo herramientas, terminamos aquí.
        if not tool_calls_chunks:
            logger.info("✅ Respuesta sin herramientas – una sola llamada")
            return

        # ── AGRUPAR tool_calls y ejecutar cada una ──────────────────────────
        tool_calls = merge_tool_calls(tool_calls_chunks)
        for tc in tool_calls:
            logger.info("  - %s: %s", tc.function.name, tc.function.arguments)

        response_pase1 = ChatCompletionMessage(
            content=full_content, role="assistant", tool_calls=tool_calls
        )

        # Historial para segundo pase
        second_pass_history = list(history)
        second_pass_history.append(response_pase1.model_dump())

        for tc in response_pase1.tool_calls:
            tc_id = tc.id
            result = handle_tool_execution(tc)
            logger.info("📊 RESULTADO %s: %s", tc.function.name, json.dumps(result, ensure_ascii=False)[:200])
            # Señal para colgar
            if result.get("call_ended_reason"):
                yield "__END_CALL__"
                return
            second_pass_history.append({
                "tool_call_id": tc_id,
                "role": "tool",
                "name": tc.function.name,
                "content": json.dumps(result),
            })

        # ── LOG del 2º pase ────────────────────────────────────────────────
        logger.info("=" * 50)
        logger.info("📋 HISTORIAL COMPLETO PARA SEGUNDA LLAMADA:")
        for i, m in enumerate(second_pass_history):
            short = (str(m.get("content", ""))[:100] + "…") if len(str(m.get("content", ""))) > 100 else m.get("content", "")
            logger.info("  [%d] %s: %s", i, m.get("role", ""), short)
        logger.info("📏 Total mensajes pase 2: %d", len(second_pass_history))
        logger.info("=" * 50)

        # ── SEGUNDA LLAMADA (modelo rápido) ────────────────────────────────
        fast_model = "gpt-4.1-mini"
        logger.info("🏃 Segunda llamada con modelo rápido: %s", fast_model)

        stream_response_2 = client.chat.completions.create(
            model=fast_model,
            messages=generate_openai_prompt(
                second_pass_history,
                modo=modo,
                pending_question=pending_question,       # ← NUEVO
            ),
            max_tokens=100,
            temperature=0.2,
            stream=True,
        )

        final_text = ""
        first_chunk_2 = True
        for chunk in stream_response_2:
            if chunk.choices[0].delta.content:
                txt = chunk.choices[0].delta.content
                final_text += txt
                if first_chunk_2:
                    logger.info("⏱️ [LATENCIA-2-SECOND-FIRST] GPT segundo pase primer chunk: %.1f ms",
                                (time.perf_counter() - start_gpt_time) * 1000)
                    first_chunk_2 = False
                yield txt

        logger.info("💬 GPT RESPUESTA FINAL: '%s'", final_text)

    except Exception as e:
        logger.exception("generate_openai_response_main falló")
        yield "Lo siento, estoy experimentando un problema técnico."
