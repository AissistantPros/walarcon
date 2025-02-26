# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio con depuración mejorada.
"""
import io
import logging
import time
import tempfile
import subprocess
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

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)
_speech_client = speech.SpeechClient(credentials=get_google_credentials())

def convert_mulaw_to_pcm8k(mulaw_data: bytes) -> io.BytesIO:
    """Convierte audio mu-law a PCM con logging mejorado."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".raw") as f_in:
            f_in.write(mulaw_data)
            input_path = f_in.name

        output_path = tempfile.mktemp(suffix=".wav")
        cmd = [
            "ffmpeg", "-y",
            "-f", "mulaw", "-ar", "8000", "-ac", "1",
            "-i", input_path,
            "-ar", "8000", "-ac", "1", output_path
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        with open(output_path, "rb") as f:
            wav_data = io.BytesIO(f.read())

        Path(input_path).unlink(missing_ok=True)
        Path(output_path).unlink(missing_ok=True)
        return wav_data
    except Exception as e:
        logger.error(f"[CONVERSIÓN] Error: {e}")
        return io.BytesIO()

def speech_to_text(mulaw_data: bytes) -> str:
    """Procesa STT con logging detallado."""
    start_total = time.perf_counter()
    logger.info(f"[STT] Inicio | Tamaño: {len(mulaw_data)} bytes")

    # Guardar audio original
    debug_path = DEBUG_DIR / f"stt_input_{time.time()}.ulaw"
    with open(debug_path, "wb") as f:
        f.write(mulaw_data)
    logger.info(f"[STT] Audio guardado: {debug_path}")

    if len(mulaw_data) < 2400:
        logger.info("[STT] Chunk demasiado corto (<300ms)")
        return ""

    try:
        wav_data = convert_mulaw_to_pcm8k(mulaw_data)
        if len(wav_data.getvalue()) < 44:
            logger.error("[STT] WAV inválido (encabezado faltante)")
            return ""

        # Configurar STT
        config_stt = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
        )
        audio_msg = speech.RecognitionAudio(content=wav_data.getvalue())

        # Llamar a Google
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

def text_to_speech(text: str) -> bytes:
    """Genera TTS con formato ulaw_8000."""
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
            output_format="ulaw_8000"
        )
        result = b"".join(audio_stream)
        logger.info(f"[TTS] Audio generado | Tiempo: {time.perf_counter() - start_total:.2f}s")
        return result
    except Exception as e:
        logger.error(f"[TTS] Error: {str(e)}")
        return b""