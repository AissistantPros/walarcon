# deepgram_stt_streamer.py
import os
import json
import asyncio
import logging
import warnings
from deepgram import DeepgramClient, LiveTranscriptionEvents, LiveOptions
# from fastapi.websockets import WebSocketState # No se usa directamente aquí

logger = logging.getLogger("deepgram_stt_streamer")
# logger.setLevel(logging.INFO) # Puedes ajustar el nivel de log como necesites

# Silenciar logs molestos del WebSocket de Deepgram al cancelar tareas
# logging.getLogger("deepgram.clients.common.v1.abstract_async_websocket").setLevel(logging.ERROR)
# warnings.filterwarnings("ignore", message="coroutine .* was never awaited")
# warnings.filterwarnings("ignore", category=RuntimeWarning)

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
        if DEEPGRAM_KEY: # Solo inicializar cliente si la KEY existe
            try:
                self.deepgram = DeepgramClient(DEEPGRAM_KEY)
            except Exception as e:
                logger.error(f"FALLO AL INICIALIZAR DeepgramClient: {e}")
                self.deepgram = None # Asegurar que es None si falla
        else:
            logger.error("DeepgramClient no se inicializó porque DEEPGRAM_KEY falta.")

        self.dg_connection = None
        self._started = False           # Indica si la conexión está activa y operativa
        self._is_closing = False        # Flag para indicar un cierre manual en curso

    async def start_streaming(self):
        """
        Inicia la conexión con Deepgram.
        """
        if not self.deepgram:
            logger.error("No se puede iniciar streaming: Cliente Deepgram no inicializado (falta API Key o error previo).")
            self._started = False
            return

        if self._started:
            logger.info("Deepgram ya estaba iniciado y conectado.")
            return
        if self._is_closing:
            logger.warning("Intento de iniciar streaming mientras se está cerrando. Abortando.")
            return
        logger.info(f"Intentando CONECTAR con Deepgram...")

        try:
            if self.dg_connection:
                logger.debug("Limpiando conexión dg_connection existente antes de iniciar.")
                try:
                    await asyncio.wait_for(self.dg_connection.finish(), timeout=0.5)
                except Exception:
                    pass 
                self.dg_connection = None

            self._started = False 

            self.dg_connection = self.deepgram.listen.asynclive.v("1")
            self.dg_connection.on(LiveTranscriptionEvents.Open, self._on_open)
            self.dg_connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
            self.dg_connection.on(LiveTranscriptionEvents.Close, self._on_close)
            self.dg_connection.on(LiveTranscriptionEvents.Error, self._on_error)
            self.dg_connection.on(LiveTranscriptionEvents.Unhandled, self._on_unhandled)
            self.dg_connection.on(LiveTranscriptionEvents.Metadata, self._on_metadata)

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
            # _on_open se encargará de self._started = True

        except Exception as e:
            logger.error(f"❌ Error CRÍTICO durante start_streaming de Deepgram: {e}", exc_info=True)
            self._started = False
            self.dg_connection = None


    async def send_audio(self, chunk: bytes):
        """Envía un chunk de audio a Deepgram."""
        if self.dg_connection and self._started and not self._is_closing: # <--- Quitar chequeo de _is_reconnecting
            try:
                await self.dg_connection.send(chunk)
            except Exception as e:
                logger.error(f"❌ Error enviando audio a Deepgram: {e}. Estado: _started={self._started}, _is_closing={self._is_closing}")
                self._started = False # Asumir que la conexión ya no es válida
                # Ya no se llama a attempt_reconnect aquí
        elif self._is_closing:
            logger.warning("⚠️ Audio ignorado por STT: conexión en proceso de cierre.")
        # Eliminar este bloque else if:
        # elif self._is_reconnecting:
        #     logger.warning("⚠️ Audio ignorado por STT: conexión en proceso de reconexión.")
        else:
            state_info = f"_started={self._started}, _is_closing={self._is_closing}, dg_connection_exists={self.dg_connection is not None}"
            logger.warning(f"⚠️ Audio ignorado por STT: conexión no iniciada o no operativa. Estado: {state_info}")

    async def close(self):
        """Cierra la conexión con Deepgram de forma controlada."""
        if not self.dg_connection or self._is_closing:
            logger.debug(f"Cierre de Deepgram no necesario o ya en progreso (is_closing={self._is_closing}).")
            self._started = False # Asegurar que _started esté False
            self.dg_connection = None # Asegurar que se limpia la conexión
            return

        logger.info("Iniciando cierre de conexión Deepgram...")
        self._is_closing = True # Marcar que hemos iniciado el proceso de cierre
        self._started = False   # Marcar como no iniciado inmediatamente
        

        try:
            # Enviar CloseStream solo si la conexión aún podría estar un poco viva
            # Si Deepgram ya la cerró, esto fallará, por eso el try/except.
            if self.dg_connection: # Chequeo extra
                try:
                    logger.debug("Intentando enviar CloseStream a Deepgram...")
                    await self.dg_connection.send(json.dumps({"type": "CloseStream"}))
                    logger.info("📨 'CloseStream' enviado a Deepgram.")
                    await asyncio.sleep(0.1) # Pequeña pausa para que Deepgram procese
                except Exception as e_cs:
                    logger.warning(f"No se pudo enviar 'CloseStream' (puede que la conexión ya estuviera cerrada): {e_cs}")

            if self.dg_connection: # Chequeo extra
                await asyncio.wait_for(self.dg_connection.finish(), timeout=2.0)
                logger.info("✅ Conexión Deepgram finalizada (método finish() del SDK).")
            
        except asyncio.TimeoutError:
            logger.warning("⏳ Timeout (2s) esperando a que Deepgram SDK termine con finish().")
        except Exception as e:
            logger.error(f"❌ Error durante el proceso de cierre de Deepgram: {e}", exc_info=True)
        finally:
            self.dg_connection = None # Asegurar que se limpia
            self._started = False
            self._is_closing = False # Resetear el flag de cierre
            logger.info("🧹 Estado de DeepgramSTTStreamer limpiado después del cierre.")

    # --- Callbacks de Eventos de Deepgram ---
    async def _on_open(self, _connection, *args, **kwargs): # _connection es dg_connection
        logger.info("🔛 Conexión Deepgram ABIERTA (evento Open recibido).")
        self._started = True
        self._is_closing = False


    async def _on_transcript(self, _connection, result, *args, **kwargs):
        if not result or not hasattr(result, 'channel') or not result.channel.alternatives or not result.channel.alternatives[0].transcript:
            # logger.debug("Recibido resultado de transcripción vacío o malformado.")
            return
            
        transcript = result.channel.alternatives[0].transcript
        if transcript: # Asegurarse que el transcript no sea una cadena vacía
            # logger.debug(f"Transcript recibido: '{transcript}', is_final: {result.is_final}")
            self.callback(transcript, result.is_final)
        # else:
            # logger.debug(f"Transcript vacío recibido. is_final: {result.is_final}")


    async def _on_close(self, _connection, *args, **kwargs): # Evento de Deepgram cuando ELLOS cierran
        logger.warning(f"🔒 Conexión Deepgram CERRADA (evento Close [{args}] [{kwargs}] recibido desde Deepgram).")
        was_started_before_event = self._started # Capturar el estado antes de modificarlo
        
        self.dg_connection = None # La conexión ya no es válida
        self._started = False
        self._is_closing = False # Ya está cerrada, no "cerrándose" desde nuestro lado
        logger.info("Evento _on_close de Deepgram procesado. La conexión está cerrada.")


    async def _on_error(self, _connection, error, *args, **kwargs):
        logger.error(f"💥 Error en conexión Deepgram (evento Error recibido): {error}")

        self.dg_connection = None
        self._started = False
        self._is_closing = False

        logger.info("Evento _on_error de Deepgram procesado. La conexión no es válida.")
            
    async def _on_unhandled(self, _connection, event_data, *args, **kwargs):
        logger.warning(f"Evento Deepgram NO MANEJADO recibido: {event_data}")

    async def _on_metadata(self, _connection, metadata, *args, **kwargs):
        logger.debug(f"Metadatos de Deepgram recibidos: {metadata}")