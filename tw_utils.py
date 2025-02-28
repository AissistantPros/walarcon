# tw_utils.py
import json
import base64
import time
import asyncio
import logging
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from google_stt_streamer import GoogleSTTStreamer

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AUDIO_DIR = "audio"

class TwilioWebSocketManager:
    """
    Maneja la conexión WebSocket con Twilio usando Google STT en streaming.
    - Implementa reinicio automático del stream STT antes de 240 s.
    - Maneja el cierre robusto del WebSocket.
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
        self.stream_start_time = time.time()
        self.google_task = asyncio.create_task(self._listen_google_results())
        try:
            while True:
                # Reiniciar el stream STT automáticamente cada 240 segundos
                elapsed = time.time() - self.stream_start_time
                if elapsed >= 240:
                    logger.info("Reiniciando stream STT por límite de duración.")
                    await self._restart_stt_stream()
                    self.stream_start_time = time.time()

                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
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
                    self.stt_streamer.add_audio_chunk(mulaw_chunk)
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
            await websocket.close()

    async def _listen_google_results(self):
        async for result in self.stt_streamer.recognize_stream():
            transcript = result.alternatives[0].transcript
            if result.is_final:
                logger.info(f"USUARIO (final): {transcript}")
                await self._process_final_transcript(transcript)
            else:
                logger.info(f"(parcial) => {transcript}")

    async def _process_final_transcript(self, transcript: str):
        logger.info(f"Procesando la transcripción final: {transcript}")
        # Aquí se llamaría a GPT y luego a TTS.
        response = "Ejemplo de respuesta de GPT"
        # Placeholder para la lógica de TTS.
        logger.info(f"(TTS) Respuesta IA: {response}")

    async def _restart_stt_stream(self):
        logger.info("Reiniciando el stream STT...")
        await self.stt_streamer.close()
        if self.google_task:
            self.google_task.cancel()
            try:
                await self.google_task
            except asyncio.CancelledError:
                logger.debug("Tarea Google STT cancelada correctamente.")
        # Reinicia el STT streamer y la tarea
        self.stt_streamer = GoogleSTTStreamer()
        self.google_task = asyncio.create_task(self._listen_google_results())
        logger.info("Nuevo stream STT iniciado.")

    async def _hangup_call(self, websocket: WebSocket):
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("Terminando la llamada.")
        await self.stt_streamer.close()
        if self.google_task:
            self.google_task.cancel()
            try:
                await self.google_task
            except asyncio.CancelledError:
                logger.debug("Tarea Google STT cancelada correctamente.")
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close()
                logger.info("WebSocket cerrado correctamente.")
        except Exception as e:
            logger.error(f"Error al cerrar WebSocket: {e}")

    async def _play_audio_file(self, websocket: WebSocket, filename: str):
        if not self.stream_sid:
            return
        try:
            filepath = f"{AUDIO_DIR}/{filename}"
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
            logger.error(f"No se encontró el archivo: {filepath}")
        except Exception as e:
            logger.error(f"Error reproduciendo {filename}: {e}")
