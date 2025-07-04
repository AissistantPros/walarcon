# aiagent.py
# -*- coding: utf-8 -*-
"""
aiagent – motor de decisión para la asistente telefónica
────────────────────────────────────────────────────────
• Único modelo → (o el que estés usando)
• Flujos main / edit / delete con redirecciones internas
• Nueva "súper herramienta" process_appointment_request
• Métricas de latencia (🕒 ms) en todos los pases Chat-GPT
• Logging DEBUG uniforme (cambia LOG_LEVEL a INFO en producción)
"""

from __future__ import annotations

import asyncio
import json
import logging
from time import perf_counter
import time
from typing import Dict, List, Any # Añadido Any para el tipado de retorno de handle_tool_execution
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
# Asegúrate que CHATGPT_SECRET_KEY esté en tu .env o configuración
try:
    client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))
except Exception as e:
    logger.critical(f"No se pudo inicializar el cliente OpenAI. Verifica CHATGPT_SECRET_KEY: {e}")
    # Podrías querer que el sistema falle aquí si OpenAI es esencial.
    # raise SystemExit("Fallo al inicializar OpenAI client.") from e # Descomenta para fallar

# ────────────────── IMPORTS DE TOOLS DE NEGOCIO ───────────────────
import buscarslot
from utils import search_calendar_event_by_phone # Sigue siendo necesaria para editar/eliminar
from consultarinfo import get_consultorio_data_from_cache
from crearcita import create_calendar_event
from editarcita import edit_calendar_event # Asumo que estas existen
from eliminarcita import delete_calendar_event # Asumo que estas existen



from prompt import generate_minimal_prompt



# prompt dinámico (system)
from prompt import generate_openai_prompt # Asegúrate que el nombre del archivo prompt.py sea correcto

# ══════════════════ HELPERS ═══════════════════════════════════════
def _t(start: float) -> str:
    """Devuelve el tiempo transcurrido desde *start* en ms formateado."""
    return f"{(perf_counter() - start) * 1_000:6.1f} ms"





def merge_tool_calls(tool_calls_chunks):
    """
    Junta tool_calls fragmentadas en streaming usando su 'index' y concatena sus argumentos.
    """
    if not tool_calls_chunks:
        return None

    tool_calls_by_index = {}
    for tc in tool_calls_chunks:
        # Obtén el índice del fragmento
        index = getattr(tc, "index", None)
        if index is None:
            # Si no tiene index, asígnale uno secuencial (fallback paranoico)
            index = len(tool_calls_by_index)
        if index not in tool_calls_by_index:
            tool_calls_by_index[index] = {
                "id": "",
                "type": "function",
                "function": {"name": "", "arguments": ""}
            }
        # ID
        if getattr(tc, "id", None):
            tool_calls_by_index[index]["id"] = tc.id
        # Nombre de función
        if getattr(tc, "function", None) and getattr(tc.function, "name", None):
            tool_calls_by_index[index]["function"]["name"] = tc.function.name
        # Acumula arguments (pueden venir partidos)
        if getattr(tc, "function", None) and getattr(tc.function, "arguments", None):
            tool_calls_by_index[index]["function"]["arguments"] += tc.function.arguments

    merged = []
    for data in tool_calls_by_index.values():
        # Solo si ya tenemos todo lo necesario
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







