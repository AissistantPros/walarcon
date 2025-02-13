import logging
import time
import json
from decouple import config
from openai import OpenAI
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from eliminarcita import delete_calendar_event
from editarcita import edit_calendar_event
from utils import search_calendar_event_by_phone  # Importar la búsqueda de citas
from prompt import generate_openai_prompt  # Importar la función del prompt
from datetime import datetime, timedelta
import pytz

# Configuración de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicialización del cliente OpenAI
client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

# ==================================================
# 🔹 Definición de herramientas disponibles
# ==================================================

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
            "description": "Buscar el siguiente horario disponible",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {"type": "string", "format": "date"},
                    "target_hour": {"type": "string", "format": "time"},
                    "urgent": {"type": "boolean"}
                },
                "required": ["target_date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_availability",
            "description": "Verificar si un horario específico está disponible en Google Calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_time": {"type": "string", "format": "date-time"},
                    "end_time": {"type": "string", "format": "date-time"}
                },
                "required": ["start_time", "end_time"]
            }
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
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar una cita por número de teléfono en Google Calendar",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"}
                },
                "required": ["phone"]
            }
        }
    }
]

# ==================================================
# 🔹 Generación de respuestas con OpenAI
# ==================================================

def generate_openai_response(conversation_history: list):
    """Procesa la conversación y genera una respuesta usando GPT-4o-mini"""
    try:
        start_time = time.time()
        logger.info("Generando respuesta con OpenAI...")

        # 📌 Asegurar que el prompt del sistema esté en la conversación
        if not any(msg["role"] == "system" for msg in conversation_history):
            conversation_history = generate_openai_prompt(conversation_history)

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conversation_history,
            tools=TOOLS,
            tool_choice="auto",
        )

        tool_calls = response.choices[0].message.tool_calls
        if tool_calls:
            tool_call = tool_calls[0]
            tool_result = handle_tool_execution(tool_call, conversation_history)

            # Agregar los datos obtenidos al historial de la conversación
            conversation_history.append({
                "role": "function",
                "name": tool_call.function.name,
                "content": json.dumps(tool_result)
            })

            # Hacer una nueva llamada a OpenAI con el historial actualizado
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=conversation_history,
            )

        ai_response = response.choices[0].message.content
        logger.info(f"🗣️ Respuesta generada para el usuario: {ai_response}")
        logger.info(f"Respuesta generada en {time.time() - start_time:.2f}s")
        return ai_response

    except Exception as e:
        logger.error(f"Error en OpenAI: {str(e)}")
        return "Lo siento, estoy teniendo dificultades técnicas. ¿Podría repetir su pregunta?"

# ==================================================
# 🔹 Manejo de herramientas de OpenAI
# ==================================================

def handle_tool_execution(tool_call, conversation_history):
    """Ejecuta la herramienta solicitada por OpenAI y devuelve datos crudos."""
    function_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)

    logger.info(f"🛠️ Ejecutando herramienta: {function_name} con argumentos {json.dumps(args, indent=2)}")

    try:
        if function_name == "create_calendar_event":
            start_time = datetime.fromisoformat(args["start_time"])
            end_time = start_time + timedelta(minutes=45)  # 🔹 Asegurar que end_time siempre sea +45 min

            result = create_calendar_event(
                args["name"], args["phone"], args.get("reason", "No especificado"),
                start_time.isoformat(), end_time.isoformat()
            )
            return {"event": result}
        
        return {"error": "No entendí esa solicitud."}

    except Exception as e:
        logger.error(f"❌ Error ejecutando herramienta: {str(e)}")
        return {"error": "Hubo un error técnico al procesar tu solicitud."}
