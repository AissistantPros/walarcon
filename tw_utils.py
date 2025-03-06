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
from aiagent import generate_openai_response
from tts_utils import text_to_speech

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

AUDIO_DIR = "audio"

def stt_callback_factory(manager):
    """
    Genera un callback para STT que maneja resultados parciales y finales.
    NO llama directamente a GPT. Sólo actualiza 'current_partial' y 'last_partial_time'.
    """
    def stt_callback(result):
        transcript = result.alternatives[0].transcript
        if transcript:
            # Actualizar la transcripción parcial en manager
            manager.current_partial = transcript
            manager.last_partial_time = time.time()
            logger.debug(f"(parcial) => {transcript}")
            
            if result.is_final:
                logger.debug("Google marcó is_final=True. (Pero no cerramos stream)")
                # Igual actualizamos, en caso de que Google mande un final real
                manager.current_partial = transcript
                manager.last_partial_time = time.time()
        # Si result no tiene transcript, no hacemos nada
    return stt_callback

class TwilioWebSocketManager:
    """
    Maneja la conexión WebSocket con Twilio.
    + Integración con GoogleSTTStreamer (multi-turn, single_utterance=False).
    + Detección local de silencio.
    """
    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = None
        self.stream_start_time = time.time()

        # Para la detección de silencio
        self.current_partial = ""       # guarda la última transcripción recibida
        self.last_partial_time = 0.0    # timestamp del último parcial
        self.silence_threshold = 1.5    # seg de silencio para "final local"

        self._silence_task = None
        self.main_loop = None

    async def handle_twilio_websocket(self, websocket: WebSocket):
        # Guardamos el event loop principal para tareas
        self.main_loop = asyncio.get_running_loop()

        await websocket.accept()
        logger.info("Conexión WebSocket aceptada con Twilio.")

        # Instanciar STT
        try:
            self.stt_streamer = GoogleSTTStreamer()
        except Exception as e:
            logger.error(f"Error inicializando Google STT: {e}", exc_info=True)
            await websocket.close(code=1011)
            return

        # Crear callback para STT
        stt_callback = stt_callback_factory(self)
        self.stt_streamer.start_streaming(stt_callback)

        # Iniciamos una tarea asíncrona que revisa silencio cada 0.2 seg
        self._silence_task = asyncio.create_task(self._silence_watcher(websocket))

        self.stream_start_time = time.time()
        try:
            while True:
                elapsed = time.time() - self.stream_start_time
                if elapsed >= 290:
                    logger.info("Reiniciando stream STT por límite de duración (290s).")
                    await self._restart_stt_stream()

                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=60.0)
                except asyncio.TimeoutError:
                    logger.warning("Timeout de inactividad, cerrando conexión.")
                    await self._hangup_call(websocket)
                    break

                data = json.loads(message)
                event_type = data.get("event")
                if event_type == "start":
                    self.stream_sid = data.get("streamSid")
                    logger.info(f"Nuevo stream SID: {self.stream_sid}")
                    await self._play_audio_file(websocket, "saludo.wav")

                elif event_type == "media":
                    payload_base64 = data["media"]["payload"]
                    mulaw_chunk = base64.b64decode(payload_base64)
                    self._save_mulaw_chunk(mulaw_chunk)

                    # Enviarlo a STT
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

    async def _silence_watcher(self, websocket: WebSocket):
        """
        Revisa periódicamente si pasó suficiente tiempo sin nuevos parciales.
        Si es así, consideramos que es "final local" y llamamos GPT.
        """
        check_interval = 0.2  # cada 200ms
        while not self.call_ended and websocket.client_state == WebSocketState.CONNECTED:
            await asyncio.sleep(check_interval)

            # Si tenemos algo en current_partial y ha pasado silence_threshold
            if self.current_partial and (time.time() - self.last_partial_time > self.silence_threshold):
                # Guardamos el texto final
                final_text = self.current_partial.strip()
                # Limpiamos para no disparar varias veces
                self.current_partial = ""

                # Llamar GPT
                asyncio.create_task(self.process_gpt_response(final_text, websocket))

    async def process_gpt_response(self, user_text: str, websocket: WebSocket):
        """
        Llama a GPT con la transcripción final y reproduce la respuesta.
        """
        try:
            if self.call_ended or websocket.client_state != WebSocketState.CONNECTED:
                return

            logger.info(f"USUARIO (FINAL_LOCAL): {user_text}")

            # Podrías mantener un historial más grande, 
            # por ahora un único turno:
            conversation_history = [
                {"role": "user", "content": user_text}
            ]

            gpt_response = generate_openai_response(conversation_history)
            if not gpt_response:
                gpt_response = "Lo siento, no comprendí. ¿Podría repetir, por favor?"

            # Convertir texto a audio
            audio_file = text_to_speech(gpt_response, "respuesta_audio.wav")
            if not audio_file:
                logger.error("No se pudo generar archivo TTS.")
                return

            await self._play_audio_file(websocket, audio_file)

        except Exception as e:
            logger.error(f"Error en process_gpt_response: {e}", exc_info=True)

    async def _hangup_call(self, websocket: WebSocket):
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("Terminando la llamada.")

        if self.stt_streamer:
            self.stt_streamer.close()

        # Cancelar tarea de silencio
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()

        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=1000)
                logger.info("WebSocket cerrado correctamente.")
        except Exception as e:
            logger.error(f"Error al cerrar WebSocket: {e}", exc_info=True)

    async def _restart_stt_stream(self):
        if self.stt_streamer:
            self.stt_streamer.close()
        self.stt_streamer = GoogleSTTStreamer()
        callback = stt_callback_factory(self)
        self.stt_streamer.start_streaming(callback)
        self.stream_start_time = time.time()
        logger.info("Nuevo STT streamer iniciado tras reinicio.")

    def _save_mulaw_chunk(self, chunk: bytes, filename="raw_audio.ulaw"):
        try:
            with open(filename, "ab") as f:
                f.write(chunk)
        except Exception as e:
            logger.error(f"Error guardando mu-law: {e}")

    async def _play_audio_file(self, websocket: WebSocket, filename: str):
        if not self.stream_sid or self.call_ended:
            return
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        try:
            os.makedirs(AUDIO_DIR, exist_ok=True)
            filepath = os.path.join(AUDIO_DIR, filename)
            if not os.path.exists(filepath):
                logger.error(f"No se encontró el archivo: {filepath}")
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
            logger.error(f"Error reproduciendo {filename}: {e}", exc_info=True)
