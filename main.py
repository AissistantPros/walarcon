# main.py
import os, time, logging
from fastapi import FastAPI, Response, WebSocket
from fastapi.responses import FileResponse
from tw_utils import TwilioWebSocketManager

# ───────────────────────── LOGGING GLOBAL ─────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

# ───────────────────────── FASTAPI APP ─────────────────────────
app = FastAPI(title="Wilfrido-Voice-Backend", version="1.0")

# Carpetas para debugging ─────────
@app.on_event("startup")
def _startup():
    for folder in ("audio", "audio_debug"):
        os.makedirs(folder, exist_ok=True)
    logger.info("🚀 Backend listo – carpetas de audio verificadas.")

# Ping rápido ─────────
@app.get("/")
async def root():
    return {"status": "ok", "msg": "Backend activo, streaming STT listo."}

# ───────────────────────── ENDPOINT Twilio <Voice> webhook ─────────────────────────
@app.post("/twilio-voice")
async def twilio_voice():
    """
    Twilio pega a este endpoint al iniciar la llamada.
    Respondemos con TwiML que abre un <Stream> WebSocket.
    """
    logger.info("📞 Nueva llamada entrante (Twilio webhook).")
    start = time.perf_counter()

    # ¡OJO! Cambia la URL si el dominio de Render cambia
    ws_url = os.getenv("WS_PUBLIC_URL", "wss://walarcon.onrender.com/twilio-websocket")

    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream name="AudioStream" url="{ws_url}" />
  </Connect>
</Response>"""

    logger.info("🕒 Twilio-voice generado en %.2f ms.", (time.perf_counter() - start) * 1e3)
    return Response(content=twiml, media_type="application/xml")

# ───────────────────────── WebSocket de audio ─────────────────────────
@app.websocket("/twilio-websocket")
async def twilio_websocket(ws: WebSocket):
    """
    Recibe audio μ-law 8 kHz desde Twilio en tiempo real.
    Delegamos la lógica a TwilioWebSocketManager.
    """
    manager = TwilioWebSocketManager()
    await manager.handle_twilio_websocket(ws)

# ───────────────────────── Descargas de debugging ─────────────────────────
@app.get("/download-raw")
async def download_raw():
    """Descarga raw_audio.ulaw (audio original μ-law)."""
    path = os.path.abspath("raw_audio.ulaw")
    return (
        FileResponse(path, media_type="audio/basic", filename="raw_audio.ulaw")
        if os.path.exists(path)
        else {"error": "Archivo raw_audio.ulaw no encontrado"}
    )

@app.get("/download-linear16")
async def download_linear16():
    """Descarga converted_8k.raw (PCM lineal 16-bit)."""
    path = os.path.abspath("audio_debug/converted_8k.raw")
    return (
        FileResponse(path, media_type="application/octet-stream", filename="converted_8k.raw")
        if os.path.exists(path)
        else {"error": "Archivo converted_8k.raw no encontrado"}
    )
