# google_stt_streamer.py
import os
import asyncio
import audioop  # type: ignore
from google.cloud import speech
from google.oauth2 import service_account

class GoogleSTTStreamer:
    def __init__(self):
        # Configuración de credenciales desde variables de entorno (como ya hicimos antes)
        credentials_info = {
            "type": "service_account",
            "project_id": os.getenv("STT_PROJECT_ID", ""),
            "private_key_id": os.getenv("STT_PRIVATE_KEY_ID", ""),
            "private_key": os.getenv("STT_PRIVATE_KEY", "").replace("\\n", "\n"),
            "client_email": os.getenv("STT_CLIENT_EMAIL", ""),
            "client_id": os.getenv("STT_CLIENT_ID", ""),
            "auth_uri": os.getenv("STT_AUTH_URI", ""),
            "token_uri": os.getenv("STT_TOKEN_URI", ""),
            "auth_provider_x509_cert_url": os.getenv("STT_AUTH_PROVIDER_X509_CERT_URL", ""),
            "client_x509_cert_url": os.getenv("STT_CLIENT_X509_CERT_URL", "")
        }

        credentials = service_account.Credentials.from_service_account_info(credentials_info)

        self.client = speech.SpeechClient(credentials=credentials)
        self.closed = False
        self.audio_queue = asyncio.Queue()

        self.config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
            model="medical_conversation",
            enable_automatic_punctuation=True
        )

        self.streaming_config = speech.StreamingRecognitionConfig(
            config=self.config,
            interim_results=True
        )

    def _sync_request_generator(self):
        """Convierte la cola async en un generador síncrono para gRPC."""
        while not self.closed:
            chunk = asyncio.run(self.audio_queue.get())  # Espera un chunk de la cola
            if chunk is None:
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    async def recognize_stream(self):
        """Stream de audio a Google STT, enviando y recibiendo transcripciones."""
        requests = self._sync_request_generator()  # ⚡ Ahora es síncrono
        responses = self.client.streaming_recognize(
            config=self.streaming_config,
            requests=requests
        )

        # Procesar respuestas en un hilo separado para evitar bloqueos
        loop = asyncio.get_running_loop()
        for response in await loop.run_in_executor(None, lambda: list(responses)):
            for result in response.results:
                yield result

    def add_audio_chunk(self, mulaw_data: bytes):
        """Convierte mu-law a PCM16 y lo envía a la cola."""
        pcm16 = audioop.ulaw2lin(mulaw_data, 2)
        asyncio.create_task(self.audio_queue.put(pcm16))

    async def close(self):
        """Cierra la transmisión enviando None a la cola."""
        self.closed = True
        await self.audio_queue.put(None)
