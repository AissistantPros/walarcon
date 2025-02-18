# -*- coding: utf-8 -*-
"""
Módulo para manejar la transcripción de voz a texto (STT) con Whisper de OpenAI
y la generación de audio (TTS) con ElevenLabs.
"""

import io
import logging
import asyncio
import time
from typing import Optional
from decouple import config
from openai import OpenAI
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings

# Configuración del sistema de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicializar clientes de OpenAI y ElevenLabs
openai_client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))
elevenlabs_client = ElevenLabs(api_key=config("ELEVEN_LABS_API_KEY"))

# ==================================================
# 🔹 Transcripción de Audio con Whisper (OpenAI)
# ==================================================
async def speech_to_text(audio_bytes: bytes) -> Optional[str]:
    """
    Convierte audio en texto utilizando Whisper de OpenAI.

    Parámetros:
        audio_bytes (bytes): El audio en formato de bytes.

    Retorna:
        Optional[str]: Texto transcrito si la conversión es exitosa, de lo contrario None.
    """
    try:
        start_time = time.time()  # Medir tiempo de ejecución
        logger.info("🎙️ Procesando audio con Whisper...")

        # Guardar temporalmente el audio en un buffer para enviarlo a Whisper
        audio_buffer = io.BytesIO(audio_bytes)
        audio_buffer.name = "audio.mp3"  # Whisper requiere un nombre de archivo

        # Enviar audio a OpenAI Whisper
        response = await asyncio.to_thread(
            openai_client.audio.transcriptions.create,
            model="whisper-1",
            file=audio_buffer,
            language="es"
        )

        transcript = response.text.strip()

        if not transcript:
            raise Exception("Whisper no pudo transcribir el audio.")

        end_time = time.time()
        logger.info(f"✅ Transcripción completada en {end_time - start_time:.2f} seg: {transcript}")

        return transcript

    except Exception as e:
        logger.error(f"❌ Error en transcripción con Whisper: {str(e)}")
        return None

# ==================================================
# 🔹 Generación de Audio con ElevenLabs
# ==================================================
async def text_to_speech(text: str) -> Optional[bytes]:
    """
    Convierte un texto en audio usando ElevenLabs.

    Parámetros:
        text (str): El texto que se convertirá en audio.

    Retorna:
        Optional[bytes]: Audio en formato MP3 si la conversión es exitosa, de lo contrario None.
    """
    try:
        if not text.strip():
            raise ValueError("El texto para generar audio está vacío.")

        start_time = time.time()
        logger.info(f"🗣️ Generando audio con ElevenLabs...")

        # Convertir texto a audio en un hilo separado
        audio_stream = await asyncio.to_thread(
            elevenlabs_client.text_to_speech.convert,
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.3,         # Controla la estabilidad de la voz
                similarity_boost=0.8,  # Ajusta la similitud con la voz base
                style=0.5,             # Ajuste de estilo (naturalidad)
                speed=1.5,             # Velocidad de la voz (ajustable según necesidad)
                use_speaker_boost=True # Potencia la expresividad de la voz
            )
        )

        # Convertir el audio stream a bytes
        audio_bytes = b''.join(audio_stream)

        end_time = time.time()
        logger.info(f"✅ Audio generado en {end_time - start_time:.2f} seg.")

        return audio_bytes

    except ValueError as ve:
        logger.warning(f"⚠️ Error de validación en TTS: {str(ve)}")
        return None
    except Exception as e:
        logger.error(f"❌ Error en ElevenLabs al generar audio: {str(e)}")
        return None
