#deepgram_stt_streamer.py
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
                model="enhanced",
                language="es",
                encoding="mulaw",
                sample_rate=8000,
                channels=1,
                smart_format=True,
                interim_results=True,
                endpointing=True,
                utterance_end_ms=2000
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
        if self.dg_connection and self._started:
            try:
                await self.dg_connection.send(chunk)
            except Exception as e:
                logger.error(f"❌ Error enviando audio a Deepgram: {e}")
        else:
            logger.warning("⚠️ Audio ignorado: conexión no iniciada.")

    async def close(self):
        """
        Cierra la conexión con Deepgram y asegura cierre limpio.
        """
        if self.dg_connection:
            try:
                await self.dg_connection.finish()
                await asyncio.sleep(0.1)  # pequeña pausa para que finalice correctamente
                self._started = False
                logger.info("🔚 Conexión Deepgram finalizada")
            except asyncio.CancelledError:
                logger.warning("⚠️ Tarea cancelada durante el cierre de Deepgram.")
            except Exception as e:
                if "cancelled" in str(e).lower():
                    logger.info("🔇 Deepgram cerró la conexión por cancelación de tareas (normal).")
                else:
                    logger.error(f"❌ Error al cerrar conexión Deepgram: {e}")

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
