# google_stt_streamer.py

import os
import time
import queue
import threading
import logging
import asyncio
import audioop  # type: ignore
from google.cloud import speech
from google.cloud.speech_v1 import StreamingRecognitionConfig, StreamingRecognizeRequest
from decouple import config  # O python-dotenv, o la librería que uses para tus env

logger = logging.getLogger("google_stt_streamer")
logger.setLevel(logging.DEBUG)

class GoogleSTTStreamer:
    """
    Maneja la conexión con Google Speech-to-Text en un hilo secundario.
    - Recibe chunks PCM16 8kHz
    - Agrupa ~100ms (1600 bytes) antes de enviar
    - single_utterance=False => no cierra al primer silencio
    - interim_results=True => manda resultados parciales
    """

    REQUIRED_BYTES_100MS = 1600

    def __init__(self):
        """
        Carga credenciales desde variables de entorno y crea el cliente de STT.
        """
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

        # Config de reconocimiento
        recognition_config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
            enable_automatic_punctuation=True,
        )
        self.streaming_config = StreamingRecognitionConfig(
            config=recognition_config,
            interim_results=True,      # <--- Resultados parciales
            single_utterance=False     # <--- No cerrar al primer silencio
        )
        logger.info("[GoogleSTTStreamer] Instanciado con config 8k LINEAR16, single_utterance=False, interim=True.")

        # Cola donde se ponen los chunks PCM16
        self.audio_queue = asyncio.Queue()

        self.closed = False
        self._buffer = bytearray()
        self._stop_event = threading.Event()
        self._thread = None

    def start_streaming(self, callback):
        """
        Inicia el hilo que hará streaming_recognize con Google.
        :param callback: función(result) con la transcripción parcial/final
        """
        if self._thread and self._thread.is_alive():
            logger.warning("[GoogleSTTStreamer] Ya existe un stream activo.")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_streaming_recognize,
            args=(callback,),
            name="_run_streaming_recognize",
            daemon=True
        )
        self._thread.start()

    def _run_streaming_recognize(self, callback):
        """
        Corre en un hilo. Produce 'resp.results' que llama callback(result).
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
            logger.debug(f"[GoogleSTTStreamer] _run_streaming_recognize error: {e}")
            logger.error(f"❌ Error en el streaming con Google STT: {str(e)}")
        finally:
            logger.debug("[GoogleSTTStreamer] Finalizó recognize_stream()")

    def _request_generator(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            while not self._stop_event.is_set() and not self.closed:
                try:
                    chunk = loop.run_until_complete(
                        asyncio.wait_for(self.audio_queue.get(), timeout=0.05)
                    )
                    if chunk:
                        self._buffer.extend(chunk)

                    while len(self._buffer) >= self.REQUIRED_BYTES_100MS:
                        send_chunk = self._buffer[: self.REQUIRED_BYTES_100MS]
                        self._buffer = self._buffer[self.REQUIRED_BYTES_100MS:]
                        yield StreamingRecognizeRequest(audio_content=bytes(send_chunk))
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    logger.debug(f"[GoogleSTTStreamer] _request_generator error: {e}")
                    break

            # Al terminar
            if self._buffer:
                yield StreamingRecognizeRequest(audio_content=bytes(self._buffer))
                self._buffer.clear()
        finally:
            loop.close()

    async def add_audio_chunk(self, mulaw_data: bytes):
        """
        Convierte mu-law a PCM16 y lo pone en la cola.
        """
        try:
            pcm16 = audioop.ulaw2lin(mulaw_data, 2)
            if pcm16:
                await self.audio_queue.put(pcm16)
            else:
                logger.warning("[GoogleSTTStreamer] Chunk PCM16 vacío tras conversión mu-law.")
        except Exception as e:
            logger.error(f"[GoogleSTTStreamer] Error al convertir mu-law: {e}")

    def close(self):
        """
        Cierra la transmisión con Google.
        """
        logger.info("[GoogleSTTStreamer] 🛑 Cerrando GoogleSTTStreamer. Vaciando cola de audio.")
        self.closed = True
        self._stop_event.set()

        try:
            self._clear_queue()
        except:
            pass

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

        logger.info("[GoogleSTTStreamer] ✅ GoogleSTTStreamer cerrado.")

    def _clear_queue(self):
        while not self.audio_queue.empty():
            self.audio_queue.get()
            self.audio_queue.task_done()
