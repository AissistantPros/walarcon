# deepgram_stt_streamer.py
import os
import json
import asyncio
import logging
import time
from typing import Optional
import warnings
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
# from fastapi.websockets import WebSocketState # No se usa directamente aquí

logger = logging.getLogger("deepgram_stt_streamer")
# logger.setLevel(logging.INFO) # Puedes ajustar el nivel de log como necesites

# Silenciar logs molestos del WebSocket de Deepgram al cancelar tareas
# logging.getLogger("deepgram.clients.common.v1.abstract_async_websocket").setLevel(logging.ERROR)
# warnings.filterwarnings("ignore", message="coroutine .* was never awaited")
# warnings.filterwarnings("ignore", category=RuntimeWarning)

# —— mantener viva la sesión STT ——
KEEPALIVE_INTERVAL_SEC = 10         # envía ping cada 10 s
WATCHDOG_TIMEOUT_SEC   = 35         # si en 35 s no llega pong ⇒ forzar reconexión

DEEPGRAM_KEY = os.getenv("DEEPGRAM_KEY")
if not DEEPGRAM_KEY:
    # Considera un logger.critical o elevar el error de forma más directa
    # si el flujo no puede continuar sin DEEPGRAM_KEY
    logger.error("❌ Variable DEEPGRAM_KEY no encontrada. DeepgramSTTStreamer no funcionará.")
    # raise ValueError("❌ Variable DEEPGRAM_KEY no encontrada") # Descomentar si quieres que falle fuerte


class DeepgramSTTStreamer:
    def __init__(self, callback):
        """
        callback: función que recibe transcript (str) e is_final (bool)
        """
        self.callback = callback
        self.deepgram = None
        self.dg_connection = None
        self._started = False
        self._is_closing = False
        self._is_reconnecting = False
        self._last_pong: float = time.time()
        self._keepalive_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._closed = asyncio.Event()

        # Configuración de reconexión
        self.max_reconnect_attempts = 3
        self.reconnect_delay = 1.5

        # Inicialización del cliente de Deepgram
        if DEEPGRAM_KEY:
            try:
                self.deepgram = DeepgramClient(DEEPGRAM_KEY)
            except Exception as e:
                logger.error(f"FALLO AL INICIALIZAR DeepgramClient: {e}")
                self.deepgram = None
        else:
            logger.error("DeepgramClient no se inicializó porque DEEPGRAM_KEY falta.")

    # —— CONEXIÓN ——
    async def start_streaming(self, is_reconnect_attempt=False):
        if not self.deepgram:
            logger.error("No se puede iniciar streaming: Cliente Deepgram no inicializado.")
            return

        if self._started:
            logger.info("Deepgram ya estaba iniciado y conectado.")
            return

        try:
            if self.dg_connection:
                await self.dg_connection.finish()

            self._is_closing = False
            self._is_reconnecting = is_reconnect_attempt

            self.dg_connection = self.deepgram.listen.asynclive.v("1")
            self.dg_connection.on(LiveTranscriptionEvents.Open, self._on_open)
            self.dg_connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
            self.dg_connection.on(LiveTranscriptionEvents.Close, self._on_close)
            self.dg_connection.on(LiveTranscriptionEvents.Error, self._on_error)

            options = LiveOptions(
                model="nova-2",
                language="es",
                encoding="mulaw",
                sample_rate=8000,
                channels=1,
                smart_format=True,
                interim_results=True,
                endpointing=False,
                utterance_end_ms="1200",
                vad_events=False,
            )
            
            await self.dg_connection.start(options)

            # Tareas asíncronas para mantener la conexión
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

        except Exception as e:
            logger.error(f"❌ Error al iniciar streaming con Deepgram: {e}")

    # —— MANTENIMIENTO DE CONEXIÓN ——
    async def _keepalive_loop(self):
        try:
            while not self._closed.is_set():
                await asyncio.sleep(KEEPALIVE_INTERVAL_SEC)
                if self.dg_connection and not self._is_closing:
                    await self.dg_connection.send(json.dumps({"type": "KeepAlive"}))
                    logger.info("🔵 KeepAlive enviado a Deepgram.")
        except asyncio.CancelledError:
            pass

    async def _watchdog_loop(self):
        try:
            while not self._closed.is_set():
                await asyncio.sleep(1)
                if time.time() - self._last_pong > WATCHDOG_TIMEOUT_SEC:
                    logger.warning("⚠️ Deepgram inactivo, iniciando reconexión...")
                    await self.attempt_reconnect()
        except asyncio.CancelledError:
            pass

    async def attempt_reconnect(self):
        """Intenta reconectar a Deepgram automáticamente."""
        if self._is_reconnecting:
            return
        self._is_reconnecting = True

        for attempt in range(1, self.max_reconnect_attempts + 1):
            logger.info(f"Intentando reconexión #{attempt} a Deepgram...")
            await self.start_streaming(is_reconnect_attempt=True)
            if self._started:
                logger.info(f"✅ Reconexión exitosa en intento #{attempt}")
                return
            await asyncio.sleep(self.reconnect_delay)

        logger.error("❌ No se pudo reconectar a Deepgram después de varios intentos.")

    # —— MANEJO DE EVENTOS ——
    async def _on_open(self, _connection, *args, **kwargs):
        logger.info("🔛 Conexión Deepgram abierta.")
        self._started = True
        self._last_pong = time.time()

    async def _on_transcript(self, _connection, result, *args, **kwargs):
        if result and result.channel.alternatives:
            transcript = result.channel.alternatives[0].transcript
            is_final = result.is_final
            if transcript:
                self.callback(transcript, is_final)

    async def _on_close(self, _connection, *args, **kwargs):
        logger.warning("🔒 Deepgram cerró la conexión inesperadamente.")
        self._started = False
        await self.attempt_reconnect()

    async def _on_error(self, _connection, error, *args, **kwargs):
        logger.error(f"💥 Error en Deepgram: {error}")
        self._started = False
        await self.attempt_reconnect()

    async def send_audio(self, chunk: bytes):
        """Envía un fragmento de audio a Deepgram."""
        if self.dg_connection and self._started:
            try:
                await self.dg_connection.send(chunk)
            except Exception as e:
                logger.error(f"❌ Error enviando audio a Deepgram: {e}")
                await self.attempt_reconnect()

    async def close(self):
        """Cierra la conexión con Deepgram de manera controlada."""
        if self.dg_connection:
            await self.dg_connection.finish()
        self._started = False
        self._closed.set()
        logger.info("🔴 Conexión Deepgram cerrada correctamente.")
