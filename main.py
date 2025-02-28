# main.py
from fastapi import FastAPI, Response, WebSocket
import logging
from tw_utils import TwilioWebSocketManager

logging.basicConfig(level=logging.INFO)
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
