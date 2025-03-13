# deepgram_stt.py - Integración con Deepgram usando DeepgramClient

import asyncio
import os
import json
import logging
from deepgram import DeepgramClient, DeepgramClientOptions, LiveTranscriptionEvents, LiveOptions

logger = logging.getLogger("deepgram_stt")
logger.setLevel(logging.INFO)

DEEPGRAM_KEY = os.getenv("DEEPGRAM_KEY")

if not DEEPGRAM_KEY or len(DEEPGRAM_KEY) != 40:
    logger.error(f"❌ ERROR: API Key de Deepgram no encontrada o incorrecta: {DEEPGRAM_KEY}")
else:
    logger.info(f"🔑 API Key obtenida correctamente: {DEEPGRAM_KEY[:5]}**********")


class DeepgramSTT:
    """
    Maneja la transcripción en tiempo real con Deepgram utilizando DeepgramClient.
    """

    def __init__(self, callback):
        """
        Inicializa el cliente de Deepgram y registra los eventos.
        :param callback: Función que procesará la transcripción de voz.
        """
        self.callback = callback
        self.deepgram = DeepgramClient(DEEPGRAM_KEY)
        self.dg_connection = None

    async def start_streaming(self):
        """
        Inicia la conexión con Deepgram y comienza a procesar transcripciones.
        """
        try:
            self.dg_connection = self.deepgram.listen.websocket.v("1")

            # Eventos de Deepgram
            self.dg_connection.on(LiveTranscriptionEvents.Open, self.on_open)
            self.dg_connection.on(LiveTranscriptionEvents.Transcript, self.on_transcript)
            self.dg_connection.on(LiveTranscriptionEvents.Close, self.on_close)
            self.dg_connection.on(LiveTranscriptionEvents.Error, self.on_error)

            # Configuración de la conexión
            options = LiveOptions(
                model="nova-3",
                language="es",
                encoding="mulaw",
                sample_rate=8000,  # Ajustar según el formato de Twilio
                smart_format=True,
            )

            logger.info("🎙️ Conectando a Deepgram...")
            if self.dg_connection.start(options) is False:
                logger.error("❌ No se pudo conectar a Deepgram")
                return

        except Exception as e:
            logger.error(f"❌ Error en Deepgram STT: {e}")

    def send_audio(self, audio_chunk: bytes):
        """
        Envía un fragmento de audio a Deepgram para transcripción.
        """
        if self.dg_connection:
            self.dg_connection.send(audio_chunk)

    async def on_open(self, *_):
        """
        Evento cuando se abre la conexión con Deepgram.
        """
        logger.info("✅ Conectado a Deepgram STT")

    async def on_transcript(self, result, *_):
        """
        Maneja la transcripción recibida de Deepgram.
        """
        transcript = result.channel.alternatives[0].transcript
        if transcript:
            logger.info(f"📝 Transcripción: {transcript}")
            self.callback(transcript, result.is_final)

    async def on_close(self, *_):
        """
        Maneja el cierre de la conexión.
        """
        logger.info("🛑 Deepgram STT cerrado")

    async def on_error(self, error, *_):
        """
        Maneja errores de Deepgram.
        """
        logger.error(f"❌ Error en Deepgram STT: {error}")

    def close(self):
        """
        Cierra la conexión con Deepgram.
        """
        if self.dg_connection:
            self.dg_connection.finish()
        logger.info("🛑 Deepgram STT cerrado")
