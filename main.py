# -*- coding: utf-8 -*-
"""
Archivo principal del backend FastAPI.
Maneja la comunicación con Twilio, WebSockets y las operaciones del asistente virtual.
"""

# ==================================================
# 📌 Importaciones necesarias
# ==================================================
from fastapi import FastAPI, Response, WebSocket
import logging
from tw_utils import handle_twilio_websocket  # Función para manejar WebSockets de Twilio

# Configuración del sistema de logs
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================================================
# 📌 Inicialización de FastAPI
# ==================================================
app = FastAPI()

# ==================================================
# 🔹 Endpoint para responder a Twilio con TwiML (inicia WebSockets)
# ==================================================
@app.post("/twilio-voice")
async def twilio_voice():
    """
    Este endpoint es llamado por Twilio cuando una llamada entra.
    Devuelve un XML en formato TwiML que le dice a Twilio que inicie una conexión WebSocket con nuestro backend.
    """
    logger.info("📞 Nueva llamada entrante desde Twilio.")

    twiml_response = f"""
    <?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Connect>
            <Stream url="wss://walarcon.onrender.com/twilio-websocket" />
        </Connect>
    </Response>
    """
    
    return Response(content=twiml_response, media_type="application/xml")

# ==================================================
# 🔹 WebSocket para manejar el audio en tiempo real con Twilio
# ==================================================
@app.websocket("/twilio-websocket")
async def twilio_websocket(websocket: WebSocket):
    """
    Maneja la conexión WebSocket con Twilio para recibir audio en tiempo real
    y enviar respuestas generadas por la IA en formato de audio.
    """
    await handle_twilio_websocket(websocket)

# ==================================================
# 📌 Mensaje de bienvenida en la raíz del backend
# ==================================================
@app.get("/")
async def root():
    """
    Endpoint de prueba para verificar que el backend está funcionando correctamente.
    """
    return {"message": "🚀 Backend de Asistente Virtual en ejecución. Conexión establecida correctamente."}
