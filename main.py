# main.py
import os
import logging
from fastapi import FastAPI, Response, WebSocket
from fastapi.responses import FileResponse
from tw_utils import TwilioWebSocketManager

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = FastAPI()

@app.on_event("startup")
def startup_event():
    # Asegurarnos de que existan directorios para almacenar audios
    os.makedirs("audio", exist_ok=True)
    os.makedirs("audio_debug", exist_ok=True)

@app.get("/")
async def root():
    return {"message": "Backend activo, streaming STT listo."}

@app.post("/twilio-voice")
async def twilio_voice():
    """
    Twilio apuntará aquí cuando reciba una llamada. 
    Retornamos la respuesta con <Stream> a /twilio-websocket
    """
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
    """
    Maneja la comunicación WebSocket con Twilio.
    """
    manager = TwilioWebSocketManager()
    await manager.handle_twilio_websocket(websocket)

@app.get("/download-raw")
async def download_raw():
    """
    Descarga raw_audio.ulaw (lo que Twilio envía en mu-law).
    """
    file_path = os.path.abspath("raw_audio.ulaw")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="audio/basic", filename="raw_audio.ulaw")
    return {"error": "Archivo raw_audio.ulaw no encontrado"}

@app.get("/download-linear16")
async def download_linear16():
    """
    Descarga converted_8k.raw (PCM16) que se envía a Google STT tras conversión.
    Permite inspeccionarlo con Audacity.
    """
    file_path = os.path.abspath("audio_debug/converted_8k.raw")
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="application/octet-stream", filename="converted_8k.raw")
    return {"error": "Archivo converted_8k.raw no encontrado"}
