# -*- coding: utf-8 -*-
"""
Módulo para generar audio con ElevenLabs.
Convierte texto en audio para su uso en llamadas telefónicas con Twilio.
"""

import io
import logging
import asyncio
from typing import Optional
from elevenlabs import set_api_key, generate
from decouple import config

# Configuración del sistema de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuración de la API Key de ElevenLabs
set_api_key(config("ELEVEN_LABS_API_KEY"))

# ==================================================
# 🔹 Generación de audio con ElevenLabs
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

        # Generar audio con ElevenLabs (sin necesidad de `asyncio.to_thread()`)
        audio_stream = await generate(
            text=text,
            voice=config("ELEVEN_LABS_VOICE_ID"),
            model="eleven_multilingual_v2",
            stability=0.3,        # Más expresividad y menos monotonía
            similarity_boost=0.8, # Permite más variabilidad en la voz
            style=0.5,
            speed=1.8,            # Ajustado para mejor naturalidad
            use_speaker_boost=True # Activa el boost de expresividad
        )

        # Verificar si hay contenido en el stream
        if not audio_stream:
            raise Exception("El stream de audio está vacío.")

        # Buffer para almacenar el audio
        buffer = io.BytesIO()
        buffer.write(audio_stream)
        buffer.seek(0)

        logger.info("✅ Audio generado con éxito")
        return buffer

    except ValueError as ve:
        logger.warning(f"⚠️ Error de validación en generación de audio: {str(ve)}")
        return None
    except Exception as e:
        logger.error(f"❌ Error en ElevenLabs al generar audio: {str(e)}")
        return None
