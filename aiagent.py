# aiagent.py
# -*- coding: utf-8 -*-

"""
Versión limpia que:
1) Usa sólo un modelo GPT (gpt-4o-mini) en todos los flujos.
2) Mantiene las funciones para main, edit y delete.
3) Conserva la lógica de tools y la estructura original sin romper nada.

Autor: ChatGPT con ajustes solicitados por Esteban.
"""

import logging
import time
import json
from typing import List, Dict
from decouple import config
from openai import OpenAI

# Módulos (del usuario)
from consultarinfo import get_consultorio_data_from_cache
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from utils import search_calendar_event_by_phone

# Prompts
from prompt import generate_openai_prompt
from prompts.prompt_editar_cita import prompt_editar_cita
from prompts.prompt_eliminar_cita import prompt_eliminar_cita


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

# ─────────────────────────────────────────────────────────────────
# 1) MAIN_TOOLS
# ─────────────────────────────────────────────────────────────────
MAIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información del consultorio"
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_next_available_slot",
            "description": "Buscar horario disponible",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {"type": "string", "format": "date"},
                    "target_hour": {"type": "string"},
                    "urgent": {"type": "boolean"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear cita médica",
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
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detectar si el usuario quiere editar, eliminar o crear",
            "parameters": {
                "type": "object",
                "properties": {
                    "intention": {
                        "type": "string",
                        "enum": ["create", "edit", "delete", "unknown"]
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
            "description": "Finalizar la llamada",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": [
                            "user_request", "silence", "spam", "time_limit", "error"
                        ]
                    }
                },
                "required": ["reason"]
            }
        }
    }
]

# ─────────────────────────────────────────────────────────────────
# 2) EDIT_TOOLS
# ─────────────────────────────────────────────────────────────────
EDIT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información del consultorio"
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_next_available_slot",
            "description": "Buscar horario disponible para reprogramar",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {"type": "string", "format": "date"},
                    "target_hour": {"type": "string"},
                    "urgent": {"type": "boolean"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas por número de teléfono",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"}
                },
                "required": ["phone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_calendar_event",
            "description": "Modificar cita",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "original_start_time": {"type": "string", "format": "date-time"},
                    "new_start_time": {"type": "string", "format": "date-time"},
                    "new_end_time": {"type": "string", "format": "date-time"}
                },
                "required": ["phone", "original_start_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detectar si el usuario quiere crear, eliminar, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intention": {
                        "type": "string",
                        "enum": ["create", "edit", "delete", "unknown"]
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
            "description": "Finalizar la llamada",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": [
                            "user_request", "silence", "spam", "time_limit", "error"
                        ]
                    }
                },
                "required": ["reason"]
            }
        }
    }
]

# ─────────────────────────────────────────────────────────────────
# 3) DELETE_TOOLS
# ─────────────────────────────────────────────────────────────────
DELETE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información del consultorio"
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas por número de teléfono",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"}
                },
                "required": ["phone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Eliminar cita médica",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "original_start_time": {"type": "string", "format": "date-time"},
                    "patient_name": {"type": "string"}
                },
                "required": ["phone", "original_start_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detectar si el usuario quiere crear, editar, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intention": {
                        "type": "string",
                        "enum": ["create", "edit", "delete", "unknown"]
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
            "description": "Finalizar la llamada",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": [
                            "user_request", "silence", "spam", "time_limit", "error"
                        ]
                    }
                },
                "required": ["reason"]
            }
        }
    }
]


