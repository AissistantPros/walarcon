# google_stt_streamer.py

import os
import time
import threading
import logging
import asyncio
import audioop  # type: ignore
from google.cloud import speech
from google.cloud.speech_v1 import StreamingRecognitionConfig, StreamingRecognizeRequest
from decouple import config  # Se usan las variables de entorno

logger = logging.getLogger("google_stt_streamer")
logger.setLevel(logging.DEBUG)

class GoogleSTTStreamer:
    """
    Maneja la conexión con Google Speech-to-Text en un hilo secundario.
    - Recibe chunks PCM16 8kHz (convertidos desde mu-law)
    - Agrupa aproximadamente 100ms de audio (1600 bytes) antes de enviar
    """

    # 100ms de audio a 8kHz, 16 bits, 1 canal: 8000 * 0.1 * 2 = 1600 bytes
    REQUIRED_BYTES_100MS = 1600

    def __init__(self):
        # Cargar credenciales desde variables de entorno
        project_id = config("GOOGLE_PROJECT_ID", default="")
        private_key_id = config("GOOGLE_PRIVATE_KEY_ID", default="")
        private_key = config("GOOGLE_PRIVATE_KEY", default="").replace("\\n", "\n")
        client_email = config("GOOGLE_CLIENT_EMAIL", default="")
        token_uri = config("GOOGLE_TOKEN_URI", default="https://oauth2.googleapis.com/token")

        if not project_id or not private_key_id or not private_key or not client_email:
            raise RuntimeError("Faltan credenciales de Google STT en las variables de entorno.")

        from google.oauth2.service_account import Credentials

        credentials_info = {
            "type": "service_account",
            "project_id": project_id,
            "private_key_id": private_key_id,
            "private_key": private_key,
            "client_email": client_email,
            "client_id": config("GOOGLE_CLIENT_ID", default=""),
            "auth_uri": config("GOOGLE_AUTH_URI", default="https://accounts.google.com/o/oauth2/auth"),
            "token_uri": token_uri,
            "auth_provider_x509_cert_url": config("GOOGLE_AUTH_PROVIDER_X509_CERT_URL", default=""),
            "client_x509_cert_url": config("GOOGLE_CLIENT_CERT_URL", default="")
        }

        creds = Credentials.from_service_account_info(credentials_info)
        self.client = speech.SpeechClient(credentials=creds)
        logger.info("[GoogleSTTStreamer] Credenciales cargadas correctamente desde variables de entorno.")

        # Configuración de reconocimiento
        recognition_config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
            enable_automatic_punctuation=True,
        )
        self.streaming_config = StreamingRecognitionConfig(
            config=recognition_config,
            interim_results=True,
            single_utterance=False
        )
        logger.info("[GoogleSTTStreamer] Instanciado con config 8k LINEAR16.")

        # Cola para los chunks PCM16 (convertidos de mu-law)
        self.audio_queue = asyncio.Queue()
        self.closed = False
        self._buffer = bytearray()
        self._stop_event = threading.Event()
        self._thread = None

    def _request_generator(self):
        """
        Generador síncrono (para el hilo secundario) que agrupa ~100ms de audio
        antes de enviar cada request a Google STT.
        Se crea un event loop local para manejar la cola de forma independiente.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            while not self._stop_event.is_set() and not self.closed:
                try:
                    # Intentamos leer un chunk con timeout de 0.05s para reducir latencia
                    chunk = loop.run_until_complete(
                        asyncio.wait_for(self.audio_queue.get(), timeout=0.05)
                    )
                    if chunk:
                        self._buffer.extend(chunk)

                    # Si ya tenemos al menos 100ms de audio (REQUIRED_BYTES_100MS), enviar ese grupo
                    while len(self._buffer) >= self.REQUIRED_BYTES_100MS:
                        send_chunk = self._buffer[:self.REQUIRED_BYTES_100MS]
                        self._buffer = self._buffer[self.REQUIRED_BYTES_100MS:]
                        yield StreamingRecognizeRequest(audio_content=bytes(send_chunk))
                except asyncio.TimeoutError:
                    # En timeout, si hay datos en el buffer, enviarlos (esto puede ayudar en pausas prolongadas)
                    if self._buffer:
                        yield StreamingRecognizeRequest(audio_content=bytes(self._buffer))
                        self._buffer.clear()
                except Exception as e:
                    logger.debug(f"[GoogleSTTStreamer] _request_generator error: {e}")
                    break

            # Al cerrar, si queda algo en el buffer, enviarlo
            if self._buffer:
                yield StreamingRecognizeRequest(audio_content=bytes(self._buffer))
                self._buffer.clear()
        finally:
            loop.close()

    async def add_audio_chunk(self, mulaw_data: bytes):
        """
        Convierte mu-law a PCM16 y lo pone en la cola asíncrona.
        También guarda el audio convertido para depuración.
        """
        try:
            pcm16 = audioop.ulaw2lin(mulaw_data, 2)
            if pcm16:
                await self._save_pcm16_debug(pcm16)
                await self.audio_queue.put(pcm16)
            else:
                logger.warning("[GoogleSTTStreamer] Chunk PCM16 vacío tras conversión mu-law.")
        except Exception as e:
            logger.error(f"[GoogleSTTStreamer] Error al convertir mu-law: {e}", exc_info=True)

    async def _save_pcm16_debug(self, chunk: bytes):
        """
        Guarda el chunk PCM16 en un archivo para depuración.
        (Para reproducirlo: play -t raw -r 8000 -e signed-integer -b 16 -c 1 audio_debug/converted_8k.raw)
        """
        try:
            os.makedirs("audio_debug", exist_ok=True)
            with open("audio_debug/converted_8k.raw", "ab") as f:
                f.write(chunk)
            # Se comenta el log para evitar saturación:
            # logger.debug(f"[GoogleSTTStreamer] Guardado chunk PCM16 ({len(chunk)} bytes) en audio_debug/converted_8k.raw")
        except Exception as e:
            logger.error(f"[GoogleSTTStreamer] Error guardando PCM16 debug: {e}")

    def _run_streaming_recognize(self, callback):
        """
        Corre en un hilo secundario. Toma los requests de _request_generator()
        y procesa las respuestas de streaming_recognize().
        """
        try:
            requests = self._request_generator()
            responses = self.client.streaming_recognize(
                config=self.streaming_config,
                requests=requests
            )
            for resp in responses:
                for result in resp.results:
                    callback(result)
        except Exception as e:
            logger.error(f"[GoogleSTTStreamer] _run_streaming_recognize error: {e}")
        finally:
            logger.debug("[GoogleSTTStreamer] Finalizó recognize_stream()")

    async def close(self):
        """
        Señala que debe dejar de procesar. Cierra la cola y espera que el hilo termine.
        La API de streaming_recognize se interrumpirá.
        """
        logger.info("[GoogleSTTStreamer] 🛑 Cerrando GoogleSTTStreamer. Vaciando cola de audio.")
        self.closed = True
        self._stop_event.set()
        try:
            # Intentar vaciar la cola asíncrona
            loop = asyncio.get_event_loop()
            await self._clear_queue()
        except Exception as e:
            logger.debug(f"[GoogleSTTStreamer] Error al vaciar cola: {e}")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("[GoogleSTTStreamer] ✅ GoogleSTTStreamer cerrado.")

    async def _clear_queue(self):
        """
        Vacía la cola de audio.
        """
        while not self.audio_queue.empty():
            await self.audio_queue.get()
            self.audio_queue.task_done()
