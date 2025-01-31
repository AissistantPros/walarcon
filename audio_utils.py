from elevenlabs import ElevenLabs, VoiceSettings
import io
from decouple import config
import logging
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = ElevenLabs(api_key=config("ELEVEN_LABS_API_KEY"))

def generate_audio_with_eleven_labs(text: str):
    try:
        start_time = time.time()
        
        audio = client.text_to_speech.convert(
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.5,
                similarity_boost=0.8,
                speed=1.2  # +20% de velocidad
            )
        )
        
        buffer = io.BytesIO()
        for chunk in audio:
            buffer.write(chunk)
        buffer.seek(0)
        
        logger.info(f"🔊 Audio generado en {time.time() - start_time:.2f}s")
        return buffer
        
    except Exception as e:
        logger.error(f"❌ Error en ElevenLabs: {str(e)}")
        return None
