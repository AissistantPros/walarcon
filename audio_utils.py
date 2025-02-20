# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio: transcripción y síntesis de voz (usando Google STT + ElevenLabs).

Este módulo asume que el audio que recibe (por ejemplo, desde Twilio)
ya viene en formato mu-law (MULAW), con sample rate de 8000 Hz y un canal (mono),
por lo que se puede enviar directamente a Google STT sin conversión.
Además, se crean las credenciales de Google a partir de variables de entorno
(prefijo STT_) en lugar de utilizar un archivo JSON.
"""

import io
import logging
from decouple import config
from google.cloud import speech
from google.oauth2.service_account import Credentials
from elevenlabs import ElevenLabs, VoiceSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializa ElevenLabs con su API key
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def get_google_credentials():
    """
    Crea las credenciales de Google a partir de las variables de entorno.
    Asegúrate de tener definidas las siguientes variables en tu .env o en el entorno:
      STT_TYPE, STT_PROJECT_ID, STT_PRIVATE_KEY_ID, STT_PRIVATE_KEY,
      STT_CLIENT_EMAIL, STT_CLIENT_ID, STT_AUTH_URI, STT_TOKEN_URI,
      STT_AUTH_PROVIDER_X509_CERT_URL, STT_CLIENT_X509_CERT_URL.
    Se reemplaza "\\n" por saltos de línea reales en la clave privada.
    """
    credentials_info = {
       "type": config("STT_TYPE"),
       "project_id": config("STT_PROJECT_ID"),
       "private_key_id": config("STT_PRIVATE_KEY_ID"),
       "private_key": config("STT_PRIVATE_KEY").replace("\\n", "\n"),
       "client_email": config("STT_CLIENT_EMAIL"),
       "client_id": config("STT_CLIENT_ID"),
       "auth_uri": config("STT_AUTH_URI"),
       "token_uri": config("STT_TOKEN_URI"),
       "auth_provider_x509_cert_url": config("STT_AUTH_PROVIDER_X509_CERT_URL"),
       "client_x509_cert_url": config("STT_CLIENT_X509_CERT_URL")
    }
    return Credentials.from_service_account_info(credentials_info)

def speech_to_text(audio_bytes: bytes) -> str:
    """
    Transcribe el audio directamente usando Google Cloud Speech-to-Text,
    asumiendo que el audio ya viene en formato mu-law (MULAW), 8000 Hz, mono.

    Las credenciales se crean a partir de variables de entorno (prefijo STT_).
    """
    try:
        # Crear el cliente con las credenciales personalizadas
        credentials = get_google_credentials()
        client = speech.SpeechClient(credentials=credentials)
        
        audio_data_google = speech.RecognitionAudio(content=audio_bytes)
        config_stt = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code="es-MX"  # Para pruebas con español; se puede parametrizar luego
        )

        response = client.recognize(config=config_stt, audio=audio_data_google)

        if not response.results:
            return ""
        transcript = response.results[0].alternatives[0].transcript.strip()
        logger.info(f"👤 Transcripción (Google STT Directa): {transcript}")
        return transcript

    except Exception as e:
        logger.error(f"❌ Error en speech_to_text: {e}")
        return ""

def text_to_speech(text: str, lang="es") -> bytes:
    """
    Convierte texto a voz usando ElevenLabs (formato mu-law 8kHz).

    El parámetro 'lang' se reserva para futuras personalizaciones, por ahora se usa la configuración actual.
    """
    try:
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.4,
                similarity_boost=0.7,
                style=0.9,
                speed=1.9,
                use_speaker_boost=False
            ),
            output_format="ulaw_8000"
        )
        return b"".join(audio_stream)
    except Exception as e:
        logger.error(f"❌ Error en text_to_speech: {e}")
        return b""
