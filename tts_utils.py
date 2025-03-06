import time
import wave
import logging
import io
from decouple import config
from elevenlabs import ElevenLabs, VoiceSettings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def text_to_speech(text: str) -> bytes:
    """
    Convierte texto a voz utilizando ElevenLabs y devuelve el audio en formato WAV como bytes.
    Se usa BytesIO para evitar la escritura en disco.
    
    Args:
        text (str): Texto a convertir.
        
    Returns:
        bytes: Audio WAV (mono, 16-bit, 8000 Hz) o b"" en caso de error.
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
            output_format="pcm_8000"  # Formato compatible
        )
        audio_data = b"".join(audio_stream)
        
        # Escribir el audio PCM a WAV en memoria usando BytesIO
        wav_io = io.BytesIO()
        with wave.open(wav_io, "wb") as wav_file:
            wav_file.setnchannels(1)     # Mono
            wav_file.setsampwidth(2)       # 16-bit
            wav_file.setframerate(8000)    # 8000 Hz
            wav_file.writeframes(audio_data)
        wav_bytes = wav_io.getvalue()
        logger.info(f"[TTS] Audio generado en memoria | Tiempo: {time.perf_counter() - start_total:.2f}s")
        return wav_bytes
    except Exception as e:
        logger.error(f"[TTS] Error: {str(e)}")
        return b""
