# google_stt_streamer.py

import os
import asyncio
import audioop  # type: ignore
from google.cloud import speech
from google.oauth2 import service_account

class GoogleSTTStreamer:
    """
    Maneja la conexión en streaming con Google Speech-to-Text
    usando el modelo "medical_conversation" (u otro).
    """

    def __init__(self):
        # 1) Lee variables STT_... del entorno
        stt_type = os.getenv("STT_TYPE", "service_account")
        stt_project_id = os.getenv("STT_PROJECT_ID", "")
        stt_private_key_id = os.getenv("STT_PRIVATE_KEY_ID", "")
        stt_private_key = os.getenv("STT_PRIVATE_KEY", "").replace("\\n", "\n")
        stt_client_email = os.getenv("STT_CLIENT_EMAIL", "")
        stt_client_id = os.getenv("STT_CLIENT_ID", "")
        stt_auth_uri = os.getenv("STT_AUTH_URI", "https://accounts.google.com/o/oauth2/auth")
        stt_token_uri = os.getenv("STT_TOKEN_URI", "https://oauth2.googleapis.com/token")
        stt_auth_provider_x509_cert_url = os.getenv("STT_AUTH_PROVIDER_X509_CERT_URL", "")
        stt_client_x509_cert_url = os.getenv("STT_CLIENT_X509_CERT_URL", "")

        # 2) Armamos el dict con formato JSON de service account
        credentials_info = {
            "type": stt_type,
            "project_id": stt_project_id,
            "private_key_id": stt_private_key_id,
            "private_key": stt_private_key,
            "client_email": stt_client_email,
            "client_id": stt_client_id,
            "auth_uri": stt_auth_uri,
            "token_uri": stt_token_uri,
            "auth_provider_x509_cert_url": stt_auth_provider_x509_cert_url,
            "client_x509_cert_url": stt_client_x509_cert_url
        }

        # 3) Creamos el objeto Credentials
        credentials = service_account.Credentials.from_service_account_info(credentials_info)

        # 4) Iniciamos speech client con esas credenciales
        self.client = speech.SpeechClient(credentials=credentials)
        self.closed = False

        # Config reconocimiento
        self.config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
            model="medical_conversation",  # Usa el modelo médico
            enable_automatic_punctuation=True
        )

        self.streaming_config = speech.StreamingRecognitionConfig(
            config=self.config,
            interim_results=True
        )

        # Cola asíncrona donde meteremos chunks de audio
        self.audio_queue = asyncio.Queue()

    async def recognize_stream(self):
        async def request_generator():
            while not self.closed:
                chunk = await self.audio_queue.get()
                if chunk is None:
                    break
                yield speech.StreamingRecognizeRequest(audio_content=chunk)

        requests = request_generator()
        responses = self.client.streaming_recognize(
            config=self.streaming_config,
            requests=requests
        )
        async for response in self._handle_responses(responses):
            yield response
        self.closed = True

    async def _handle_responses(self, responses_generator):
        loop = asyncio.get_event_loop()
        def sync_generator():
            for r in responses_generator:
                yield r
        async_gen = loop.run_in_executor(None, lambda: sync_generator())
        async for resp in async_gen:
            for result in resp.results:
                yield result

    def add_audio_chunk(self, mulaw_data: bytes):
        pcm16 = audioop.ulaw2lin(mulaw_data, 2)
        asyncio.create_task(self.audio_queue.put(pcm16))

    async def close(self):
        self.closed = True
        await self.audio_queue.put(None)
