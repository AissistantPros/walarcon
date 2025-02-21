# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio: transcripción y síntesis de voz (usando Google STT + ElevenLabs).
Asume que el audio ya viene en mu-law (8kHz, mono) de Twilio.

Se utiliza chunking manual, por lo que esta parte se encarga sólo de transcribir
un chunk de audio y devolver el resultado.
"""

import logging
import time
from decouple import config
from google.cloud import speech
from google.oauth2.service_account import Credentials
from elevenlabs import ElevenLabs, VoiceSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def get_google_credentials():
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
        "client_x509_cert_url": config("STT_CLIENT_X509_CERT_URL"),
    }
    return Credentials.from_service_account_info(credentials_info)

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def speech_to_text(audio_bytes: bytes) -> str:
    """
    Transcribe un chunk de audio mu-law (8kHz, mono) usando Google STT.
    Retorna la transcripción parcial.
    """
    start_total = time.perf_counter()
    try:
        # Credenciales y cliente
        start_cred = time.perf_counter()
        creds = get_google_credentials()
        client = speech.SpeechClient(credentials=creds)
        end_cred = time.perf_counter()
        logger.info(f"[STT] Tiempo credenciales+cliente: {end_cred - start_cred:.3f}s")

        # Config
        config_stt = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code="es-MX"
        )

        # Llamada STT
        start_rec = time.perf_counter()
        audio_data = speech.RecognitionAudio(content=audio_bytes)
        response = client.recognize(config=config_stt, audio=audio_data)
        end_rec = time.perf_counter()
        logger.info(f"[STT] Tiempo reconocimiento: {end_rec - start_rec:.3f}s")

        if not response.results:
            return ""

        transcript = response.results[0].alternatives[0].transcript.strip()
        end_total = time.perf_counter()
        logger.info(f"[STT] Chunk transcrito en {end_total - start_total:.3f}s: \"{transcript}\"")
        return transcript
    except Exception as e:
        logger.error(f"[STT] Error: {e}")
        return ""

def text_to_speech(text: str) -> bytes:
    """
    Convierte texto a voz usando ElevenLabs (mu-law 8kHz).
    """
    start_total = time.perf_counter()
    try:
        start_conv = time.perf_counter()
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
        end_conv = time.perf_counter()
        logger.info(f"[TTS] Tiempo conversión: {end_conv - start_conv:.3f}s")

        result = b"".join(audio_stream)
        end_total = time.perf_counter()
        logger.info(f"[TTS] Respuesta en {end_total - start_total:.3f}s. Longitud: {len(result)} bytes.")
        return result

    except Exception as e:
        logger.error(f"[TTS] Error: {e}")
        return b""
