import time
import wave
import logging
import io
import os
import audioop  # type: ignore # <-- Asegúrate de importar audioop
from decouple import config
from elevenlabs import ElevenLabs, VoiceSettings

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def text_to_speech(text: str) -> bytes:
    """
    Convierte texto a voz utilizando ElevenLabs y devuelve el audio en formato mu-law (raw PCM convertido) como bytes.
    Twilio espera audio en formato mu-law (8 kHz, mono, 8 bits).
    
    Args:
        text (str): Texto a convertir.
        
    Returns:
        bytes: Audio en formato mu-law o b"" en caso de error.
    """
    start_total = time.perf_counter()
    try:
        # Opcional: puedes pasar output_options si la API lo admite, para asegurar 8kHz y 1 canal
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
            # Si la API lo permite, podrías agregar: output_options={"sample_rate": 8000, "channels": 1}
        )
        audio_data = b"".join(audio_stream)
        logger.info(f"[TTS] Audio generado en memoria (raw PCM) | Tiempo: {time.perf_counter() - start_total:.2f}s")
        
        # Convertir el audio PCM (16 bits) a mu-law (8 bits)
        try:
            mulaw_data = audioop.lin2ulaw(audio_data, 2)  # 2 = sample width de 16 bits
            logger.info(f"[TTS] Audio convertido a mu-law | Tamaño: {len(mulaw_data)} bytes")
        except Exception as conv_err:
            logger.error(f"[TTS] Error al convertir a mu-law: {conv_err}")
            return b""
        
        # Opcional: guardar archivo para depuración
        debug_path = os.path.join("debug_tts.ulaw")
        with open(debug_path, "wb") as f:
            f.write(mulaw_data)
        logger.info(f"[TTS] Archivo de depuración guardado en: {debug_path}")

        return mulaw_data

    except Exception as e:
        logger.error(f"[TTS] Error: {str(e)}")
        return b""
