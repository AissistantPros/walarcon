from openai import OpenAI
from decouple import config
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from eliminarcita import delete_calendar_event
from editarcita import edit_calendar_event
from prompt import generate_openai_prompt
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

def generate_openai_response(conversation_history: list):
    try:
        start_time = time.time()
        
        messages = generate_openai_prompt(conversation_history)
        
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_sheet_data",
                        "description": "Obtiene información del consultorio."
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "find_next_available_slot",
                        "description": "Busca horarios disponibles para citas."
                    }
                },
                {
                    "type": "function",
                    "function": {
                        "name": "create_calendar_event",
                        "description": "Agenda una nueva cita.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "phone": {"type": "string"},
                                "start_time": {"type": "string", "format": "date-time"},
                                "end_time": {"type": "string", "format": "date-time"}
                            },
                            "required": ["name", "phone", "start_time", "end_time"]
                        }
                    }
                }
            ],
            tool_choice="auto",
        )
        
        # Manejo robusto de respuesta
        if not response.choices:
            raise ValueError("No se recibieron opciones en la respuesta")
            
        ai_response = response.choices[0].message.content
        
        if not ai_response:
            raise ValueError("Respuesta vacía de OpenAI")
            
        ai_response = ai_response.strip()
        
        if "[END_CALL]" in ai_response:
            logger.info("🛑 IA solicitó finalizar llamada")
            
        logger.info(f"🤖 OpenAI respondió en {time.time() - start_time:.2f}s")
        return ai_response
        
    except Exception as e:
        logger.error(f"❌ Error en OpenAI: {str(e)}")
        logger.debug(f"Conversación al error: {conversation_history}")
        return "Lo siento, hubo un error. ¿Podría repetir su solicitud?"