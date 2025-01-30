from twilio.twiml.voice_response import VoiceResponse, Gather
from aiagent import generate_openai_response
from audio_utils import generate_audio_with_eleven_labs
from prompt import generate_openai_prompt
import logging
import time
import os
import asyncio  # Nueva importación

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"
conversation_history = []
call_start_time = None

def handle_twilio_call(gather_action: str):
    global call_start_time
    call_start_time = time.time()
    response = VoiceResponse()
    
    try:
        greeting_message = "Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        audio_buffer = generate_audio_with_eleven_labs(greeting_message)
        
        if audio_buffer:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio_buffer.getvalue())
            response.play("/audio-response")
        else:
            response.say(greeting_message)
        
        gather = Gather(
            input="speech",
            action=gather_action,
            method="POST",
            timeout=5,
            language="es-MX"
        )
        response.append(gather)
        
    except Exception as e:
        logger.error(f"Error en saludo: {str(e)}")
        response.say("Bienvenido. Estamos teniendo problemas técnicos.")
    
    return str(response)

async def process_user_input(user_input: str):  # Ahora es async
    global conversation_history, call_start_time
    response = VoiceResponse()
    
    try:
        response.say("Un momento, por favor...", voice="alice", language="es-MX")
        
        # Paso 1: Generar respuesta IA (síncrono)
        conversation_history.append({"role": "user", "content": user_input})
        ai_response = generate_openai_response(conversation_history)
        conversation_history.append({"role": "assistant", "content": ai_response})
        
        # Paso 2: Generar audio en paralelo (asíncrono)
        audio_buffer = await asyncio.to_thread(  # Cambio clave
            generate_audio_with_eleven_labs, 
            ai_response
        )
        
        if audio_buffer:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio_buffer.getvalue())
            response.play("/audio-response")
        else:
            response.say(ai_response)
        
        gather = Gather(
            input="speech",
            action="/process-user-input",
            method="POST",
            timeout=10,
            language="es-MX"
        )
        response.append(gather)
        
        logger.info(f"Tiempo total: {time.time() - call_start_time:.2f}s")
        
    except Exception as e:
        logger.error(f"Error crítico: {str(e)}")
        response.say("Lo siento, ha ocurrido un error.")
    
    return str(response)