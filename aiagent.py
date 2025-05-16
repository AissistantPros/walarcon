# aiagent.py
# -*- coding: utf-8 -*-
"""
aiagent – motor de decisión para la asistente telefónica
────────────────────────────────────────────────────────
• Único modelo → gpt-4.1-mini (o el que estés usando)
• Flujos main / edit / delete con redirecciones internas
• Nueva "súper herramienta" process_appointment_request
• Métricas de latencia (🕒 ms) en todos los pases Chat-GPT
• Logging DEBUG uniforme (cambia LOG_LEVEL a INFO en producción)
"""

from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Dict, List, Any # Añadido Any para el tipado de retorno de handle_tool_execution
from decouple import config
from openai import OpenAI
# from utils import calculate_structured_date # <--- ELIMINADA

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
# from buscarslot import find_next_available_slot # <--- ELIMINADA
from buscarslot import process_appointment_request # <--- NUEVA IMPORTACIÓN
from crearcita import create_calendar_event
from editarcita import edit_calendar_event # Asumo que estas existen
from eliminarcita import delete_calendar_event # Asumo que estas existen

# prompt dinámico (system)
from prompt import generate_openai_prompt # Asegúrate que el nombre del archivo prompt.py sea correcto

# ══════════════════ HELPERS ═══════════════════════════════════════
def _t(start: float) -> str:
    """Devuelve el tiempo transcurrido desde *start* en ms formateado."""
    return f"{(perf_counter() - start) * 1_000:6.1f} ms"


# ══════════════════ TOOLS DEFINITIONS ═════════════════════════════
# Herramientas disponibles en el flujo principal (agendar)
MAIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información general del consultorio como dirección, horarios de atención general, servicios principales, o políticas de cancelación. No usar para verificar disponibilidad de citas."
        }
    },
    { # Definición de la NUEVA "SúPER HERRAMIENTA"
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
                    "day_param": {
                        "type": "integer",
                        "description": "Día numérico del mes si el usuario lo menciona explícitamente (ej. 15 para 'el 15 de mayo'). Opcional."
                    },
                    "month_param": {
                        "type": ["string", "integer"], # Puede ser nombre o número
                        "description": "Mes, como nombre (ej. 'mayo', 'enero') o número (ej. 5, 1) si el usuario lo menciona. Opcional."
                    },
                    "more_late_param": { 
                        "type": "boolean",
                        "description": "Cuando el usuario pide ‘más tarde’. Opcional." 
                    },
                    "more_early_param": { 
                        "type": "boolean", 
                        "description": "Cuando el usuario pide ‘más temprano’. Opcional." 
                    },
                    "year_param": {
                        "type": "integer",
                        "description": "Año si el usuario lo especifica (ej. 2025). Opcional, si no se da, se asume el actual o el siguiente si la fecha es pasada."
                    },
                    "fixed_weekday_param": {
                        "type": "string",
                        "description": "Día de la semana solicitado por el usuario (ej. 'lunes', 'martes'). Opcional."
                    },
                    "explicit_time_preference_param": {
                        "type": "string",
                        "description": "Preferencia explícita de franja horaria como 'mañana' o 'tarde', si el usuario la indica claramente. Opcional.",
                        "enum": ["mañana", "tarde"]
                    },
                    "is_urgent_param": {
                        "type": "boolean",
                        "description": "Poner a True si el usuario indica urgencia o quiere la cita 'lo más pronto posible', 'cuanto antes', etc. Esto priorizará la búsqueda inmediata. Opcional, default False."
                    }
                },
                "required": ["user_query_for_date_time"] # El query del usuario es el input principal
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
                "required": ["name", "phone", "start_time", "end_time"] # Reason puede ser opcional para la creación del evento en sí
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detecta la intención principal del usuario cuando no está claro si quiere agendar, modificar o cancelar una cita existente, o si cambia de opinión.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intention": {
                        "type": "string",
                        "enum": ["create", "edit", "delete", "informational", "unknown", "more_late", "more_early"],
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
            "description": "Finaliza la llamada telefónica. Usar solo cuando la conversación ha concluido natural o infructuosamente, o si el usuario lo pide.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["user_request", "task_completed", "task_failed", "silence", "spam", "time_limit", "error", "no_slot_accepted"],
                        "description": "Razón por la que se finaliza la llamada."
                    }
                },
                "required": ["reason"]
            }
        }
    }
    # La herramienta calculate_structured_date YA NO ES NECESARIA AQUÍ
    # La herramienta find_next_available_slot YA NO ES NECESARIA AQUÍ
]

