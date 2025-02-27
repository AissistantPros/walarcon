import asyncio
import os
import json
import audioop  # type: ignore
from google.cloud import speech
from google.oauth2.service_account import Credentials
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class GoogleSTTStreamer:
    """
    Conecta con Google Speech-to-Text en tiempo real.
    - Convierte audio mulaw (Twilio) a PCM16.
    - Usa autenticación con credenciales en variables de entorno.
    - Procesa la transcripción en vivo.
    """

    def __init__(self):
        self.closed = False

        # 📌 Autenticación con variables de entorno
        self.credentials = self._get_google_credentials()
        self.client = speech.SpeechClient(credentials=self.credentials)

        # Configuración de STT
        self.config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
            model="medical_conversation",  # Cambia a "default" si no quieres modo médico
            enable_automatic_punctuation=True
        )

        self.streaming_config = speech.StreamingRecognitionConfig(
            config=self.config,
            interim_results=True  # Para recibir transcripción parcial
        )

        # Cola de audio
        self.audio_queue = asyncio.Queue()

    def _get_google_credentials(self):
        """
        Carga las credenciales de Google desde variables de entorno.
        """
        try:
            google_creds = {
                "type": "service_account",
                "project_id": os.getenv("GOOGLE_PROJECT_ID"),
                "private_key_id": os.getenv("GOOGLE_PRIVATE_KEY_ID"),
                "private_key": os.getenv("GOOGLE_PRIVATE_KEY").replace("\\n", "\n"),
                "client_email": os.getenv("GOOGLE_CLIENT_EMAIL"),
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "auth_uri": os.getenv("GOOGLE_AUTH_URI"),
                "token_uri": os.getenv("GOOGLE_TOKEN_URI"),
                "auth_provider_x509_cert_url": os.getenv("GOOGLE_AUTH_PROVIDER_CERT_URL"),
                "client_x509_cert_url": os.getenv("GOOGLE_CLIENT_X509_CERT_URL")
            }
            return Credentials.from_service_account_info(google_creds)
        except Exception as e:
            logger.error(f"⚠️ Error cargando credenciales de Google: {str(e)}")
            return None

    async def recognize_stream(self):
        """
        Envía audio a Google STT en streaming y recibe respuestas en tiempo real.
        """

        async def request_generator():
            """Generador asíncrono que envía los chunks de audio a Google STT."""
            while not self.closed:
                chunk = await self.audio_queue.get()
                if chunk is None:
                    break
                yield speech.StreamingRecognizeRequest(audio_content=chunk)

        # Conexión con Google STT en tiempo real
        try:
            responses = self.client.streaming_recognize(
                config=self.streaming_config,
                requests=request_generator()
            )

            # Procesar respuestas en tiempo real
            async for response in self._handle_responses(responses):
                yield response

        except Exception as e:
            logger.error(f"❌ Error en el streaming con Google STT: {str(e)}")

        self.closed = True

    async def _handle_responses(self, responses):
        """
        Maneja las respuestas de Google STT en tiempo real.
        """
        async for response in responses:
            for result in response.results:
                transcript = result.alternatives[0].transcript
                if result.is_final:
                    logger.info(f"🗣️ [USUARIO] (Final): {transcript}")
                else:
                    logger.info(f"🗣️ [USUARIO] (Parcial): {transcript}")
                yield result

    def add_audio_chunk(self, mulaw_data: bytes):
        """
        Convierte audio mu-law a PCM16 y lo envía a la cola para Google STT.
        """
        try:
            pcm16 = audioop.ulaw2lin(mulaw_data, 2)  # Convierte mulaw → PCM16
            asyncio.create_task(self.audio_queue.put(pcm16))
        except Exception as e:
            logger.error(f"⚠️ [Google STT] Error al convertir audio: {str(e)}")

    async def close(self):
        """Cierra la conexión y vacía la cola de audio."""
        self.closed = True
        await self.audio_queue.put(None)
