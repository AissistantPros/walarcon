from twilio.twiml.voice_response import VoiceResponse, Gather
from aiagent import generate_openai_response
from audio_utils import generate_audio_with_eleven_labs
import logging
import time
import asyncio

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"
conversation_history = []
call_start_time = None

def handle_twilio_call(gather_action: str):
    global call_start_time, conversation_history
    call_start_time = time.time()
    conversation_history = []
    
    response = VoiceResponse()
    try:
        greeting = "Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        audio = generate_audio_with_eleven_labs(greeting)
        
        if audio:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio.getvalue())
            response.play("/audio-response")
        else:
            response.say(greeting)
            
        response.append(Gather(
            input="speech",
            action=gather_action,
            method="POST",
            timeout=5,
            language="es-MX"
        ))
        
    except Exception as e:
        logger.error(f"Error en saludo: {str(e)}")
        call_start_time = time.time()
        response.say("Bienvenido. Estamos teniendo problemas técnicos.")
        
    return str(response)

async def process_user_input(user_input: str):
    global conversation_history, call_start_time
    response = VoiceResponse()
    
    if call_start_time is None:
        call_start_time = time.time()
        logger.warning("⚠️ call_start_time inicializado de emergencia")
    
    try:
        conversation_history.append({"role": "user", "content": user_input.strip()})
        
        # Generar respuesta de IA
        ai_response = await asyncio.to_thread(
            generate_openai_response, 
            conversation_history
        )
        
        if not ai_response:
            raise ValueError("Respuesta vacía de OpenAI")
            
        conversation_history.append({"role": "assistant", "content": ai_response})
        
        # Generar y reproducir audio
        audio_buffer = await asyncio.to_thread(
            generate_audio_with_eleven_labs, 
            ai_response.replace("[END_CALL]", "").strip()
        )
        
        if audio_buffer:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio_buffer.getvalue())
            response.play("/audio-response")
        else:
            response.say("Lo siento, no pude generar la respuesta. ¿Podría repetir su pregunta?")

        # Continuar la conversación
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
        response.say("Lo siento, ha ocurrido un error. ¿Podría repetir su solicitud?")
        
    return str(response)

def end_twilio_call(farewell_message: str):
    global conversation_history, call_start_time
    response = VoiceResponse()
    
    try:
        audio = generate_audio_with_eleven_labs(farewell_message)
        if audio:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio.getvalue())
            response.play("/audio-response")
            
        response.hangup()
        logger.info("📞 Llamada finalizada correctamente")
        
    except Exception as e:
        logger.error(f"Error en despedida: {str(e)}")
        response.hangup()
    
    # Resetear variables globales
    conversation_history = []
    call_start_time = None
    
    return str(response)