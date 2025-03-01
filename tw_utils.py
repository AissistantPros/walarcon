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

    - Inicia GoogleSTTStreamer (credentials from env).
    - Recibe chunks mu-law 8k, pasa a stt_streamer.add_audio_chunk().
    - Llama stt_streamer.recognize_stream() en tarea asíncrona.
    - Reinicia cada ~290s para evitar límite de 5 min.
    - Maneja hangup, stop, etc.
    """

    def __init__(self):
        self.call_ended = False
        self.stream_sid = None

        # Instanciar STT (manejo de credenciales)
        self.stt_streamer = GoogleSTTStreamer()

        self.google_task = None
        self.stream_start_time = time.time()

    async def handle_twilio_websocket(self, websocket: WebSocket):
        # Primero, intentar credenciales correctas
        await websocket.accept()
        logger.info("Conexión WebSocket aceptada con Twilio.")

        # Iniciamos la tarea asíncrona de streaming
        self.google_task = asyncio.create_task(self.stt_streamer.recognize_stream())

        try:
            while True:
                # Reiniciar STT cada 290 seg
                elapsed = time.time() - self.stream_start_time
                if elapsed >= 290:
                    logger.info("Reiniciando stream STT por límite de duración.")
                    await self._restart_stt_stream()
                    self.stream_start_time = time.time()

                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
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
                    payload_base64 = data["media"]["payload"]
                    mulaw_chunk = base64.b64decode(payload_base64)

                    logger.info(f"📢 Chunk de audio (mu-law) recibido, {len(mulaw_chunk)} bytes.")
                    self._save_mulaw_chunk(mulaw_chunk)

                    # Enviar chunk al STT
                    await self.stt_streamer.add_audio_chunk(mulaw_chunk)

                elif event_type == "stop":
                    logger.info("Twilio envió evento 'stop'.")
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
        Guarda el audio mu-law en un archivo local, para debug.
        """
        try:
            with open(filename, "ab") as f:
                f.write(chunk)
            logger.debug(f"Guardado chunk mu-law en {filename}, tamaño: {len(chunk)} bytes.")
        except Exception as e:
            logger.error(f"Error al guardar chunk mu-law: {e}")


    async def _restart_stt_stream(self):
        """
        Cierra streamer actual, cancela tarea, crea uno nuevo, descarta cola vieja (cutoff).
        """
        logger.info("Reiniciando STT streamer.")
        old_queue = self.stt_streamer.audio_queue

        await self.stt_streamer.close()

        if self.google_task:
            self.google_task.cancel()
            try:
                await self.google_task
            except asyncio.CancelledError:
                logger.debug("Tarea STT cancelada correctamente.")

        # Crear un nuevo STT
        self.stt_streamer = GoogleSTTStreamer()

        # (Opcional) Transición suave: transferir colas pendientes
        # while not old_queue.empty():
        #     chunk = await old_queue.get()
        #     if chunk:
        #         await self.stt_streamer.audio_queue.put(chunk)

        self.google_task = asyncio.create_task(self.stt_streamer.recognize_stream())
        logger.info("Nuevo STT streamer iniciado.")


    async def _hangup_call(self, websocket: WebSocket):
        """
        Cierra la llamada: STT + WebSocket
        """
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("Terminando la llamada.")

        # Cerrar STT
        await self.stt_streamer.close()

        if self.google_task:
            self.google_task.cancel()
            try:
                await self.google_task
            except asyncio.CancelledError:
                logger.debug("Tarea STT cancelada correctamente.")

        # Cerrar WebSocket
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
                logger.info("WebSocket cerrado correctamente.")
        except Exception as e:
            if "close message has been sent" not in str(e):
                logger.error(f"Error al cerrar WebSocket: {e}")


    async def _play_audio_file(self, websocket: WebSocket, filename: str):
        """
        Reproduce un WAV en Twilio. Debe ser 8k, mono, 16bit, PCM.
        """
        if not self.stream_sid:
            return
        try:
            filepath = os.path.join(AUDIO_DIR, filename)
            with open(filepath, "rb") as f:
                wav_data = f.read()

            encoded = base64.b64encode(wav_data).decode("utf-8")
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": encoded}
            }))
            logger.info(f"Reproduciendo: {filename}")

        except FileNotFoundError:
            logger.error(f"No se encontró el archivo: {filename}")
        except Exception as e:
            logger.error(f"Error reproduciendo {filename}: {e}")
