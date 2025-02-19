# -*- coding: utf-8 -*-
"""
Módulo para audio con ajustes de voz y formato PCMU (Twilio).
"""

import io
import logging
import requests
from decouple import config
from openai import OpenAI
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings
from pydub import AudioSegment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Claves
OPENAI_API_KEY = config("CHATGPT_SECRET_KEY")
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")

# Inicializar clientes
openai_client = OpenAI(api_key=OPENAI_API_KEY)
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def text_to_speech(text: str) -> bytes:
    """
    Convierte texto en voz usando ElevenLabs con ajustes personalizados.
    Genera audio mu-law 8 kHz (ulaw_8000), compatible con Twilio.
    """
    try:
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.3,
                similarity_boost=0.8,
                style=0.5,
                speed=1.5,
                use_speaker_boost=True
            ),
            # OJO: ElevenLabs NO soporta "pcm_mulaw",
            # usa "ulaw_8000" en su lugar.
            output_format="ulaw_8000"
        )
        return b"".join(audio_stream)
    except Exception as e:
        logger.error(f"❌ Error ElevenLabs: {str(e)}")
        return b""

def convert_mulaw_to_wav(mulaw_data: bytes) -> io.BytesIO:
    """
    Convierte mu-law 8kHz a WAV (16-bit PCM, 8kHz).
    """
    try:
        segment = AudioSegment(
            data=mulaw_data,
            sample_width=1,  # mu-law = 8 bits
            frame_rate=8000,
            channels=1
        )
        wav_data = io.BytesIO()
        segment.export(wav_data, format="wav")
        wav_data.seek(0)
        return wav_data
    except Exception as e:
        logger.error(f"❌ Error al convertir mu-law a WAV: {e}")
        return io.BytesIO()  # vacío

def speech_to_text(audio_bytes: bytes) -> str:
    """
    Función simple que asume que 'audio_bytes' viene en mu-law/8000
    y lo convierte a WAV y lo manda a Whisper.
    (un solo chunk)
    """
    try:
        wav_data = convert_mulaw_to_wav(audio_bytes)
        if not wav_data.getbuffer().nbytes:
            logger.error("❌ WAV result is empty, can't send to Whisper.")
            return ""

        files = {
            "file": ("audio.wav", wav_data, "audio/wav")
        }
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }
        data = {
            "model": "whisper-1"
        }
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers,
            files=files,
            data=data
        )
        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        else:
            logger.error(f"❌ Error Whisper: {resp.status_code} - {resp.text}")
            return ""
    except Exception as e:
        logger.error(f"❌ Error speech_to_text: {str(e)}")
        return ""