# ══════════════════ UNIFIED TOOLS DEFINITION ══════════════════════
# Esta es la ÚNICA lista de herramientas que necesitarás.
# Contiene TODAS las herramientas que la IA podría usar,
# guiada por el system_prompt de prompt.py.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información general del consultorio como dirección, horarios de atención general, servicios principales, o políticas de cancelación. No usar para verificar disponibilidad de citas."
            # No necesita parámetros explícitos aquí si la función Python usa defaults
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cancun_weather",
            "description": "Obtener el estado del tiempo actual en Cancún, como temperatura, descripción (soleado, nublado, lluvia), y sensación térmica. Útil si el usuario pregunta específicamente por el clima."
            # No necesita parámetros ya que la ciudad está fija en la función.
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_appointment_request",
            "description": (
                "Procesa la solicitud de agendamiento o consulta de disponibilidad de citas. "
                "Interpreta la petición de fecha/hora del usuario (ej. 'próxima semana', 'el 15 a las 10', 'esta semana en la tarde', 'lo más pronto posible') "
                "y busca un slot disponible en el calendario que cumpla con los criterios. "
                "Devuelve un slot encontrado, un mensaje si no hay disponibilidad, o pide aclaración si la fecha es ambigua o conflictiva."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_query_for_date_time": {
                        "type": "string",
                        "description": "La frase textual completa del usuario referente a la fecha y/o hora deseada. Ej: 'quiero una cita para el próximo martes por la tarde', '¿tienes algo para el 15 de mayo a las 10 am?', 'lo más pronto posible'."
                    },
                    "day_param": {"type": "integer", "description": "Día numérico del mes si el usuario lo menciona explícitamente (ej. 15 para 'el 15 de mayo'). Opcional."},
                    "month_param": {"type": ["string", "integer"], "description": "Mes, como nombre (ej. 'mayo', 'enero') o número (ej. 5, 1) si el usuario lo menciona. Opcional."},
                    "year_param": {"type": "integer", "description": "Año si el usuario lo especifica (ej. 2025). Opcional, si no se da, se asume el actual o el siguiente si la fecha es pasada."},
                    "fixed_weekday_param": {"type": "string", "description": "Día de la semana solicitado por el usuario (ej. 'lunes', 'martes'). Opcional."},
                    "explicit_time_preference_param": {"type": "string", "description": "Preferencia explícita de franja horaria como 'mañana', 'tarde' o 'mediodia', si el usuario la indica claramente. Opcional.", "enum": ["mañana", "tarde", "mediodia"]},
                    "is_urgent_param": {"type": "boolean", "description": "Poner a True si el usuario indica urgencia o quiere la cita 'lo más pronto posible', 'cuanto antes', etc. Esto priorizará la búsqueda inmediata. Opcional, default False."},
                    "more_late_param": {"type": "boolean", "description": "Cuando el usuario pide ‘más tarde’ después de ofrecerle un horario. Opcional."},
                    "more_early_param": {"type": "boolean", "description": "Cuando el usuario pide ‘más temprano’ después de ofrecerle un horario. Opcional."}
                },
                "required": ["user_query_for_date_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear una nueva cita médica en el calendario DESPUÉS de que el usuario haya confirmado un slot específico, nombre, teléfono y motivo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nombre completo del paciente."},
                    "phone": {"type": "string", "description": "Número de teléfono del paciente (10 dígitos)."},
                    "reason": {"type": "string", "description": "Motivo de la consulta."},
                    "start_time": {"type": "string", "format": "date-time", "description": "Hora de inicio de la cita en formato ISO8601 con offset (ej. YYYY-MM-DDTHH:MM:SS-05:00). Obtenido de 'process_appointment_request'."},
                    "end_time": {"type": "string", "format": "date-time", "description": "Hora de fin de la cita en formato ISO8601 con offset. Obtenido de 'process_appointment_request'."}
                },
                "required": ["name", "phone", "start_time", "end_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas existentes de un paciente por su número de teléfono para poder modificarlas o cancelarlas.",
            "parameters": {
                "type": "object",
                "properties": {"phone": {"type": "string", "description": "Número de teléfono del paciente (10 dígitos)."}},
                "required": ["phone"]
            }
        }
    },
    {
    "type": "function",
    "function": {
        "name": "select_calendar_event_by_index",
        "description": (
            "Marca cuál de las citas encontradas (events_found) "
            "es la que el paciente quiere modificar o cancelar. "
            "Úsalo después de enumerar las citas y recibir la confirmación "
            "del paciente. selected_index = 0 para la primera cita listada."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "selected_index": {
                    "type": "integer",
                    "description": "Índice de la cita (0, 1, 2…)."
                }
            },
            "required": ["selected_index"]
        }
    }
},
    {
        "type": "function",
        "function": {
            "name": "edit_calendar_event",
            "description": "Modificar una cita existente en el calendario. Requiere el ID del evento y los nuevos detalles de fecha/hora. Opcionalmente puede actualizar nombre, motivo o teléfono en la descripción.", # Descripción ligeramente ajustada
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "El ID del evento de calendario a modificar. Obtenido de 'search_calendar_event_by_phone'."},
                    "new_start_time_iso": {"type": "string", "format": "date-time", "description": "Nueva hora de inicio para la cita en formato ISO8601 con offset (ej. 2025-MM-DDTHH:MM:SS-05:00). Obtenida de 'process_appointment_request'."}, # CAMBIADO a _iso
                    "new_end_time_iso": {"type": "string", "format": "date-time", "description": "Nueva hora de fin para la cita en formato ISO8601 con offset. Obtenida de 'process_appointment_request'."}, # CAMBIADO a _iso
                    "new_name": {"type": "string", "description": "Opcional. Nuevo nombre del paciente si el usuario desea cambiarlo."},
                    "new_reason": {"type": "string", "description": "Opcional. Nuevo motivo de la consulta si el usuario desea cambiarlo."},
                    "new_phone_for_description": {"type": "string", "description": "Opcional. Nuevo teléfono para la descripción de la cita si el usuario desea cambiarlo."}
                },
                
                "required": ["event_id", "new_start_time_iso", "new_end_time_iso"] # CAMBIADO a _iso
            }
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
                    "event_id": {"type": "string", "description": "El ID del evento de calendario a eliminar. Obtenido de 'search_calendar_event_by_phone'."},
                    "original_start_time_iso": {"type": "string", "format": "date-time", "description": "Hora de inicio original de la cita a eliminar en formato ISO8601 con offset (ej. 2025-MM-DDTHH:MM:SS-05:00), para confirmación."} # CAMBIADO a _iso
                },
                "required": ["event_id", "original_start_time_iso"]
            }
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
}
]

