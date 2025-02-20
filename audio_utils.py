# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio: transcripción y síntesis de voz (usando Google STT + ElevenLabs).

Este módulo asume que el audio que recibe (por ejemplo, desde Twilio)
ya viene en formato mu-law (MULAW), con sample rate de 8000 Hz y un canal (mono),
por lo que se puede enviar directamente a Google STT sin conversión.
"""

import io
import logging
from decouple import config
from google.cloud import speech
from elevenlabs import ElevenLabs, VoiceSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializa ElevenLabs con su API key
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def speech_to_text_direct(audio_bytes: bytes) -> str:
    """
    Transcribe el audio directamente usando Google Cloud Speech-to-Text,
    asumiendo que el audio ya viene en formato mu-law (MULAW), 8000 Hz, mono.
    """
    try:
        client = speech.SpeechClient()
        audio_data_google = speech.RecognitionAudio(content=audio_bytes)
        config_stt = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,
            language_code="es-MX"  # Ajusta según el idioma que necesites
        )

        response = client.recognize(config=config_stt, audio=audio_data_google)

        if not response.results:
            return ""
        transcript = response.results[0].alternatives[0].transcript.strip()
        logger.info(f"👤 Transcripción (Google STT Directa): {transcript}")
        return transcript

    except Exception as e:
        logger.error(f"❌ Error en speech_to_text_direct: {e}")
        return ""

def text_to_speech(text: str, lang="es") -> bytes:
    """
    Convierte texto a voz usando ElevenLabs (formato mu-law 8kHz).
    """
    try:
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.4,
                similarity_boost=0.7,
                style=0.3,
                speed=1.3,
                use_speaker_boost=False
            ),
            output_format="ulaw_8000"
        )
        return b"".join(audio_stream)
    except Exception as e:
        logger.error(f"❌ Error en text_to_speech: {e}")
        return b""
