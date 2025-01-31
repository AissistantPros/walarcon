from elevenlabs import ElevenLabs, VoiceSettings
from decouple import config
import io
import logging
import time
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = ElevenLabs(api_key=config("ELEVEN_LABS_API_KEY"))

def generate_audio_with_eleven_labs(text: str) -> Optional[io.BytesIO]:
    """
    Genera audio a partir de texto usando ElevenLabs API con manejo robusto de errores.
    
    Args:
        text (str): Texto a convertir en audio (máx. 5000 caracteres)
    
    Returns:
        io.BytesIO: Buffer de audio en formato MP3 o None en caso de error
    """
    try:
        # Validaciones iniciales
        if not text.strip():
            logger.error("🚨 Texto vacío recibido para generación de audio")
            return None
            
        if len(text) > 5000:
            logger.warning("⚠️ Texto demasiado largo, truncando a 5000 caracteres")
            text = text[:5000]

        start_time = time.time()
        
        # Configuración mejorada con valores desde .env
        audio = client.text_to_speech.convert(
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id=config("ELEVEN_MODEL_ID", default="eleven_multilingual_v2"),
            voice_settings=VoiceSettings(
                stability=config("VOICE_STABILITY", default=0.5, cast=float),
                similarity_boost=config("VOICE_SIMILARITY", default=0.8, cast=float),
                speed=config("VOICE_SPEED", default=1.2, cast=float)
            )
        )
        
        # Manejo eficiente del buffer de audio
        buffer = io.BytesIO()
        bytes_written = 0
        for chunk in audio:
            if chunk:
                buffer.write(chunk)
                bytes_written += len(chunk)
        
        if bytes_written == 0:
            raise ValueError("Respuesta vacía de ElevenLabs")
            
        buffer.seek(0)
        
        logger.info(f"🔊 Audio generado | Tiempo: {time.time() - start_time:.2f}s | Tamaño: {bytes_written / 1024:.2f}KB")
        return buffer
        
    except Exception as e:
        logger.error(f"❌ Error en generación de audio: {str(e)}")
        logger.debug(f"Texto problemático: {text[:100]}...")  # Log parcial para debug
        return None