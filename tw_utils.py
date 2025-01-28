from twilio.twiml.voice_response import VoiceResponse, Gather
from aiagent import generate_openai_response
from labs_utils import generate_audio_with_eleven_labs
from prompt import generate_openai_prompt


def handle_twilio_call(greeting_message, gather_action):
    """
    Maneja la llamada inicial de Twilio y reproduce un saludo generado por Eleven Labs.

    Args:
        greeting_message (str): Mensaje de saludo inicial.
        gather_action (str): URL para procesar la respuesta del usuario.

    Returns:
        VoiceResponse: Respuesta de Twilio para el saludo inicial y Gather.
    """
    response = VoiceResponse()
    audio_buffer = generate_audio_with_eleven_labs(greeting_message)

    if audio_buffer:
        # Producir el saludo inicial generado por Eleven Labs
        response.play("/audio-response")  # Ruta donde se aloja el audio
    else:
        # Fallback en caso de fallo en Eleven Labs
        response.say(greeting_message)

    # Configurar Gather para la entrada del usuario
    gather = Gather(action=gather_action, method="POST", timeout=10)
    response.append(gather)

    return response


def process_user_input(user_input, gather_action, farewell_action):
    """
    Procesa la entrada del usuario, pasa la respuesta a la IA, y genera un audio dinámico.

    Args:
        user_input (str): Texto proporcionado por el usuario.
        gather_action (str): URL para Gather en caso de continuación de la conversación.
        farewell_action (str): URL para terminar la llamada.

    Returns:
        VoiceResponse: Respuesta de Twilio con la respuesta generada o acción de despedida.
    """
    response = VoiceResponse()

    # Detectar intención de terminar la llamada
    if any(keyword in user_input for keyword in ["adiós", "hasta luego", "gracias, eso es todo"]):
        farewell_message = "Gracias por llamar al consultorio del Doctor Alarcón. Que tenga un excelente día."
        return end_twilio_call(farewell_message)

    # Generar respuesta usando el prompt actualizado y OpenAI
    prompt = generate_openai_prompt(user_input)
    openai_response = generate_openai_response(prompt)

    # Convertir la respuesta de OpenAI en audio
    audio_buffer = generate_audio_with_eleven_labs(openai_response)

    if audio_buffer:
        # Producir la respuesta generada
        response.play("/audio-response")
    else:
        # Fallback en caso de fallo en Eleven Labs
        response.say("Lo siento, no puedo procesar tu solicitud en este momento.")

    # Configurar Gather para continuar la conversación
    gather = Gather(action=gather_action, method="POST", timeout=10)
    response.append(gather)

    return response


def end_twilio_call(farewell_message):
    """
    Genera una respuesta para terminar la llamada con un mensaje de despedida.

    Args:
        farewell_message (str): Mensaje de despedida que se leerá antes de colgar.

    Returns:
        VoiceResponse: Objeto VoiceResponse configurado.
    """
    response = VoiceResponse()
    response.say(farewell_message)
    response.pause(length=7)  # Pausa de 7 segundos antes de colgar
    response.hangup()
    return response
