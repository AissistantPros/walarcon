# -*- coding: utf-8 -*-
"""
Módulo para generar audio con ElevenLabs.
Convierte texto en audio para su uso en llamadas telefónicas con Twilio.
"""

import io
import logging
import asyncio
from typing import Optional
from elevenlabs import ElevenLabs, VoiceSettings
from decouple import config

# Configuración del sistema de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicialización del cliente de ElevenLabs con la API Key
client = ElevenLabs(api_key=config("ELEVEN_LABS_API_KEY"))

# ==================================================
# 🔹 Generación de audio con ElevenLabs (Corrección de async/await)
# ==================================================
async def generate_audio_with_eleven_labs(text: str) -> Optional[io.BytesIO]:
    """
    Convierte un texto en audio usando ElevenLabs.

    Parámetros:
        text (str): El texto que se convertirá en audio.

    Retorna:
        Optional[io.BytesIO]: Un buffer de audio en formato MP3 si la conversión es exitosa, de lo contrario None.
    """
    try:
        # Validación: El texto no debe estar vacío
        if not text.strip():
            raise ValueError("El texto para generar audio está vacío.")

        logger.info("🗣️ Generando audio con ElevenLabs...")

        # Convertir texto a audio en un hilo separado (para evitar bloqueos)
        audio_stream = await asyncio.to_thread(
        client.text_to_speech.convert,
        text=text,
        voice_id=config("ELEVEN_LABS_VOICE_ID"),
        model_id="eleven_multilingual_v2",
        voice_settings=VoiceSettings(
        stability=0.5,         # Más expresividad y menos monotonía
        similarity_boost=0.7,   # Permite más variabilidad en la voz
        style=0.9,
        speed=2,              # Aumenta la velocidad para mayor energía
        use_speaker_boost=True  # Activa el boost de expresividad
    )
)


        # Verificar si hay contenido en el stream
        if not audio_stream:
            raise Exception("El stream de audio está vacío.")

        # Buffer para almacenar el audio
        buffer = io.BytesIO()
        for chunk in audio_stream:
            buffer.write(chunk)

        buffer.seek(0)

        logger.info("✅ Audio generado con éxito")
        return buffer

    except ValueError as ve:
        logger.warning(f"⚠️ Error de validación en generación de audio: {str(ve)}")
        return None
    except Exception as e:
        logger.error(f"❌ Error en ElevenLabs al generar audio: {str(e)}")
        return None