# Herramientas para el flujo de EDICIÓN de citas
# Podría incluir `process_appointment_request` si se permite cambiar la fecha de una cita existente.
EDIT_TOOLS = [
    tool for tool in MAIN_TOOLS if tool["function"]["name"] not in ["create_calendar_event"] # No crear en flujo de edición
] + [
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
            "name": "edit_calendar_event",
            "description": "Modificar una cita existente en el calendario. Requiere la hora de inicio original y los nuevos detalles.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "El ID del evento de calendario a modificar. Obtenido de 'search_calendar_event_by_phone'."},
                    # "phone": {"type": "string", "description": "Número de teléfono del paciente asociado a la cita original."}, # Podría ser redundante si ya se usó event_id
                    "original_start_time": {"type": "string", "format": "date-time", "description": "Hora de inicio original de la cita a modificar (ISO8601)."},
                    "new_start_time": {"type": "string", "format": "date-time", "description": "Nueva hora de inicio para la cita (ISO8601). Obtenida de 'process_appointment_request'."},
                    "new_end_time": {"type": "string", "format": "date-time", "description": "Nueva hora de fin para la cita (ISO8601). Obtenida de 'process_appointment_request'."},
                    # Se podrían añadir otros campos si se permite modificar nombre, motivo, etc.
                    # "new_name": {"type": "string", "description": "Nuevo nombre del paciente, si cambia."},
                    # "new_reason": {"type": "string", "description": "Nuevo motivo de la consulta, si cambia."}
                },
                "required": ["event_id", "original_start_time", "new_start_time", "new_end_time"]
            }
        }
    }
    # Si al editar se busca nueva fecha, process_appointment_request ya está en MAIN_TOOLS, que se incluye.
]

# Herramientas para el flujo de ELIMINACIÓN de citas
DELETE_TOOLS = [
    tool for tool in MAIN_TOOLS if tool["function"]["name"] not in ["create_calendar_event", "process_appointment_request"] # No crear ni buscar slot en flujo de eliminación
] + [
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone", # Reutilizada
            "description": "Buscar citas existentes de un paciente por su número de teléfono para poder cancelarlas.",
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
            "name": "delete_calendar_event",
            "description": "Eliminar/Cancelar una cita existente del calendario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "El ID del evento de calendario a eliminar. Obtenido de 'search_calendar_event_by_phone'."},
                    "original_start_time": {"type": "string", "format": "date-time", "description": "Hora de inicio original de la cita a eliminar (ISO8601), para confirmación."},
                    # "phone": {"type": "string", "description": "Número de teléfono del paciente, para confirmación."} # Podría ser redundante
                },
                "required": ["event_id", "original_start_time"]
            }
        }
    }
]


