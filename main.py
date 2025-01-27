from fastapi import FastAPI, Request, Response
from fastapi.responses import Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from decouple import config
from elevenlabs import ElevenLabs, VoiceSettings
import openai
import io

# **SECCIÓN 1: Configuración del sistema**
CHATGPT_SECRET_KEY = config("CHATGPT_SECRET_KEY")
TWILIO_ACCOUNT_SID = config("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = config("TWILIO_AUTH_TOKEN")
TWILIO_PHONE = config("TWILIO_PHONE")
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

# Inicializamos la aplicación FastAPI
app = FastAPI()

# Inicialización de clientes externos
client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)
openai.api_key = CHATGPT_SECRET_KEY

# **SECCIÓN 2: Generar audio con Eleven Labs**
def generate_audio_with_eleven_labs(text, voice_id=ELEVEN_LABS_VOICE_ID):
    try:
        audio_data = client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(stability=0.7, similarity_boost=0.85)
        )
        audio_buffer = io.BytesIO()
        for chunk in audio_data:
            audio_buffer.write(chunk)
        audio_buffer.seek(0)
        print("Audio generado en memoria.")
        return audio_buffer
    except Exception as e:
        print(f"Error al generar audio con ElevenLabs: {e}")
        return None

# **SECCIÓN 3: Generar respuesta de OpenAI**
def generate_openai_response(prompt):
    try:
        response = openai.Completion.create(
            model="gpt-4o",
            prompt=prompt,
            max_tokens=400,
            temperature=0.7,
        )
        return response["choices"][0]["text"].strip()
    except Exception as e:
        print(f"Error generando respuesta con OpenAI: {e}")
        return "Lo siento, hubo un error procesando tu solicitud."

# **SECCIÓN 4: Manejar llamadas desde Twilio**
@app.post("/twilio-call")
async def handle_call(request: Request):
    try:
        data = await request.form()
        user_input = data.get("SpeechResult", "").lower()
        response = VoiceResponse()

        if not user_input:
            # Saludo inicial
            response_text = "Buenas noches, Consultorio del Doctor Wilfrido Alarcón. ¿En qué puedo ayudar?"
            audio_buffer = generate_audio_with_eleven_labs(response_text)
            if not audio_buffer:
                response.say("Hubo un problema al procesar tu solicitud. Por favor, intenta más tarde.")
            else:
                response.play(f"/audio-response")
            gather = Gather(input="speech", action="/process-user-input", method="POST", timeout=5)
            response.append(gather)
        else:
            response.redirect("/process-user-input")

        return Response(content=str(response), media_type="text/xml")

    except Exception as e:
        print(f"Error manejando la llamada desde Twilio: {e}")
        response = VoiceResponse()
        response.say("Hubo un error procesando tu llamada. Intenta más tarde.")
        return Response(content=str(response), media_type="text/xml")

# **SECCIÓN 5: Procesar la entrada del usuario**
@app.post("/process-user-input")
async def process_user_input(request: Request):
    try:
        data = await request.form()
        user_input = data.get("SpeechResult", "").lower()
        response = VoiceResponse()

        if any(keyword in user_input for keyword in ["adiós", "hasta luego", "gracias"]):
            farewell_text = "Gracias por llamar al consultorio del Doctor Wilfrido Alarcón. Que tenga una excelente noche."
            audio_buffer = generate_audio_with_eleven_labs(farewell_text)
            if not audio_buffer:
                response.say(farewell_text)
            else:
                response.play(f"/audio-response")
            response.pause(length=5)
            response.hangup()
        else:
            prompt = f"Usuario: {user_input}\nAsistente:"
            openai_response = generate_openai_response(prompt)
            audio_buffer = generate_audio_with_eleven_labs(openai_response)

            if not audio_buffer:
                response.say("Lo siento, no puedo procesar tu solicitud en este momento.")
            else:
                response.play(f"/audio-response")

            gather = Gather(input="speech", action="/process-user-input", method="POST", timeout=5)
            response.append(gather)

        return Response(content=str(response), media_type="text/xml")

    except Exception as e:
        print(f"Error procesando la entrada del usuario: {e}")
        response = VoiceResponse()
        response.say("Ocurrió un error procesando tu solicitud. Por favor, intenta nuevamente.")
        return Response(content=str(response), media_type="text/xml")
