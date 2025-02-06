# -*- coding: utf-8 -*-
"""
Módulo principal del agente de IA - Dr. Alarcón IVR System
Procesa entradas del usuario y gestiona integraciones con APIs externas.
"""

import logging
import time
import asyncio
import json
from decouple import config
from openai import OpenAI
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from eliminarcita import delete_calendar_event
from editarcita import edit_calendar_event

# Configuración de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicialización del cliente OpenAI
client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

# Definición de herramientas disponibles
TOOLS = [
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
            "description": "Buscar el siguiente horario disponible"
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear una nueva cita médica",
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
            "description": "Modificar una cita existente",
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
            "description": "Eliminar una cita médica",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "patient_name": {"type": "string"}
                },
                "required": ["phone"]
            }
        }
    }
]

# Generación de respuestas con OpenAI
def generate_openai_response(conversation_history: list):
    """Procesa la conversación y genera una respuesta usando GPT-4o"""
    try:
        start_time = time.time()
        logger.info("Generando respuesta con OpenAI...")

        # Llamada a OpenAI
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=conversation_history,
            tools=TOOLS,
            tool_choice="auto",
        )

        # Manejo de herramientas (si OpenAI sugiere una)
        tool_calls = response.choices[0].message.tool_calls
        if tool_calls:
            tool_call = tool_calls[0]
            return handle_tool_execution(tool_call)

        # Respuesta directa de la IA
        ai_response = response.choices[0].message.content
        logger.info(f"Respuesta generada en {time.time() - start_time:.2f}s")
        return ai_response

    except Exception as e:
        logger.error(f"Error en OpenAI: {str(e)}")
        return "Lo siento, estoy teniendo dificultades técnicas. ¿Podría repetir su pregunta?"

# Ejecución de herramientas solicitadas por OpenAI
def handle_tool_execution(tool_call):
    """Ejecuta la herramienta solicitada por OpenAI"""
    function_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)  # Parsea los argumentos en formato JSON

    logger.info(f"🛠️ Ejecutando herramienta: {function_name} con argumentos {args}")

    try:
        # 🔹 Obtener información desde Google Sheets
        if function_name == "read_sheet_data":
            data = read_sheet_data()
            if not data:
                return {"error": "No pude obtener la información en este momento."}

            # ⏩ En lugar de armar una respuesta fija, devolvemos los datos tal cual
            return {"data": data}

        # 🔹 Buscar el siguiente horario disponible
        elif function_name == "find_next_available_slot":
            slot = find_next_available_slot()
            if not slot:
                return {"message": "No hay horarios disponibles en este momento."}
            return {"message": f"El siguiente horario disponible es el {slot['start_time']}."}

        # 🔹 Crear una nueva cita en el calendario
        elif function_name == "create_calendar_event":
            event = create_calendar_event(
                args["name"],
                args["phone"],
                args.get("reason", "No especificado"),
                args["start_time"],
                args["end_time"]
            )
            return {"message": f"Tu cita ha sido agendada para el {event['start']}."}

        # 🔹 Editar una cita existente
        elif function_name == "edit_calendar_event":
            result = edit_calendar_event(
                args["phone"],
                args["original_start_time"],
                args.get("new_start_time"),
                args.get("new_end_time")
            )
            return {"message": f"Tu cita ha sido modificada. La nueva fecha es {result['start']}."}

        # 🔹 Eliminar una cita existente
        elif function_name == "delete_calendar_event":
            result = delete_calendar_event(args["phone"], args.get("patient_name"))
            return {"message": f"La cita ha sido cancelada. {result['message']}."}

        return {"error": "No entendí esa solicitud."}

    except ConnectionError as e:
        return {"error": format_error_response(str(e))}
    except Exception as e:
        logger.error(f"❌ Error ejecutando herramienta: {str(e)}")
        return {"error": "Hubo un error técnico al procesar tu solicitud."}

    except ConnectionError as e:
        return format_error_response(str(e))
    except Exception as e:
        logger.error(f"Error ejecutando herramienta: {str(e)}")
        return "Hubo un error técnico."

# Manejo de errores
def format_error_response(error_code: str) -> str:
    """Convierte códigos de error técnicos en mensajes amigables"""
    error_messages = {
        "GOOGLE_SHEETS_UNAVAILABLE": "No puedo acceder a la base de datos en este momento.",
        "GOOGLE_CALENDAR_UNAVAILABLE": "El sistema de citas no está disponible.",
        "DEFAULT": "Ocurrió un error técnico. Por favor, intenta de nuevo."
    }
    return error_messages.get(error_code, error_messages["DEFAULT"])