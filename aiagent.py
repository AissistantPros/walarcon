#aiagent.py

# -*- coding: utf-8 -*-

import logging
import json
from typing import List, Dict
from decouple import config
from openai import OpenAI
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from eliminarcita import delete_calendar_event
from editarcita import edit_calendar_event
from utils import search_calendar_event_by_phone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

# ==================================================
# 🔹 Herramientas Disponibles (Actualizadas)
# ==================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información general del consultorio (precios, horarios, ubicación)"
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_next_available_slot",
            "description": "Buscar próximo horario disponible para citas",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {"type": "string", "format": "date", "description": "Fecha objetivo YYYY-MM-DD"},
                    "target_hour": {"type": "string", "description": "Hora objetivo HH:MM"},
                    "urgent": {"type": "boolean", "description": "Indica si es urgencia"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear nueva cita médica",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nombre completo del paciente"},
                    "phone": {"type": "string", "description": "Número de teléfono (10 dígitos)"},
                    "reason": {"type": "string", "description": "Motivo de la consulta"},
                    "start_time": {"type": "string", "format": "date-time", "description": "Fecha/hora inicio (ISO8601)"},
                    "end_time": {"type": "string", "format": "date-time", "description": "Fecha/hora fin (ISO8601)"}
                },
                "required": ["name", "phone", "start_time", "end_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_calendar_event",
            "description": "Modificar cita existente",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Número de teléfono registrado"},
                    "original_start_time": {"type": "string", "format": "date-time", "description": "Fecha/hora original"},
                    "new_start_time": {"type": "string", "format": "date-time", "description": "Nueva fecha/hora inicio"},
                    "new_end_time": {"type": "string", "format": "date-time", "description": "Nueva fecha/hora fin"}
                },
                "required": ["phone", "original_start_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Eliminar una cita médica",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string", "description": "Número de teléfono registrado"},
                    "patient_name": {"type": "string", "description": "Nombre (opcional)"}
                },
                "required": ["phone"]
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
                    "phone": {"type": "string", "description": "Número de teléfono a buscar"}
                },
                "required": ["phone"]
            }
        }
    }
]

# ==================================================
# 🔹 Manejador de Herramientas
# ==================================================
def handle_tool_execution(tool_call) -> Dict:
    function_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    logger.info(f"🛠️ Ejecutando {function_name} con: {args}")

    try:
        if function_name == "read_sheet_data":
            return {"data": read_sheet_data()}

        elif function_name == "find_next_available_slot":
            return {"slot": find_next_available_slot(
                target_date=args.get("target_date"),
                target_hour=args.get("target_hour"),
                urgent=args.get("urgent", False)
            )}

        elif function_name == "create_calendar_event":
            return {"event": create_calendar_event(
                args["name"],
                args["phone"],
                args.get("reason", "Consulta general"),
                args["start_time"],
                args["end_time"]
            )}

        elif function_name == "edit_calendar_event":
            return {"event": edit_calendar_event(
                args["phone"],
                args["original_start_time"],
                args.get("new_start_time"),
                args.get("new_end_time")
            )}

        elif function_name == "delete_calendar_event":
            return {"status": delete_calendar_event(
                args["phone"],
                args.get("patient_name")
            )}

        elif function_name == "search_calendar_event_by_phone":
            return {"events": search_calendar_event_by_phone(args["phone"])}

        else:
            logger.error(f"❌ Función no reconocida: {function_name}")
            return {"error": "Función no implementada"}

    except Exception as e:
        logger.error(f"💥 Error en {function_name}: {str(e)}")
        return {"error": f"No se pudo ejecutar {function_name}"}

# ==================================================
# 🔹 Generación de Respuestas
# ==================================================
def generate_openai_response(conversation_history: List[Dict]) -> str:
    """
    conversation_history: lista de dict con mensajes:
      [{role: "system", content: "..."},
       {role: "user", content: "..."} ...]
    """
    from prompt import generate_openai_prompt  # asumas que existe, ajusta según tu caso
    try:
        # Si no hay un "system" prompt aún, lo generamos
        if not any(msg["role"] == "system" for msg in conversation_history):
            conversation_history = generate_openai_prompt(conversation_history)

        # Primer request a GPT (posible invocación de herramientas)
        first_response = client.chat.completions.create(
            model="gpt-3.5-turbo-0125",
            messages=conversation_history,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=150,
            temperature=0.3,
            timeout=10
        )

        tool_messages = []
        for tool_call in first_response.choices[0].message.tool_calls or []:
            result = handle_tool_execution(tool_call)
            tool_messages.append({
                "role": "tool",
                "content": json.dumps(result),
                "tool_call_id": tool_call.id
            })

        # Si no invoca herramientas, devolvemos su texto
        if not tool_messages:
            return first_response.choices[0].message.content

        # GPT invocó herramientas: segundo request con sus resultados
        second_response = client.chat.completions.create(
            model="gpt-3.5-turbo-0125",
            messages=conversation_history + tool_messages
        )
        return second_response.choices[0].message.content

    except Exception as e:
        logger.error(f"💣 Error crítico: {str(e)}")
        return "Disculpe, tuve un problema técnico. Por favor, intente nuevamente."
