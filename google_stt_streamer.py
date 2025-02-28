# google_stt_streamer.py
import asyncio
import logging
import time
import audioop # type: ignore
import os
from google.cloud import speech_v1 as speech
from google.oauth2.service_account import Credentials

logger = logging.getLogger("google_stt_streamer")
logger.setLevel(logging.DEBUG)

def _get_google_credentials():
    """
    Carga las credenciales de Google desde las variables de entorno.
    Asegúrate de tener configuradas las siguientes variables en tu entorno:
      - GOOGLE_PROJECT_ID
      - GOOGLE_PRIVATE_KEY_ID
      - GOOGLE_PRIVATE_KEY (con los saltos de línea escapados, por ejemplo, "\\n")
      - GOOGLE_CLIENT_EMAIL
      - GOOGLE_CLIENT_ID
      - GOOGLE_AUTH_URI
      - GOOGLE_TOKEN_URI
      - GOOGLE_AUTH_PROVIDER_CERT_URL
      - GOOGLE_CLIENT_X509_CERT_URL
    """
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
        "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_X509_CERT_URL")
    }
    logger.info("Credenciales de Google cargadas correctamente.")
    return Credentials.from_service_account_info(creds_info)

class GoogleSTTStreamer:
    def __init__(self):
        self.credentials = _get_google_credentials()
        self.client = speech.SpeechClient(credentials=self.credentials)
        self.streaming_config = speech.StreamingRecognitionConfig(
            config=speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=8000,
                language_code="es-MX"
            ),
            interim_results=True
        )
        self.audio_queue = asyncio.Queue()
        self.closed = False  # Flag para indicar si el streamer está cerrado
        self.stream_start_time = time.time()

    async def recognize_stream(self):
        """Gestiona el streaming de reconocimiento de voz con Google STT."""
        self.stream_start_time = time.time()
        try:
            responses = self.client.streaming_recognize(
                config=self.streaming_config,
                requests=self._request_generator()
            )
            async for response in responses:
                for result in response.results:
                    if result.is_final:
                        logger.info(f"📝 Transcripción final: {result.alternatives[0].transcript}")
                    else:
                        logger.debug(f"⏳ Parcial: {result.alternatives[0].transcript}")
        except Exception as e:
            if "Exceeded maximum allowed stream duration" in str(e):
                logger.warning("⚠️ Stream STT cerrado por límite de duración.")
            else:
                logger.error(f"❌ Error en el streaming con Google STT: {e}")
        finally:
            self.closed = True  # Marcar el streamer como cerrado

    async def _request_generator(self):
        """Generador que envía los chunks de audio a Google STT."""
        while not self.closed:
            try:
                chunk = await asyncio.wait_for(self.audio_queue.get(), timeout=0.5)
                if chunk:
                    logger.debug(f"🚀 Enviando chunk PCM16 a Google ({len(chunk)} bytes)")
                    yield speech.StreamingRecognizeRequest(audio_content=chunk)
                else:
                    logger.warning("⚠️ Chunk None detectado, ignorando...")
            except asyncio.TimeoutError:
                logger.debug("⏳ No se obtuvo chunk en 0.5 s. Enviando request vacío.")
                yield speech.StreamingRecognizeRequest(audio_content=b"")

    async def add_audio_chunk(self, mulaw_data: bytes):
        """Convierte mu-law a PCM16 y lo añade a la cola de audio."""
        try:
            pcm16 = audioop.ulaw2lin(mulaw_data, 2)
            if not pcm16:
                logger.error("❌ Chunk PCM16 convertido está vacío!")
                return
            await self.audio_queue.put(pcm16)
        except Exception as e:
            logger.error(f"🔥 Error en add_audio_chunk: {str(e)}", exc_info=True)

    async def close(self):
        """Cierra el stream y vacía la cola de audio."""
        logger.info("🛑 Cerrando GoogleSTTStreamer. Vaciando cola de audio.")
        self.closed = True
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
            self.audio_queue.task_done()
        await asyncio.sleep(0.1)
        logger.info("✅ GoogleSTTStreamer cerrado.")
