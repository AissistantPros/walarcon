# -*- coding: utf-8 -*-
"""
Módulo de integración con Twilio - Dr. Alarcón IVR System
Función principal: Manejar el flujo de llamadas y procesar entradas de voz.
"""

from fastapi import HTTPException, Request, Form
from twilio.twiml.voice_response import VoiceResponse, Gather, Hangup
from aiagent import generate_openai_response
from audio_utils import generate_audio_with_eleven_labs
import logging
import time
import asyncio
from decouple import config

# Configuración de logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Variables globales para la llamada
conversation_history = []
call_start_time = None
AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"

# ==================================================
# 🔹 Manejo de llamadas entrantes de Twilio
# ==================================================
async def handle_twilio_call(gather_action: str):
    """Inicia el flujo de la llamada con un mensaje de bienvenida."""
    global call_start_time, conversation_history
    call_start_time = time.time()
    conversation_history = []

    response = VoiceResponse()
    try:
        # Generar saludo inicial
        greeting = "Consultorio del Dr. Wilfrido Alarcón. ¿En qué puedo ayudarle?"
        
        # CORREGIDO: Usar "await" para esperar la respuesta de la función asíncrona
        audio = await generate_audio_with_eleven_labs(greeting)

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
# 🔹 Procesamiento de entradas del usuario
# ==================================================
async def process_user_input(request: Request):
    """Procesa la entrada de voz del usuario y genera una respuesta."""
    global conversation_history, call_start_time
    response = VoiceResponse()

    try:
        # 📌 Verifica que el request sea del tipo correcto
        if not isinstance(request, Request):
            logger.error("❌ Error: 'request' no es una instancia válida de Request.")
            response.say("Ha ocurrido un error interno en la solicitud.")
            return str(response)

        # 📌 Extraer los datos del formulario
        form_data = await request.form()
        
        # 📌 Depuración para verificar qué está llegando
        logger.info(f"📌 Datos recibidos en request.form(): {form_data}")

        # 📌 Verifica que 'SpeechResult' exista en los datos
        if "SpeechResult" not in form_data:
            logger.warning("⚠️ 'SpeechResult' no encontrado en los datos del formulario.")
            return handle_no_input(response)

        user_input = form_data["SpeechResult"].strip()

        if not user_input:
            return handle_no_input(response)

        conversation_history.append({"role": "user", "content": user_input})
        logger.info(f"🗣️ Entrada del usuario: {user_input}")

        # 📌 Generar respuesta de IA en un hilo separado
        ai_response = await asyncio.to_thread(generate_openai_response, conversation_history)

        if "[ERROR]" in ai_response:
            error_code = ai_response.split("[ERROR] ")[1].strip()
            ai_response = f"Hubo un problema con la consulta. {map_error_to_message(error_code)}."

        conversation_history.append({"role": "assistant", "content": ai_response})

        return await generate_twilio_response(response, ai_response)

    except Exception as e:
        logger.error(f"❌ Error en el procesamiento de voz: {str(e)}")
        response.say("Lo siento, ha ocurrido un error. ¿Podría repetir su solicitud?")
        return str(response)

# ==================================================
# 🔹 Herramienta para que la IA finalice la llamada
# ==================================================
async def end_call(response, reason=""):
    """Permite que la IA termine la llamada de manera natural según la razón."""
    farewell_messages = {
        "silence": "Lo siento, no puedo escuchar. Terminaré la llamada. Que tenga buen día.",
        "user_request": "Fue un placer atenderle, que tenga un excelente día.",
        "spam": "Hola colega, este número es solo para información y citas del Dr. Wilfrido Alarcón. Hasta luego.",
        "time_limit": "Qué pena, tengo que terminar la llamada. Si puedo ayudar en algo más, por favor, marque nuevamente."
    }

    message = farewell_messages.get(reason, "Gracias por llamar. Hasta luego.")

    audio_buffer = await asyncio.to_thread(generate_audio_with_eleven_labs, message)

    if audio_buffer:
        with open(AUDIO_TEMP_PATH, "wb") as f:
            f.write(audio_buffer.getvalue())
        response.play("/audio-response")
    else:
        response.say(message)

    # Si es una despedida normal, esperar 5 segundos antes de colgar
    if reason == "user_request":
        await asyncio.sleep(5)

    response.hangup()
    return str(response)

# ==================================================
# 🔹 Generación de respuesta de Twilio
# ==================================================
async def generate_twilio_response(response, ai_response):
    """Genera el audio de respuesta y lo envía a Twilio."""
    try:
        audio_buffer = await asyncio.to_thread(generate_audio_with_eleven_labs, ai_response)

        if audio_buffer:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio_buffer.getvalue())
            response.play("/audio-response")
        else:
            response.say(ai_response)

        response.append(Gather(
            input="speech",
            action="/process-user-input",
            method="POST",
            timeout=10,
            language="es-MX"
        ))

    except Exception as e:
        logger.error(f"Error en la generación de audio: {str(e)}")
        response.say("Lo siento, no pude generar la respuesta.")

    logger.info(f"Tiempo total de llamada: {time.time() - call_start_time:.2f}s")
    return str(response)

# ==================================================
# 🔹 Manejo de errores
# ==================================================
def handle_no_input(response):
    """Maneja el caso en el que el usuario no dice nada."""
    response.say("No escuché ninguna respuesta. ¿Podría repetir, por favor?")
    response.append(Gather(
        input="speech",
        action="/process-user-input",
        method="POST",
        timeout=10,
        language="es-MX"
    ))
    return str(response)

def map_error_to_message(error_code: str) -> str:
    """Traduce códigos de error a mensajes amigables."""
    error_messages = {
        "GOOGLE_SHEETS_UNAVAILABLE": "No puedo acceder a la base de datos en este momento.",
        "GOOGLE_CALENDAR_UNAVAILABLE": "El sistema de citas no responde.",
        "DEFAULT": "Hubo un problema técnico."
    }
    return error_messages.get(error_code, error_messages["DEFAULT"])