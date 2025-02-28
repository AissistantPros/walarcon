# google_stt_streamer.py
import asyncio
import logging
import time
import audioop # type: ignore
import os

from google.cloud import speech_v1 as speech

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

class GoogleSTTStreamer:
    """
    Convierte mulaw (8k) a LINEAR16 (8k) y envía el audio a Google STT de manera continua.
    Guarda el audio PCM16 interceptado en 'audio_debug/converted_8k.raw' para ver qué se manda a Google.
    """
    def __init__(self):
        # Inicializa cliente de STT. Ajustar credenciales si es necesario
        self.client = speech.SpeechClient()

        # streaming_config con sample_rate_hertz=8000 para banda telefónica
        self.streaming_config = speech.StreamingRecognitionConfig(
            config=speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=8000,
                language_code="es-MX"
            ),
            interim_results=True
        )
        self.audio_queue = asyncio.Queue()
        self.closed = False
        self.stream_start_time = time.time()

    async def recognize_stream(self):
        """
        Lógica asíncrona que consume el audio de la cola y obtiene respuestas parciales/finales.
        """
        self.stream_start_time = time.time()
        try:
            responses = self.client.streaming_recognize(
                config=self.streaming_config,
                requests=self._request_generator()
            )
            # iterate over the streaming responses as they arrive
            async for response in responses:
                for result in response.results:
                    if result.is_final:
                        logger.info(f"📝 Transcripción final: {result.alternatives[0].transcript}")
                    else:
                        logger.debug(f"(parcial) => {result.alternatives[0].transcript}")

        except Exception as e:
            if "Exceeded maximum allowed stream duration" in str(e):
                logger.warning("⚠️ Stream STT cerrado por límite de duración.")
            else:
                logger.error(f"❌ Error en el streaming con Google STT: {e}")
        finally:
            self.closed = True
            logger.debug("Finalizó recognize_stream()")

    async def _request_generator(self):
        """
        Genera peticiones StreamingRecognizeRequest con los chunks PCM16 de la cola.
        """
        while not self.closed:
            try:
                # Esperamos medio segundo por un chunk
                chunk = await asyncio.wait_for(self.audio_queue.get(), timeout=0.5)
                if chunk:
                    logger.debug(f"🚀 Enviando chunk PCM16 a Google ({len(chunk)} bytes)")
                    yield speech.StreamingRecognizeRequest(audio_content=chunk)
                else:
                    logger.warning("⚠️ Chunk None detectado (sin datos).")
            except asyncio.TimeoutError:
                # Enviamos request vacío para "mantener vivo" el stream
                logger.debug("⏳ No se recibió chunk en 0.5s, enviando vacío.")
                yield speech.StreamingRecognizeRequest(audio_content=b"")

    async def add_audio_chunk(self, mulaw_data: bytes):
        """
        Recibe audio en mulaw 8k, lo convierte a LINEAR16 y lo envía a la cola.
        Además, guardamos el chunk LINEAR16 en un archivo para su análisis.
        """
        try:
            pcm16 = audioop.ulaw2lin(mulaw_data, 2)
            if not pcm16:
                logger.error("❌ Error: chunk PCM16 quedó vacío tras la conversión.")
                return
            # Guardar chunk PCM16 en un archivo
            self._save_pcm16_chunk(pcm16)

            # Enviar chunk a la cola de audio
            await self.audio_queue.put(pcm16)

        except Exception as e:
            logger.error(f"🔥 Error en add_audio_chunk: {e}", exc_info=True)

    def _save_pcm16_chunk(self, pcm_data: bytes, filename: str = "audio_debug/converted_8k.raw"):
        """
        Guarda el audio PCM16 resultante en un archivo local para revisar.
        """
        try:
            os.makedirs("audio_debug", exist_ok=True)
            with open(filename, "ab") as f:
                f.write(pcm_data)
            logger.debug(f"🎵 Guardado chunk PCM16 ({len(pcm_data)} bytes) en {filename}")
        except Exception as e:
            logger.error(f"Error al guardar PCM16: {e}")

    async def close(self):
        """
        Marca el streamer como cerrado y vacía la cola de audio.
        """
        logger.info("🛑 Cerrando GoogleSTTStreamer. Vaciando cola de audio.")
        self.closed = True
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
            self.audio_queue.task_done()
        # Pausa breve para evitar tareas colgantes
        await asyncio.sleep(0.1)
        logger.info("✅ GoogleSTTStreamer cerrado.")
