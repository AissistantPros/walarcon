from twilio.twiml.voice_response import VoiceResponse, Gather

def generate_twilio_response(message=None, gather_action=None, gather_timeout=5, end_call=False):
    """
    Genera una respuesta para Twilio Voice Response.

    Args:
        message (str): Mensaje de texto que será leído por el sistema.
        gather_action (str): URL donde se enviará la respuesta del usuario.
        gather_timeout (int): Tiempo de espera para recibir entrada del usuario.
        end_call (bool): Si es True, termina la llamada después de reproducir el mensaje.

    Returns:
        VoiceResponse: Objeto VoiceResponse configurado.
    """
    response = VoiceResponse()

    # Agregar mensaje al inicio
    if message:
        response.say(message)

    # Configurar acción de Gather para esperar entrada del usuario
    if gather_action and not end_call:
        gather = Gather(input="speech", action=gather_action, method="POST", timeout=gather_timeout)
        response.append(gather)

    # Terminar la llamada si end_call es True
    if end_call:
        response.hangup()

    return response


def play_audio_with_twilio(audio_url, gather_action=None, gather_timeout=5, end_call=False):
    """
    Genera una respuesta de Twilio para reproducir un archivo de audio.

    Args:
        audio_url (str): URL del archivo de audio que se reproducirá.
        gather_action (str): URL donde se enviará la respuesta del usuario.
        gather_timeout (int): Tiempo de espera para recibir entrada del usuario.
        end_call (bool): Si es True, termina la llamada después de reproducir el audio.

    Returns:
        VoiceResponse: Objeto VoiceResponse configurado.
    """
    response = VoiceResponse()

    # Reproducir el audio
    response.play(audio_url)

    # Configurar Gather si la conversación debe continuar
    if gather_action and not end_call:
        gather = Gather(input="speech", action=gather_action, method="POST", timeout=gather_timeout)
        response.append(gather)

    # Terminar la llamada si end_call es True
    if end_call:
        response.hangup()

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
    response.pause(length=5)  # Agregar una pausa de 5 segundos antes de colgar
    response.hangup()
    return response