# ─────────────────────────────────────────────────────────────────
# Ejecutar las tools 
# ─────────────────────────────────────────────────────────────────
def handle_tool_execution(tool_call) -> Dict:
    function_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments or '{}')

    logger.info(f"🛠️ Ejecutando tool {function_name} con args: {args}")

    try:
        if function_name == "read_sheet_data":
            return {"data": get_consultorio_data_from_cache()}

        elif function_name == "find_next_available_slot":
            slot = find_next_available_slot(**args)
            return {"slot": slot}

        elif function_name == "create_calendar_event":
            phone = args.get("phone", "")
            if not (phone.isdigit() and len(phone) == 10):
                return {
                    "error": "El número de teléfono debe tener 10 dígitos numéricos."
                }
            event = create_calendar_event(**args)
            return {"event_created": event}

        elif function_name == "edit_calendar_event":
            result = edit_calendar_event(**args)
            return {"event_edited": result}

        elif function_name == "delete_calendar_event":
            result = delete_calendar_event(**args)
            return {"event_deleted": result}

        elif function_name == "search_calendar_event_by_phone":
            found = search_calendar_event_by_phone(**args)
            return {"search_results": found}

        elif function_name == "detect_intent":
            intention = args.get("intention")
            logger.info(f"🔎 Intención detectada: {intention}")
            return {"intent_detected": intention}

        elif function_name == "end_call":
            return {"call_ended": args["reason"]}

        else:
            return {"error": f"Herramienta desconocida: {function_name}"}

    except Exception as e:
        logger.error(f"❌ Error en tool {function_name}: {e}", exc_info=True)
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────────
# PROMPT PRINCIPAL (MAIN): SIEMPRE usa gpt-4o-mini
# ─────────────────────────────────────────────────────────────────
async def generate_openai_response_main(conversation_history: List[Dict], model="gpt-4.1-mini") -> str:
    """
    Usa prompt.py con MAIN_TOOLS.
    No hay banderas ni cambio a gpt-4o.
    """
    try:
        chosen_model = model  # gpt-4o-mini

        # Insertar prompt si no hay system
        if not any(msg["role"] == "system" for msg in conversation_history):
            conversation = generate_openai_prompt(conversation_history)
        else:
            conversation = list(conversation_history)

        logger.info("📤 (MAIN 1er PASE) Mensajes:")
        for i, m in enumerate(conversation):
            logger.info(f"[{i}] {m['role']} -> {m.get('content', '[SIN CONTENIDO]')[:200]}")


        first_response = client.chat.completions.create(
            model=chosen_model,
            messages=conversation,
            tools=MAIN_TOOLS,
            tool_choice="auto",
            max_tokens=200,
            temperature=0.3,
            timeout=10
        )

        assistant_msg = first_response.choices[0].message
        tool_calls = assistant_msg.tool_calls or []

        if tool_calls:
            for tc in tool_calls:
                logger.info(f"🛠️ [MAIN 1] IA llamó {tc.function.name} con args: {tc.function.arguments}")
        else:
            logger.info("🤖 [MAIN 1] Sin tools.")

        if not tool_calls:
            return assistant_msg.content or "Disculpe, no entendí su última frase."

        # Ejecutar tools
        tool_msgs = []
        for tc in tool_calls:
            result = handle_tool_execution(tc)
            if "call_ended" in result:
                return "__END_CALL__"
            if "error" in result:
                return f"Lo siento, ocurrió un error: {result['error']}"
            tool_msgs.append({
                "role": "tool",
                "content": json.dumps(result),
                "tool_call_id": tc.id
            })

        updated_msgs = conversation + [
            {
                "role": assistant_msg.role,
                "content": assistant_msg.content,
                "tool_calls": [t.model_dump() for t in assistant_msg.tool_calls]
            }
        ] + tool_msgs

        logger.info("📤 (MAIN 2do PASE) Mensajes:")
        for i, msg in enumerate(updated_msgs):
            logger.info(f"[{i}] {m['role']} -> {m.get('content', '[SIN CONTENIDO]')[:200]}")


        second_response = client.chat.completions.create(
            model=chosen_model,
            messages=updated_msgs,
            max_tokens=200,
            temperature=0.3,
            timeout=10
        )

        second_msg = second_response.choices[0].message
        second_tool_calls = second_msg.tool_calls or []

        if second_tool_calls:
            for tc2 in second_tool_calls:
                logger.info(f"🛠️ [MAIN 2] IA llamó {tc2.function.name} con args: {tc2.function.arguments}")
                if tc2.function.name == "detect_intent":
                    intent_args = json.loads(tc2.function.arguments or '{}')
                    intention = intent_args.get("intention")
                    if intention == "edit":
                        return await generate_openai_response_edit(updated_msgs, model)
                    elif intention == "delete":
                        return await generate_openai_response_delete(updated_msgs, model)
        else:
            logger.info("🤖 [MAIN 2] Sin tools.")

        return second_msg.content

    except Exception as e:
        logger.error(f"💥 Error en generate_openai_response_main: {e}", exc_info=True)
        return "Lo siento, ocurrió un error técnico en el prompt principal."


# ─────────────────────────────────────────────────────────────────
# EDIT: SIEMPRE gpt-4o-mini
# ─────────────────────────────────────────────────────────────────
async def generate_openai_response_edit(conversation_history: List[Dict], model="gpt-4.1-mini") -> str:
    """
    Usa prompt_editar_cita.py con EDIT_TOOLS.
    """
    from prompts.prompt_editar_cita import prompt_editar_cita
    try:
        chosen_model = model  

        if not any(msg["role"] == "system" for msg in conversation_history):
            conversation = prompt_editar_cita(conversation_history)
        else:
            conversation = list(conversation_history)

        logger.info("📤 (EDIT 1er PASE):")
        for i, m in enumerate(conversation):
            logger.info(f"[{i}] {m['role']} -> {m.get('content', '[SIN CONTENIDO]')[:200]}")


        first_response = client.chat.completions.create(
            model=chosen_model,
            messages=conversation,
            tools=EDIT_TOOLS,
            tool_choice="auto",
            max_tokens=200,
            temperature=0.3,
            timeout=10
        )
        assistant_msg = first_response.choices[0].message
        tool_calls = assistant_msg.tool_calls or []

        if tool_calls:
            for tc in tool_calls:
                logger.info(f"🛠️ [EDIT 1] {tc.function.name} args: {tc.function.arguments}")
        else:
            logger.info("🤖 [EDIT 1] Sin tools.")

        if not tool_calls:
            return assistant_msg.content

        tool_msgs = []
        for tc in tool_calls:
            result = handle_tool_execution(tc)
            if "call_ended" in result:
                return "__END_CALL__"
            if "error" in result:
                return f"Error al editar cita: {result['error']}"
            tool_msgs.append({
                "role": "tool",
                "content": json.dumps(result),
                "tool_call_id": tc.id
            })

        updated_msgs = conversation + [
            {
                "role": assistant_msg.role,
                "content": assistant_msg.content,
                "tool_calls": [t.model_dump() for t in assistant_msg.tool_calls]
            }
        ] + tool_msgs

        logger.info("📤 (EDIT 2do PASE):")
        for i, msg in enumerate(updated_msgs):
            logger.info(f"[{i}] {m['role']} -> {m.get('content', '[SIN CONTENIDO]')[:200]}")


        chosen_model_2 = model
        second_response = client.chat.completions.create(
            model=chosen_model_2,
            messages=updated_msgs,
            max_tokens=200,
            temperature=0.3,
            timeout=10
        )

        second_msg = second_response.choices[0].message
        second_tool_calls = second_msg.tool_calls or []

        if second_tool_calls:
            for tc2 in second_tool_calls:
                logger.info(f"🛠️ [EDIT 2] {tc2.function.name}, args: {tc2.function.arguments}")
                if tc2.function.name == "detect_intent":
                    intent_args = json.loads(tc2.function.arguments or '{}')
                    intention = intent_args.get("intention")
                    if intention == "create":
                        return await generate_openai_response_main(updated_msgs, model)
                    elif intention == "delete":
                        return await generate_openai_response_delete(updated_msgs, model)
        else:
            logger.info("🤖 [EDIT 2] Sin tools.")

        return second_msg.content

    except Exception as e:
        logger.error(f"💥 Error en generate_openai_response_edit: {e}", exc_info=True)
        return "Lo siento, ocurrió un error al editar la cita."


