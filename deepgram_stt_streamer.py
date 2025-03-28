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
        self.full_transcription = ""  # Para acumular transcripciones

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
            self.dg_connection.on(LiveTranscriptionEvents.UtteranceEnd, self._on_utterance_end)
            self.dg_connection.on(LiveTranscriptionEvents.SpeechStarted, self._on_speech_started)

            options = LiveOptions(
                model="nova-2-telephony",  # Mejor para telefonía
                language="es",             # Idioma principal español
                encoding="mulaw",
                sample_rate=8000,
                channels=1,
                smart_format=True,
                interim_results=True,
                endpointing=3000,          # 3 segundos para detectar fin de habla
                utterance_end_ms="5000",   # 5 segundos para detectar fin de enunciado
                vad_events=True,           # Activar eventos de detección de voz
                punctuate=True             # Mejorar la legibilidad con puntuación
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
                await asyncio.sleep(0.5)  # Pausa más larga para asegurar cierre correcto
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
        self.full_transcription = ""  # Reiniciar transcripción al abrir nueva conexión

    async def _on_transcript(self, _connection, result, *args, **kwargs):
        transcript = result.channel.alternatives[0].transcript
        if not transcript:
            return
            
        # Manejo mejorado de transcripciones finales e intermedias
        if result.is_final:
            if result.speech_final:
                # Transcripción final completa
                final_text = self.full_transcription + " " + transcript
                self.callback(final_text.strip(), True)
                self.full_transcription = ""  # Reiniciar para el siguiente enunciado
            else:
                # Parte final pero no el fin del enunciado
                self.full_transcription += " " + transcript
        else:
            # Resultados intermedios
            self.callback(transcript, False)
            
        logger.debug(f"Transcripción: {transcript} (final: {result.is_final}, speech_final: {result.speech_final})")

    async def _on_utterance_end(self, _connection, utterance_end, *args, **kwargs):
        logger.info(f"🔊 Fin de enunciado detectado: {utterance_end}")
        # Si hay texto acumulado, enviarlo como final
        if self.full_transcription:
            self.callback(self.full_transcription.strip(), True)
            self.full_transcription = ""

    async def _on_speech_started(self, _connection, speech_started, *args, **kwargs):
        logger.info("🎤 Habla detectada")

    async def _on_close(self, *args, **kwargs):
        logger.info("🔒 Deepgram streaming cerrado")
        self._started = False

    async def _on_error(self, _connection, error, *args, **kwargs):
        logger.error(f"💥 Error Deepgram: {error}")
        self._started = False