# -*- coding: utf-8 -*-
"""
Módulo para generar audio con ElevenLabs.
Convierte texto en audio para su uso en llamadas telefónicas con Twilio.
"""

# ==================================================
# Parte 1: Importaciones y Configuración
# ==================================================
from elevenlabs import ElevenLabs, VoiceSettings
from decouple import config
import io
import logging
import time
from typing import Optional

# Configuración del sistema de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Inicialización del cliente de ElevenLabs con la API Key
client = ElevenLabs(api_key=config("ELEVEN_LABS_API_KEY"))








# ==================================================
# Parte 2: Generación de audio con ElevenLabs
# ==================================================
def generate_audio_with_eleven_labs(text: str) -> Optional[io.BytesIO]:
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

        # Medir el tiempo de generación
        start_time = time.time()

        # Configuración de la voz y modelo
        audio_stream = client.text_to_speech.convert(
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.5,         # Controla la variabilidad en la voz
                similarity_boost=0.8,  # Aumenta la similitud con la voz predefinida
                speed=1.2              # Ajusta la velocidad del habla
            )
        )

        # Buffer para almacenar el audio
        buffer = io.BytesIO()
        for chunk in audio_stream:
            buffer.write(chunk)
        buffer.seek(0)

        # Log de tiempo de generación
        logger.info(f"🔊 Audio generado en {time.time() - start_time:.2f}s")
        return buffer

    except ValueError as ve:
        # Log para errores de validación (texto vacío)
        logger.warning(f"⚠️ Error de validación en generación de audio: {str(ve)}")
        return None
    except Exception as e:
        # Log de errores generales en la conversión de texto a voz
        logger.error(f"❌ Error en ElevenLabs al generar audio: {str(e)}")
        return None









# ==================================================
# Parte 3: Prueba Local del Módulo
# ==================================================
if __name__ == "__main__":
    """
    Prueba rápida de la conversión de texto a audio.
    Se recomienda ejecutar este archivo directamente para verificar la funcionalidad.
    """
    test_text = "Hola, esta es una prueba del sistema de generación de voz."
    audio_buffer = generate_audio_with_eleven_labs(test_text)

    if audio_buffer:
        with open("test_audio.mp3", "wb") as f:
            f.write(audio_buffer.getvalue())
        print("✅ Prueba exitosa: Archivo 'test_audio.mp3' generado.")
    else:
        print("❌ Prueba fallida: No se generó audio.")