# ══════════════════ TOOL EXECUTOR ═════════════════════════════════
def handle_tool_execution(tc: Any) -> Dict[str, Any]: # tc es un ToolCall object de OpenAI
    fn_name = tc.function.name
    try:
        args = json.loads(tc.function.arguments or "{}")
    except json.JSONDecodeError:
        logger.error(f"Error al decodificar argumentos JSON para {fn_name}: {tc.function.arguments}")
        return {"error": f"Argumentos inválidos para {fn_name}"}
        
    logger.debug("🛠️ Ejecutando herramienta: %s con args: %s", fn_name, args)

    try:
        if fn_name == "read_sheet_data":
            # Asumimos que get_consultorio_data_from_cache no toma argumentos o usa defaults.
            return {"data_consultorio": get_consultorio_data_from_cache()}

        # --- Nueva llamada a la súper herramienta ---
        elif fn_name == "process_appointment_request":
            # Llama a la función de buscarslot.py y devuelve su resultado directamente,
            # ya que esta función está diseñada para devolver el diccionario esperado por la IA.
            return buscarslot.process_appointment_request(**args)

        elif fn_name == "create_calendar_event":
            # Aquí podrías añadir validaciones de argumentos si es necesario antes de llamar
            phone = args.get("phone", "")
            if not (phone.isdigit() and len(phone) == 10):
                # Este error debería ser manejado por la IA basada en el prompt,
                # pero una validación aquí no hace daño.
                logger.warning(f"Teléfono inválido '{phone}' para crear evento. La IA debería haberlo validado.")
                return {"error": "Teléfono inválido proporcionado para crear la cita. Debe tener 10 dígitos."}
            # create_calendar_event debería devolver un dict con "id", "start", "end" o "error"
            return create_calendar_event(**args) # Asume que esta función devuelve un dict

        elif fn_name == "edit_calendar_event":
            # edit_calendar_event debería devolver un dict con el resultado
            return edit_calendar_event(**args)

        elif fn_name == "delete_calendar_event":
            # delete_calendar_event debería devolver un dict con el resultado
            return delete_calendar_event(**args)

        elif fn_name == "search_calendar_event_by_phone":
            # search_calendar_event_by_phone devuelve una lista de eventos o error
            return {"search_results": search_calendar_event_by_phone(**args)}

        elif fn_name == "detect_intent":
            # Simplemente devuelve la intención detectada por la IA
            return {"intent_detected": args.get("intention")}

        elif fn_name == "end_call":
            # Devuelve la razón para que el manejador principal de la llamada actúe
            return {"call_ended_reason": args.get("reason", "unknown")}

        # --- Las siguientes herramientas ya no existen ---
        # elif fn_name == "calculate_structured_date":
        #     # Esta lógica ahora está en process_appointment_request
        #     return {"error": "Herramienta obsoleta: calculate_structured_date"}
        # elif fn_name == "find_next_available_slot":
        #     # Esta lógica ahora está en process_appointment_request
        #     return {"error": "Herramienta obsoleta: find_next_available_slot"}

        else:
            logger.warning(f"Función {fn_name} no reconocida en handle_tool_execution.")
            return {"error": f"Función desconocida: {fn_name}"}

    except Exception as e:
        logger.exception("Error crítico durante la ejecución de la herramienta %s", fn_name)
        # Devuelve un error genérico para que la IA pueda intentar manejarlo o informar al usuario.
        return {"error": f"Error interno al ejecutar {fn_name}: {str(e)}"}








