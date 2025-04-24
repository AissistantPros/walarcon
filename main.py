# main.py
import os
import logging
from fastapi import FastAPI, Response, WebSocket
from tw_utils import TwilioWebSocketManager, set_debug   # ⬅️ nuevo import

# ───────── CONFIGURACIÓN DE LOGGING ────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

# Silenciamos verbosidad de librerías externas
for noisy in ("httpcore", "httpx", "websockets"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

# ───────── FASTAPI ─────────────────────────────────────────────
app = FastAPI()


@app.on_event("startup")
def startup_event() -> None:
    """Crea carpetas de depuración y habilita modo DEBUG de nuestros módulos."""
    os.makedirs("audio", exist_ok=True)
    os.makedirs("audio_debug", exist_ok=True)

    # Activa métricas detalladas ⏱️  – pon False en producción:
    set_debug(True)

    logger.info("🚀 Backend listo, streaming STT activo.")


@app.get("/")
async def root():
    return {"message": "Backend activo, streaming STT listo."}


@app.post("/twilio-voice")
async def twilio_voice():
    """
    Endpoint que Twilio llama cuando entra una llamada.
    Responde con TwiML que abre un <Stream> hacia nuestro WebSocket.
    """
    logger.info("📞 Nueva llamada entrante desde Twilio.")
    twiml_response = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream name="AudioStream"
            url="wss://walarcon.onrender.com/twilio-websocket" />
  </Connect>
</Response>"""
    return Response(content=twiml_response, media_type="application/xml")


@app.websocket("/twilio-websocket")
async def twilio_websocket(websocket: WebSocket):
    """
    WebSocket que recibe el audio μ-law en tiempo real desde Twilio
    y delega la lógica a TwilioWebSocketManager.
    """
    manager = TwilioWebSocketManager()
    await manager.handle_twilio_websocket(websocket)
