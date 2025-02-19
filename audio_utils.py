# -*- coding: utf-8 -*-
"""
Módulo para audio con ajustes de voz y formato PCMU (Twilio).
"""

import io
import logging
import httpx
from decouple import config
from openai import OpenAI
from elevenlabs import ElevenLabs, VoiceSettings
from pydub import AudioSegment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Claves API
OPENAI_API_KEY = config("CHATGPT_SECRET_KEY")
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")

# Inicializar clientes
openai_client = OpenAI(api_key=OPENAI_API_KEY)
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def text_to_speech(text: str, language: str = "es") -> bytes:
    """
    Convierte texto en voz usando ElevenLabs con ajustes personalizados.
    Genera audio en formato mu-law 8 kHz, compatible con Twilio.
    """
    try:
        voice_id = config("ELEVEN_LABS_VOICE_ID")

        # Ajustar voz según el idioma detectado
        if language == "en":
            voice_id = config("ELEVEN_LABS_VOICE_ID_EN")  # Asegúrate de definir esto en tu .env
        elif language == "fr":
            voice_id = config("ELEVEN_LABS_VOICE_ID_FR")  # Definir si quieres usar una voz específica para francés

        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=voice_id,
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.4,
                similarity_boost=0.7,
                style=0.8,
                speed=1.9,
                use_speaker_boost=False
            ),
            output_format="ulaw_8000"
        )
        return b"".join(audio_stream)
    except Exception as e:
        logger.error(f"❌ Error en text_to_speech: {str(e)}")
        return b""

def convert_mulaw_to_wav(mulaw_data: bytes) -> io.BytesIO:
    """
    Convierte audio mu-law 8kHz a WAV (16-bit PCM, 8kHz).
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
        return io.BytesIO()

async def speech_to_text(audio_bytes: bytes) -> tuple[str, str]:
    """
    Convierte audio a texto usando Whisper y detecta el idioma.
    """
    try:
        async with httpx.AsyncClient() as client:
            files = {"file": ("audio.wav", convert_mulaw_to_wav(audio_bytes), "audio/wav")}
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
            data = {"model": "whisper-1"}

            resp = await client.post("https://api.openai.com/v1/audio/transcriptions",
                                     headers=headers, files=files, data=data)

        if resp.status_code == 200:
            json_resp = resp.json()
            return json_resp.get("text", "").strip(), json_resp.get("language", "es")
        else:
            logger.error(f"❌ Error Whisper: {resp.status_code} - {resp.text}")
            return "", "es"
    except Exception as e:
        logger.error(f"❌ Error en speech_to_text: {str(e)}")
        return "", "es"