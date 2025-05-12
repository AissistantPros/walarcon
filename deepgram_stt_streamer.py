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






    # En deepgram_stt_streamer.py

    async def close(self):
        """
        Cierra la conexión con Deepgram de forma limpia:
        1. Envía {"type": "CloseStream"} si es posible.
        2. Llama a .finish() (o .close() del SDK) para rematar.
        3. Marca la conexión como cerrada.
        """
        if not self.dg_connection or not self._started:
            logger.debug("🔄 Conexión Deepgram ya estaba cerrada o no iniciada.")
            return

        try:
            logger.info("🔒 Intentando cerrar conexión Deepgram...")

            # Paso 1: Enviar el mensaje "CloseStream"
            try:
                # El SDK debería manejar si el socket ya está cerrado,
                # pero envolvemos por si acaso para no detener el flujo de cierre.
                await self.dg_connection.send(json.dumps({"type": "CloseStream"}))
                logger.info("📨 'CloseStream' enviado a Deepgram.")
                await asyncio.sleep(0.1) # Pequeña pausa para procesamiento
            except Exception as e_send_close_stream:
                logger.warning(f"⚠️ No se pudo enviar 'CloseStream' (puede que la conexión ya estuviera cerrándose/cerrada): {e_send_close_stream}")

            # Paso 2: Finalizar la conexión usando el método del SDK
            logger.debug("⏳ Llamando a finish() en la conexión Deepgram...")
            try:
                await asyncio.wait_for(self.dg_connection.finish(), timeout=2.0) # Timeout de 2s para finish()
                logger.info("✅ Método finish() de Deepgram SDK ejecutado (o timeout).")
            except asyncio.TimeoutError:
                logger.warning("⏳ Timeout (2s) esperando a que Deepgram SDK termine con finish().")
            except Exception as e_finish:
                logger.error(f"❌ Error durante finish() del SDK de Deepgram: {e_finish}", exc_info=True)

        except asyncio.CancelledError:
            logger.info("🧹 Cierre de Deepgram cancelado (probablemente por shutdown general).")
        except Exception as e: # Captura cualquier otra excepción durante el proceso de cierre
            logger.error(f"💥 Error inesperado general al intentar cerrar Deepgram: {e}", exc_info=True)
        finally:
            self._started = False
            self.dg_connection = None
            logger.info("🧹 Estado de DeepgramSTTStreamer limpiado y conexión puesta a None.")




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
