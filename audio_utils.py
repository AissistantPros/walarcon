# audio_utils.py

import io
import logging
import requests
from pydub import AudioSegment
from decouple import config
from openai import OpenAI
from elevenlabs.client import ElevenLabs
from elevenlabs import VoiceSettings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPENAI_API_KEY = config("CHATGPT_SECRET_KEY")
ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)
elevenlabs_client = ElevenLabs(api_key=ELEVEN_LABS_API_KEY)

# Buffer global para acumular audio entrante
AUDIO_BUFFER = b''

def text_to_speech(text: str) -> bytes:
    """
    Convierte texto en voz usando ElevenLabs con ajustes personalizados.
    Genera audio mu-law 8 kHz (ulaw_8000), compatible con Twilio.
    """
    try:
        # OJO: sin 'await' si esta función NO es async.
        audio_stream = elevenlabs_client.text_to_speech.convert(
            text=text,
            voice_id=config("ELEVEN_LABS_VOICE_ID"),
            model_id="eleven_multilingual_v2",
            voice_settings=VoiceSettings(
                stability=0.3,
                similarity_boost=0.8,
                style=0.5,
                speed=1.5,
                use_speaker_boost=True
            ),
            output_format="ulaw_8000"  # <--- Cambiado a ulaw_8000
        )
        return b''.join(audio_stream)
    except Exception as e:
        logger.error(f"❌ Error ElevenLabs: {str(e)}")
        return b''

def accumulate_audio_mulaw(chunk: bytes):
    """
    Acumula chunks de audio mu-law en un buffer global.
    Devuelve True si ya tenemos más de 0.1 seg, False si no.
    """
    global AUDIO_BUFFER
    AUDIO_BUFFER += chunk

    # 800 bytes ~ 0.1 seg a 8000 Hz mu-law
    if len(AUDIO_BUFFER) >= 800:
        return True
    return False

def convert_mulaw_to_wav(mulaw_data: bytes) -> io.BytesIO:
    """
    Convierte mu-law 8kHz a WAV para poder enviarlo a Whisper.
    """
    segment = AudioSegment(
        data=mulaw_data,
        sample_width=1,  # mu-law = 8 bits
        frame_rate=8000,
        channels=1
    )
    wav_bytes = io.BytesIO()
    segment.export(wav_bytes, format="wav")
    wav_bytes.seek(0)
    return wav_bytes

def transcribe_whisper(wav_data: io.BytesIO) -> str:
    """
    Envía un WAV a Whisper y retorna el texto.
    """
    try:
        files = {
            "file": ("audio.wav", wav_data, "audio/wav")
        }
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}"
        }
        resp = requests.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers=headers,
            files=files,
            data={"model": "whisper-1"}
        )

        if resp.status_code == 200:
            return resp.json().get("text", "").strip()
        else:
            logger.error(f"❌ Error Whisper: {resp.status_code} - {resp.text}")
            return ""
    except Exception as e:
        logger.error(f"❌ Error transcribe_whisper: {str(e)}")
        return ""

def process_incoming_audio(chunk: bytes) -> str:
    """
    Llamar cada vez que Twilio mande un chunk de audio.
    Acumula y, si ya hay suficiente, convierte a WAV y lo manda a Whisper.
    """
    global AUDIO_BUFFER

    # Acumula chunk
    if not accumulate_audio_mulaw(chunk):
        return ""  # aún es muy corto

    # Si ya hay suficiente (>=0.1 seg),
    # convertimos a WAV y limpiamos buffer
    wav_data = convert_mulaw_to_wav(AUDIO_BUFFER)
    AUDIO_BUFFER = b''  # limpiar buffer

    # Llamamos a Whisper
    text = transcribe_whisper(wav_data)
    return text
