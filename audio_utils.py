# -*- coding: utf-8 -*-
"""
Módulo para audio con ajustes de voz y formato PCMU (Twilio).
"""

import io
import logging
import asyncio
from decouple import config
from openai import OpenAI
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializar clientes
openai_client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))
elevenlabs_client = ElevenLabs(api_key=config("ELEVEN_LABS_API_KEY"))

async def speech_to_text(audio_bytes: bytes) -> str:
    try:
        audio_buffer = io.BytesIO(audio_bytes)
        audio_buffer.name = "audio.mp3"
        
        transcript = await asyncio.to_thread(
            openai_client.audio.transcriptions.create,
            model="whisper-1",
            file=audio_buffer,
            language="es"
        )
        return transcript.text.strip()
    except Exception as e:
        logger.error(f"❌ Error Whisper: {str(e)}")
        return ""

async def text_to_speech(text: str) -> bytes:
    try:
        audio_stream = await asyncio.to_thread(
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
            output_format="pcm_mulaw"  # Formato requerido por Twilio
        )
        return b''.join(audio_stream)
    except Exception as e:
        logger.error(f"❌ Error ElevenLabs: {str(e)}")
        return b''