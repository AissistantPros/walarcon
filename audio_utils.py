# -*- coding: utf-8 -*-
"""
Módulo para audio con ajustes de voz y formato PCMU (Twilio).
"""

import io
import logging
import asyncio
import requests
import tempfile
from pydub import AudioSegment
from decouple import config
from openai import OpenAI
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializar clientes
OPENAI_API_KEY = config("CHATGPT_SECRET_KEY")
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)
openai_client = OpenAI(api_key=OPENAI_API_KEY)

async def speech_to_text(audio_bytes: bytes) -> str:
    """
    Convierte audio en texto usando Whisper de OpenAI.
    """
    try:
        wav_file = convert_audio_to_wav(audio_bytes)
        if not wav_file:
            return ""
        
        # Guardar temporalmente para depuración
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_audio:
            temp_audio.write(wav_file.getvalue())
            temp_audio_path = temp_audio.name
            logger.info(f"📂 Archivo WAV temporal guardado en: {temp_audio_path}")
        
        with open(temp_audio_path, "rb") as audio_file:
            files = {"file": ("audio.wav", audio_file, "audio/wav")}
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
            response = requests.post("https://api.openai.com/v1/audio/transcriptions", headers=headers, files=files, data={"model": "whisper-1"})
        
        if response.status_code == 200:
            return response.json().get("text", "")
        else:
            logger.error(f"❌ Error Whisper: {response.status_code} - {response.json()}")
            return ""
    except Exception as e:
        logger.error(f"❌ Error en speech_to_text: {e}")
        return ""

def text_to_speech(text: str) -> bytes:
    """
    Convierte texto en voz usando ElevenLabs con ajustes personalizados.
    """
    try:
        audio_stream = asyncio.run(asyncio.to_thread(
            elevenlabs_client.text_to_speech.convert,
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
            output_format="ulaw_8000"  # Formato compatible con Twilio
        ))
        return b''.join(audio_stream)
    except Exception as e:
        logger.error(f"❌ Error ElevenLabs: {str(e)}")
        return b''

def convert_audio_to_wav(audio_bytes: bytes) -> io.BytesIO:
    """
    Convierte audio en formato mu-law a WAV antes de enviarlo a Whisper.
    """
    try:
        ulaw_audio = AudioSegment(
            data=audio_bytes,
            sample_width=1,  # mu-law es de 8 bits (1 byte)
            frame_rate=8000,
            channels=1
        )
        wav_file = io.BytesIO()
        ulaw_audio.export(wav_file, format="wav")
        wav_file.seek(0)
        logger.info("✅ Conversión a WAV exitosa.")
        return wav_file
    except Exception as e:
        logger.error(f"❌ Error al convertir audio: {e}")
        return None