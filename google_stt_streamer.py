# google_stt_streamer.py
import asyncio
import os
import audioop  # type: ignore
from google.cloud import speech
from google.oauth2.service_account import Credentials
import logging
import statistics

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class GoogleSTTStreamer:
    """
    Conecta con Google Speech-to-Text en tiempo real.
    - Convierte audio mu-law (Twilio) a PCM16.
    - Se autentica usando credenciales desde variables de entorno.
    - Envía audio en streaming y retorna respuestas (parciales y finales).
    """

    def __init__(self):
        self.closed = False
        self.loop = asyncio.get_event_loop()
        self.credentials = self._get_google_credentials()
        self.client = speech.SpeechClient(credentials=self.credentials)

        self.config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
            enable_automatic_punctuation=True
        )

        self.streaming_config = speech.StreamingRecognitionConfig(
            config=self.config,
            interim_results=True
        )

        self.audio_queue = asyncio.Queue()
        logger.info("GoogleSTTStreamer inicializado.")

    def _get_google_credentials(self):
        """Carga las credenciales de Google desde las variables de entorno."""
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
            logger.info("Credenciales de Google cargadas correctamente.")
            return Credentials.from_service_account_info(creds_info)
        except Exception as e:
            logger.error(f"Error al cargar credenciales de Google: {e}")
            return None

    def _request_generator(self):
        """
        Generador síncrono que envía los chunks de audio a Google STT.
        Utiliza asyncio.run_coroutine_threadsafe() para obtener el siguiente chunk
        desde el event loop correcto. Si no llega audio en 0.1 s, envía un request vacío.
        """
        while not self.closed:
            try:
                future = asyncio.run_coroutine_threadsafe(self.audio_queue.get(), self.loop)
                chunk = future.result(timeout=0.1)
                logger.debug(f"Chunk obtenido de la cola, tamaño PCM16: {len(chunk)} bytes." if chunk else "Recibido chunk None.")
            except Exception as e:
                logger.debug(f"No se obtuvo chunk en 0.1 s ({e}). Enviando request vacío.")
                yield speech.StreamingRecognizeRequest(audio_content=b"")
                continue

            if chunk is None:
                logger.info("Chunk None detectado, finalizando generador.")
                break

            # Opcional: calcular energía promedio para verificar la conversión
            try:
                # Dividir en bytes individuales y calcular valor promedio
                samples = [b - 128 for b in chunk]
                avg_energy = statistics.mean([abs(s) for s in samples])
                logger.debug(f"Energía promedio del chunk: {avg_energy:.2f}")
            except Exception as ex:
                logger.debug(f"No se pudo calcular energía: {ex}")

            logger.debug(f"Enviando request con chunk de tamaño: {len(chunk)} bytes.")
            yield speech.StreamingRecognizeRequest(audio_content=chunk)

    async def recognize_stream(self):
        """
        Envía audio a Google STT en streaming y retorna respuestas en tiempo real.
        Se ejecuta la iteración en un hilo separado para evitar bloquear el event loop.
        """
        try:
            logger.info("Iniciando streaming de reconocimiento a Google STT.")
            # Ejecuta la llamada en un thread separado
            loop = asyncio.get_event_loop()
            def get_responses():
                return list(self.client.streaming_recognize(
                    config=self.streaming_config,
                    requests=self._request_generator()
                ))
            responses = await loop.run_in_executor(None, get_responses)
            logger.info(f"Se recibieron {len(responses)} respuestas de Google STT.")
            for response in responses:
                for result in response.results:
                    logger.debug("Respuesta recibida de Google STT.")
                    yield result
        except Exception as e:
            if "Exceeded maximum allowed stream duration" not in str(e):
                logger.error(f"❌ Error en el streaming con Google STT: {e}")
            else:
                logger.info("Stream STT cerrado por límite de duración.")
        self.closed = True

    def add_audio_chunk(self, mulaw_data: bytes):
        """
        Convierte audio mu-law a PCM16 y lo añade a la cola para Google STT.
        Registra el tamaño del chunk original y convertido.
        """
        try:
            logger.debug(f"Chunk mulaw recibido de tamaño: {len(mulaw_data)} bytes.")
            pcm16 = audioop.ulaw2lin(mulaw_data, 2)
            logger.debug(f"Chunk convertido a PCM16, tamaño: {len(pcm16)} bytes.")
            asyncio.run_coroutine_threadsafe(self.audio_queue.put(pcm16), self.loop)
        except Exception as e:
            logger.error(f"Error al convertir audio: {e}")

    async def close(self):
        """
        Cierra el stream y vacía la cola de audio.
        """
        self.closed = True
        logger.info("Cerrando GoogleSTTStreamer. Vaciando cola de audio.")
        while not self.audio_queue.empty():
            try:
                await self.audio_queue.get()
            except asyncio.QueueEmpty:
                break
        await self.audio_queue.put(None)
        logger.info("GoogleSTTStreamer cerrado.")
