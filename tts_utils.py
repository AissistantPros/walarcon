# tts_utils.py
"""
Manejo de Text-to-Speech (TTS) con ElevenLabs.
Convierte texto a voz en formato compatible con Twilio.
"""

import time
import wave
import logging
from decouple import config
from elevenlabs import ElevenLabs, VoiceSettings

# Configuración de logs
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Configuración de ElevenLabs
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def text_to_speech(text: str, output_path: str = "respuesta_audio.wav") -> str:
    """
    Convierte texto a voz utilizando ElevenLabs y lo guarda en un archivo WAV
    que Twilio puede reproducir directamente.

    Args:
        text (str): Texto a convertir en audio.
        output_path (str, opcional): Ruta donde se guardará el archivo de audio.

    Returns:
        str: Ruta del archivo generado.
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
            output_format="pcm_16"
        )
        audio_data = b"".join(audio_stream)

        # Convertir PCM a WAV para Twilio
        with wave.open(output_path, "wb") as wav_file:
            wav_file.setnchannels(1)  # Mono
            wav_file.setsampwidth(2)  # 16-bit
            wav_file.setframerate(8000)  # 8000 Hz
            wav_file.writeframes(audio_data)

        logger.info(f"[TTS] Audio guardado en: {output_path} | Tiempo: {time.perf_counter() - start_total:.2f}s")
        return output_path

    except Exception as e:
        logger.error(f"[TTS] Error: {str(e)}")
        return ""
