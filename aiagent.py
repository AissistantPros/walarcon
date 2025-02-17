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
"""
Las herramientas que siempre tienen que estar disponibles son:
1. Cómo buscar información de consultarinfo.py: "def read_sheet_data(sheet_range="Generales!A:B"):"
2. Encontrar una fecha y hora específica para la cita, de buscarslot.py: "check_availability(start_time, end_time)"
3. Encontrar una fecha y hora relativa de buscarslot.py: "def find_next_available_slot(target_date=None, target_hour=None, urgent=False):"
4. Crear una nueva cita de crearcita.py: "def create_calendar_event(name, phone, reason, start_time, end_time):"
5. Buscar una cita previamente hecha de utils.py "def search_calendar_event_by_phone(phone, name=None):".
6. Editar una cita previamente hecha, de editarcita.py "def edit_calendar_event(phone, new_start_time=None, new_end_time=None)."
7. Eliminar una cita previamente hecha, de eliminarcita.py "def delete_calendar_event(phone, patient_name=None):".
8. Terminar la llamada de utils.py "async def end_call(response, reason=""):".

NOTA: El 'end_call' se maneja diferente, porque necesita un 'response' de Twilio. Pero igual
lo listamos para que la IA sepa que existe.
"""

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
             "name": "end_call",
             "description": "Finaliza la llamada con un mensaje de despedida adecuado.",
             "parameters": {
                 "type": "object",
                 "properties": {
                     "reason": {
                         "type": "string",
                         "enum": ["silence", "user_request", "spam", "time_limit"],
                         "description": "Razón para finalizar la llamada."
                     }
                 },
                 "required": ["reason"]
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

        # Asegurar que el prompt del sistema esté en la conversación
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

            # Agregar los datos obtenidos al historial
            conversation_history.append({
                "role": "function",
                "name": tool_call.function.name,
                "content": json.dumps(tool_result)
            })

            # Nueva llamada a OpenAI con el historial actualizado
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
        # 1) read_sheet_data
        if function_name == "read_sheet_data":
            data = read_sheet_data()
            return {"data": data}

        # 2) find_next_available_slot
        elif function_name == "find_next_available_slot":
            target_date_str = args.get("target_date")
            target_hour = args.get("target_hour")
            urgent = args.get("urgent", False)

            # Convertir target_date_str (YYYY-MM-DD) a datetime si existe
            if target_date_str:
                dt = datetime.strptime(target_date_str, "%Y-%m-%d")
                dt = dt.replace(tzinfo=pytz.timezone("America/Cancun"))
                result = find_next_available_slot(target_date=dt, target_hour=target_hour, urgent=urgent)
            else:
                # Si no hay target_date, se pasa None
                result = find_next_available_slot(target_date=None, target_hour=target_hour, urgent=urgent)
            return {"slot": result}


        # 3) create_calendar_event
        elif function_name == "create_calendar_event":
            start_time_dt = datetime.fromisoformat(args["start_time"])
            end_time_dt = datetime.fromisoformat(args["end_time"])

            result = create_calendar_event(
                args["name"],
                args["phone"],
                args.get("reason", "No especificado"),
                start_time_dt.isoformat(),
                end_time_dt.isoformat()
            )
            return {"event": result}

        # 4) edit_calendar_event
        elif function_name == "edit_calendar_event":
            phone = args["phone"]
            original_start_time = datetime.fromisoformat(args["original_start_time"])

            new_start = args.get("new_start_time")
            new_end = args.get("new_end_time")

            if new_start:
                new_start = datetime.fromisoformat(new_start)
            if new_end:
                new_end = datetime.fromisoformat(new_end)

            result = edit_calendar_event(
                phone=phone,
                original_start_time=original_start_time,
                new_start_time=new_start,
                new_end_time=new_end
            )
            return {"event": result}

        # 5) delete_calendar_event
        elif function_name == "delete_calendar_event":
            phone = args["phone"]
            patient_name = args.get("patient_name", None)
            result = delete_calendar_event(phone, patient_name)
            return {"event": result}

        # 6) end_call
        elif function_name == "end_call":
            # La IA está solicitando terminar la llamada.
            # Normalmente esto requiere un "response" de Twilio, así que
            # en tw_utils debemos interpretar esta respuesta final.
            reason = args["reason"]
            return {"end_call": reason}

        # 7) search_calendar_event_by_phone
        elif function_name == "search_calendar_event_by_phone":
            phone = args["phone"]
            # name es opcional, a veces la IA puede enviar "name" para filtrar
            name = args.get("name")
            result = search_calendar_event_by_phone(phone, name)
            return {"event": result}

        # Si la herramienta no se reconoce:
        else:
            return {"error": "No entendí esa solicitud."}

    except Exception as e:
        logger.error(f"❌ Error ejecutando herramienta: {str(e)}")
        return {"error": "Hubo un error técnico al procesar tu solicitud."}
