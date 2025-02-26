# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio con filtros mejorados.
"""
import io
import logging
import time
import tempfile
import subprocess
import numpy as np
import librosa
import noisereduce as nr
import webrtcvad
import wave
import scipy.signal
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

# Aumentamos a 2 para ser más estricto con el ruido
vad = webrtcvad.Vad(1)

def convert_mulaw_to_pcm16k(mulaw_data: bytes) -> io.BytesIO:
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

def apply_noise_reduction_and_vad(audio_bytes: bytes, sr=16000) -> bytes:
    start_proc = time.perf_counter()
    try:
        audio_data, _ = librosa.load(io.BytesIO(audio_bytes), sr=sr)

        if len(audio_data) < 1600:
            logger.info("[apply_noise_reduction_and_vad] Audio MUY corto, se omite noisereduce.")
            reduced = audio_data
        else:
            reduced = nr.reduce_noise(y=audio_data, sr=sr, stationary=True)

        # Filtro pasa-banda (300Hz - 3400Hz)
        sos = scipy.signal.butter(4, [300, 3400], 'bandpass', fs=sr, output='sos')
        filtered = scipy.signal.sosfilt(sos, reduced)

        # VAD mejorado
        samples = np.int16(filtered * 32767)
        frame_ms = 10
        frame_len = int(sr * frame_ms / 1000)
        voiced_frames = []
        idx = 0

        while idx + frame_len <= len(samples):
            frame = samples[idx:idx+frame_len]
            idx += frame_len
            if vad.is_speech(frame.tobytes(), sr):
                voiced_frames.extend(frame)

        if not voiced_frames:
            logger.info("[apply_noise_reduction_and_vad] No se detectó voz tras VAD.")
            return b""

        final_array = np.array(voiced_frames, dtype=np.int16)
        buf_out = io.BytesIO()
        with wave.open(buf_out, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(final_array.tobytes())

        buf_out.seek(0)
        end_proc = time.perf_counter()
        logger.info(f"[apply_noise_reduction_and_vad] OK en {end_proc - start_proc:.3f}s")
        return buf_out.read()

    except Exception as e:
        logger.error(f"[apply_noise_reduction_and_vad] Error: {e}")
        return b""

def speech_to_text(mulaw_data: bytes) -> str:
    start_total = time.perf_counter()
    # Evita procesar si el chunk es muy corto
    if len(mulaw_data) < 2400:
        logger.info(f"[speech_to_text] Chunk <300ms => skip. len={len(mulaw_data)}")
        return ""

    try:
        wav_data = convert_mulaw_to_pcm16k(mulaw_data)
        if len(wav_data.getvalue()) < 44:
            return ""

        processed_wav = apply_noise_reduction_and_vad(wav_data.getvalue())
        if len(processed_wav) < 44:
            return ""

        config_stt = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="es-MX",
        )
        audio_msg = speech.RecognitionAudio(content=processed_wav)
        response = _speech_client.recognize(config=config_stt, audio=audio_msg)

        if not response.results:
            return ""
        transcript = response.results[0].alternatives[0].transcript.strip()
        end_total = time.perf_counter()
        logger.info(f"[speech_to_text] Texto='{transcript}' total={end_total - start_total:.3f}s")
        return transcript

    except Exception as e:
        logger.error(f"[speech_to_text] Error: {e}")
        return ""

def text_to_speech(text: str) -> bytes:
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
