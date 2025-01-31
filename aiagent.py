from openai import OpenAI
from decouple import config
from consultarinfo import read_sheet_data
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from eliminarcita import delete_calendar_event
from editarcita import edit_calendar_event
from utils import get_cancun_time  # Asegura la referencia de la hora actual en Cancún
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
        
        # Agregar referencia de hora de Cancún al contexto
        cancun_time = get_cancun_time().strftime("%Y-%m-%d %H:%M:%S")
        conversation_history.insert(0, {"role": "system", "content": f"La hora actual en Cancún es: {cancun_time}"})
        
        # Generar prompt con contexto
        messages = generate_openai_prompt(conversation_history)
        
        # Llamar a OpenAI
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=[
                {"type": "function", "function": {"name": "read_sheet_data", "description": "Obtiene información del consultorio."}},
                {"type": "function", "function": {"name": "find_next_available_slot", "description": "Busca el próximo horario disponible para citas."}},
                {"type": "function", "function": {"name": "create_calendar_event", "description": "Agenda una cita.", "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "phone": {"type": "string"}, "start_time": {"type": "string", "format": "date-time"}, "end_time": {"type": "string", "format": "date-time"}}, "required": ["name", "phone", "start_time", "end_time"]}}},
                {"type": "function", "function": {"name": "delete_calendar_event", "description": "Cancela una cita existente.", "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}, "required": ["phone"]}}},
                {"type": "function", "function": {"name": "edit_calendar_event", "description": "Reagenda una cita.", "parameters": {"type": "object", "properties": {"phone": {"type": "string"}, "original_start_time": {"type": "string", "format": "date-time"}, "new_start_time": {"type": "string", "format": "date-time"}, "new_end_time": {"type": "string", "format": "date-time"}}, "required": ["phone", "original_start_time"]}}}
            ],
            tool_choice="auto",
        )
        
        # Obtener la respuesta de la IA
        ai_response = response.choices[0].message.content.strip()
        
        # Verificar si la IA indica que debe finalizar la llamada
        if "[END_CALL]" in ai_response:
            logger.info("🛑 IA solicitó finalizar llamada")
            return ai_response.replace("[END_CALL]", "").strip()
        
        logger.info(f"🤖 OpenAI respondió en {time.time() - start_time:.2f}s")
        return ai_response
        
    except Exception as e:
        logger.error(f"❌ Error en OpenAI: {str(e)}")
        return "Lo siento, hubo un error procesando tu solicitud."