# ══════════════════ TOOL EXECUTOR ═════════════════════════════════
# (Esta función se mantiene prácticamente igual, solo asegúrate que los nombres
# de las funciones coincidan con los definidos en TOOLS y los imports)
def handle_tool_execution(tc: Any) -> Dict[str, Any]:  # tc es un ToolCall object de OpenAI
    fn_name = tc.function.name
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        logger.error(f"Error, al decodificar argumentos JSON para {fn_name}: {tc.function.arguments}")
        return {"error": f"Argumentos inválidos para {fn_name}"}

    # Validación PRO de argumentos requeridos
    required_params = next(
        (tool["function"].get("parameters", {}).get("required", [])
         for tool in TOOLS if tool["function"]["name"] == fn_name),
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
        elif fn_name == "get_cancun_weather": # <--- AÑADE ESTA LÍNEA Y LA DE ABAJO
            return get_cancun_weather()
        elif fn_name == "process_appointment_request":
            return buscarslot.process_appointment_request(**args) #
        elif fn_name == "create_calendar_event":
            phone = args.get("phone", "")
            if not (phone.isdigit() and len(phone) == 10): #
                logger.warning(f"Teléfono inválido '{phone}' para crear evento. La IA debería haberlo validado.")
                return {"error": "Teléfono inválido proporcionado para crear la cita. Debe tener 10 dígitos."}
            return create_calendar_event(**args) #
        elif fn_name == "edit_calendar_event":
            return edit_calendar_event(**args) #
        elif fn_name == "delete_calendar_event":
            return delete_calendar_event(**args) #
        elif fn_name == "search_calendar_event_by_phone":
            return {"search_results": search_calendar_event_by_phone(**args)} #
        elif fn_name == "detect_intent":
            # Simplemente devuelve la intención detectada por la IA
            # El system_prompt guiará al modelo sobre cómo actuar con esta información.
            return {"intent_detected": args.get("intention")}
        elif fn_name == "end_call":
            return {"call_ended_reason": args.get("reason", "unknown")}
        else:
            logger.warning(f"Función {fn_name} no reconocida en handle_tool_execution.")
            return {"error": f"Función desconocida: {fn_name}"}

    except Exception as e:
        logger.exception("Error crítico durante la ejecución de la herramienta %s", fn_name)
        return {"error": f"Error interno al ejecutar {fn_name}: {str(e)}"}



# ══════════════════ CORE – UNIFIED RESPONSE GENERATION ═════════════
async def generate_openai_response_main(history: List[Dict], model: str = "gpt-4.1-mini"):
    """
    Versión con modelo dual: 4.1-mini para decisiones, 4.1-nano para formatear respuestas
    """
    start_gpt_time = time.perf_counter()
    logger.info(f"⏱️ [LATENCIA-2] GPT llamada iniciada")

    try:
        full_conversation_history = generate_openai_prompt(list(history))



        # 🔍 LOG: Ver el prompt completo
        logger.info("="*50)
        logger.info("📋 PROMPT COMPLETO PARA GPT:")
        for i, msg in enumerate(full_conversation_history):
            role = msg.get('role', 'unknown')
            content = msg.get('content', '')[:200] + '...' if len(msg.get('content', '')) > 200 else msg.get('content', '')
            logger.info(f"  [{i}] {role}: {content}")
        logger.info(f"📏 Total mensajes: {len(full_conversation_history)}")
        logger.info(f"📏 Caracteres totales: {sum(len(str(m)) for m in full_conversation_history)}")
        logger.info("="*50)







        if not client:
            logger.error("Cliente OpenAI no inicializado.")
            yield "Lo siento, estoy teniendo problemas técnicos para conectarme."
            return

        # PRIMERA LLAMADA - Modelo inteligente para decisiones complejas
        stream_response = client.chat.completions.create(
            model=model,  # gpt-4.1-mini (inteligente)
            messages=full_conversation_history,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=150,
            temperature=0.3,
            timeout=15,
            stream=True,
        )

        full_content = ""
        tool_calls_chunks = []
        first_chunk = True
        
        # Procesar stream
        for chunk in stream_response:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                full_content += content
                
                if first_chunk:
                    delta_ms = (time.perf_counter() - start_gpt_time) * 1000
                    logger.info(f"⏱️ [LATENCIA-2-FIRST] GPT primer chunk: {delta_ms:.1f} ms")
                    first_chunk = False
                
                yield content
                
            if chunk.choices[0].delta.tool_calls is not None:
                for tc in chunk.choices[0].delta.tool_calls:
                    if not hasattr(tc, "index"):
                        tc.index = len(tool_calls_chunks)
                    tool_calls_chunks.append(tc)


        # 🔍 LOG: Respuesta del primer pase
        logger.info(f"💬 GPT RESPUESTA PASE 1: '{full_content}'")


        # Si NO hay herramientas, terminamos
        if not tool_calls_chunks:
            logger.info("✅ Respuesta sin herramientas - una sola llamada")
            return

        # Si HAY herramientas, procesarlas
        tool_calls = merge_tool_calls(tool_calls_chunks)
        

        # 🔍 LOG: Herramientas detectadas
        logger.info(f"🔧 HERRAMIENTAS DETECTADAS: {len(tool_calls)}")
        for tc in tool_calls:
            logger.info(f"  - {tc.function.name}: {tc.function.arguments}")




        response_pase1 = ChatCompletionMessage(
            content=full_content,
            tool_calls=tool_calls,
            role="assistant",
        )
        
        # Mensajes mínimos para segunda llamada
        minimal_messages = generate_minimal_prompt(history[-1]["content"])
        minimal_messages.append(response_pase1.model_dump())
        
        # Ejecutar herramientas
        for tool_call in response_pase1.tool_calls:
            tool_call_id = tool_call.id
            function_result = handle_tool_execution(tool_call)


            # 🔍 LOG: Resultado de herramienta
            logger.info(f"📊 RESULTADO {tool_call.function.name}: {json.dumps(function_result, ensure_ascii=False)[:200]}...")



            if function_result.get("call_ended_reason"):
                yield "__END_CALL__"
                return

            minimal_messages.append({
                "tool_call_id": tool_call_id,
                "role": "tool", 
                "name": tool_call.function.name,
                "content": json.dumps(function_result),
            })



        # 🔍 LOG: Mensajes para segunda llamada
        logger.info("="*50)
        logger.info("📋 MENSAJES PARA SEGUNDA LLAMADA:")
        for i, msg in enumerate(minimal_messages):
            role = msg.get('role', 'unknown')
            content = str(msg.get('content', ''))[:100] + '...' if len(str(msg.get('content', ''))) > 100 else msg.get('content', '')
            logger.info(f"  [{i}] {role}: {content}")
        logger.info(f"📏 Total mensajes pase 2: {len(minimal_messages)}")
        logger.info("="*50)






        # SEGUNDA LLAMADA - Modelo RÁPIDO solo para formatear
        fast_model = "gpt-4.1-nano"  # ¡El más rápido!
        logger.info(f"🏃 Segunda llamada con modelo rápido: {fast_model}")
        
        stream_response_2 = client.chat.completions.create(
            model=fast_model,  # ¡Modelo súper rápido!
            messages=minimal_messages,
            max_tokens=150,
            temperature=0.1,
            stream=True,
        )

        second_response = ""
        for chunk in stream_response_2:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                second_response += content
                yield content
        
        # 🔍 LOG: Respuesta final
        logger.info(f"💬 GPT RESPUESTA FINAL: '{second_response}'")

    except Exception as e:
        logger.exception("generate_openai_response_main falló")
        yield "Lo siento, estoy experimentando un problema técnico."