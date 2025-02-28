# google_stt_streamer.py
import asyncio
import os
import time
import audioop  # type: ignore
from google.cloud import speech
from google.oauth2.service_account import Credentials
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class GoogleSTTStreamer:
    """
    Conecta con Google Speech-to-Text en tiempo real.
    - Convierte audio mu-law (Twilio) a PCM16.
    - Usa autenticación con credenciales en variables de entorno.
    - Procesa la transcripción en vivo.
    - Mantiene un timer para reinicios (se reinicia desde TwilioWebSocketManager).
    """

    def __init__(self):
        self.closed = False
        self.stream_start_time = None  # Se asignará al iniciar el streaming.
        self.loop = asyncio.get_event_loop()
        self.credentials = self._get_google_credentials()
        self.client = speech.SpeechClient(credentials=self.credentials)

        self.config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
            # Usamos el modelo por defecto para español.
            enable_automatic_punctuation=True
        )

        self.streaming_config = speech.StreamingRecognitionConfig(
            config=self.config,
            interim_results=True  # Para recibir transcripción parcial.
        )

        self.audio_queue = asyncio.Queue()

    def _get_google_credentials(self):
        """Carga las credenciales de Google desde variables de entorno."""
        try:
            creds_info = {
                "type": "service_account",
                "project_id": os.getenv("GOOGLE_PROJECT_ID"),
                "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_ID"),
                "private_key": os.getenv("GOOGLE_PRIVATE_KEY", "").replace("\\n", "\n"),
                "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "auth_uri": os.getenv("GOOGLE_AUTH_URI"),
                "token_uri": os.getenv("GOOGLE_TOKEN_URI"),
                "auth_provider_x509_cert_url": os.getenv("GOOGLE_AUTH_PROVIDER_CERT_URL"),
                "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_X509_CERT_URL"),
            }
            return Credentials.from_service_account_info(creds_info)
        except Exception as e:
            logger.error(f"Error al cargar credenciales de Google: {e}")
            return None

    def _request_generator(self):
        """
        Generador síncrono que envía los chunks de audio a Google STT.
        Usa asyncio.run_coroutine_threadsafe() para obtener el siguiente chunk
        desde el event loop correcto. Si no hay audio en 0.1 s, envía un request vacío.
        """
        while not self.closed:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.audio_queue.get(), self.loop
                )
                chunk = future.result(timeout=0.1)
            except Exception:
                yield speech.StreamingRecognizeRequest(audio_content=b"")
                continue

            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    async def recognize_stream(self):
        """
        Envía audio a Google STT en streaming y recibe respuestas en tiempo real.
        Asigna el tiempo de inicio del stream.
        """
        self.stream_start_time = time.time()
        try:
            responses = self.client.streaming_recognize(
                config=self.streaming_config,
                requests=self._request_generator()
            )
            for response in responses:
                for result in response.results:
                    yield result
        except Exception as e:
            logger.error(f"❌ Error en el streaming con Google STT: {e}")
        self.closed = True

    def add_audio_chunk(self, mulaw_data: bytes):
        """
        Convierte audio mu-law a PCM16 y lo añade a la cola para Google STT.
        """
        try:
            pcm16 = audioop.ulaw2lin(mulaw_data, 2)
            asyncio.run_coroutine_threadsafe(self.audio_queue.put(pcm16), self.loop)
        except Exception as e:
            logger.error(f"Error al convertir audio: {e}")

    async def close(self):
        """
        Cierra la conexión y vacía la cola de audio.
        """
        self.closed = True
        # Vaciar la cola
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
                self.audio_queue.task_done()
            except asyncio.QueueEmpty:
                break
        await self.audio_queue.put(None)
