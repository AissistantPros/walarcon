# -*- coding: utf-8 -*-
"""
Módulo de IA con herramientas completas y manejo robusto de funciones.
"""

import logging
import time
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

# Inicialización del cliente OpenAI
client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

# ==================================================
# 🔹 Herramientas Disponibles (Actualizadas)
# ==================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información general del consultorio (precios, horarios, ubicación)",
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
                    "target_date": {"type": "string", "format": "date", "description": "Fecha objetivo en formato YYYY-MM-DD"},
                    "target_hour": {"type": "string", "description": "Hora objetivo en formato HH:MM"},
                    "urgent": {"type": "boolean", "description": "Si es una solicitud urgente"}
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
                    "phone": {"type": "string", "description": "Número de teléfono con WhatsApp (10 dígitos)"},
                    "reason": {"type": "string", "description": "Motivo de la consulta"},
                    "start_time": {"type": "string", "format": "date-time", "description": "Fecha y hora de inicio en ISO8601"},
                    "end_time": {"type": "string", "format": "date-time", "description": "Fecha y hora de fin en ISO8601"}
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
                    "original_start_time": {"type": "string", "format": "date-time", "description": "Fecha/hora original de la cita"},
                    "new_start_time": {"type": "string", "format": "date-time", "description": "Nueva fecha/hora de inicio"},
                    "new_end_time": {"type": "string", "format": "date-time", "description": "Nueva fecha/hora de fin"}
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
                    "patient_name": {"type": "string", "description": "Nombre del paciente (opcional para confirmación)"}
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
# 🔹 Manejador de Herramientas (Completo)
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
# 🔹 Generación de Respuestas (Versión Final)
# ==================================================
def generate_openai_response(conversation_history: List[Dict]) -> str:
    try:
        # Añadir prompt del sistema si falta
        if not any(msg["role"] == "system" for msg in conversation_history):
            from prompt import generate_openai_prompt
            conversation_history = generate_openai_prompt(conversation_history)

        # Primera llamada a OpenAI
        first_response = client.chat.completions.create(
            model="gpt-3.5-turbo-1106",
            messages=conversation_history,
            tools=TOOLS,
            tool_choice="auto",
        )

        # Procesar tool calls
        tool_messages = []
        for tool_call in first_response.choices[0].message.tool_calls or []:
            result = handle_tool_execution(tool_call)
            tool_messages.append({
                "role": "tool",
                "content": json.dumps(result),
                "tool_call_id": tool_call.id
            })

        if not tool_messages:
            return first_response.choices[0].message.content

        # Segunda llamada con resultados
        second_response = client.chat.completions.create(
            model="gpt-3.5-turbo-1106",
            messages=conversation_history + tool_messages,
        )

        return second_response.choices[0].message.content

    except Exception as e:
        logger.error(f"💣 Error crítico: {str(e)}")
        return "Disculpe, estoy teniendo dificultades técnicas. Por favor intente nuevamente."