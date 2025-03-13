# deepgram_stt.py - Transcripción en tiempo real con Deepgram

import asyncio
import websockets
import json
import logging
import os

logger = logging.getLogger("deepgram_stt")
logger.setLevel(logging.INFO)

DEEPGRAM_KEY = os.getenv("DEEPGRAM_KEY")  # Usa la clave guardada en tu .env
DEEPGRAM_URL = "wss://api.deepgram.com/v1/listen?punctuate=true&language=es"

class DeepgramSTT:
    """
    Clase para manejar la transcripción en tiempo real con Deepgram.
    Se conecta al servicio de WebSockets de Deepgram y recibe texto transcrito.
    """

    def __init__(self, callback):
        """
        Inicializa el servicio de Deepgram STT.
        :param callback: Función que procesará la transcripción de voz.
        """
        self.callback = callback
        self.closed = False

    async def start_streaming(self):
        """
        Inicia la conexión con Deepgram y recibe transcripciones en tiempo real.
        """
        try:
            async with websockets.connect(
                DEEPGRAM_URL, extra_headers={"Authorization": f"Token {DEEPGRAM_KEY}"}
            ) as ws:
                logger.info("✅ Conectado a Deepgram STT")
                while not self.closed:
                    response = await ws.recv()
                    result = json.loads(response)
                    transcript = result.get("channel", {}).get("alternatives", [{}])[0].get("transcript", "")
                    is_final = result.get("is_final", False)
                    
                    if transcript:
                        logger.info(f"📝 Transcripción: {transcript} (Final: {is_final})")
                        self.callback(transcript, is_final)

        except Exception as e:
            logger.error(f"❌ Error en Deepgram STT: {e}")

    def close(self):
        """
        Cierra la conexión con Deepgram.
        """
        self.closed = True
        logger.info("🛑 Deepgram STT cerrado")
