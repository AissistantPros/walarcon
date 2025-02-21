# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio: reducción de ruido, VAD agresivo, transcripción y síntesis de voz.
Recibe mu-law 8kHz, lo convierte a PCM 16kHz, aplica noisereduce + webrtcVAD,
y manda chunks filtrados a Google STT (cuando se llame a speech_to_text).
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
from decouple import config
from google.cloud import speech
from google.oauth2.service_account import Credentials
from elevenlabs import ElevenLabs, VoiceSettings

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
        "client_x509_cert_url": config("STT_CLIENT_X509_CERT_URL"),
    }
    return Credentials.from_service_account_info(credentials_info)

ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

# Instancia global (opcional) para SpeechClient, para ahorrar overhead
_credentials = get_google_credentials()
_speech_client = speech.SpeechClient(credentials=_credentials)

# VAD agresivo
vad = webrtcvad.Vad(2)

def convert_mulaw_to_pcm16k(mulaw_data: bytes) -> io.BytesIO:
    """
    Convierte mu-law 8kHz a WAV PCM 16kHz, 16-bit, mono usando ffmpeg por subprocess.
    Retorna BytesIO con el WAV resultante.
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

        # limpieza
        import os
        os.remove(input_path)
        os.remove(output_path)

        wav_data.seek(0)
        return wav_data
    except Exception as e:
        logger.error(f"[convert_mulaw_to_pcm16k] Error: {e}")
        return io.BytesIO()















def apply_noise_reduction_and_vad(audio_bytes: bytes, sr=16000) -> bytes:
    """
    Aplica reducción de ruido y filtra frames sin voz usando webrtcvad.
    Retorna un WAV 16kHz PCM con solo frames que tienen voz.
    """
    import time
    start_proc = time.perf_counter()

    # 1) Cargar el audio con librosa
    audio_data, _ = librosa.load(io.BytesIO(audio_bytes), sr=sr)

    # Si el audio es muy corto (menos de ~0.1 s, por ejemplo),
    # no vale la pena noisereduce, pues STFT de noisereduce dará problemas.
    # Ajusta 0.1 s * sr=16000 => 1600 muestras:
    if len(audio_data) < 1600:
        logger.info("[apply_noise_reduction_and_vad] Audio MUY corto, se omite noisereduce.")
        reduced = audio_data  # no tocamos la señal
    else:
        # Aplica reduce_noise normalmente
        reduced = nr.reduce_noise(y=audio_data, sr=sr, stationary=True)

    # 2) Dividir en frames y aplicar webrtcvad
    frame_ms = 20
    frame_len = int(sr * frame_ms / 1000)
    voiced_frames = []
    idx = 0
    samples = np.int16(reduced * 32767)  # convertir a int16

    while idx + frame_len <= len(samples):
        frame = samples[idx:idx+frame_len]
        idx += frame_len
        raw_frame = frame.tobytes()
        if vad.is_speech(raw_frame, sr):
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
    logger.info(f"[apply_noise_reduction_and_vad] Procesado en {end_proc - start_proc:.3f}s, frames con voz: {len(voiced_frames)}")
    return buf_out.read()

















def speech_to_text(mulaw_data: bytes) -> str:
    """
    Convierte mu-law 8kHz a PCM 16kHz, aplica reducción de ruido y VAD,
    y transcribe con Google STT. Retorna texto.
    Filtra si no hay data >100 bytes.
    """
    start_total = time.perf_counter()
    if len(mulaw_data) < 100:
        # chunk demasiado pequeño => skip
        logger.info(f"[speech_to_text] Chunk con <100 bytes (len={len(mulaw_data)}) => ''")
        return ""

    try:
        # 1) Convert mulaw -> pcm16k
        wav_data = convert_mulaw_to_pcm16k(mulaw_data)
        if len(wav_data.getvalue()) < 44:  # WAV header ~44 bytes
            logger.info("[speech_to_text] Audio convertido vacío => ''")
            return ""

        # 2) noise reduction + VAD
        processed_wav = apply_noise_reduction_and_vad(wav_data.getvalue(), sr=16000)
        if len(processed_wav) < 44:
            logger.info("[speech_to_text] Post-VAD => no frames => ''")
            return ""

        # 3) Llamar a STT
        config_stt = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code="es-MX",  # idioma principal
            alternative_language_codes=["en-US", "fr-FR", "ru-RU", "he-IL", "zh-CN"]
        )

        audio_msg = speech.RecognitionAudio(content=processed_wav)

        start_rec = time.perf_counter()
        response = _speech_client.recognize(config=config_stt, audio=audio_msg)
        end_rec = time.perf_counter()
        logger.info(f"[speech_to_text] Google STT reconocimiento en {end_rec - start_rec:.3f}s")

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
    """
    ElevenLabs con output ulaw_8000. Regresa bytes.
    Ajusta style/ speed para reducir latencia y longitud de audio.
    """
    start_total = time.perf_counter()
    try:
        start_conv = time.perf_counter()
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.5,
                similarity_boost=0.8,
                style=0.9,     # Menos expresivo => genera más rápido
                speed=1.5,     # Reducir velocidad final
                use_speaker_boost=False
            ),
            output_format="ulaw_8000"
        )
        end_conv = time.perf_counter()
        logger.info(f"[TTS] ElevenLabs convert => {end_conv - start_conv:.3f}s")

        result = b"".join(audio_stream)
        end_total = time.perf_counter()
        logger.info(f"[TTS] Final => {end_total - start_total:.3f}s. Bytes={len(result)}")
        return result
    except Exception as e:
        logger.error(f"[TTS] Error: {e}")
        return b""
