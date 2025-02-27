# google_stt_streamer.py
import asyncio
import audioop # type: ignore
from google.cloud import speech

class GoogleSTTStreamer:
    """
    Maneja la conexión en streaming con Google Speech-to-Text
    usando el modelo "medical_conversation" (u otro).
    
    - Se encarga de abrir la conexión gRPC con Google.
    - Recibe audio chunk a chunk (PCM16 8kHz).
    - Emite partial/final results cuando Google detecta pausas.
    """

    def __init__(self):
        self.client = speech.SpeechClient()
        self.closed = False

        # Configuración del reconocimiento
        self.config = speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=8000,
            language_code="es-MX",
            model="medical_conversation",  # O "default" si no quieres modo médico
            enable_automatic_punctuation=True
        )

        self.streaming_config = speech.StreamingRecognitionConfig(
            config=self.config,
            interim_results=True  # Recibir resultados parciales
        )

        # Cola asíncrona donde meteremos chunks de audio
        self.audio_queue = asyncio.Queue()

    async def recognize_stream(self):
        """
        Ciclo principal: abre la conexión, envía audio, recibe resultados.
        Se recomienda correrlo en una tarea asíncrona y 'escuchar' lo que devuelva.

        Uso:
          async for result in self.recognize_stream():
              # result = speech.StreamingRecognitionResult
              # if result.is_final: ...
        """

        # Generador que envía datos de la cola a Google
        async def request_generator():
            while not self.closed:
                chunk = await self.audio_queue.get()
                if chunk is None:
                    # Señal de cierre
                    break
                yield speech.StreamingRecognizeRequest(audio_content=chunk)

        # Iniciamos la llamada a la API
        requests = request_generator()
        responses = self.client.streaming_recognize(
            config=self.streaming_config,
            requests=requests
        )

        # Procesar las respuestas de forma asíncrona
        async for response in self._handle_responses(responses):
            # yield each 'result' al consumidor
            yield response

        self.closed = True

    async def _handle_responses(self, responses_generator):
        """
        Convierte el generador de responses en un generador asíncrono.
        Cada 'response' puede tener varios 'results'.
        """
        loop = asyncio.get_event_loop()

        def sync_generator():
            # responses_generator es sincrónico en este punto
            for r in responses_generator:
                yield r

        async_gen = loop.run_in_executor(None, lambda: sync_generator())

        async for resp in async_gen:
            for result in resp.results:
                # result => StreamingRecognitionResult
                yield result

    def add_audio_chunk(self, mulaw_data: bytes):
        """
        Convierte mu-law a PCM16 8kHz y lo mete en la cola asíncrona
        para enviarlo a Google en streaming.
        """
        # Convierte mu-law a PCM16 (2 bytes/16bits)
        pcm16 = audioop.ulaw2lin(mulaw_data, 2)
        # Enviarlo a la cola asíncrona
        asyncio.create_task(self.audio_queue.put(pcm16))

    async def close(self):
        """
        Cierra el stream. Pone None en la cola para avisar que no hay más datos.
        """
        self.closed = True
        await self.audio_queue.put(None)
