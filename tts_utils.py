import time
import wave
import logging
import os
from decouple import config
from elevenlabs import ElevenLabs, VoiceSettings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def text_to_speech(text: str) -> bytes:
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

        # Guardar audio en disco para depuración
        output_path = os.path.join("audio", "respuesta_audio_debug.wav")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "wb") as f:
            # Usamos wave para convertir PCM a WAV
            with wave.open(f, "wb") as wav_file:
                wav_file.setnchannels(1)   # Mono
                wav_file.setsampwidth(2)     # 16-bit
                wav_file.setframerate(8000)  # 8000 Hz
                wav_file.writeframes(audio_data)
        
        logger.info(f"[TTS] Audio guardado en: {output_path} | Tiempo: {time.perf_counter() - start_total:.2f}s")
        
        # Leer el archivo y devolver los bytes (para mantener la interfaz)
        with open(output_path, "rb") as f:
            return f.read()
    except Exception as e:
        logger.error(f"[TTS] Error: {str(e)}")
        return b""
