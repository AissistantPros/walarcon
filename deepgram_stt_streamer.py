#deepgram_stt_streamer.py
import os
import json
import asyncio
import logging
import warnings
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
from fastapi.websockets import WebSocketState

logger = logging.getLogger("deepgram_stt_streamer")
logger.setLevel(logging.INFO)

# Silenciar logs molestos del WebSocket de Deepgram al cancelar tareas
logging.getLogger("deepgram.clients.common.v1.abstract_async_websocket").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message="coroutine .* was never awaited")
warnings.filterwarnings("ignore", category=RuntimeWarning)

DEEPGRAM_KEY = os.getenv("DEEPGRAM_KEY")
if not DEEPGRAM_KEY:
    raise ValueError("❌ Variable DEEPGRAM_KEY no encontrada")

class DeepgramSTTStreamer:
    def __init__(self, callback):
        """
        callback: función que recibe result (con .is_final y .alternatives[0].transcript)
        """
        self.callback = callback
        self.dg_connection = None
        self.deepgram = DeepgramClient(DEEPGRAM_KEY)
        self._started = False

    async def start_streaming(self):
        """
        Inicia la conexión con Deepgram.
        """
        if self._started:
            logger.warning("⚠️ Deepgram ya estaba iniciado.")
            return

        try:
            self.dg_connection = self.deepgram.listen.asynclive.v("1")
            self.dg_connection.on(LiveTranscriptionEvents.Open, self._on_open)
            self.dg_connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
            self.dg_connection.on(LiveTranscriptionEvents.Close, self._on_close)
            self.dg_connection.on(LiveTranscriptionEvents.Error, self._on_error)

            options = LiveOptions(
                model="nova-2",
                language="es",
                encoding="mulaw",
                sample_rate=8000,
                channels=1,
                smart_format=True,
                interim_results=True,
                endpointing=False,
                utterance_end_ms="1200",
                vad_events=True,
            )

            await self.dg_connection.start(options)
            self._started = True
            logger.info("✅ Conexión Deepgram establecida")

        except Exception as e:
            logger.error(f"❌ Error al iniciar conexión Deepgram: {e}")

    async def send_audio(self, chunk: bytes):
        """
        Envía audio mu-law a Deepgram. Solo si la conexión está iniciada.
        """
        ##logger.debug("📡 Audio enviado a Deepgram (%d bytes)", len(chunk))
        if self.dg_connection and self._started:
            try:
                await self.dg_connection.send(chunk)
            except Exception as e:
                logger.error(f"❌ Error enviando audio a Deepgram: {e}")
        else:
            logger.warning("⚠️ Audio ignorado: conexión no iniciada.")

    async def close(self):
        """Cierra el stream de Deepgram de manera explícita y ordenada."""
        try:
            # 1. Enviar el mensaje de cierre explícito
            logger.info("🚪 Enviando mensaje de cierre explícito a Deepgram.")
            close_message = json.dumps({"type": "CloseStream"})
            if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
                await self.websocket.send_text(close_message)
                logger.debug("✅ Mensaje 'CloseStream' enviado a Deepgram.")

            # 2. Esperar la respuesta final (transcripción y metadata)
            try:
                response = await self.websocket.receive_text()
                logger.debug(f"📥 Respuesta final de Deepgram recibida: {response}")
            except Exception as e:
                logger.warning(f"⚠️ No se recibió respuesta final de Deepgram: {e}")

            # 3. Cerrar el WebSocket de manera ordenada
            await self.websocket.close()
            logger.info("✅ WebSocket cerrado exitosamente después de enviar 'CloseStream'.")

        except Exception as e:
            logger.error(f"❌ Error durante el cierre del stream de Deepgram: {e}")
        finally:
            self.websocket = None
            logger.info("🔒 Deepgram streaming cerrado de manera ordenada.")


    async def _on_open(self, *_):
        logger.info("🔛 Deepgram streaming iniciado")

    async def _on_transcript(self, _connection, result, *args, **kwargs):
        transcript = result.channel.alternatives[0].transcript
        if transcript:
            self.callback(transcript, result.is_final)

    async def _on_close(self, *args, **kwargs):
        logger.info("🔒 Deepgram streaming cerrado")
        self._started = False

    async def _on_error(self, _connection, error, *args, **kwargs):
        logger.error(f"💥 Error Deepgram: {error}")
        self._started = False
