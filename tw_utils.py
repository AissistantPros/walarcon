from twilio.twiml.voice_response import VoiceResponse, Gather
from aiagent import generate_openai_response
from audio_utils import generate_audio_with_eleven_labs
import logging
import time
import asyncio
import random

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

async def process_user_input(user_input: str):
    global conversation_history, call_start_time
    response = VoiceResponse()
    
    try:
        conversation_history.append({"role": "user", "content": user_input})

        # Generar frase de relleno mientras la IA responde
        filler_phrases = [
            "Déjeme revisar eso...",
            "Un momento, por favor...",
            "Mmm, revisando la información...",
            "Permítame checarlo..."
        ]
        filler_message = random.choice(filler_phrases)

        # Ejecutar IA y generación de audio en paralelo para reducir latencia
        ai_task = asyncio.to_thread(generate_openai_response, conversation_history)
        filler_audio_task = asyncio.to_thread(generate_audio_with_eleven_labs, filler_message)

        ai_response, filler_audio = await asyncio.gather(ai_task, filler_audio_task)

        conversation_history.append({"role": "assistant", "content": ai_response})

        if filler_audio:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(filler_audio.getvalue())
            response.play("/audio-response")

        # Verificar si la IA indica que debe finalizar la llamada
        if "[END_CALL]" in ai_response:
            logger.info("🛑 IA solicitó finalizar llamada")
            clean_response = ai_response.replace("[END_CALL]", "").strip()
            return end_twilio_call(clean_response)
        
        # Generar audio de la respuesta de la IA en paralelo
        audio_buffer = await asyncio.to_thread(generate_audio_with_eleven_labs, ai_response)
        
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

        if call_start_time is not None:
            logger.info(f"Tiempo total: {time.time() - call_start_time:.2f}s")
        else:
            logger.warning("⚠️ call_start_time no está definido, no se puede calcular el tiempo total")

    except Exception as e:
        logger.error(f"Error crítico: {str(e)}")
        response.say("Lo siento, ha ocurrido un error.")

    return str(response)

def end_twilio_call(farewell_message: str):
    response = VoiceResponse()
    
    try:
        audio_buffer = generate_audio_with_eleven_labs(farewell_message)
        
        if audio_buffer:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio_buffer.getvalue())
            response.play("/audio-response")
        else:
            logger.error("⚠️ No se pudo generar el audio de despedida")
        
        response.pause(length=5)
        response.hangup()
        logger.info("📞 Llamada finalizada (audio personalizado)")
        
    except Exception as e:
        logger.error(f"❌ Error en despedida: {str(e)}")
        response.hangup()
    
    return str(response)
