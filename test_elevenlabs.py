#!/usr/bin/env python3
# test_elevenlabs.py
# --------------------------------------------------
# Descarga TTS de Eleven Labs en μ-law 8 kHz crudo,
# verifica si trae cabecera y genera un WAV para escuchar.
# --------------------------------------------------

import os
import requests
import subprocess
import logging

# ======= CREDENCIALES EN DURO =======
ELEVEN_LABS_API_KEY = "sk_35abd9f8c1e86371af7df3c4a877fde78d1108a74705c37b"
ELEVEN_LABS_VOICE_ID = "CaJslL1xziwefCeTNzHv"
# ====================================

# ======= CONFIG RÁPIDA ==============
OUT_DIR = os.path.expanduser("~/Desktop")
TEXT    = "Prueba técnica de audio en formato mu-law para Twilio"
MODEL   = "eleven_multilingual_v2"
# ====================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_elevenlabs_ulaw")

def test_elevenlabs_ulaw():
    api_key  = ELEVEN_LABS_API_KEY
    voice_id = ELEVEN_LABS_VOICE_ID

    # ---------- URL con formato μ-law ----------
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream?output_format=ulaw_8000"

    headers = {
        "xi-api-key": api_key,
        "Accept": "audio/mulaw"  # ← importante para que no devuelva MP3
    }

    payload = {
        "text": TEXT,
        "model_id": MODEL,
        "voice_settings": {
            "stability": 0.7,
            "style": 0.5,
            "use_speaker_boost": False,
            "speed": 1.0
        }
    }

    # ---------- Descarga ----------
    raw_path = os.path.join(OUT_DIR, "prueba_ulaw.raw")
    logger.info("Solicitando a Eleven Labs (μ-law 8 kHz crudo)…")
    with requests.post(url, json=payload, headers=headers, stream=True) as r:
        r.raise_for_status()
        with open(raw_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=4096):
                if chunk:
                    f.write(chunk)

    raw_size = os.path.getsize(raw_path)
    logger.info(f"✅ Audio recibido → {raw_path}  ({raw_size:,} bytes)")

    # ---------- Validación inicial ----------
    with open(raw_path, "rb") as f:
        magic = f.read(4)

    if magic == b"RIFF":
        logger.warning("⚠️  ¡Sorpresa! Recibiste WAV (RIFF). Quitando cabecera…")
        fixed_path = os.path.join(OUT_DIR, "prueba_ulaw_fixed.raw")
        with open(raw_path, "rb") as src, open(fixed_path, "wb") as dst:
            src.seek(44)
            dst.write(src.read())
        raw_path = fixed_path
        logger.info(f"🩹 Cabecera eliminada → {raw_path}")
    else:
        logger.info("👌 Recibiste μ-law crudo sin cabecera WAV.")

    # ---------- WAV para escuchar ----------
    wav_path = os.path.join(OUT_DIR, "prueba_ulaw_reproducible.wav")
    ffmpeg_cmd = [
        "ffmpeg",
        "-f", "mulaw", "-ar", "8000", "-ac", "1",
        "-i", raw_path,
        "-c:a", "pcm_s16le", "-ar", "8000",
        wav_path, "-y"
    ]
    subprocess.run(ffmpeg_cmd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logger.info(f"🎧 WAV reproducible generado → {wav_path}")

    # ---------- Validaciones técnicas opcionales ----------
    subprocess.run(["ffprobe", "-hide_banner", wav_path])
    subprocess.run(["file", raw_path])
    subprocess.run(["xxd", "-l", "32", raw_path])


if __name__ == "__main__":
    test_elevenlabs_ulaw()
