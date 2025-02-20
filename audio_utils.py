# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio: transcripción y síntesis de voz (usando Google STT + ElevenLabs).

Este módulo asume que el audio que recibe (por ejemplo, desde Twilio)
ya viene en formato mu-law (MULAW), con sample rate de 8000 Hz y un canal (mono),
por lo que se puede enviar directamente a Google STT sin conversión.
Se miden los tiempos en cada paso para evaluar la latencia.
"""

import io
import logging
import time
from decouple import config
from google.cloud import speech
from google.oauth2.service_account import Credentials
from elevenlabs import ElevenLabs, VoiceSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_google_credentials():
    """
    Crea las credenciales de Google a partir de las variables de entorno.
    """
    credentials_info = {
       "type": config("STT_TYPE"),
       "project_id": config("STT_PROJECT_ID"),
       "private_key_id": config("STT_PRIVATE_KEY_ID"),
       "private_key": config("STT_PRIVATE_KEY").replace("\\n", "\n"),
       "client_email": config("STT_CLIENT_EMAIL"),
       "client_id": config("STT_CLIENT_ID"),
       "auth_uri": config("STT_AUTH_URI"),
       "token_uri": config("STT_TOKEN_URI", default="https://oauth2.googleapis.com/token"),
       "auth_provider_x509_cert_url": config("STT_AUTH_PROVIDER_X509_CERT_URL"),
       "client_x509_cert_url": config("STT_CLIENT_X509_CERT_URL")
    }
    return Credentials.from_service_account_info(credentials_info)

# Inicializa ElevenLabs con su API key
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def speech_to_text(audio_bytes: bytes) -> str:
    """
    Transcribe el audio directamente usando Google Cloud Speech-to-Text,
    asumiendo que el audio ya viene en formato mu-law (MULAW), 8000 Hz, mono.
    Se miden los tiempos en cada paso.
    """
    try:
        start_total = time.perf_counter()

        # Crear credenciales y cliente
        start_cred = time.perf_counter()
        credentials = get_google_credentials()
        client = speech.SpeechClient(credentials=credentials)
        end_cred = time.perf_counter()
        logger.info(f"Tiempo creación credenciales y cliente: {end_cred - start_cred:.3f} s")

        # Preparar el audio para STT
        start_audio = time.perf_counter()
        audio_data_google = speech.RecognitionAudio(content=audio_bytes)
        end_audio = time.perf_counter()
        logger.info(f"Tiempo preparación de audio: {end_audio - start_audio:.3f} s")

        # Configuración de STT
        start_config = time.perf_counter()
        config_stt = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code="es-MX"  # Pruebas con español; se puede parametrizar
        )
        end_config = time.perf_counter()
        logger.info(f"Tiempo configuración STT: {end_config - start_config:.3f} s")

        # Llamada a STT
        start_recognize = time.perf_counter()
        response = client.recognize(config=config_stt, audio=audio_data_google)
        end_recognize = time.perf_counter()
        logger.info(f"Tiempo de reconocimiento (STT): {end_recognize - start_recognize:.3f} s")

        if not response.results:
            logger.info("No se obtuvieron resultados en STT.")
            return ""
        transcript = response.results[0].alternatives[0].transcript.strip()

        end_total = time.perf_counter()
        logger.info(f"Tiempo total en speech_to_text: {end_total - start_total:.3f} s")
        logger.info(f"👤 Transcripción (Google STT Directa): {transcript}")
        return transcript

    except Exception as e:
        logger.error(f"❌ Error en speech_to_text: {e}")
        return ""

def text_to_speech(text: str, lang="es") -> bytes:
    """
    Convierte texto a voz usando ElevenLabs (formato mu-law 8kHz).
    Se miden los tiempos en cada paso.
    """
    try:
        start_total = time.perf_counter()
        start_convert = time.perf_counter()
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
        end_convert = time.perf_counter()
        logger.info(f"Tiempo conversión TTS: {end_convert - start_convert:.3f} s")
        result = b"".join(audio_stream)
        end_total = time.perf_counter()
        logger.info(f"Tiempo total en text_to_speech: {end_total - start_total:.3f} s")
        return result
    except Exception as e:
        logger.error(f"❌ Error en text_to_speech: {e}")
        return b""
