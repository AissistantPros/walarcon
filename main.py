from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from decouple import config
from utils import get_cancun_time
import requests
from datetime import datetime

# Configuración del sistema desde el archivo .env
CHATGPT_SECRET_KEY = config("CHATGPT_SECRET_KEY")  # Clave secreta de OpenAI
TWILIO_ACCOUNT_SID = config("TWILIO_ACCOUNT_SID")  # SID de la cuenta de Twilio
TWILIO_AUTH_TOKEN = config("TWILIO_AUTH_TOKEN")  # Token de autenticación de Twilio
TWILIO_PHONE = config("TWILIO_PHONE")  # Número de teléfono de Twilio
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")  # API Key para Eleven Labs
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")  # ID de la voz seleccionada en Eleven Labs

app = FastAPI()

# 1. Función para obtener el saludo dinámico según la hora en Cancún
def get_greeting():
    """
    Genera un saludo dinámico basado en la hora local de Cancún.

    Retorna:
        str: Saludo según la hora del día.
             - 3:00 AM a 11:59 AM: "Buenos días"
             - 12:00 PM a 7:20 PM: "Buenas tardes"
             - 7:21 PM a 2:59 AM: "Buenas noches"
    """
    now = get_cancun_time().time()

    if now >= datetime.strptime("03:00:00", "%H:%M:%S").time() and now <= datetime.strptime("11:59:59", "%H:%M:%S").time():
        return "¡Buenos días!"
    elif now >= datetime.strptime("12:00:00", "%H:%M:%S").time() and now <= datetime.strptime("19:20:00", "%H:%M:%S").time():
        return "¡Buenas tardes!"
    else:
        return "¡Buenas noches!"

# 2. Función para validar si el texto recibido de Twilio es válido
def is_valid_speech(speech: str) -> bool:
    """
    Evalúa si el texto recibido cumple con los criterios mínimos.

    Parámetros:
        speech (str): Texto enviado por Twilio (transcripción del usuario).

    Retorna:
        bool: True si el texto es válido (al menos 3 palabras), False en caso contrario.
    """
    if not speech:
        return False

    # Separar en palabras
    words = speech.split()

    # Validar si tiene al menos 3 palabras
    if len(words) < 3:
        return False

    return True

# 3. Función para generar audio usando Eleven Labs
def generate_audio_with_eleven_labs(text):
    """
    Convierte texto a audio usando la API de Eleven Labs.

    Parámetros:
        text (str): Texto que se convertirá a audio.

    Configuración de voz:
        - stability (float): 0.0 a 1.0 (menor estabilidad genera más variaciones).
        - similarity_boost (float): 0.0 a 1.0 (mayor valor mejora la emoción).
        - speed (float): 0.5 (muy lento) a 2.0 (muy rápido).
        - pitch (float): -2.0 (tono bajo) a 2.0 (tono alto).

    Retorna:
        str: URL del archivo de audio generado o None si hay un error.
    """
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}"
    headers = {
        "xi-api-key": ELEVEN_LABS_API_KEY,
        "Content-Type": "application/json"
    }
    payload = {
        "text": text,
        "voice_settings": {
            "stability": 0.5,           # 0.5 para balance entre variación y estabilidad
            "similarity_boost": 0.75,  # 0.75 para un tono más consistente y emocional
            "speed": 1.0,              # Velocidad estándar
            "pitch": 0.0               # Tono neutral
        }
    }
    response = requests.post(url, json=payload, headers=headers)
    if response.status_code == 200:
        return response.json().get("audio_url")
    else:
        print(f"Error al generar audio: {response.status_code}, {response.text}")
        return None

# 4. Ruta para manejar llamadas de Twilio
@app.post("/twilio-call")
async def handle_call(request: Request):
    """
    Ruta que procesa las llamadas entrantes desde Twilio.

    Procesos:
        - Recibe el texto transcrito por Twilio (SpeechResult).
        - Valida si el texto cumple con los criterios mínimos.
        - Genera un saludo dinámico.
        - Llama a la IA para procesar la respuesta (simulado por ahora).
        - Convierte la respuesta en audio usando Eleven Labs.
        - Envía el audio generado como respuesta a Twilio para reproducirlo.

    Respuesta a Twilio:
        - Si el texto no es válido, envía un mensaje de error.
        - Si no se puede generar el audio, informa al usuario.
        - Reproduce el audio generado con la etiqueta <Play>.
    """
    data = await request.form()
    user_input = data.get("SpeechResult")  # Texto enviado por Twilio

    # Validar si el texto cumple con los criterios
    if not is_valid_speech(user_input):
        return PlainTextResponse(
            "<Response><Say>No se recibió una respuesta válida o suficiente para interrumpir.</Say></Response>",
            media_type="text/xml"
        )

    # Generar saludo dinámico
    greeting = get_greeting()

    # Crear texto de respuesta (por ahora simulado)
    response_text = f"{greeting} Consultorio del Doctor Wilfrido Alarcón. {user_input}. ¿En qué puedo ayudarte?"

    # Convertir texto a audio
    audio_url = generate_audio_with_eleven_labs(response_text)

    if not audio_url:
        return PlainTextResponse(
            "<Response><Say>No se pudo generar el audio en este momento.</Say></Response>",
            media_type="text/xml"
        )

    # Responder a Twilio con el audio generado y permitir interrupciones
    return PlainTextResponse(
        f"<Response><Play bargeIn='true'>{audio_url}</Play></Response>",
        media_type="text/xml"
    )