# ─────────────────────────────────────────────────────────────────
# DELETE: SIEMPRE gpt-4o-mini
# ─────────────────────────────────────────────────────────────────
async def generate_openai_response_delete(conversation_history: List[Dict], model="gpt-4.1-mini") -> str:
    """
    Usa prompt_eliminar_cita.py con DELETE_TOOLS.
    """
    from prompts.prompt_eliminar_cita import prompt_eliminar_cita
    try:
        chosen_model = model

        if not any(msg["role"] == "system" for msg in conversation_history):
            conversation = prompt_eliminar_cita(conversation_history)
        else:
            conversation = list(conversation_history)

        logger.info("📤 (DELETE 1er PASE):")
        for i, m in enumerate(conversation):
            logger.info(f"[{i}] {m['role']} -> {m.get('content', '[SIN CONTENIDO]')[:200]}")


        first_response = client.chat.completions.create(
            model=chosen_model,
            messages=conversation,
            tools=DELETE_TOOLS,
            tool_choice="auto",
            max_tokens=200,
            temperature=0.3,
            timeout=10
        )
        assistant_msg = first_response.choices[0].message
        tool_calls = assistant_msg.tool_calls or []

        if tool_calls:
            for tc in tool_calls:
                logger.info(f"🛠️ [DELETE 1] {tc.function.name} args: {tc.function.arguments}")
        else:
            logger.info("🤖 [DELETE 1] Sin tools.")

        if not tool_calls:
            return assistant_msg.content

        tool_msgs = []
        for tc in tool_calls:
            result = handle_tool_execution(tc)
            if "call_ended" in result:
                return "__END_CALL__"
            if "error" in result:
                return f"Error al eliminar cita: {result['error']}"
            tool_msgs.append({
                "role": "tool",
                "content": json.dumps(result),
                "tool_call_id": tc.id
            })

        updated_msgs = conversation + [
            {
                "role": assistant_msg.role,
                "content": assistant_msg.content,
                "tool_calls": [t.model_dump() for t in assistant_msg.tool_calls]
            }
        ] + tool_msgs

        logger.info("📤 (DELETE 2do PASE):")
        for i, msg in enumerate(updated_msgs):
            logger.info(f"[{i}] {m['role']} -> {m.get('content', '[SIN CONTENIDO]')[:200]}")


        chosen_model_2 = model
        second_response = client.chat.completions.create(
            model=chosen_model_2,
            messages=updated_msgs,
            max_tokens=200,
            temperature=0.3,
            timeout=10
        )

        second_msg = second_response.choices[0].message
        second_tool_calls = second_msg.tool_calls or []

        if second_tool_calls:
            for tc2 in second_tool_calls:
                logger.info(f"🛠️ [DELETE 2] {tc2.function.name}, args: {tc2.function.arguments}")
                if tc2.function.name == "detect_intent":
                    intent_args = json.loads(tc2.function.arguments or '{}')
                    intention = intent_args.get("intention")
                    if intention == "create":
                        return await generate_openai_response_main(updated_msgs, model)
                    elif intention == "edit":
                        return await generate_openai_response_edit(updated_msgs, model)
        else:
            logger.info("🤖 [DELETE 2] Sin tools.")

        return second_msg.content

    except Exception as e:
        logger.error(f"💥 Error en generate_openai_response_delete: {e}", exc_info=True)
        return "Lo siento, ocurrió un error al eliminar la cita."


# Aliases si quieres uno genérico
generate_openai_response = generate_openai_response_main
