# -*- coding: utf-8 -*-
"""
Módulo para manejo de audio: reducción de ruido, transcripción y síntesis de voz.
"""

import io
import logging
import httpx
import numpy as np
import librosa
import noisereduce as nr
import webrtcvad
import wave
from decouple import config
from openai import OpenAI
from elevenlabs import ElevenLabs, VoiceSettings
from pydub import AudioSegment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Claves API
OPENAI_API_KEY = config("CHATGPT_SECRET_KEY")
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")

# Inicializar clientes
openai_client = OpenAI(api_key=OPENAI_API_KEY)
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

# Configuración de WebRTC VAD para detectar voz
vad = webrtcvad.Vad(3)  # Nivel 3 = agresivo (menos ruido de fondo)

def apply_noise_reduction(audio_data, sample_rate=16000):
    """Reduce ruido en el audio usando noisereduce."""
    return nr.reduce_noise(y=audio_data, sr=sample_rate, stationary=True)

def convert_mulaw_to_wav(mulaw_data: bytes) -> io.BytesIO:
    """Convierte audio mu-law 8kHz a WAV (16-bit PCM, 16kHz)."""
    try:
        segment = AudioSegment(
            data=mulaw_data,
            sample_width=1,
            frame_rate=8000,
            channels=1
        )
        segment = segment.set_frame_rate(16000).set_sample_width(2)
        segment = segment.normalize(headroom=5)  # Baja el volumen para evitar ruido de fondo
        wav_data = io.BytesIO()
        segment.export(wav_data, format="wav")
        wav_data.seek(0)
        return wav_data
    except Exception as e:
        logger.error(f"❌ Error al convertir mu-law a WAV: {e}")
        return io.BytesIO()

def contains_voice(y, sr, frame_duration_ms=20, voice_threshold=0.5):
    """
    Detecta si hay voz en el audio dividiéndolo en frames de duración fija.
    Se consideran frames de 20 ms y si la proporción de frames con voz supera
    el umbral (por defecto 0.5), se determina que hay voz.
    """
    frame_length = int(sr * frame_duration_ms / 1000)
    num_frames = len(y) // frame_length
    voiced_frames = 0
    for i in range(num_frames):
        start = i * frame_length
        end = start + frame_length
        frame = y[start:end]
        if len(frame) < frame_length:
            break
        frame_bytes = np.int16(frame * 32768).tobytes()
        if vad.is_speech(frame_bytes, sr):
            voiced_frames += 1
    if num_frames == 0:
        return False
    return (voiced_frames / num_frames) > voice_threshold

def numpy_to_wav_bytes(y, sr):
    """Convierte un array de numpy a un archivo WAV en memoria."""
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(sr)
        wf.writeframes(np.int16(y * 32768).tobytes())
    buf.seek(0)
    return buf

async def speech_to_text(audio_bytes: bytes) -> str:
    """Convierte audio a texto usando Whisper, filtrando ruido y usando VAD."""
    try:
        wav_data = convert_mulaw_to_wav(audio_bytes)
        y, sr = librosa.load(wav_data, sr=16000)
        y_denoised = apply_noise_reduction(y, sr)  # Aplica reducción de ruido

        # Verifica si hay voz dividiendo el audio en frames adecuados
        if not contains_voice(y_denoised, sr):
            logger.warning("⚠️ No se detectó voz, descartando.")
            return ""

        # Convierte el audio procesado a un archivo WAV en memoria
        processed_wav = numpy_to_wav_bytes(y_denoised, sr)

        async with httpx.AsyncClient() as client:
            files = {"file": ("audio.wav", processed_wav, "audio/wav")}
            headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
            data = {"model": "whisper-1", "temperature": 0.1, "suppress_tokens": [-1]}

            resp = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers=headers,
                files=files,
                data=data
            )

        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        else:
            logger.error(f"❌ Error Whisper: {resp.status_code} - {resp.text}")
            return ""
    except Exception as e:
        logger.error(f"❌ Error en speech_to_text: {str(e)}")
        return ""

def text_to_speech(text: str, lang="es") -> bytes:
    """Convierte texto a voz usando ElevenLabs."""
    try:
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.4,
                similarity_boost=0.7,
                style=0.3,
                speed=1.3,
                use_speaker_boost=False
            ),
            output_format="ulaw_8000"
        )
        return b"".join(audio_stream)
    except Exception as e:
        logger.error(f"❌ Error en text_to_speech: {str(e)}")
        return b""
