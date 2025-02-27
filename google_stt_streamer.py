import asyncio
import os
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
    """

    def __init__(self):
        self.closed = False

        # Autenticación
        self.credentials = self._get_google_credentials()
        self.client = speech.SpeechClient(credentials=self.credentials)

        # Config STT
        self.config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
            model="medical_conversation",  # o "default"
            enable_automatic_punctuation=True
        )

        self.streaming_config = speech.StreamingRecognitionConfig(
            config=self.config,
            interim_results=True
        )

        # Cola de audio
        self.audio_queue = asyncio.Queue()

    def _get_google_credentials(self):
        """Carga las credenciales desde variables de entorno."""
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
        Generador sincrónico que extrae datos de self.audio_queue (async)
        usando asyncio.run(...) de forma bloqueante.
        """
        while not self.closed:
            # Bloquea hasta que llegue un chunk o 'None'
            chunk = asyncio.run(self.audio_queue.get())
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    async def recognize_stream(self):
        """
        Envía audio a Google STT y recibe respuestas en tiempo real.
        """
        try:
            responses = self.client.streaming_recognize(
                config=self.streaming_config,
                requests=self._request_generator()
            )

            # Manejar las respuestas
            async for response in self._handle_responses(responses):
                yield response

        except Exception as e:
            logger.error(f"❌ Error en el streaming con Google STT: {e}")
        self.closed = True

    async def _handle_responses(self, responses):
        """Procesa las respuestas de Google en tiempo real."""
        for response in responses:
            for result in response.results:
                transcript = result.alternatives[0].transcript
                if result.is_final:
                    logger.info(f"[USUARIO Final] => {transcript}")
                else:
                    logger.info(f"[USUARIO Parcial] => {transcript}")
                yield result

    def add_audio_chunk(self, mulaw_data: bytes):
        """
        Convierte mu-law → PCM16 y lo mete en la cola.
        """
        try:
            pcm16 = audioop.ulaw2lin(mulaw_data, 2)
            # Agrega chunk sin bloquear
            asyncio.create_task(self.audio_queue.put(pcm16))
        except Exception as e:
            logger.error(f"Error al convertir audio: {e}")

    async def close(self):
        """Indica fin de audio y cierra el stream."""
        self.closed = True
        await self.audio_queue.put(None)
