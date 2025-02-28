# tw_utils.py
import json
import base64
import time
import asyncio
import logging
import os
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from google_stt_streamer import GoogleSTTStreamer

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

AUDIO_DIR = "audio"

class TwilioWebSocketManager:
    """
    Maneja la conexión WebSocket con Twilio y el streaming con Google STT.
    - Reinicia el stream cada 290s si hace falta.
    - Guarda el audio mu-law en un archivo local (raw_audio.ulaw).
    - Cierra robustamente la llamada.
    """
    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = GoogleSTTStreamer()
        self.google_task = None
        self.stream_start_time = time.time()

    async def handle_twilio_websocket(self, websocket: WebSocket):
        await websocket.accept()
        logger.info("Conexión WebSocket aceptada con Twilio.")

        # Iniciamos la tarea asíncrona para Google STT
        self.google_task = asyncio.create_task(self.stt_streamer.recognize_stream())

        try:
            while True:
                # Verificar si han pasado 290 seg para reiniciar
                elapsed = time.time() - self.stream_start_time
                if elapsed >= 290:
                    logger.info("Reiniciando stream STT por límite de duración.")
                    await self._restart_stt_stream()
                    self.stream_start_time = time.time()

                # Esperar mensaje de Twilio (máx 60s)
                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                    logger.debug("Mensaje recibido de Twilio.")
                except asyncio.TimeoutError:
                    logger.warning("Timeout de inactividad, cerrando conexión.")
                    await self._hangup_call(websocket)
                    break

                data = json.loads(message)
                event_type = data.get("event")

                if event_type == "start":
                    self.stream_sid = data["streamSid"]
                    logger.info(f"Nuevo stream SID: {self.stream_sid}")
                    await self._play_audio_file(websocket, "saludo.wav")

                elif event_type == "media":
                    # Twilio envía audio mu-law 8k base64
                    payload_base64 = data["media"]["payload"]
                    mulaw_chunk = base64.b64decode(payload_base64)

                    logger.info(f"📢 Chunk de audio recibido de Twilio, tamaño: {len(mulaw_chunk)} bytes.")
                    self._save_mulaw_chunk(mulaw_chunk)

                    # Añadimos el chunk al GoogleSTTStreamer
                    await self.stt_streamer.add_audio_chunk(mulaw_chunk)

                elif event_type == "stop":
                    logger.info("Twilio envió evento 'stop'. Colgando.")
                    await self._hangup_call(websocket)
                    break

        except Exception as e:
            logger.error(f"Error en handle_twilio_websocket: {e}", exc_info=True)
            await self._hangup_call(websocket)
        finally:
            if not self.call_ended:
                await self._hangup_call(websocket)
            logger.info("WebSocket con Twilio cerrado.")

    def _save_mulaw_chunk(self, chunk: bytes, filename: str = "raw_audio.ulaw"):
        """
        Guarda el audio mu-law en un archivo local
        """
        try:
            with open(filename, "ab") as f:
                f.write(chunk)
            logger.debug(f"Guardado chunk mu-law en {filename}, tamaño: {len(chunk)} bytes.")
        except Exception as e:
            logger.error(f"Error al guardar chunk mu-law: {e}")

    async def _restart_stt_stream(self):
        """
        Reinicia el STT: cierra el streamer viejo y crea uno nuevo,
        transfiriendo los chunks pendientes de la cola.
        """
        logger.info("Reiniciando el stream STT...")
        old_queue = self.stt_streamer.audio_queue

        # Cerrar el streamer actual
        await self.stt_streamer.close()

        # Cancelar la tarea previa
        if self.google_task:
            self.google_task.cancel()
            try:
                await self.google_task
            except asyncio.CancelledError:
                logger.debug("Tarea Google STT cancelada correctamente.")

        # Crear un nuevo streamer
        self.stt_streamer = GoogleSTTStreamer()

        # Pasar los chunks restantes a la nueva cola
        while not old_queue.empty():
            try:
                chunk = await old_queue.get()
                if chunk:
                    await self.stt_streamer.audio_queue.put(chunk)
                    logger.debug(f"Chunk transferido al nuevo stream, tamaño: {len(chunk)} bytes.")
            except Exception as e:
                logger.error(f"Error al transferir chunk pendiente: {e}")

        # Lanzar la nueva tarea
        self.google_task = asyncio.create_task(self.stt_streamer.recognize_stream())
        logger.info("Nuevo stream STT iniciado.")

    async def _hangup_call(self, websocket: WebSocket):
        """
        Corta la llamada, cierra Google STT y el WebSocket
        """
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("Terminando la llamada.")

        # Cerrar Google STT
        await self.stt_streamer.close()

        # Cancelar la tarea asíncrona
        if self.google_task:
            self.google_task.cancel()
            try:
                await self.google_task
            except asyncio.CancelledError:
                logger.debug("Tarea Google STT cancelada correctamente.")

        # Cerrar WebSocket si sigue conectado
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=1000)
                logger.info("WebSocket cerrado correctamente.")
        except Exception as e:
            if "close message has been sent" not in str(e):
                logger.error(f"Error al cerrar WebSocket: {e}")

    async def _play_audio_file(self, websocket: WebSocket, filename: str):
        """
        Envía un archivo WAV pregrabado (8k, mono, 16bit) a Twilio.
        """
        if not self.stream_sid:
            return
        try:
            filepath = os.path.join(AUDIO_DIR, filename)
            if not os.path.exists(filepath):
                logger.error(f"No se encontró el archivo WAV: {filepath}")
                return

            with open(filepath, "rb") as f:
                wav_data = f.read()

            encoded = base64.b64encode(wav_data).decode("utf-8")
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": encoded}
            }))
            logger.info(f"Reproduciendo: {filename}")
        except Exception as e:
            logger.error(f"Error reproduciendo {filename}: {e}")
