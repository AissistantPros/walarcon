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
        self._is_reconnecting = False   # Flag para indicar un proceso de reconexión automática en curso

        # Configuración de reconexión
        self.max_reconnect_attempts = 3  # Número máximo de intentos de reconexión
        self.reconnect_delay = 1.5       # Segundos entre intentos (puedes ajustar esto)

    async def start_streaming(self, is_reconnect_attempt=False):
        """
        Inicia o reinicia la conexión con Deepgram.
        """
        if not self.deepgram:
            logger.error("No se puede iniciar streaming: Cliente Deepgram no inicializado (falta API Key o error previo).")
            self._started = False
            return

        if self._started and not is_reconnect_attempt:
            logger.info("Deepgram ya estaba iniciado y conectado.")
            return
        if self._is_closing:
            logger.warning("Intento de iniciar streaming mientras se está cerrando. Abortando.")
            return
        if self._is_reconnecting and not is_reconnect_attempt:
             logger.warning("Intento de iniciar streaming mientras ya se está reconectando. Abortando.")
             return

        # Si no es un intento de reconexión explícito desde attempt_reconnect, marcar que estamos intentando conectar/reconectar.
        # Si es un intento desde attempt_reconnect, _is_reconnecting ya será True.
        if not is_reconnect_attempt:
            self._is_reconnecting = True # Asumimos que cualquier start es un intento de (re)conectar

        action_verb = "RECONECTAR" if is_reconnect_attempt or self._is_reconnecting else "CONECTAR"
        logger.info(f"Intentando {action_verb} con Deepgram...")

        try:
            # Si hay una conexión antigua, intentar cerrarla limpiamente antes de crear una nueva.
            # Esto es más una medida defensiva.
            if self.dg_connection:
                logger.debug("Limpiando conexión dg_connection existente antes de (re)iniciar.")
                try:
                    # No enviar CloseStream aquí, solo finalizarla.
                    await asyncio.wait_for(self.dg_connection.finish(), timeout=0.5)
                except Exception:
                    pass # Ignorar errores al limpiar la vieja conexión
                self.dg_connection = None
            
            self._started = False # Asegurar que _started es False antes de intentar conectar

            self.dg_connection = self.deepgram.listen.asynclive.v("1")
            self.dg_connection.on(LiveTranscriptionEvents.Open, self._on_open)
            self.dg_connection.on(LiveTranscriptionEvents.Transcript, self._on_transcript)
            self.dg_connection.on(LiveTranscriptionEvents.Close, self._on_close)
            self.dg_connection.on(LiveTranscriptionEvents.Error, self._on_error)
            self.dg_connection.on(LiveTranscriptionEvents.Unhandled, self._on_unhandled) # Para depurar eventos inesperados
            self.dg_connection.on(LiveTranscriptionEvents.Metadata, self._on_metadata) # Opcional, para ver metadatos

            options = LiveOptions(
                model="nova-2",
                language="es",
                encoding="mulaw",
                sample_rate=8000,
                channels=1,
                smart_format=True,
                interim_results=True, 
                endpointing=False, 
                utterance_end_ms="1000", 
                vad_events=False, 
               
            )
            
            ##logger.debug(f"Opciones de Deepgram para start: {options}")
            await self.dg_connection.start(options)
            # El callback _on_open será el encargado de poner self._started = True
            # y self._is_reconnecting = False al tener éxito.

        except Exception as e:
            logger.error(f"❌ Error CRÍTICO durante start_streaming de Deepgram: {e}", exc_info=True)
            self._started = False
            self.dg_connection = None # Asegurar limpieza
            if not is_reconnect_attempt: # Si el intento inicial (no un reintento) falla
                self._is_reconnecting = False
            # Si esto fue llamado desde attempt_reconnect, ese bucle continuará o fallará.

    async def attempt_reconnect(self):
        """Intenta reconectar a Deepgram con múltiples intentos."""
        if not self.deepgram:
            logger.error("No se puede reconectar: Cliente Deepgram no inicializado.")
            return

        if self._is_reconnecting and asyncio.current_task().get_name() != "ReconnectTask": # Evitar reentrada si ya hay una tarea de reconexión
            logger.info("Intento de reconexión ya en progreso.")
            return
        
        # Crear una tarea nombrada para la reconexión si no existe ya una con ese nombre
        # Esto es una forma simple de evitar múltiples tareas de reconexión concurrentes
        # No es una solución perfecta para concurrencia, pero ayuda.
        current_tasks = [t.get_name() for t in asyncio.all_tasks()]
        if "ReconnectTask" in current_tasks and asyncio.current_task().get_name() != "ReconnectTask":
            logger.info("Tarea de reconexión 'ReconnectTask' ya existe y está activa.")
            return

        # Nombrar la tarea actual si es la primera vez que entra aquí
        if asyncio.current_task().get_name() != "ReconnectTask":
             asyncio.current_task().set_name("ReconnectTask")


        logger.info("Iniciando proceso de reconexión automática con Deepgram...")
        self._is_reconnecting = True
        self._started = False # Marcar como no iniciado mientras se reconecta

        for attempt in range(1, self.max_reconnect_attempts + 1):
            if self._started: # Si en algún momento se conecta (ej. por otra vía)
                logger.info("Reconexión ya no necesaria, conexión establecida.")
                self._is_reconnecting = False
                return

            logger.info(f"Intento de reconexión a Deepgram #{attempt}/{self.max_reconnect_attempts}...")
            await self.start_streaming(is_reconnect_attempt=True)
            
            if self._started: # _on_open debería haber puesto esto a True
                logger.info("✅ Reconexión a Deepgram EXITOSA en intento #{attempt}.")
                # _is_reconnecting se pondrá a False en _on_open
                return 
            
            if attempt < self.max_reconnect_attempts:
                logger.info(f"Reconexión fallida. Esperando {self.reconnect_delay}s para el siguiente intento.")
                await asyncio.sleep(self.reconnect_delay)
            else:
                logger.error(f"Todos los {self.max_reconnect_attempts} intentos de reconexión a Deepgram fallaron.")
        
        self._is_reconnecting = False # Resetear después de todos los intentos, exitosos o no
        logger.info("Proceso de reconexión automática finalizado (pudo o no tener éxito).")


    async def send_audio(self, chunk: bytes):
        """Envía un chunk de audio a Deepgram."""
        if self.dg_connection and self._started and not self._is_closing and not self._is_reconnecting:
            try:
                # logger.debug(f"Enviando chunk de audio a Deepgram ({len(chunk)} bytes)")
                await self.dg_connection.send(chunk)
            except Exception as e:
                logger.error(f"❌ Error enviando audio a Deepgram: {e}. Estado: _started={self._started}, _is_closing={self._is_closing}, _is_reconnecting={self._is_reconnecting}")
                self._started = False # Asumir que la conexión ya no es válida
                if not self._is_reconnecting: # Solo intentar reconectar si no estamos ya en ello
                    asyncio.create_task(self.attempt_reconnect(), name="ReconnectTask_SendAudioError")
        elif self._is_closing:
            logger.warning("⚠️ Audio ignorado por STT: conexión en proceso de cierre.")
        elif self._is_reconnecting:
            logger.warning("⚠️ Audio ignorado por STT: conexión en proceso de reconexión.")
        else:
            state_info = f"_started={self._started}, _is_closing={self._is_closing}, _is_reconnecting={self._is_reconnecting}, dg_connection_exists={self.dg_connection is not None}"
            logger.warning(f"⚠️ Audio ignorado por STT: conexión no iniciada o no operativa. Estado: {state_info}")

    async def send_keep_alive(self):
        """Envía un mensaje KeepAlive a Deepgram."""
        if not self.deepgram: return False # No intentar si el cliente no está inicializado

        if self.dg_connection and self._started and not self._is_closing and not self._is_reconnecting:
            try:
                ##logger.info("Intentando enviar KeepAlive a Deepgram...")
                await self.dg_connection.send(json.dumps({"type": "KeepAlive"}))
                logger.info("🔵 KeepAlive enviado a Deepgram.")
                return True
            except Exception as e:
                logger.warning(f"⚠️ Error enviando KeepAlive a Deepgram: {e}")
                self._started = False # Asumir que la conexión ya no es válida tras este error
                if not self._is_reconnecting:
                     asyncio.create_task(self.attempt_reconnect(), name="ReconnectTask_KeepAliveError")
                return False
        else:
            state_info = f"_started={self._started}, _is_closing={self._is_closing}, _is_reconnecting={self._is_reconnecting}, dg_connection_exists={self.dg_connection is not None}"
            logger.debug(f"No se envió KeepAlive. Estado: {state_info}")
            return False

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
        
        # Cancelar cualquier tarea de reconexión pendiente si estamos cerrando manualmente
        if self._is_reconnecting:
            logger.info("Cancelando tarea de reconexión debido a cierre manual.")
            # Esta es una forma simple de señalar, la tarea debería chequear self._is_closing
            # Idealmente, las tareas de reconexión se cancelarían explícitamente si tienes sus handles.
            self._is_reconnecting = False 

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
            self._is_reconnecting = False # Asegurar que no quede en estado de reconexión
            logger.info("🧹 Estado de DeepgramSTTStreamer limpiado después del cierre.")

    # --- Callbacks de Eventos de Deepgram ---
    async def _on_open(self, _connection, *args, **kwargs): # _connection es dg_connection
        logger.info("🔛 Conexión Deepgram ABIERTA (evento Open recibido).")
        self._started = True
        self._is_closing = False
        self._is_reconnecting = False # Si se abrió, ya no estamos reconectando

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
        
        # Intentar reconectar solo si:
        # 1. La conexión ESTABA iniciada (para no reconectar si nunca se estableció bien)
        # 2. NO estamos ya en un proceso de reconexión (para evitar bucles)
        # 3. NO fue un cierre iniciado por nosotros mismos (aunque _is_closing ya debería estar False)
        if was_started_before_event and not self._is_reconnecting:
            logger.info("Cierre inesperado de Deepgram detectado por evento _on_close. Intentando reconectar...")
            # Lanzar como tarea para no bloquear el callback de la librería de Deepgram
            asyncio.create_task(self.attempt_reconnect(), name="ReconnectTask_OnClose")
        elif self._is_reconnecting:
             logger.info("_on_close recibido mientras ya se estaba reconectando. No se iniciará nueva reconexión.")


    async def _on_error(self, _connection, error, *args, **kwargs): # Evento de Deepgram
        logger.error(f"💥 Error en conexión Deepgram (evento Error recibido): {error}")
        was_started_before_event = self._started

        self.dg_connection = None
        self._started = False
        self._is_closing = False

        if was_started_before_event and not self._is_reconnecting:
            logger.info("Error de Deepgram detectado por evento _on_error. Intentando reconectar...")
            asyncio.create_task(self.attempt_reconnect(), name="ReconnectTask_OnError")
        elif self._is_reconnecting:
            logger.info("_on_error recibido mientras ya se estaba reconectando. No se iniciará nueva reconexión.")
            
    async def _on_unhandled(self, _connection, event_data, *args, **kwargs):
        logger.warning(f"Evento Deepgram NO MANEJADO recibido: {event_data}")

    #async def _on_metadata(self, _connection, metadata, *args, **kwargs):
        ##logger.debug(f"Metadatos de Deepgram recibidos: {metadata}")