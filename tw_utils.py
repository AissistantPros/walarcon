# -*- coding: utf-8 -*-
"""
Módulo de integración con Twilio - Dr. Alarcón IVR System
Función principal: Manejar el flujo de llamadas y procesar entradas de voz
"""










# ==================================================
# Parte 1: Configuración inicial y dependencias
# ==================================================
from twilio.twiml.voice_response import VoiceResponse, Gather
from aiagent import generate_openai_response
from audio_utils import generate_audio_with_eleven_labs
import logging
import time
import asyncio
from decouple import config

# Configurar logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variables globales (para estado de la llamada)
conversation_history = []
call_start_time = None
AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"









# ==================================================
# Parte 2: Manejo de llamadas entrantes de Twilio
# ==================================================
def handle_twilio_call(gather_action: str):
    """Inicia el flujo de la llamada con mensaje de bienvenida"""
    global call_start_time, conversation_history
    call_start_time = time.time()
    conversation_history = []
    
    response = VoiceResponse()
    try:
        # Generar saludo inicial
        greeting = "Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        audio = generate_audio_with_eleven_labs(greeting)
        
        if audio:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio.getvalue())
            response.play("/audio-response")
        else:
            response.say(greeting)
            
        # Configurar recolección de voz
        response.append(Gather(
            input="speech",
            action=gather_action,
            method="POST",
            timeout=5,
            language="es-MX"
        ))
        
    except Exception as e:
        logger.error(f"Error en saludo inicial: {str(e)}")
        response.say("Bienvenido. Estamos teniendo problemas técnicos.")
        
    return str(response)









# ==================================================
# Parte 3: Procesamiento de entradas del usuario
# ==================================================
async def process_user_input(user_input: str):
    """Procesa la entrada de voz y genera respuesta"""
    global conversation_history, call_start_time
    response = VoiceResponse()
    
    try:
        # Registrar entrada del usuario
        conversation_history.append({"role": "user", "content": user_input.strip()})
        
        # Obtener respuesta de IA
        ai_response = await asyncio.to_thread(generate_openai_response, conversation_history)
        
        # NEW: Manejar errores específicos
        if "[ERROR]" in ai_response:
            error_code = ai_response.split("[ERROR] ")[1].strip()
            ai_response = f"Ups, {map_error_to_message(error_code)}. ¿Podemos intentarlo de nuevo?"
            logger.warning(f"Error en herramienta: {error_code}")
        
        # Registrar respuesta de IA
        conversation_history.append({"role": "assistant", "content": ai_response})
        
        # Generar audio respuesta
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

        # Continuar conversación
        response.append(Gather(
            input="speech",
            action="/process-user-input",
            method="POST",
            timeout=10,
            language="es-MX"
        ))
        
        logger.info(f"Tiempo total de llamada: {time.time() - call_start_time:.2f}s")
        
    except Exception as e:
        logger.error(f"Error crítico: {str(e)}")
        response.say("Lo siento, ha ocurrido un error. ¿Podría repetir su solicitud?")
        
    return str(response)









# ==================================================
# Parte 4: Mapeo de errores técnicos a mensajes
# ==================================================
def map_error_to_message(error_code: str) -> str:
    """Traduce códigos de error a mensajes amigables"""
    error_messages = {
        "GOOGLE_SHEETS_UNAVAILABLE": "no puedo acceder a la base de datos 😕",
        "GOOGLE_CALENDAR_UNAVAILABLE": "el sistema de citas no responde 😥",
        "DEFAULT": "hubo un problema técnico"
    }
    return error_messages.get(error_code, error_messages["DEFAULT"])









# ==================================================
# Parte 5: Bloque principal (solo para pruebas)
# ==================================================
if __name__ == "__main__":
    # Ejemplo de prueba rápida
    print("Probando manejo de errores:")
    test_error = "[ERROR] GOOGLE_SHEETS_UNAVAILABLE"
    print(f"Entrada: {test_error} -> Salida: {map_error_to_message(test_error.split()[-1])}")