# ══════════════════ CORE – MAIN FLOW (AGENDAR) ═══════════════════
async def generate_openai_response_main(history: List[Dict], model: str = "gpt-4.1-mini") -> str: # o el modelo que uses
    try:
        # Asegura prompt system
        # generate_openai_prompt ahora es responsable de construir la lista completa de mensajes
        # incluyendo el system prompt si no está.
        full_conversation_history = generate_openai_prompt(list(history)) # Usar una copia de history

        # Pase 1 ──────────────────────────────────────────────────────
        t1_start = perf_counter()
        logger.debug("OpenAI Main Flow - Pase 1: Enviando a %s", model)
        
        if not client: # Chequeo por si falló la inicialización del cliente OpenAI
            logger.error("Cliente OpenAI no inicializado. Abortando generate_openai_response_main.")
            return "Lo siento, estoy teniendo problemas técnicos para conectarme. Por favor, intente más tarde."

        response_pase1 = client.chat.completions.create(
            model=model,
            messages=full_conversation_history,
            tools=MAIN_TOOLS,
            tool_choice="auto", # Permitir a la IA decidir si llama a una herramienta
            # max_tokens=250, # Ajustar según necesidad
            # temperature=0.3, # Ajustar para creatividad vs. determinismo
            # timeout=15, # Timeout para la llamada a OpenAI
        ).choices[0].message
        
        logger.debug("🕒 OpenAI Main Flow - Pase 1 completado en %s", _t(t1_start))

        # Si la IA responde directamente sin llamar a herramienta
        if not response_pase1.tool_calls:
            logger.debug("OpenAI Main Flow - Pase 1: Respuesta directa de la IA: %s", response_pase1.content)
            return response_pase1.content or "No he podido procesar su solicitud en este momento."

        # Si la IA decide llamar a herramientas
        # Añadir la respuesta del asistente (que contiene las tool_calls) al historial
        full_conversation_history.append(response_pase1.model_dump()) # .model_dump() para pydantic v2

        tool_messages_for_pase2 = []
        for tool_call in response_pase1.tool_calls:
            tool_call_id = tool_call.id
            function_result = handle_tool_execution(tool_call) # tc es tool_call
            
            # Si la herramienta indica terminar la llamada (ej. process_appointment_request -> end_call vía prompt)
            # O si la propia herramienta `end_call` fue llamada.
            if function_result.get("call_ended_reason"):
                logger.info("Solicitud de finalizar llamada recibida desde ejecución de herramienta: %s", function_result["call_ended_reason"])
                # El prompt debe instruir a la IA para que se despida ANTES de llamar a end_call.
                # Aquí solo propagamos la señal de fin.
                return "__END_CALL__" # Señal para el manejador de la llamada
            
            tool_messages_for_pase2.append({
                "tool_call_id": tool_call_id,
                "role": "tool",
                "name": tool_call.function.name,
                "content": json.dumps(function_result), # El contenido DEBE ser un string JSON
            })

        # Añadir los resultados de las herramientas al historial para el Pase 2
        full_conversation_history.extend(tool_messages_for_pase2)

        # Pase 2 ──────────────────────────────────────────────────────
        t2_start = perf_counter()
        logger.debug("OpenAI Main Flow - Pase 2: Enviando a %s con resultados de herramientas.", model)
        
        response_pase2 = client.chat.completions.create(
            model=model,
            messages=full_conversation_history, # Historial ya actualizado
            tools=MAIN_TOOLS, # Puede que la IA necesite otra herramienta o ninguna.
            tool_choice="auto",
            # max_tokens=250,
            # temperature=0.3,
            # timeout=15,
        ).choices[0].message
        logger.debug("🕒 OpenAI Main Flow - Pase 2 completado en %s", _t(t2_start))

        # Revisar si la IA quiere desviar a edit / delete después del Pase 2
        # (Aunque con la súper herramienta, esto es menos probable aquí, más bien el prompt guiará)
        if response_pase2.tool_calls:
            for tc_pase2 in response_pase2.tool_calls:
                if tc_pase2.function.name == "detect_intent":
                    try:
                        args_intent = json.loads(tc_pase2.function.arguments or "{}")
                        intention = args_intent.get("intention")
                        logger.info("OpenAI Main Flow - Pase 2: Intención detectada para desvío: %s", intention)
                        if intention == "edit":
                            # Pasar el historial *actualizado hasta este punto* al flujo de edición
                            return await generate_openai_response_edit(full_conversation_history, model)
                        if intention == "delete":
                            return await generate_openai_response_delete(full_conversation_history, model)
                    except json.JSONDecodeError:
                        logger.error("Error decodificando argumentos para detect_intent en Pase 2.")
                    except Exception as e_intent:
                         logger.error(f"Error procesando detect_intent en Pase 2: {e_intent}")


        logger.debug("OpenAI Main Flow - Pase 2: Respuesta final de la IA: %s", response_pase2.content)
        return response_pase2.content or "No tengo una respuesta en este momento."

    except Exception as e:
        logger.exception("generate_openai_response_main falló gravemente")
        return "Lo siento mucho, estoy experimentando un problema técnico y no puedo continuar. Por favor, intente llamar más tarde."


