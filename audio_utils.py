from elevenlabs import ElevenLabs, VoiceSettings
import io
from decouple import config
import logging  # Mejora añadida

# Configurar logging (mejora opcional)
logging.basicConfig(level=logging.INFO)

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def generate_audio_with_eleven_labs(text):
    try:
        # Validar texto vacío (mejora añadida)
        if not text.strip():
            raise ValueError("El texto para generar audio está vacío")

        audio_stream = client.text_to_speech.convert(
            text=text,
            voice_id=ELEVEN_LABS_VOICE_ID,
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(stability=0.7, similarity_boost=0.85)
        )

        buffer = io.BytesIO()
        for chunk in audio_stream:
            if chunk:  # Validación adicional (mejora)
                buffer.write(chunk)
        buffer.seek(0)
        
        logging.info(f"Audio generado correctamente para texto: {text[:50]}...")  # Mejora
        return buffer

    except Exception as e:
        logging.error(f"Error en ElevenLabs: {str(e)}")  # Mejora
        return None