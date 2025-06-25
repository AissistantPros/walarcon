import requests
from decouple import config
import logging
import subprocess
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_elevenlabs")

def test_elevenlabs_ulaw():
    ELEVEN_LABS_API_KEY = config("ELEVEN_LABS_API_KEY")
    ELEVEN_LABS_VOICE_ID = config("ELEVEN_LABS_VOICE_ID")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_LABS_VOICE_ID}/stream"
    headers = {
        "xi-api-key": ELEVEN_LABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/wav"  # Critical for μ-law WAV
    }
    data = {
        "text": "Prueba técnica de audio en formato μ-law para Twilio",
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {
            "stability": 0.7,
            "style": 0.5,
            "use_speaker_boost": False,
            "speed": 1.2
        },
        "output_format": "ulaw_8000"
    }

    try:
        # 1. Descargar el archivo WAV μ-law
        response = requests.post(url, json=data, headers=headers, stream=True)
        response.raise_for_status()
        
        wav_path = "/Users/esteban/Desktop/prueba_ulaw.wav"
        with open(wav_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=1024):
                if chunk:
                    f.write(chunk)
        
        logger.info(f"✅ WAV μ-law descargado ({os.path.getsize(wav_path)} bytes)")

        # 2. Extraer el audio raw μ-law del WAV
        raw_path = "/Users/esteban/Desktop/prueba_ulaw_fixed.raw"
        ffmpeg_cmd = [
            "ffmpeg",
            "-i", wav_path,
            "-f", "mulaw",      # Forzar formato μ-law
            "-ar", "8000",      # 8kHz sample rate
            "-ac", "1",         # Mono
            "-y",               # Sobrescribir
            raw_path
        ]
        subprocess.run(ffmpeg_cmd, check=True)
        
        logger.info(f"✅ Audio μ-law crudo generado ({os.path.getsize(raw_path)} bytes)")

        # 3. Verificación adicional
        subprocess.run(["ffprobe", wav_path])
        subprocess.run(["file", wav_path])
        subprocess.run(["xxd", "-l", "32", raw_path])

    except Exception as e:
        logger.error(f"🚨 Error: {str(e)}")

if __name__ == "__main__":
    test_elevenlabs_ulaw()