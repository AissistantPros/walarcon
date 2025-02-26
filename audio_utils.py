# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio con integración de Google STT y ElevenLabs TTS.
"""
import io
import wave
import logging
import time
from pathlib import Path
from decouple import config
from google.cloud import speech
from google.oauth2.service_account import Credentials
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEBUG_DIR = Path(__file__).parent / "audio_debug"
DEBUG_DIR.mkdir(exist_ok=True)

# ==================================================
# 🔑 CONFIGURACIÓN DE GOOGLE STT
# ==================================================
def get_google_credentials():
    credentials_info = {
        "type": config("STT_TYPE"),
        "project_id": config("STT_PROJECT_ID"),
        "private_key_id": config("STT_PRIVATE_KEY_ID"),
        "private_key": config("STT_PRIVATE_KEY").replace("\\n", "\n"),
        "client_email": config("STT_CLIENT_EMAIL"),
        "client_id": config("STT_CLIENT_ID"),
        "auth_uri": config("STT_AUTH_URI"),
        "token_uri": config("STT_TOKEN_URI", default="https://oauth2.googleapis.com/token"),
        "auth_provider_x509_cert_url": config("STT_AUTH_PROVIDER_X509_CERT_URL"),
        "client_x509_cert_url": config("STT_CLIENT_X509_CERT_URL")
    }
    return Credentials.from_service_account_info(credentials_info)

_speech_client = speech.SpeechClient(credentials=get_google_credentials())

# ==================================================
# 🎙️ FUNCIÓN DE SPEECH-TO-TEXT (STT)
# ==================================================
def speech_to_text(mulaw_data: bytes) -> str:
    """
    Envía datos de audio directamente a Google Speech-to-Text sin conversión previa.
    Se asume que el audio está en formato MULAW con 8000 Hz.

    Args:
        mulaw_data (bytes): Datos de audio en formato raw.

    Returns:
        str: Transcripción generada por Google STT.
    """
    start_total = time.perf_counter()
    logger.info(f"[STT] Inicio | Tamaño: {len(mulaw_data)} bytes")

    # Guardar audio original para depuración
    debug_path = DEBUG_DIR / f"stt_input_{time.time()}.ulaw"
    with open(debug_path, "wb") as f:
        f.write(mulaw_data)
    logger.info(f"[STT] Audio guardado: {debug_path}")

    if len(mulaw_data) < 2400:
        logger.info("[STT] Chunk demasiado corto (<300ms)")
        return ""

    try:
        # Configuración para Google STT (directo en MULAW)
        config_stt = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.MULAW,
            sample_rate_hertz=8000,  # Asegurarse de que coincida con Twilio
            language_code="es-MX",
        )
        audio_msg = speech.RecognitionAudio(content=mulaw_data)

        # Llamar a Google STT
        response = _speech_client.recognize(config=config_stt, audio=audio_msg)
        if not response.results:
            logger.info("[STT] Sin resultados")
            return ""

        transcript = response.results[0].alternatives[0].transcript.strip()
        logger.info(f"[STT] Transcripción: '{transcript}' | Tiempo: {time.perf_counter() - start_total:.2f}s")
        return transcript

    except Exception as e:
        logger.error(f"[STT] Error crítico: {str(e)}")
        return ""

# ==================================================
# 🔊 CONFIGURACIÓN DE ELEVENLABS (TTS)
# ==================================================
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

def text_to_speech(text: str, output_path: str = "respuesta_audio.wav") -> str:
    """
    Convierte texto a voz utilizando ElevenLabs y lo guarda en un archivo WAV,
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
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
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

# ==================================================
# 🛠️ PRUEBA DE FUNCIONES
# ==================================================
if __name__ == "__main__":
    test_audio_data = b"Test audio data for STT"  # Simulación de datos de audio en MULAW
    transcript = speech_to_text(test_audio_data)
    print("Transcripción de prueba:", transcript)

    output_wav = text_to_speech("Mensaje de prueba")
    print("Archivo de audio generado para Twilio:", output_wav)
