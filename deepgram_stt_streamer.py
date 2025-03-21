import os
import json
import asyncio
import logging
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions

logger = logging.getLogger("deepgram_stt_streamer")
logger.setLevel(logging.INFO)

DEEPGRAM_KEY = os.getenv("DEEPGRAM_KEY")
if not DEEPGRAM_KEY:
    raise ValueError("❌ Variable DEEPGRAM_KEY no encontrada")

class DeepgramSTTStreamer:
    def __init__(self):
        self.deepgram = DeepgramClient(DEEPGRAM_KEY)
        self.dg_connection = None
        self._callback = None
        self._started = False

    def start_streaming(self, callback):
        """
        Inicia la conexión con Deepgram y establece el callback para resultados.
        callback: función que recibe result (con .is_final y .alternatives[0].transcript)
        """
        self._callback = callback
        loop = asyncio.get_event_loop()
        loop.create_task(self._start_async_stream())

    async def _start_async_stream(self):
        try:
            self.dg_connection = self.deepgram.listen.asynclive.v("1")
            self.dg_connection.on(LiveTranscriptionEvents.Open, self._on_open)
            self.dg_connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
            self.dg_connection.on(LiveTranscriptionEvents.Close, self._on_close)
            self.dg_connection.on(LiveTranscriptionEvents.Error, self._on_error)

            options = LiveOptions(
                model="enhanced",
                language="es",
                encoding="mulaw",
                sample_rate=8000,
                channels=1,
                smart_format=True,
                interim_results=True,
            )

            await self.dg_connection.start(options)
            logger.info("✅ Conexión Deepgram establecida")
            self._started = True

        except Exception as e:
            logger.error(f"❌ Error al iniciar conexión Deepgram: {e}")

    async def send_audio(self, chunk: bytes):
        """
        Envía audio mu-law a Deepgram. Solo si la conexión está iniciada.
        """
        if self.dg_connection and self._started:
            try:
                await self.dg_connection.send(chunk)
            except Exception as e:
                logger.error(f"❌ Error enviando audio a Deepgram: {e}")

    async def close(self):
        """
        Cierra la conexión con Deepgram.
        """
        if self.dg_connection:
            try:
                await self.dg_connection.finish()
                logger.info("🔚 Conexión Deepgram finalizada")
            except Exception as e:
                logger.error(f"❌ Error al cerrar conexión Deepgram: {e}")

    async def _on_open(self, *_):
        logger.info("🔛 Deepgram streaming iniciado")

    async def _on_transcript(self, _connection, result, *args, **kwargs):
        transcript = result.channel.alternatives[0].transcript
        if transcript and self._callback:
            self._callback(result)

    async def _on_close(self, *_):
        logger.info("🔒 Deepgram streaming cerrado")

    async def _on_error(self, _connection, error, *args, **kwargs):
        logger.error(f"💥 Error Deepgram: {error}")