# ══════════════════ TWO-PASS FLOW GENÉRICO (EDIT / DELETE) ════════
async def _two_pass_flow(
    history: List[Dict],    # Historial de conversación ya existente
    prompt_fn: callable,    # Función que genera el system prompt específico (ej. prompt_editar_cita)
    tools_list: List[Dict], # Lista de herramientas para este flujo (EDIT_TOOLS o DELETE_TOOLS)
    model: str,
    flow_tag: str, # "EDIT" o "DELETE" para logging
) -> str:
    try:
        # El historial que llega aquí puede venir de generate_openai_response_main
        # y ya contener un system prompt. Si no, prompt_fn lo añade.
        # Si generate_openai_prompt ya añade el system prompt principal,
        # las funciones prompt_editar_cita / prompt_eliminar_cita
        # podrían solo *modificar* o *añadir* instrucciones al historial existente.
        # O, si son system prompts completos, asegurarse que no se dupliquen.

        # Por ahora, asumimos que history ya está bien para usarse o que prompt_fn lo prepara.
        # Si prompt_fn es como generate_openai_prompt, creará una nueva lista.
        # Si es un system prompt simple, se añade al inicio si no existe.
        
        current_conversation_history: List[Dict]
        if not any(m.get("role") == "system" for m in history):
            # Esto es un placeholder, necesitas adaptar cómo prompt_fn (ej. prompt_editar_cita)
            # interactúa con el historial. Si prompt_fn DEVUELVE la lista completa de mensajes:
            # current_conversation_history = prompt_fn(list(history))
            # O si solo devuelve el texto del system prompt:
            system_prompt_text = prompt_fn() # Asumiendo que devuelve solo el string del system prompt
            current_conversation_history = [{"role": "system", "content": system_prompt_text}] + list(history)
            logger.debug("Añadido system prompt específico para flujo %s.", flow_tag)
        else:
            # Si ya hay un system prompt, podríamos querer reemplazarlo o añadir instrucciones.
            # Por simplicidad, si ya existe, usamos el historial tal cual.
            # La lógica más compleja de "fusionar" prompts sistémicos debería estar en prompt_fn.
            current_conversation_history = list(history)
            # Opcionalmente, podrías llamar a prompt_fn para que MODIFIQUE el historial si es necesario:
            # current_conversation_history = prompt_fn(list(history)) # Si prompt_fn está diseñado para modificar
            logger.debug("Usando historial existente (que ya tiene system prompt) para flujo %s.", flow_tag)


        # Pase 1 (para flujos de edición/eliminación)
        t1_start_subflow = perf_counter()
        logger.debug("OpenAI %s Flow - Pase 1: Enviando a %s", flow_tag, model)

        if not client:
            logger.error("Cliente OpenAI no inicializado. Abortando _two_pass_flow.")
            return "Lo siento, problemas técnicos."

        response_pase1_subflow = client.chat.completions.create(
            model=model,
            messages=current_conversation_history,
            tools=tools_list,
            tool_choice="auto",
        ).choices[0].message
        logger.debug("🕒 OpenAI %s Flow - Pase 1 completado en %s", flow_tag, _t(t1_start_subflow))

        if not response_pase1_subflow.tool_calls:
            logger.debug("OpenAI %s Flow - Pase 1: Respuesta directa: %s", flow_tag, response_pase1_subflow.content)
            return response_pase1_subflow.content or "No pude procesar eso."

        current_conversation_history.append(response_pase1_subflow.model_dump())
        
        tool_messages_for_pase2_subflow = []
        for tool_call_subflow in response_pase1_subflow.tool_calls:
            tool_call_id_subflow = tool_call_subflow.id
            function_result_subflow = handle_tool_execution(tool_call_subflow)

            if function_result_subflow.get("call_ended_reason"):
                logger.info("%s Flow: Solicitud de finalizar llamada desde herramienta: %s", flow_tag, function_result_subflow["call_ended_reason"])
                return "__END_CALL__"
            
            tool_messages_for_pase2_subflow.append({
                "tool_call_id": tool_call_id_subflow,
                "role": "tool",
                "name": tool_call_subflow.function.name,
                "content": json.dumps(function_result_subflow),
            })
        
        current_conversation_history.extend(tool_messages_for_pase2_subflow)

        # Pase 2 (para flujos de edición/eliminación)
        t2_start_subflow = perf_counter()
        logger.debug("OpenAI %s Flow - Pase 2: Enviando a %s con resultados de herramientas.", flow_tag, model)
        
        response_pase2_subflow = client.chat.completions.create(
            model=model,
            messages=current_conversation_history,
            tools=tools_list,
            tool_choice="auto",
        ).choices[0].message
        logger.debug("🕒 OpenAI %s Flow - Pase 2 completado en %s", flow_tag, _t(t2_start_subflow))

        # Manejo de redirecciones cruzadas (ej. de editar quiere crear)
        if response_pase2_subflow.tool_calls:
            for tc_pase2_subflow in response_pase2_subflow.tool_calls:
                if tc_pase2_subflow.function.name == "detect_intent":
                    try:
                        args_intent_subflow = json.loads(tc_pase2_subflow.function.arguments or "{}")
                        intention_subflow = args_intent_subflow.get("intention")
                        logger.info("OpenAI %s Flow - Pase 2: Intención detectada para desvío: %s", flow_tag, intention_subflow)
                        if intention_subflow == "create":
                            return await generate_openai_response_main(current_conversation_history, model)
                        if flow_tag == "EDIT" and intention_subflow == "delete":
                            return await generate_openai_response_delete(current_conversation_history, model)
                        if flow_tag == "DELETE" and intention_subflow == "edit":
                            return await generate_openai_response_edit(current_conversation_history, model)
                    except json.JSONDecodeError:
                        logger.error("Error decodificando argumentos para detect_intent en Pase 2 de %s Flow.", flow_tag)
                    except Exception as e_intent_sub:
                        logger.error(f"Error procesando detect_intent en Pase 2 de {flow_tag} Flow: {e_intent_sub}")


        logger.debug("OpenAI %s Flow - Pase 2: Respuesta final: %s", flow_tag, response_pase2_subflow.content)
        return response_pase2_subflow.content or "No tengo una respuesta en este momento."

    except Exception as e:
        logger.exception("%s Flow falló gravemente", flow_tag)
        return f"Lo siento, ocurrió un error al intentar {flow_tag.lower()} la cita. Por favor, intente más tarde."












