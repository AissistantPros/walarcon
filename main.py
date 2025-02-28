# main.py
from fastapi import FastAPI, Response, WebSocket
import logging
from tw_utils import TwilioWebSocketManager
from fastapi.responses import FileResponse
import os

logging.basicConfig(level=logging.DEBUG)  # Para ver todos los logs
logger = logging.getLogger(__name__)

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "Backend activo, streaming STT listo."}

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
    manager = TwilioWebSocketManager()
    await manager.handle_twilio_websocket(websocket)


@app.get("/download-audio")
async def download_audio():
    file_path = "/opt/render/project/src/raw_audio.ulaw"
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="audio/basic", filename="raw_audio.ulaw")
    return {"error": "Archivo no encontrado"}