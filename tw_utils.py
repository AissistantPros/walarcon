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
from tts_utils import text_to_speech  # Devuelve audio en formato mu-law (bytes)

# Reducir la verbosidad de otros módulos
logging.getLogger("tts_utils").setLevel(logging.WARNING)
logging.getLogger("google_stt_streamer").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

AUDIO_DIR = "audio"  # Carpeta para archivos pregrabados (por ejemplo, saludo.wav)

def stt_callback_factory(manager):
    """
    Genera un callback para STT que actualiza 'current_partial' y 'last_partial_time'
    con cada resultado parcial o final, siempre que no se haya procesado un final reciente.
    """
    def stt_callback(result):
        transcript = result.alternatives[0].transcript
        if transcript:
            current_time = time.time()
            # Si se procesó un final en los últimos 2 seg, ignoramos nuevos parciales
            if current_time - manager.last_final_time < 2.0:
                return
            manager.current_partial = transcript
            manager.last_partial_time = current_time
            logger.debug(f"(parcial) => {transcript}")
            if result.is_final:
                logger.debug("Google marcó is_final=True.")
                manager.current_partial = transcript
                manager.last_partial_time = current_time
    return stt_callback

class TwilioWebSocketManager:
    """
    Maneja la conexión WebSocket con Twilio:
      - Utiliza GoogleSTTStreamer en modo multi-turn (single_utterance=False).
      - Implementa detección local de silencio para disparar la respuesta.
      - Mide latencias de GSTT, GPT, ElevenLabs y del sistema completo.
      - Imprime la respuesta completa de GPT para análisis.
    """
    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = None
        self.stream_start_time = time.time()

        # Variables para detección de silencio
        self.current_partial = ""
        self.last_partial_time = 0.0
        self.silence_threshold = 1.5  # Segundos de silencio para considerar fin de frase
        self.last_final_time = 0.0    # Tiempo del último final procesado

        self._silence_task = None
        self.main_loop = None

    async def handle_twilio_websocket(self, websocket: WebSocket):
        self.main_loop = asyncio.get_running_loop()
        await websocket.accept()
        logger.info("Conexión WebSocket aceptada con Twilio.")

        try:
            self.stt_streamer = GoogleSTTStreamer()
        except Exception as e:
            logger.error(f"Error inicializando Google STT: {e}", exc_info=True)
            await websocket.close(code=1011)
            return

        stt_callback = stt_callback_factory(self)
        self.stt_streamer.start_streaming(stt_callback)

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
                    # Cuando llega audio, se reinicia el debounce (last_final_time) para permitir nuevos turnos.
                    payload_base64 = data["media"]["payload"]
                    mulaw_chunk = base64.b64decode(payload_base64)
                    self._save_mulaw_chunk(mulaw_chunk)
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
        check_interval = 0.2
        while not self.call_ended and websocket.client_state == WebSocketState.CONNECTED:
            await asyncio.sleep(check_interval)
            if self.current_partial and (time.time() - self.last_partial_time > self.silence_threshold):
                final_text = self.current_partial.strip()
                gstt_latency = time.time() - self.last_partial_time
                logger.info(f"GSTT latency (silence detection): {gstt_latency*1000:.0f} ms")
                self.last_final_time = time.time()
                self.current_partial = ""
                asyncio.create_task(self.process_gpt_response(final_text, websocket))

    async def process_gpt_response(self, user_text: str, websocket: WebSocket):
        try:
            if self.call_ended or websocket.client_state != WebSocketState.CONNECTED:
                return
            logger.info(f"USUARIO (FINAL_LOCAL): {user_text}")
            final_detection_time = time.time()

            # Medir latencia de GPT
            gpt_start = time.time()
            conversation_history = [{"role": "user", "content": user_text}]
            gpt_response = generate_openai_response(conversation_history)
            gpt_latency = time.time() - gpt_start
            logger.info(f"GPT latency: {gpt_latency*1000:.0f} ms")
            logger.info(f"GPT response: {gpt_response}")
            if not gpt_response:
                gpt_response = "Lo siento, no comprendí. ¿Podría repetir, por favor?"

            # Medir latencia de ElevenLabs TTS
            tts_start = time.time()
            audio_bytes = text_to_speech(gpt_response)
            tts_latency = time.time() - tts_start
            logger.info(f"ElevenLabs latency: {tts_latency*1000:.0f} ms")
            
            total_latency = time.time() - final_detection_time
            logger.info(f"Total system latency (from final detection to audio ready): {total_latency*1000:.0f} ms")

            if not audio_bytes:
                logger.error("No se pudo generar audio TTS.")
                return
            await self._play_audio_bytes(websocket, audio_bytes)
        except Exception as e:
            logger.error(f"Error en process_gpt_response: {e}", exc_info=True)

    async def _hangup_call(self, websocket: WebSocket):
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("Terminando la llamada.")
        if self.stt_streamer:
            self.stt_streamer.close()
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
                "media": {
                    "payload": encoded
                }
            }))
            logger.info(f"Reproduciendo: {filename}")
        except Exception as e:
            logger.error(f"Error reproduciendo {filename}: {e}", exc_info=True)

    async def _play_audio_bytes(self, websocket: WebSocket, audio_bytes: bytes):
        if not self.stream_sid or self.call_ended:
            return
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        try:
            encoded = base64.b64encode(audio_bytes).decode("utf-8")
            await websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {
                    "payload": encoded
                }
            }))
            logger.info("Reproduciendo audio TTS desde memoria")
        except Exception as e:
            logger.error(f"Error reproduciendo audio TTS desde memoria: {e}", exc_info=True)
