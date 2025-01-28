from twilio.twiml.voice_response import VoiceResponse, Gather
from labs_utils import generate_audio_with_eleven_labs
from utils import get_cancun_time

def handle_twilio_call(request_data):
    response = VoiceResponse()
    now = get_cancun_time()
    
    greeting = (
        "Buen día" if 3 <= now.hour <= 11
        else "Buenas tardes" if 12 <= now.hour <= 19
        else "Buenas noches"
    )
    message = f"{greeting}, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudar?"

    audio = generate_audio_with_eleven_labs(message)

    if audio:
        response.play("/audio-response")  # Path where the audio file is temporarily stored.
    else:
        response.say(message)

    gather = Gather(input="speech", action="/process-user-input", timeout=10)
    response.append(gather)
    return response

def process_user_input(request_data):
    response = VoiceResponse()
    user_input = request_data.get("SpeechResult", "").lower()

    if user_input in ["adiós", "hasta luego", "gracias"]:
        farewell_message = "Gracias por llamar al consultorio. Que tenga una excelente noche."
        response.say(farewell_message)
        response.pause(length=5)
        response.hangup()
        return response

    # Aquí se manejaría el envío de la entrada del usuario a la IA y generación de respuesta.
    response.say("Procesando su solicitud. Por favor espere un momento.")
    return response
