# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio SIN filtros ni VAD (versión de prueba).
"""
import io
import logging
import time
import tempfile
import subprocess
from decouple import config
from google.cloud import speech
from google.oauth2.service_account import Credentials
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

_credentials = get_google_credentials()
_speech_client = speech.SpeechClient(credentials=_credentials)

def convert_mulaw_to_pcm16k(mulaw_data: bytes) -> io.BytesIO:
    """
    Convierte de mu-law (8k mono) a PCM 16 kHz mono sin aplicar ningún filtro.
    """
    try:
        with tempfile.NamedTemporaryFile(suffix=".raw", delete=False) as f_in:
            f_in.write(mulaw_data)
            input_path = f_in.name
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f_out:
            output_path = f_out.name

        cmd = [
            "ffmpeg", "-y",
            "-f", "mulaw",
            "-ar", "8000",
            "-ac", "1",
            "-i", input_path,
            "-ar", "16000",
            "-ac", "1",
            output_path
        ]
        subprocess.run(cmd, check=True)

        with open(output_path, "rb") as f:
            wav_data = io.BytesIO(f.read())

        import os
        os.remove(input_path)
        os.remove(output_path)

        wav_data.seek(0)
        return wav_data
    except Exception as e:
        logger.error(f"[convert_mulaw_to_pcm16k] Error: {e}")
        return io.BytesIO()

def speech_to_text(mulaw_data: bytes) -> str:
    """
    Envía audio TAL CUAL a Google STT, sin VAD ni reducciones.
    """
    start_total = time.perf_counter()
    if len(mulaw_data) < 2400:
        logger.info(f"[speech_to_text] Chunk <300ms => skip. len={len(mulaw_data)}")
        return ""

    try:
        # 1) Convertimos a PCM 16k
        wav_data = convert_mulaw_to_pcm16k(mulaw_data)
        if len(wav_data.getvalue()) < 44:
            logger.info("[speech_to_text] WAV result is too short => skip.")
            return ""

        # 2) Config de reconocimiento
        config_stt = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="es-MX",
        )
        audio_msg = speech.RecognitionAudio(content=wav_data.getvalue())

        # 3) Llamamos a Google STT
        response = _speech_client.recognize(config=config_stt, audio=audio_msg)
        if not response.results:
            logger.info("[speech_to_text] STT sin resultados => ''")
            return ""

        # Tomamos la primera hipótesis
        transcript = response.results[0].alternatives[0].transcript.strip()
        end_total = time.perf_counter()
        logger.info(f"[speech_to_text] Texto='{transcript}' total={end_total - start_total:.3f}s")
        return transcript

    except Exception as e:
        logger.error(f"[speech_to_text] Error: {e}")
        return ""

def text_to_speech(text: str) -> bytes:
    """
    Envía el texto a ElevenLabs sin cambios, genera audio ulaw_8000.
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
            output_format="ulaw_8000"
        )
        result = b"".join(audio_stream)
        end_total = time.perf_counter()
        logger.info(f"[TTS] Final => {end_total - start_total:.3f}s. Bytes={len(result)}")
        return result
    except Exception as e:
        logger.error(f"[TTS] Error: {e}")
        return b""
