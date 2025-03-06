import time
import logging
from decouple import config
from elevenlabs import ElevenLabs, VoiceSettings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def text_to_speech(text: str) -> bytes:
    """
    Convierte texto a voz utilizando ElevenLabs y devuelve el audio en formato raw PCM como bytes.
    Se espera que el PCM sea 8 kHz, 16 bits, mono, sin contenedor WAV.
    
    Args:
        text (str): Texto a convertir.
        
    Returns:
        bytes: Audio raw PCM o b"" en caso de error.
    """
    start_total = time.perf_counter()
    try:
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=ELEVEN_LABS_VOICE_ID,
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.8,
                similarity_boost=0.12,
                style=0.4,
                speed=1.2,
                use_speaker_boost=False
            ),
            output_format="pcm_8000"  # Solicita raw PCM a 8000 Hz
        )
        audio_data = b"".join(audio_stream)
        logger.info(f"[TTS] Audio generado en memoria (raw PCM) | Tiempo: {time.perf_counter() - start_total:.2f}s")
        return audio_data
    except Exception as e:
        logger.error(f"[TTS] Error: {str(e)}")
        return b""
