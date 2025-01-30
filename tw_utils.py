from twilio.twiml.voice_response import VoiceResponse, Gather
from aiagent import generate_openai_response
from audio_utils import generate_audio_with_eleven_labs
from prompt import generate_openai_prompt
import os

# Configuración fija del mensaje de saludo (agregado)
GREETING_MESSAGE = "Bienvenido al consultorio del Doctor Alarcón. ¿En qué puedo ayudarle hoy?"
AUDIO_TEMP_PATH = "/tmp/audio_response.mp3"  # Debe coincidir con main.py

def handle_twilio_call(gather_action: str):
    """
    Versión corregida: Eliminamos el parámetro greeting_message y usamos uno fijo
    """
    response = VoiceResponse()
    
    try:
        # Generar audio y guardarlo en archivo temporal (agregado)
        audio_buffer = generate_audio_with_eleven_labs(GREETING_MESSAGE)
        if audio_buffer:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio_buffer.getvalue())
            response.play("/audio-response")
        else:
            response.say(GREETING_MESSAGE)
    except Exception as e:
        response.say("Bienvenido. Estamos teniendo problemas técnicos. Por favor intente más tarde.")

    # Configurar Gather con timeout reducido para mejor UX (modificado)
    gather = Gather(
        input="speech",  # Forzar detección de voz
        action=gather_action,
        method="POST",
        timeout=5,
        language="es-MX"
    )
    response.append(gather)
    
    return str(response)

def process_user_input(user_input: str):
    """
    Versión simplificada: Recibe solo el texto del usuario
    """
    response = VoiceResponse()
    
    try:
        # Detección de despedida mejorada (modificado)
        if any(keyword in user_input.lower() for keyword in ["adiós", "hasta luego", "gracias"]):
            return end_twilio_call("Gracias por llamar. Que tenga un excelente día.")
        
        # Generar respuesta con IA (modificado)
        prompt = generate_openai_prompt(user_input)
        ai_response = generate_openai_response(prompt)
        
        # Manejo de audio con guardado en archivo (agregado)
        audio_buffer = generate_audio_with_eleven_labs(ai_response)
        if audio_buffer:
            with open(AUDIO_TEMP_PATH, "wb") as f:
                f.write(audio_buffer.getvalue())
            response.play("/audio-response")
        else:
            response.say(ai_response)
            
    except Exception as e:
        response.say("Lo siento, estoy teniendo dificultades. Por favor intente nuevamente.")
    
    # Nuevo Gather para continuar conversación (agregado)
    gather = Gather(
        input="speech",
        action="/process-user-input",
        method="POST",
        timeout=5,
        language="es-MX"
    )
    response.append(gather)
    
    return str(response)

def end_twilio_call(farewell_message: str):
    """
    Versión optimizada
    """
    response = VoiceResponse()
    response.say(farewell_message, voice="alice", language="es-MX")
    response.pause(length=2)  # Pausa más natural
    response.hangup()
    return str(response)