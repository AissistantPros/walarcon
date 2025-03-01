# google_stt_streamer.py

import os
import asyncio
import audioop # type: ignore
import logging
import time

from google.oauth2.service_account import Credentials
from google.cloud import speech_v1 as speech

logger = logging.getLogger("google_stt_streamer")


class GoogleSTTStreamer:
    """
    - Carga credenciales desde variables de entorno (sin JSON local).
    - Convierte mu-law 8k a LINEAR16 (8k), agrupa ~100ms (1600 bytes) y llama streaming_recognize.
    - single_utterance=False para llamadas prolongadas.
    - Usa un generador síncrono + run_coroutine_threadsafe para consumir la cola asíncrona.
    """

    def __init__(self):
        # 1. Leer variables de entorno para credenciales STT
        stt_project_id       = os.environ.get("STT_PROJECT_ID", "")
        stt_private_key_id   = os.environ.get("STT_PRIVATE_KEY_ID", "")
        stt_private_key      = os.environ.get("STT_PRIVATE_KEY", "").replace("\\n", "\n")
        stt_client_email     = os.environ.get("STT_CLIENT_EMAIL", "")
        stt_client_id        = os.environ.get("STT_CLIENT_ID", "")
        stt_token_uri        = os.environ.get("STT_TOKEN_URI", "https://oauth2.googleapis.com/token")
        stt_auth_uri         = os.environ.get("STT_AUTH_URI", "https://accounts.google.com/o/oauth2/auth")
        stt_auth_provider_x5 = os.environ.get("STT_AUTH_PROVIDER_X509_CERT_URL", "https://www.googleapis.com/oauth2/v1/certs")
        stt_client_x5        = os.environ.get("STT_CLIENT_X509_CERT_URL", "")
        stt_type             = os.environ.get("STT_TYPE", "service_account")

        if not stt_project_id or not stt_private_key:
            raise RuntimeError("[GoogleSTTStreamer] No se definieron variables de entorno STT_... para credenciales de Google STT")

        # 2. Construir diccionario con la info de credenciales
        credentials_info = {
            "type": stt_type,
            "project_id": stt_project_id,
            "private_key_id": stt_private_key_id,
            "private_key": stt_private_key,
            "client_email": stt_client_email,
            "client_id": stt_client_id,
            "auth_uri": stt_auth_uri,
            "token_uri": stt_token_uri,
            "auth_provider_x509_cert_url": stt_auth_provider_x5,
            "client_x509_cert_url": stt_client_x5,
        }

        # 3. Crear objeto credentials
        credentials = Credentials.from_service_account_info(credentials_info)
        logger.info("[GoogleSTTStreamer] Credenciales cargadas correctamente desde variables de entorno.")

        # 4. Inicializar SpeechClient con dichas credenciales
        self.client = speech.SpeechClient(credentials=credentials)

        # 5. Config de streaming
        self.streaming_config = speech.StreamingRecognitionConfig(
            config=speech.RecognitionConfig(
                encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
                sample_rate_hertz=8000,       # mu-law 8k -> PCM16 8k
                language_code="es-MX",
            ),
            interim_results=True,
            single_utterance=False
        )

        # 6. Cola de audio LINEAR16 (8k) ya convertido
        self.audio_queue = asyncio.Queue()
        self.closed = False

        # 7. Buffer para agrupar ~100ms
        #    8000 samples/s * 2 bytes = 16000 bytes/s
        #    ~100ms => 1600 bytes
        self.REQUIRED_BYTES_100MS = 1600
        self._buffer = bytearray()

        logger.info("[GoogleSTTStreamer] Instanciado con config 8k LINEAR16.")


    async def add_audio_chunk(self, mulaw_data: bytes):
        """
        Convierte mu-law -> PCM16 (8k), guarda en debug y encola.
        """
        try:
            pcm16 = audioop.ulaw2lin(mulaw_data, 2)
            if not pcm16:
                logger.warning("[GoogleSTTStreamer] Chunk PCM16 vacío; se omite.")
                return

            # Guarda en disco (opcional), para debug
            await self._save_converted_debug(pcm16)

            # Encola para _request_generator
            await self.audio_queue.put(pcm16)

        except Exception as e:
            logger.error(f"[GoogleSTTStreamer] Error en add_audio_chunk: {e}", exc_info=True)


    async def _save_converted_debug(self, pcm16: bytes):
        """
        Guarda audio PCM16 en 'audio_debug/converted_8k.raw' (append).
        """
        debug_path = "audio_debug/converted_8k.raw"
        try:
            os.makedirs(os.path.dirname(debug_path), exist_ok=True)
            with open(debug_path, "ab") as f:
                f.write(pcm16)
            logger.debug(f"[GoogleSTTStreamer] 🎵 Guardado chunk PCM16 ({len(pcm16)} bytes) en {debug_path}")
        except Exception as e:
            logger.error(f"[GoogleSTTStreamer] Error guardando {debug_path}: {e}")


    async def recognize_stream(self):
        """
        Bucle principal: Llama streaming_recognize con un generador (síncrono).
        Convierte la iteración bloqueante en asíncrona con run_in_executor.
        """
        logger.info("[GoogleSTTStreamer] Iniciando streaming_recognize...")

        try:
            requests_gen = self._request_generator()
            responses = self.client.streaming_recognize(
                config=self.streaming_config,
                requests=requests_gen
            )
            await self._consume_responses_blocking(responses)

        except Exception as e:
            logger.error(f"[GoogleSTTStreamer] ❌ Error en streaming con Google STT: {e}")
        finally:
            self.closed = True
            logger.debug("[GoogleSTTStreamer] Finalizó recognize_stream()")


    async def _consume_responses_blocking(self, responses):
        """
        Convierte la iteración de 'responses' en asíncrona vía run_in_executor
        y luego procesa los resultados finales.
        """
        loop = asyncio.get_running_loop()

        def sync_generator():
            # 'responses' es un generador que se consume de manera bloqueante
            return list(responses)

        future = loop.run_in_executor(None, sync_generator)
        results_list = await future

        # Procesar la lista final
        for resp in results_list:
            for result in resp.results:
                if result.is_final:
                    logger.info(f"[GoogleSTTStreamer] >> Final: {result.alternatives[0].transcript}")
                else:
                    logger.debug(f"[GoogleSTTStreamer] >> Parcial: {result.alternatives[0].transcript}")


    def _request_generator(self):
        """
        Generador SÍNCRONO que consume self.audio_queue con run_coroutine_threadsafe,
        agrupa ~100ms en self._buffer, y emite un speech.StreamingRecognizeRequest
        cuando se superan 1600 bytes (o al timeout).
        """
        while not self.closed:
            try:
                # Intentar leer la cola
                chunk = asyncio.run_coroutine_threadsafe(
                    self.audio_queue.get(),
                    asyncio.get_event_loop()
                ).result(timeout=0.05)  # Ajusta a 0.05 para menor latencia

                if chunk:
                    self._buffer.extend(chunk)

                # Emitir cada 100ms
                while len(self._buffer) >= self.REQUIRED_BYTES_100MS:
                    send_chunk = self._buffer[:self.REQUIRED_BYTES_100MS]
                    self._buffer = self._buffer[self.REQUIRED_BYTES_100MS:]
                    yield speech.StreamingRecognizeRequest(audio_content=bytes(send_chunk))

            except asyncio.TimeoutError:
                # Enviar buffer parcial si existe
                if self._buffer:
                    yield speech.StreamingRecognizeRequest(audio_content=bytes(self._buffer))
                    self._buffer.clear()

            except Exception as e:
                logger.debug(f"[GoogleSTTStreamer] _request_generator error: {e}")
                break

        # Al cerrar, si queda algo en el buffer, se envía
        if self._buffer:
            yield speech.StreamingRecognizeRequest(audio_content=bytes(self._buffer))
            self._buffer.clear()


    async def close(self):
        """
        Marca cerrado y vacía la cola.
        """
        logger.info("[GoogleSTTStreamer] 🛑 Cerrando GoogleSTTStreamer. Vaciando cola de audio.")
        self.closed = True
        while not self.audio_queue.empty():
            _ = await self.audio_queue.get()
            self.audio_queue.task_done()
        logger.info("[GoogleSTTStreamer] ✅ GoogleSTTStreamer cerrado.")
