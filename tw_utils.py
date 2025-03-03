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

# Importamos la IA y TTS
from aiagent import generate_openai_response
from tts_utils import text_to_speech

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

AUDIO_DIR = "audio"

def stt_callback_factory(manager, websocket):
    """
    Retorna un callback para GoogleSTTStreamer que, al recibir texto final,
    llama a GPT y reproduce respuesta con ElevenLabs.
    """
    def stt_callback(result):
        if result.is_final:
            transcript = result.alternatives[0].transcript
            logger.info(f"USUARIO (final): {transcript}")
            # Llamamos la función asíncrona process_gpt_response sin bloquear
            asyncio.ensure_future(manager.process_gpt_response(transcript, websocket))
        # else:
        #   logger.debug(f"(parcial) => {result.alternatives[0].transcript}")
    return stt_callback

class TwilioWebSocketManager:
    """
    Maneja la conexión WebSocket con Twilio.
    - Inicia GoogleSTTStreamer en hilo secundario
    - Reinicia cada ~290s
    - Cierra robustamente
    """
    def __init__(self):
        self.call_ended = False
        self.stream_sid = None
        self.stt_streamer = None
        self.google_task = None
        self.stream_start_time = time.time()

    async def handle_twilio_websocket(self, websocket: WebSocket):
        await websocket.accept()
        logger.info("Conexión WebSocket aceptada con Twilio.")

        # Instanciar STT
        try:
            self.stt_streamer = GoogleSTTStreamer()
        except Exception as e:
            logger.error(f"Error inicializando Google STT: {e}", exc_info=True)
            await websocket.close(code=1011)
            return

        # Creamos un callback que tiene acceso a manager (self) y websocket
        stt_callback = stt_callback_factory(self, websocket)

        # Iniciar streaming en hilo
        self.stt_streamer.start_streaming(stt_callback)

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
                    # Reproducir saludo inicial
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

    async def process_gpt_response(self, user_text: str, websocket: WebSocket):
        """
        Llama a GPT con el texto del usuario, obtiene respuesta,
        lo manda a ElevenLabs y reproduce el audio resultante.
        """
        try:
            if self.call_ended or websocket.client_state != WebSocketState.CONNECTED:
                return

            # Construimos un mini-historial de 1 turno (puedes mejorar esto)
            conversation_history = [
                {"role": "user", "content": user_text}
            ]

            gpt_response = generate_openai_response(conversation_history)
            if not gpt_response:
                gpt_response = "Lo siento, no comprendí. Repita por favor."

            # Generar audio con ElevenLabs
            audio_file = text_to_speech(gpt_response, "respuesta_audio.wav")
            if not audio_file:
                logger.error("No se pudo crear el archivo de audio.")
                return

            # Reproducir en Twilio
            await self._play_audio_file(websocket, audio_file)

        except Exception as e:
            logger.error(f"Error en process_gpt_response: {e}", exc_info=True)

    async def _hangup_call(self, websocket: WebSocket):
        if self.call_ended:
            return
        self.call_ended = True
        logger.info("Terminando la llamada.")

        # Cerrar STT
        if self.stt_streamer:
            self.stt_streamer.close()

        # Cerrar WebSocket
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.close(code=1000)
                logger.info("WebSocket cerrado correctamente.")
        except Exception as e:
            logger.error(f"Error al cerrar WebSocket: {e}", exc_info=True)

    async def _restart_stt_stream(self):
        """
        Reinicia el streamer actual para evitar el límite ~5min de Google.
        Descarta lo que quede en la cola antigua para evitar latencia.
        """
        if self.stt_streamer:
            self.stt_streamer.close()
        self.stt_streamer = GoogleSTTStreamer()
        self.stt_streamer.start_streaming(stt_callback_factory(self, None))
        self.stream_start_time = time.time()
        logger.info("Nuevo STT streamer iniciado tras reinicio.")

    def _save_mulaw_chunk(self, chunk: bytes, filename="raw_audio.ulaw"):
        """
        Guarda el chunk mu-law para análisis.
        """
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