# ══════════════════ PUBLIC HELPERS (para flujos específicos) ═══════
async def generate_openai_response_edit(history: List[Dict], model: str = "gpt-4.1-mini") -> str:
    # Necesitas un prompt específico para editar, ej. prompts/prompt_editar_cita.py
    # que defina una función como `def get_edit_prompt_text(): return "Eres Dany..."`
    # O que modifique el historial directamente.
    # Por ahora, un placeholder:
    def get_placeholder_edit_prompt():
        # Este es un system prompt de ejemplo. Deberías tener uno bien definido.
        logger.warning("Usando system prompt de placeholder para edición. Define uno específico.")
        return ("Eres Dany, una IA asistente. Estás ayudando al usuario a MODIFICAR una cita médica existente. "
                "Primero, busca la cita usando el teléfono del paciente. Luego, si se encuentra la cita, "
                "pregunta qué desea modificar. Si es la fecha/hora, usa 'process_appointment_request' para buscar nueva disponibilidad. "
                "Una vez confirmada la nueva fecha/hora, usa 'edit_calendar_event' para actualizarla. "
                "Confirma todos los cambios antes de proceder.")

    from prompts.prompt_editar_cita import prompt_editar_cita # Asegúrate que esto exista y devuelva el prompt o modifique el historial
    # Adaptar cómo se usa prompt_editar_cita según si devuelve un string o modifica el historial.
    # Por ahora, asumo que devuelve un string para el system prompt.
    return await _two_pass_flow(history, prompt_editar_cita, EDIT_TOOLS, model, "EDIT")










async def generate_openai_response_delete(history: List[Dict], model: str = "gpt-4.1-mini") -> str:
    # Similar a editar, necesitas un prompt específico para eliminar.
    def get_placeholder_delete_prompt():
        logger.warning("Usando system prompt de placeholder para eliminación. Define uno específico.")
        return ("Eres Dany, una IA asistente. Estás ayudando al usuario a CANCELAR una cita médica existente. "
                "Busca la cita usando el teléfono del paciente. Si se encuentran una o más citas, "
                "presenta las opciones al usuario y pide que confirme cuál desea cancelar. "
                "Una vez confirmada, usa 'delete_calendar_event' para eliminarla. "
                "Notifica al usuario el resultado.")

    from prompts.prompt_eliminar_cita import prompt_eliminar_cita # Asegúrate que esto exista
    return await _two_pass_flow(history, prompt_eliminar_cita, DELETE_TOOLS, model, "DELETE")


# alias corto para el flujo principal, si se usa externamente
generate_openai_response = generate_openai_response_main