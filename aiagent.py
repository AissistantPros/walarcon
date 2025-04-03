# aiagent.py

# -*- coding: utf-8 -*-

import logging
import time
import json
from typing import List, Dict
from decouple import config
from openai import OpenAI
from consultarinfo import get_consultorio_data_from_cache
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from eliminarcita import delete_calendar_event
from editarcita import edit_calendar_event
from utils import search_calendar_event_by_phone, get_cancun_time
from datetime import datetime, timedelta
import pytz
from prompt import generate_openai_prompt
from prompts.prompt_crear_cita import prompt_crear_cita
from prompts.prompt_editar_cita import prompt_editar_cita
from prompts.prompt_eliminar_cita import prompt_eliminar_cita

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

def get_next_monday(reference_date: datetime) -> datetime:
    ref = reference_date
    while ref.weekday() != 0:
        ref += timedelta(days=1)
    if ref.date() == reference_date.date():
        ref += timedelta(days=7)
    return ref

def generar_system_message_resumen_fecha(original_date_str, original_hour_str, result_date_str, result_hour_str):
    dias_semana = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo"
    }
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    try:
        original_dt = datetime.strptime(f"{original_date_str} {original_hour_str}", "%Y-%m-%d %H:%M")
        result_dt = datetime.strptime(f"{result_date_str} {result_hour_str}", "%Y-%m-%d %H:%M")
        dia_orig = dias_semana[original_dt.weekday()]
        dia_disp = dias_semana[result_dt.weekday()]
        mes_orig = meses[original_dt.month]
        mes_disp = meses[result_dt.month]
        fecha_inicio = f"{dia_orig} {original_dt.day} de {mes_orig} del {original_dt.year} a las {original_dt.strftime('%H:%M')}"
        fecha_resultado = f"{dia_disp} {result_dt.day} de {mes_disp} del {result_dt.year} a las {result_dt.strftime('%H:%M')}"
        return {
            "role": "system",
            "content": (
                f"Se comenzó buscando desde el {fecha_inicio}. "
                f"El sistema encontró espacio el {fecha_resultado}. Explica esto al usuario con claridad, sin inventar intenciones."
            )
        }
    except Exception as e:
        logger.warning(f"No se pudo generar system_message de fechas: {e}")
        return None

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información general del consultorio"
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
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas por número",
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
            "name": "get_cancun_time",
            "description": "Fecha y hora actual en Cancún",
            "parameters": {"type": "object", "properties": {}}
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
                        "enum": ["user_request", "silence", "spam", "time_limit", "error"]
                    }
                },
                "required": ["reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detectar intención del usuario",
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
    }
]

def handle_tool_execution(tool_call) -> Dict:
    function_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    logger.info(f"🛠️ Ejecutando {function_name} con: {args}")

    try:
        if function_name == "read_sheet_data":
            return {"data": get_consultorio_data_from_cache()}
        elif function_name == "find_next_available_slot":
            return {"slot": find_next_available_slot(**args)}
        elif function_name == "create_calendar_event":
            return {"event_created": create_calendar_event(**args)}
        elif function_name == "edit_calendar_event":
            return {"event_edited": edit_calendar_event(**args)}
        elif function_name == "delete_calendar_event":
            return {"event_deleted": delete_calendar_event(**args)}
        elif function_name == "search_calendar_event_by_phone":
            return {"search_results": search_calendar_event_by_phone(**args)}
        elif function_name == "get_cancun_time":
            return {"cancun_time": get_cancun_time().isoformat()}
        elif function_name == "end_call":
            return {"call_ended": args["reason"]}
        elif function_name == "detect_intent":
            return {"intent_detected": args["intention"]}
    except Exception as e:
        logger.error(f"❌ Error ejecutando tool {function_name}: {e}")
        return {"error": str(e)}

async def generate_openai_response(conversation_history: List[Dict], model="gpt-4o-mini") -> str:
    try:
        last_user_msg = conversation_history[-1]["content"]

        # Step 1: Detect intent
        intent_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": last_user_msg}],
            tools=[tool for tool in TOOLS if tool["function"]["name"] == "detect_intent"],
            tool_choice="auto",
            max_tokens=10,
            temperature=0
        )

        intent_tool_call = intent_response.choices[0].message.tool_calls[0]
        intent = json.loads(intent_tool_call.function.arguments)["intention"]
        logger.info(f"💡 Intención detectada: {intent}")

        if intent == "create":
            conversation = prompt_crear_cita(conversation_history)
        elif intent == "edit":
            conversation = prompt_editar_cita(conversation_history)
        elif intent == "delete":
            conversation = prompt_eliminar_cita(conversation_history)
        else:
            conversation = conversation_history

        if not any(msg["role"] == "system" for msg in conversation):
            conversation = generate_openai_prompt(conversation)

        logger.info("📤 Enviando mensajes a GPT (1er request):")
        for i, msg in enumerate(conversation):
            logger.info(f"[{i}] {msg['role']} → {msg['content'][:150]}")

        first_response = client.chat.completions.create(
            model=model,
            messages=conversation,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=150,
            temperature=0.3,
            timeout=10
        )

        tool_calls = first_response.choices[0].message.tool_calls
        if not tool_calls:
            return first_response.choices[0].message.content

        tool_messages = []
        for tool_call in tool_calls:
            result = handle_tool_execution(tool_call)
            if result.get("status") == "__END_CALL__":
                return "__END_CALL__"
            tool_messages.append({
                "role": "tool",
                "content": json.dumps(result),
                "tool_call_id": tool_call.id
            })

        updated_messages = conversation + [first_response.choices[0].message] + tool_messages

        logger.info("📤 Enviando mensajes a GPT (2do request):")
        for i, msg in enumerate(updated_messages):
            logger.info(f"[{i}] {msg['role']} → {msg['content'][:150]}")

        second_response = client.chat.completions.create(
            model=model,
            messages=updated_messages,
            max_tokens=150,
            temperature=0.3,
            timeout=10
        )

        return second_response.choices[0].message.content

    except Exception as e:
        logger.error(f"💥 Error crítico en generate_openai_response: {e}")
        return "Disculpe, estoy teniendo dificultades técnicas. Por favor intente nuevamente."
