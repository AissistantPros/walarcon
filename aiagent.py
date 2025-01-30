from openai import OpenAI
from decouple import config
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from eliminarcita import delete_calendar_event
from editarcita import edit_calendar_event
import logging
import time
from datetime import datetime
from prompt import generate_openai_prompt

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

def generate_openai_response(conversation_history: list):
    try:
        start_time = time.time()
        
        # Definir todas las herramientas
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "read_sheet_data",
                    "description": "Obtiene información del consultorio: horarios, servicios y datos del doctor.",
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "find_next_available_slot",
                    "description": "Busca el próximo horario disponible para citas.",
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "create_calendar_event",
                    "description": "Agenda una cita. Requiere: nombre, teléfono, fecha/hora de inicio y fin.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "phone": {"type": "string"},
                            "start_time": {"type": "string", "format": "date-time"},
                            "end_time": {"type": "string", "format": "date-time"},
                        },
                        "required": ["name", "phone", "start_time", "end_time"]
                    }
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "delete_calendar_event",
                    "description": "Cancela una cita existente. Requiere: teléfono del paciente.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "phone": {"type": "string"}
                        },
                        "required": ["phone"]
                    }
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "edit_calendar_event",
                    "description": "Reagenda una cita. Requiere: teléfono, fecha original y nueva fecha.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "phone": {"type": "string"},
                            "original_start_time": {"type": "string", "format": "date-time"},
                            "new_start_time": {"type": "string", "format": "date-time"},
                            "new_end_time": {"type": "string", "format": "date-time"},
                        },
                        "required": ["phone", "original_start_time"]
                    }
                },
            }
        ]

        # Generar prompt con contexto
        messages = generate_openai_prompt(conversation_history)
        
        # Llamar a OpenAI
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
        
        # Manejar llamadas a herramientas
        if response.choices[0].message.tool_calls:
            tool_call = response.choices[0].message.tool_calls[0]
            function_name = tool_call.function.name
            args = eval(tool_call.function.arguments)
            
            if function_name == "read_sheet_data":
                logger.info("📊 Leyendo datos de Google Sheets...")
                data = read_sheet_data()
                return f"**Información del consultorio**: {data.get('servicios', 'No disponible')}. Horarios: {data.get('horarios', 'No disponible')}."
            
            elif function_name == "find_next_available_slot":
                logger.info("📅 Buscando horario disponible...")
                slot = find_next_available_slot()
                return f"Próximo horario disponible: {slot['start_time'].strftime('%d/%m/%Y a las %H:%M')}."
            
            elif function_name == "create_calendar_event":
                logger.info("➕ Agendando cita...")
                event = create_calendar_event(
                    name=args["name"],
                    phone=args["phone"],
                    start_time=datetime.fromisoformat(args["start_time"]),
                    end_time=datetime.fromisoformat(args["end_time"])
                )
                return f"✅ Cita agendada para {event['start']}."
            
            elif function_name == "delete_calendar_event":
                logger.info("➖ Cancelando cita...")
                result = delete_calendar_event(phone=args["phone"])
                return f"✅ Cita cancelada: {result}."
            
            elif function_name == "edit_calendar_event":
                logger.info("🔄 Reagendando cita...")
                result = edit_calendar_event(
                    phone=args["phone"],
                    original_start_time=datetime.fromisoformat(args["original_start_time"]),
                    new_start_time=datetime.fromisoformat(args["new_start_time"]) if args.get("new_start_time") else None,
                    new_end_time=datetime.fromisoformat(args["new_end_time"]) if args.get("new_end_time") else None
                )
                return f"✅ Cita actualizada: {result}."
        
        # Respuesta estándar
        logger.info(f"🤖 OpenAI respondió en {time.time() - start_time:.2f}s")
        return response.choices[0].message.content.strip()
        
    except Exception as e:
        logger.error(f"❌ Error en OpenAI: {str(e)}")
        return "Lo siento, hubo un error procesando tu solicitud."