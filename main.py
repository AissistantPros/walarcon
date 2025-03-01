# main.py

import os
import logging
from fastapi import FastAPI, Response, WebSocket
from fastapi.responses import FileResponse
from tw_utils import TwilioWebSocketManager

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()

# Asegurar que las carpetas de audio existen:
os.makedirs("audio", exist_ok=True)
os.makedirs("audio_debug", exist_ok=True)

@app.get("/")
async def root():
    return {"message": "Backend de streaming STT activo."}

@app.post("/twilio-voice")
async def twilio_voice():
    logger.info("📞 Nueva llamada entrante desde Twilio.")
    twiml_response = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream name="AudioStream" url="wss://walarcon.onrender.com/twilio-websocket" />
    </Connect>
</Response>
"""
    return Response(content=twiml_response, media_type="application/xml")


@app.websocket("/twilio-websocket")
async def twilio_websocket(websocket: WebSocket):
    # Si las credenciales STT no están, esto podría fallar:
    try:
        manager = TwilioWebSocketManager()
    except RuntimeError as e:
        # Notificar a Twilio si quieres
        await websocket.accept()
        await websocket.send_text(f"{{'error': 'Credenciales STT inválidas: {str(e)}'}}")
        await websocket.close()
        return

    await manager.handle_twilio_websocket(websocket)


@app.get("/download-raw")
async def download_raw_audio():
    """
    Descarga el archivo mu-law que llega desde Twilio.
    """
    raw_path = "raw_audio.ulaw"
    if os.path.exists(raw_path):
        return FileResponse(raw_path, media_type="audio/basic", filename="raw_audio.ulaw")
    return {"error": "No existe raw_audio.ulaw"}


@app.get("/download-linear16")
async def download_linear16():
    """
    Descarga el archivo con audio PCM16 8k que se envía a Google.
    """
    path_lin = "audio_debug/converted_8k.raw"
    if os.path.exists(path_lin):
        return FileResponse(path_lin, media_type="application/octet-stream", filename="converted_8k.raw")
    return {"error": "No existe audio_debug/converted_8k.raw"}
