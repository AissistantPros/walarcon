# tw_utils.py
"""
WebSocket manager para Twilio <-> Deepgram <-> GPT
----------------------------------------------------------------
Maneja la lógica de acumulación de transcripciones, interacción con GPT,
TTS, y el control del flujo de la llamada, incluyendo la gestión de timeouts
y la prevención de procesamiento de STT obsoleto.ƒ
CON LOGGING DETALLADO DE TIEMPOS.
"""

import asyncio
import base64
import json
import logging
import time
import os
from datetime import datetime # <--- Añadido para timestamps detallados
from typing import Optional, List 
from decouple import config
from fastapi import WebSocket
from starlette.websockets import WebSocketState

# Tus importaciones de módulos locales
try:
    from aiagent import generate_openai_response_main 
    from buscarslot import load_free_slots_to_cache 
    from consultarinfo import load_consultorio_data_to_cache 
    from deepgram_stt_streamer import DeepgramSTTStreamer 
    from prompt import generate_openai_prompt 
    from tts_utils import text_to_speech 
    from utils import get_cancun_time 
except ImportError as e:
    logging.exception(f"CRÍTICO: Error importando módulos locales: {e}.")
    raise SystemExit(f"No se pudieron importar módulos necesarios: {e}")

# --- Configuración de Logging ---
logger = logging.getLogger("tw_utils") 
logger.setLevel(logging.DEBUG) # Asegúrate que esté en DEBUG para ver los nuevos logs

# --- Formato para Timestamps ---
LOG_TS_FORMAT = "%H:%M:%S.%f" 

# --- Constantes Configurables para Tiempos (en segundos) ---
PAUSA_SIN_ACTIVIDAD_TIMEOUT = .4
MAX_TIMEOUT_SIN_ACTIVIDAD = 5.0
LATENCY_THRESHOLD_FOR_HOLD_MESSAGE = 4.5 # Umbral para mensaje de espera
HOLD_MESSAGE_FILE = "audio/espera_1.wav" # Asegúrate que esta sea la ruta correcta a tu archivo mu-law
SILENCE_FRAME = b'\x00' * 160          # 20 ms de μ-law @ 8 kHz
SILENCE_PERIOD = 5.0                  # cada 5 seg envía un paquete

# --- Otras Constantes Globales ---
CURRENT_CALL_MANAGER: Optional["TwilioWebSocketManager"] = None
CALL_MAX_DURATION = 600 
CALL_SILENCE_TIMEOUT = 30 
GOODBYE_PHRASE = "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"
TEST_MODE_NO_GPT = False # <--- Poner en True para pruebas sin GPT

# --------------------------------------------------------------------------

class TwilioWebSocketManager:
    def __init__(self) -> None:
        """Inicializa el estado para una nueva llamada."""
        ts = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.debug(f"⏱️ TS:[{ts}] INIT START")
        self.websocket: Optional[WebSocket] = None
        self.stt_streamer: Optional[DeepgramSTTStreamer] = None
        self.current_gpt_task: Optional[asyncio.Task] = None
        self.temporizador_pausa: Optional[asyncio.Task] = None 

        self.call_sid: str = "" 
        self.stream_sid: Optional[str] = None 
        self.call_ended: bool = False
        self.deepgram_closed = False
        self.ws_closed = False
        self.tasks_cancelled = False

        self.shutdown_reason: str = "N/A" 
        
        self.is_speaking: bool = False 
        self.ignorar_stt: bool = False 
        self.ultimo_evento_fue_parcial: bool = False 

        now = self._now()
        ts_now_str = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        self.stream_start_time: float = now
        self.last_activity_ts: float = now 
        self.last_final_ts: float = now 
        self.last_final_stt_timestamp: Optional[float] = None # Para medir latencia real
        logger.debug(f"⏱️ TS:[{ts_now_str}] INIT Timestamps set: start={self.stream_start_time:.2f}, activity={self.last_activity_ts:.2f}, final={self.last_final_ts:.2f}")

        self.finales_acumulados: List[str] = []
        self.conversation_history: List[dict] = []
        self.speaking_lock = asyncio.Lock() 
        

        self.audio_buffer_twilio: List[bytes] = []       # Buffer para audio de Twilio
        self.close_dg_task: Optional[asyncio.Task] = None   # Tarea para el cierre de Deepgram antes del TTS

        self.hold_audio_mulaw_bytes: bytes = b""
        try:
            if os.path.exists(HOLD_MESSAGE_FILE):
                with open(HOLD_MESSAGE_FILE, 'rb') as f:
                    self.hold_audio_mulaw_bytes = f.read()
                if self.hold_audio_mulaw_bytes:
                    logger.info(f"Successfully loaded hold message '{HOLD_MESSAGE_FILE}' ({len(self.hold_audio_mulaw_bytes)} bytes).")
                else:
                    logger.warning(f"Hold message file '{HOLD_MESSAGE_FILE}' is empty.")
            else:
                logger.error(f"Hold message file not found at: {HOLD_MESSAGE_FILE}")
        except Exception as e:
            logger.error(f"Failed to load hold message file '{HOLD_MESSAGE_FILE}': {e}", exc_info=True)

        if not self.hold_audio_mulaw_bytes:
             logger.warning("Hold message feature will be disabled.")
        logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] INIT END (ID: {id(self)})")


    def _now(self) -> float:
        """Devuelve el timestamp actual de alta precisión."""
        return time.perf_counter()

    def _reset_state_for_new_call(self):
        """Resetea variables de estado al inicio de una llamada."""
        ts = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.debug(f"⏱️ TS:[{ts}] RESET_STATE START")
        # Cancelar tareas si quedaron de una llamada anterior
        if self.temporizador_pausa and not self.temporizador_pausa.done():
            self.temporizador_pausa.cancel()
            logger.debug("   RESET_STATE: Temporizador pausa cancelado.")
        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()
            logger.debug("   RESET_STATE: Tarea GPT cancelada.")
            
        self.current_gpt_task = None
        self.temporizador_pausa = None
        self.call_ended = False
        self.shutdown_reason = "N/A"
        self.is_speaking = False
        self.ignorar_stt = False
        self.ultimo_evento_fue_parcial = False
        now = self._now()
        # stream_start_time se mantiene desde __init__
        self.last_activity_ts = now
        self.last_final_ts = now
        self.finales_acumulados = []
        self.conversation_history = []
        logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] RESET_STATE END")

    # --- Manejador Principal del WebSocket ---

    async def handle_twilio_websocket(self, websocket: WebSocket):
        """Punto de entrada y bucle principal."""
        ts_start_handle = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.info(f"⏱️ TS:[{ts_start_handle}] HANDLE_WS START")
        self.websocket = websocket
        try:
            await websocket.accept()
            ts_accept = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.info(f"⏱️ TS:[{ts_accept}] HANDLE_WS WebSocket accepted.")
        except Exception as e_accept:
             logger.error(f"❌ Fallo al aceptar WebSocket: {e_accept}")
             return 

        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = self 
        
        self._reset_state_for_new_call() 

        # --- Precarga de Datos ---
        ts_preload_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.debug(f"⏱️ TS:[{ts_preload_start}] HANDLE_WS Preload Start")
        try:
            preload_start_pc = self._now()
            await asyncio.gather(
                asyncio.to_thread(load_free_slots_to_cache, 90),
                asyncio.to_thread(load_consultorio_data_to_cache)
            )
            preload_duration = (self._now() - preload_start_pc) * 1000
            logger.info(f"✅ Precarga de datos completada. ⏱️ DUR:[{preload_duration:.1f}ms]")
        except Exception as e_preload:
            logger.warning(f"⚠️ Precarga de datos falló: {e_preload}", exc_info=False) 


        # --- Iniciar Deepgram ---
        ts_dg_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.debug(f"⏱️ TS:[{ts_dg_start}] HANDLE_WS Deepgram Init Start")
        monitor_task = None # Definir monitor_task aquí para que exista en el scope del finally
        try:
            dg_start_pc = self._now()
            if not self.stt_streamer: # Crear instancia si no existe (útil si el manager se reutilizara)
                 self.stt_streamer = DeepgramSTTStreamer(
                     callback=self._stt_callback,
                     on_disconnect_callback=self._reconnect_deepgram_if_needed
                 )
            
            await self.stt_streamer.start_streaming() # Intenta iniciar la conexión
            dg_duration = (self._now() - dg_start_pc) * 1000
            
            if self.stt_streamer._started: # Verificar si realmente se inició
                logger.info(f"✅ Deepgram STT iniciado. ⏱️ DUR:[{dg_duration:.1f}ms]")
            else: 
                logger.critical(f"❌ CRÍTICO: Deepgram STT NO PUDO INICIARSE después del intento. ⏱️ DUR:[{dg_duration:.1f}ms]")
                await self._shutdown(reason="Deepgram Initial Connection Failed")
                return # Salir de handle_twilio_websocket si Deepgram no inicia
        except Exception as e_dg_start:
            logger.critical(f"❌ CRÍTICO: Excepción al intentar iniciar Deepgram: {e_dg_start}", exc_info=True)
            # self._safe_close_websocket ya no es necesario aquí si _shutdown maneja el cierre del websocket de Twilio
            await self._shutdown(reason="STT Initialization Exception") # _shutdown debería manejar la limpieza
            # CURRENT_CALL_MANAGER = None # _shutdown o el finally de handle_twilio_websocket deberían manejar esto
            return
       # --- Tarea de Monitoreo ---
        monitor_task = asyncio.create_task(self._monitor_call_timeout(), name=f"MonitorTask_{self.call_sid or id(self)}")
        logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] HANDLE_WS Monitor task created.")

        # --- Bucle Principal de Recepción ---
        logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] HANDLE_WS Entering main receive loop...")
        try:
            while not self.call_ended:
                ts_loop_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                # logger.debug(f"⏱️ TS:[{ts_loop_start}] HANDLE_WS Waiting for message...")
                try:
                    raw = await websocket.receive_text()
                    data = json.loads(raw)
                    ts_msg_received = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                    # logger.debug(f"⏱️ TS:[{ts_msg_received}] HANDLE_WS Message received.")
                except Exception as e_receive:
                    if "1000" in str(e_receive) or "1001" in str(e_receive) or "1006" in str(e_receive) or "close code" in str(e_receive).lower():
                         logger.warning(f"🔌 WebSocket desconectado: {e_receive}")
                         await self._shutdown(reason="WebSocket Closed Remotely")
                    else:
                         logger.error(f"❌ Error recibiendo del WebSocket: {e_receive}", exc_info=True)
                         await self._shutdown(reason=f"WebSocket Receive Error: {type(e_receive).__name__}")
                    break 

                event = data.get("event")
                ##logger.debug(f"⏱️ TS:[{ts_msg_received}] HANDLE_WS Event received: {event}")

                if event == "start":
                    self.stream_sid = data.get("streamSid")
                    start_data = data.get("start", {})
                    received_call_sid = start_data.get("callSid")
                    if received_call_sid and self.call_sid != received_call_sid:
                         self.call_sid = received_call_sid
                         ##logger.info(f"📞 CallSid actualizado a: {self.call_sid}")
                    logger.info(f"▶️ Evento 'start'. StreamSid: {self.stream_sid}. CallSid: {self.call_sid or 'N/A'}")
                    
                    ts_greet_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                    logger.debug(f"⏱️ TS:[{ts_greet_start}] HANDLE_WS Calling greeting TTS...")
                    greeting_text = self._greeting()
                    logger.info(f"👋 Saludo: '{greeting_text}'")
                    audio_saludo = text_to_speech(greeting_text)
                    await self._play_audio_bytes(audio_saludo)
                    ##logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] HANDLE_WS Greeting TTS finished.")
                elif event == "media":
                    # ts_media_start = self._now() # Descomentar si necesitas medir esta parte
                    payload_b64 = data.get("media", {}).get("payload")

                    if payload_b64:
                        decoded_payload = base64.b64decode(payload_b64)
                        
                        # Determinar si el audio debe ir al buffer o directo a Deepgram
                        buffer_audio = False
                        if self.is_speaking:
                            # Si la IA está hablando, descartamos el audio (no lo bufferizamos ni lo enviamos)
                            # logger.debug("Audio de Twilio IGNORADO (IA está hablando).")
                            pass # No hacer nada con el audio
                        elif self.ignorar_stt:
                            #logger.debug(f"Bufferizando audio de Twilio ({len(decoded_payload)} bytes) porque ignorar_stt es True.")
                            buffer_audio = True
                        elif not self.stt_streamer:
                            #logger.debug(f"Bufferizando audio de Twilio ({len(decoded_payload)} bytes) porque stt_streamer no existe.")
                            buffer_audio = True
                        elif not self.stt_streamer._started:
                            #logger.debug(f"Bufferizando audio de Twilio ({len(decoded_payload)} bytes) porque stt_streamer no está _started.")
                            buffer_audio = True
                        # No necesitamos chequear _is_closing aquí porque si está _is_closing, _started debería ser False.
                        if buffer_audio:
                            self.audio_buffer_twilio.append(decoded_payload)
                        elif self.stt_streamer: # Implica que está _started, no _is_reconnecting, no ignorar_stt, y la IA no habla
                            try:
                                # logger.debug(f"Enviando audio directo a Deepgram ({len(decoded_payload)} bytes).")
                                await self.stt_streamer.send_audio(decoded_payload)
                            except Exception as e_send_audio:
                                logger.error(f"Error enviando audio directo a Deepgram: {e_send_audio}")
                elif event == "stop":
                    logger.info(f"🛑 Evento 'stop' recibido de Twilio (TS:{datetime.now().strftime(LOG_TS_FORMAT)[:-3]})")
                    await self._shutdown(reason="Twilio Stop Event")
                    # break # shutdown pone call_ended a True
                elif event == "mark":
                    mark_name = data.get("mark", {}).get("name")
                    logger.debug(f"🔹 Evento 'mark' recibido: {mark_name} (TS:{datetime.now().strftime(LOG_TS_FORMAT)[:-3]})")
                    pass    
                elif event == "connected": # Ignorar este evento informativo
                     pass                   
                else:
                    logger.warning(f"❓ Evento WebSocket desconocido: {event} (TS:{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}), Data: {str(data)[:200]}")

        except asyncio.CancelledError:
             logger.info("🚦 Tarea principal WebSocket cancelada (normal durante cierre).")
        except Exception as e_main_loop:
            logger.error(f"❌ Error fatal en bucle principal WebSocket: {e_main_loop}", exc_info=True)
            await self._shutdown(reason=f"Main Loop Error: {type(e_main_loop).__name__}")
        finally:
                    # Asegurar limpieza final
                    logger.info(f"🏁 Iniciando bloque finally de handle_twilio_websocket. CallSid: {self.call_sid or 'N/A'}") 
                    if monitor_task and not monitor_task.done(): # 'monitor_task' se define antes del try
                        logger.info("Cancelando tarea de monitoreo en finally...") # Log mejorado
                        monitor_task.cancel()
                        try:
                            await asyncio.wait_for(monitor_task, timeout=0.5) # Espera breve similar
                        except asyncio.TimeoutError:
                            logger.warning("Timeout (0.5s) esperando la cancelación de la tarea de monitoreo.")
                        except asyncio.CancelledError:
                            logger.debug("Tarea de monitoreo ya estaba cancelada o se canceló a tiempo.")
                        except Exception as e_cancel_mon:
                            logger.error(f"Error durante la espera de cancelación de la tarea de monitoreo: {e_cancel_mon}")
                    if not self.call_ended:
                        logger.warning("Llamada no marcada como finalizada en finally de handle_twilio_websocket, llamando a _shutdown como precaución.")
                        await self._shutdown(reason="Cleanup in handle_twilio_websocket finally")
                    logger.info(f"🏁 Finalizado handle_twilio_websocket (post-finally). CallSid: {self.call_sid or 'N/A'}")
                    if CURRENT_CALL_MANAGER is self: 
                        CURRENT_CALL_MANAGER = None





# Dentro de la clase TwilioWebSocketManager:

    async def _reconnect_deepgram_if_needed(self):
        """
        Intenta reconectar a Deepgram si la llamada aún está activa.
        Este método es llamado como callback por DeepgramSTTStreamer en caso de desconexión.
        """
        if self.call_ended:
            logger.info("RECONEXIÓN DG: Llamada ya finalizada. No se intentará reconectar a Deepgram.")
            return

        if not self.stt_streamer: # Por si acaso, aunque no debería pasar si stt_streamer lo llamó
            logger.error("RECONEXIÓN DG: stt_streamer no existe. No se puede reconectar.")
            return

        # Pequeña pausa para evitar bucles de reconexión muy rápidos si algo falla persistentemente
        await asyncio.sleep(1) # Espera 1 segundo antes de reintentar

        logger.info("RECONEXIÓN DG: Intentando reconectar a Deepgram...")

        # Crear una nueva instancia de DeepgramSTTStreamer.
        # Es importante pasarle de nuevo self._reconnect_deepgram_if_needed
        # para que futuras desconexiones también puedan ser manejadas.
        try:
            # Primero, intentamos cerrar la instancia anterior de forma limpia si aún existe y tiene conexión
            if self.stt_streamer and self.stt_streamer.dg_connection:
                logger.info("RECONEXIÓN DG: Cerrando conexión anterior de Deepgram antes de reintentar...")
                await self.stt_streamer.close() # Llama al close que ya tienes
        except Exception as e_close_old:
            logger.warning(f"RECONEXIÓN DG: Error cerrando instancia anterior de Deepgram (puede ser normal): {e_close_old}")
        
        # Creamos la nueva instancia
        self.stt_streamer = DeepgramSTTStreamer(
            callback=self._stt_callback,
            on_disconnect_callback=self._reconnect_deepgram_if_needed # ¡Importante!
        )

        await self.stt_streamer.start_streaming() # Intenta iniciar la nueva conexión

        if self.stt_streamer._started:
            logger.info("RECONEXIÓN DG: ✅ Reconexión a Deepgram exitosa.")
            # Si tienes audio bufferizado mientras estaba desconectado, aquí es donde lo enviarías.
            # La IA de Deepgram sugirió 'self.audio_buffer_twilio', vamos a usarlo.
            if self.audio_buffer_twilio:
                logger.info(f"RECONEXIÓN DG: Enviando {len(self.audio_buffer_twilio)} chunks de audio bufferizado...")
                # Hacemos una copia para iterar y limpiamos el original
                buffered_audio = list(self.audio_buffer_twilio)
                self.audio_buffer_twilio.clear()
                for chunk in buffered_audio:
                    if self.stt_streamer and self.stt_streamer._started: # Re-chequear antes de cada envío
                        await self.stt_streamer.send_audio(chunk)
                    else:
                        logger.warning("RECONEXIÓN DG: Deepgram se desconectó mientras se enviaba el buffer. Re-bufferizando audio restante.")
                        # Si la conexión se cae de nuevo MIENTRAS enviamos el buffer,
                        # volvemos a poner en el buffer los chunks que faltaron.
                        current_index = buffered_audio.index(chunk)
                        self.audio_buffer_twilio.extend(buffered_audio[current_index:])
                        break
                logger.info("RECONEXIÓN DG: Buffer de audio enviado.")
            
            # Después de una reconexión exitosa, si `ignorar_stt` estaba activo porque la IA estaba "hablando"
            # o procesando, y ya no lo está, debemos reactivar la escucha normal.
            # Sin embargo, este método `_reconnect_deepgram_if_needed` se llama por una desconexión
            # de bajo nivel. La lógica de `ignorar_stt` y `is_speaking` se maneja más arriba.
            # Lo importante aquí es que `_started` esté `True`.
            
        else:
            logger.error("RECONEXIÓN DG: ❌ Falló la reconexión a Deepgram.")
            # Aquí podrías decidir si reintentar N veces o si dar la conexión STT por perdida
            # y quizás terminar la llamada si el STT es crítico. Por ahora, solo logueamos.
            # Si falla la reconexión, la próxima vez que _on_error/_on_close ocurra, se reintentará.






    # --- Callback de Deepgram y Lógica de Acumulación ---

    def _stt_callback(self, transcript: str, is_final: bool):
        """Callback de Deepgram con Timestamps y Lógica Mejorada."""
        ts_callback_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        # logger.debug(f"⏱️ TS:[{ts_callback_start}] STT_CALLBACK START (final={is_final})")

        if self.ignorar_stt:
            logger.warning(f"🚫 STT Ignorado (ignorar_stt=True): final={is_final}, text='{transcript[:60]}...' (TS:{ts_callback_start})")
            return 

        ahora_pc = self._now() # Usar perf_counter para coherencia en timestamps relativos internos
        ahora_dt = datetime.now() # Usar datetime para logs absolutos
        
        if transcript and transcript.strip():
            self.last_activity_ts = ahora_pc # Actualizar con perf_counter
            self.ultimo_evento_fue_parcial = not is_final 
            
            log_text_brief = transcript.strip()[:60] + ('...' if len(transcript.strip()) > 60 else '')
            #logger.debug(f"🎤 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Activity: final={is_final}, flag_parcial={self.ultimo_evento_fue_parcial}, text='{log_text_brief}'")

            if is_final:
                self.last_final_ts = ahora_pc # Actualizar TS del último final
                self.last_final_stt_timestamp = ahora_pc
                #logger.info(f"📥 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Final Recibido: '{transcript.strip()}'")
                self.finales_acumulados.append(transcript.strip())
            else:
                 # Loguear parciales solo si el nivel de log es TRACE o similar (si lo implementas)
                 # logger.trace(f"📊 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Parcial: '{log_text_brief}'")
                 pass

            # Reiniciar el temporizador principal
            if self.temporizador_pausa and not self.temporizador_pausa.done():
                # logger.debug(f"   STT_CALLBACK Cancelling existing pause timer...") # Log de cancelación está en la tarea
                self.temporizador_pausa.cancel()
                
            ##logger.debug(f"⏱️ TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Reiniciando timer de pausa ({PAUSA_SIN_ACTIVIDAD_TIMEOUT}s).")
            self.temporizador_pausa = asyncio.create_task(self._intentar_enviar_si_pausa(), name=f"PausaTimer_{self.call_sid or id(self)}")
        else:
             logger.debug(f"🔇 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Recibido transcript vacío.")







    async def _intentar_enviar_si_pausa(self):
        """Tarea que espera pausa y decide si enviar, con Timestamps."""
        ts_intento_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        # logger.debug(f"⏱️ TS:[{ts_intento_start}] INTENTAR_ENVIAR START")
        
        tiempo_espera = PAUSA_SIN_ACTIVIDAD_TIMEOUT 
        timeout_maximo = MAX_TIMEOUT_SIN_ACTIVIDAD

        try:
            ##logger.debug(f"⏳ Esperando {tiempo_espera:.1f}s de pausa total...")
            await asyncio.sleep(tiempo_espera)
            
            ts_sleep_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            ahora = self._now()
            elapsed_activity = ahora - self.last_activity_ts
            # Usar getattr para evitar error si last_final_ts no se inicializó bien
            elapsed_final = ahora - getattr(self, 'last_final_ts', ahora) 
            
            ##logger.debug(f"⌛ TS:[{ts_sleep_end}] INTENTAR_ENVIAR Timer completado. Tiempo real desde últ_act: {elapsed_activity:.2f}s / desde últ_final: {elapsed_final:.2f}s")

            if self.call_ended:
                #logger.debug("⚠️ INTENTAR_ENVIAR: Llamada finalizada durante espera. Abortando.")
                return

            if not self.finales_acumulados:
                ##logger.debug("⏸️ INTENTAR_ENVIAR: Timer cumplido, pero sin finales acumulados.")
                self.ultimo_evento_fue_parcial = False # Resetear por si acaso
                return

            # --- Lógica de Decisión para Enviar ---
            
            # CONDICIÓN 1: Timeout Máximo (Failsafe)
            if elapsed_activity >= timeout_maximo:
                #logger.warning(f"⚠️ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Timeout máximo ({timeout_maximo:.1f}s) alcanzado (elapsed={elapsed_activity:.2f}s). Forzando envío.")
                await self._proceder_a_enviar() 
                return

            # CONDICIÓN 2: Pausa Normal y Último Evento fue FINAL
            # Comparamos con umbral ligeramente menor para evitar problemas de precisión flotante
            if elapsed_activity >= (tiempo_espera - 0.1) and not self.ultimo_evento_fue_parcial:
                #logger.info(f"✅ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Pausa normal ({tiempo_espera:.1f}s) detectada después de FINAL. Procediendo.")
                await self._proceder_a_enviar() 
                return
                
            # CONDICIÓN 3: Pausa Normal pero Último Evento fue PARCIAL
            if elapsed_activity >= (tiempo_espera - 0.1) and self.ultimo_evento_fue_parcial:
                #logger.info(f"⏸️ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Pausa normal ({tiempo_espera:.1f}s) detectada después de PARCIAL. Esperando 'is_final=true' correspondiente...")
                # No enviamos, esperamos que el final reinicie el timer.
                # El failsafe (Condición 1) eventualmente actuará si el final nunca llega.
                return

            #logger.debug(f"❔ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Timer cumplido, pero ninguna condición de envío activa.")

        except asyncio.CancelledError:
            ts_cancel = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            #logger.debug(f"🛑 TS:[{ts_cancel}] INTENTAR_ENVIAR: Timer de pausa cancelado/reiniciado (normal).")
        except Exception as e:
            ts_error = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.error(f"❌ TS:[{ts_error}] Error en _intentar_enviar_si_pausa: {e}", exc_info=True)








    async def _proceder_a_enviar(self):
            """Prepara y envía acumulados, activa 'ignorar_stt', con Timestamps."""
            ts_proceder_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            #logger.debug(f"⏱️ TS:[{ts_proceder_start}] PROCEDER_ENVIAR START")

            if not self.finales_acumulados or self.call_ended or self.ignorar_stt:
                #logger.warning(f"⚠️ PROCEDER_ENVIAR Abortado: finales_empty={not self.finales_acumulados}, call_ended={self.call_ended}, ignorar_stt={self.ignorar_stt}")
                # Si abortamos, aseguramos que el timestamp se limpie si no hay finales
                if not self.finales_acumulados:
                    self.last_final_stt_timestamp = None
                return

            # 1. Preparar mensaje
            mensaje_acumulado = " ".join(self.finales_acumulados).replace("\n", " ").strip()
            num_finales = len(self.finales_acumulados)

            if not mensaje_acumulado:
                #logger.warning(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] PROCEDER_ENVIAR Mensaje acumulado vacío. Limpiando y abortando.")
                self.finales_acumulados.clear()
                self.ultimo_evento_fue_parcial = False
                # Asegurarse de resetear también el timestamp si abortamos aquí
                self.last_final_stt_timestamp = None # ### NUEVO RESET AQUÍ ###
                return

            ##logger.info(f"📦 TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] PROCEDER_ENVIAR Preparado (acumulados: {num_finales}): '{mensaje_acumulado}'")

            # ### MODIFICADO ### Capturar el timestamp ANTES de limpiar
            final_ts_for_this_batch = self.last_final_stt_timestamp

            # Limpiar estado ANTES de operaciones asíncronas
            self.finales_acumulados.clear()
            self.ultimo_evento_fue_parcial = False
            self.last_final_stt_timestamp = None # ### NUEVO RESET AQUÍ ### Resetear para el próximo turno

            # 2. Activar modo "ignorar STT"
            self.ignorar_stt = True
            ##logger.info(f"🚫 TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] PROCEDER_ENVIAR Activado: Ignorando STT.")

            # Cancelar timer de pausa por si acaso
            if self.temporizador_pausa and not self.temporizador_pausa.done():
                self.temporizador_pausa.cancel()
                ##logger.debug("   PROCEDER_ENVIAR: Cancelado timer de pausa residual.")
                self.temporizador_pausa = None

            # 3. Ejecutar envío (GPT o Log)
            try:
                if TEST_MODE_NO_GPT:
                    ts_test_log = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                    logger.info(f"🧪 TS:[{ts_test_log}] MODO PRUEBA: Mensaje sería: '{mensaje_acumulado}'")
                    # En modo prueba, reactivar STT manualmente
                    asyncio.create_task(self._reactivar_stt_despues_de_envio(), name=f"ReactivarSTT_Test_{self.call_sid or id(self)}")
                else:
                    # Cancelar tarea GPT anterior (doble check)
                    if self.current_gpt_task and not self.current_gpt_task.done():
                        logger.warning("⚠️ Cancelando tarea GPT anterior activa antes de enviar nueva.")
                        self.current_gpt_task.cancel()
                        try: await asyncio.wait_for(self.current_gpt_task, timeout=0.5)
                        except asyncio.CancelledError: logger.debug(" Tarea GPT anterior cancelada.")
                        except Exception as e_gpt_cancel: logger.error(f" Error esperando cancelación tarea GPT: {e_gpt_cancel}")
                        self.current_gpt_task = None

                    # Iniciar la nueva tarea GPT que reactivará STT
                    ts_gpt_start_task = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                    #logger.info(f"🌐 TS:[{ts_gpt_start_task}] PROCEDER_ENVIAR Iniciando tarea para GPT...")
                    self.current_gpt_task = asyncio.create_task(
                        # ### MODIFICADO ### Pasar el timestamp capturado
                        self.process_gpt_and_reactivate_stt(mensaje_acumulado, final_ts_for_this_batch),
                        name=f"GPTTask_{self.call_sid or id(self)}"
                    )
            except Exception as e_proc_env:
                ts_error = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                logger.error(f"❌ TS:[{ts_error}] Error al iniciar tarea de envío/GPT: {e_proc_env}", exc_info=True)
                # Intentar reactivar STT si falla el inicio de la tarea
                await self._reactivar_stt_despues_de_envio()







    async def process_gpt_and_reactivate_stt(self, texto_para_gpt: str, last_final_ts: Optional[float]):
        """Wrapper seguro que llama a process_gpt_response y asegura reactivar STT."""
        ts_wrapper_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        ##logger.debug(f"⏱️ TS:[{ts_wrapper_start}] PROCESS_GPT_WRAPPER START")
        try:
             # ### MODIFICADO ### Pasar el timestamp
            await self.process_gpt_response(texto_para_gpt, last_final_ts)
        except Exception as e:
             logger.error(f"❌ Error capturado dentro de process_gpt_and_reactivate_stt: {e}", exc_info=True)
        finally:
            ts_wrapper_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            ##logger.debug(f"🏁 TS:[{ts_wrapper_end}] PROCESS_GPT_WRAPPER Finalizando. Reactivando STT...")
            await self._reactivar_stt_despues_de_envio()






    async def _reactivar_stt_despues_de_envio(self):
            """
            Se llama después de que la IA ha hablado.
            Verifica/restablece la conexión de Deepgram, procesa el buffer de audio y reactiva el STT.
            """
            ts_reactivar_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            log_prefix = f"ReactivarSTT_{self.call_sid or str(id(self))[-6:]}"
            ##logger.info(f"[{log_prefix}] ⏱️ Iniciando proceso de reactivación de STT Post-TTS.")

            if self.call_ended:
                logger.info(f"[{log_prefix}] Llamada ya terminó. No se procede con la reactivación de STT.")
                # Asegurarse de que la tarea de cierre de DG (si la usamos) se maneje
                if self.close_dg_task and not self.close_dg_task.done():
                    logger.debug(f"[{log_prefix}] Cancelando tarea de cierre de Deepgram pendiente (close_dg_task).")
                    self.close_dg_task.cancel()
                return

            # Si implementamos el cierre proactivo de Deepgram ANTES del TTS, esperamos aquí.
            if self.close_dg_task: # Si la tarea existe (fue creada en _proceder_a_enviar)
                if not self.close_dg_task.done():
                    logger.info(f"[{log_prefix}] Esperando a que la tarea de cierre de Deepgram (close_dg_task) se complete...")
                    try:
                        await asyncio.wait_for(self.close_dg_task, timeout=1.0) # Esperar un poco
                        logger.info(f"[{log_prefix}] Tarea de cierre de Deepgram (close_dg_task) completada.")
                    except asyncio.TimeoutError:
                        logger.warning(f"[{log_prefix}] Timeout esperando la tarea de cierre de Deepgram (close_dg_task).")
                    except Exception as e_wait_close:
                        logger.warning(f"[{log_prefix}] Error esperando tarea de cierre de Deepgram (close_dg_task): {e_wait_close}")
                # else:
                    # logger.debug(f"[{log_prefix}] Tarea de cierre de Deepgram (close_dg_task) ya había completado.")
                self.close_dg_task = None # Limpiar la referencia a la tarea

            # Verificar estado de Deepgram y reintentar conexión si es necesario
            streamer_is_operational = False
            if self.stt_streamer:
                if self.stt_streamer._started and not self.stt_streamer._is_closing and not self.stt_streamer._is_reconnecting:
                    streamer_is_operational = True
                
                # Este es el nuevo bloque que reemplaza al anterior
                if not streamer_is_operational: # Esta variable 'streamer_is_operational' se habrá seteado a False justo antes si las condiciones no se cumplieron
                    logger.warning(f"[{log_prefix}] Deepgram no está operativo (started={getattr(self.stt_streamer, '_started', 'N/A')}, "
                                f"closing={getattr(self.stt_streamer, '_is_closing', 'N/A')}). "
                                "STT no se puede reactivar completamente sin una conexión Deepgram activa.")
                    # OPCIONAL: Aquí podrías decidir si la falta de STT es crítica y apagar la llamada.
                    # if not self.call_ended:
                    #    logger.critical(f"[{log_prefix}] STT no pudo reactivarse. Iniciando shutdown de la llamada.")
                    #    await self._shutdown(reason="Critical STT Reactivation Failure Post-TTS")
                    # return # Podrías salir de la función aquí si no hay nada más que hacer.
                
                # Procesar buffer de Twilio
                if self.audio_buffer_twilio:
                    #logger.info(f"[{log_prefix}] Procesando {len(self.audio_buffer_twilio)} chunks del buffer de Twilio...")
                    # Crear una copia para iterar por si la lista se modifica (aunque no debería aquí)
                    buffered_chunks_to_send = list(self.audio_buffer_twilio)
                    self.audio_buffer_twilio.clear() # Limpiar buffer original inmediatamente

                    for i, chunk_data in enumerate(buffered_chunks_to_send):
                        # Re-chequear estado del streamer antes de cada envío del buffer, por si acaso
                        if self.stt_streamer and self.stt_streamer._started and \
                        not self.stt_streamer._is_closing and not self.stt_streamer._is_reconnecting:
                            try:
                                # logger.debug(f"[{log_prefix}] Enviando chunk {i+1} del buffer a Deepgram.")
                                await self.stt_streamer.send_audio(chunk_data)
                            except Exception as e_send_buffer:
                                logger.error(f"[{log_prefix}] Error enviando chunk {i+1} del buffer a Deepgram: {e_send_buffer}")
                                # Si falla aquí, el audio restante del buffer podría perderse o
                                # podríamos volver a añadirlo al inicio del buffer.
                                # Por simplicidad, por ahora no lo re-añadimos.
                                break 
                        else:
                            logger.warning(f"[{log_prefix}] Deepgram dejó de estar operativo durante el procesamiento del buffer. "
                                        f"Chunks restantes del buffer ({len(buffered_chunks_to_send) - i}) no enviados.")
                            # Re-añadir los chunks no enviados al buffer principal
                            self.audio_buffer_twilio.extend(buffered_chunks_to_send[i:])
                            break 
                    #logger.info(f"[{log_prefix}] Buffer de Twilio procesado.")
                else:
                    logger.info(f"[{log_prefix}] Buffer de Twilio vacío, no hay nada que procesar.")
                
                if self.ignorar_stt: # Solo cambiar si estaba True
                    self.ignorar_stt = False 
                    #logger.info(f"[{log_prefix}] ✅ Flag 'ignorar_stt' puesto a False.")
                # else:
                    # logger.debug(f"[{log_prefix}] Flag 'ignorar_stt' ya era False.")
            
            else: # Deepgram no está operativo incluso después del intento de reconexión
                logger.error(f"[{log_prefix}] ❌ Deepgram NO está operativo después de intentos. "
                            f"STT no se reactivará completamente. ignorar_stt permanecerá {self.ignorar_stt}.")
                # En este punto, self.ignorar_stt debería seguir siendo True si no se pudo reactivar,
                # para que el audio siga yendo al buffer.
                # Podríamos considerar un self._shutdown() aquí si STT es absolutamente crítico.
                if not self.call_ended: # Solo si la llamada no está ya terminando por otra razón
                    logger.critical(f"[{log_prefix}] STT no pudo reactivarse. Iniciando shutdown de la llamada.")
                    await self._shutdown(reason="Critical STT Reactivation Failure")
        
            logger.info(f"[{log_prefix}] 🏁 FIN Reactivación STT Post-TTS. Estado ignorar_stt: {self.ignorar_stt}")









    
    async def process_gpt_response(self, user_text: str, last_final_ts: Optional[float]):
        """Llama a GPT, maneja respuesta y TTS, con Timestamps y Hold Message."""
        ts_process_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        #logger.debug(f"⏱️ TS:[{ts_process_start}] PROCESS_GPT START")

        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            logger.warning("⚠️ PROCESS_GPT Ignorado: llamada terminada o WS desconectado.")
            # Asegurar reactivación si se aborta aquí ANTES de haberla desactivado
            # No es estrictamente necesario porque _reactivar_stt_despues_de_envio lo hará,
            # pero no hace daño ser explícito si la función retornara temprano.
            # self.ignorar_stt = False
            return

        if not user_text:
             logger.warning("⚠️ PROCESS_GPT Texto de usuario vacío, saltando.")
             # self.ignorar_stt = False # Similar al caso anterior
             return

        logger.info(f"🗣️ Mensaje para GPT: '{user_text}'")
        self.conversation_history.append({"role": "user", "content": user_text})

        respuesta_gpt = "Lo siento, ocurrió un problema interno."
        audio_para_reproducir = b"" # Inicializar por si falla GPT/TTS

        try:
            # --- Llamada a OpenAI ---
            start_gpt_call = self._now()
            ts_gpt_call_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            #logger.debug(f"⏱️ TS:[{ts_gpt_call_start}] PROCESS_GPT Calling generate_openai_response_main...")

            model_a_usar = config("CHATGPT_MODEL", default="gpt-4.1-mini") # Usar config con fallback
            mensajes_para_gpt = generate_openai_prompt(self.conversation_history)

            respuesta_gpt = await generate_openai_response_main(
                history=mensajes_para_gpt,
                model=model_a_usar
            )

            gpt_duration_ms = (self._now() - start_gpt_call) * 1000
            ts_gpt_call_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            ##logger.info(f"⏱️ TS:[{ts_gpt_call_end}] PROCESS_GPT Respuesta OpenAI recibida. ⏱️ DUR:[{gpt_duration_ms:.1f} ms]")

            if self.call_ended: return # Verificar después de llamada potencialmente larga

            if respuesta_gpt is None or not isinstance(respuesta_gpt, str):
                 logger.error(f"❌ PROCESS_GPT Respuesta inválida/nula de IA: {respuesta_gpt}")
                 respuesta_gpt = "Disculpe, no pude procesar eso." # Fallback específico

            reply_cleaned = respuesta_gpt.strip()

            # --- Manejar Respuesta (__END_CALL__ o Normal) ---
            if reply_cleaned == "__END_CALL__":
                # ... (código igual que antes para manejar __END_CALL__) ...
                ts_end_call = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                logger.info(f"🚪 TS:[{ts_end_call}] PROCESS_GPT Protocolo cierre (__END_CALL__) por IA.")
                despedida_dicha = any(
                    gphrase.lower() in m.get("content", "").lower()
                    for m in self.conversation_history[-2:] # Revisar últimos 2 mensajes
                    if m.get("role") == "assistant"
                    for gphrase in ["gracias", "hasta luego", "placer atenderle", "excelente día"]
                )

                frase_final = ""
                if not despedida_dicha:
                    frase_final = GOODBYE_PHRASE
                    logger.info(f"💬 PROCESS_GPT Añadiendo despedida: '{frase_final}'")
                    # Añadir a historial ANTES de TTS por si falla
                    self.conversation_history.append({"role": "assistant", "content": frase_final})
                else:
                     logger.info("   PROCESS_GPT IA ya se despidió, cerrando.")

                # Generar TTS para la despedida (si aplica)
                audio_despedida = text_to_speech(frase_final) if frase_final else b""
                await self._play_audio_bytes(audio_despedida) # Reproducir aunque sea vacío (no hará nada)
                await asyncio.sleep(0.2) # Pequeña pausa para asegurar envío
                await self._shutdown(reason="AI Request (__END_CALL__)")
                return # Importante: salir después de shutdown

            # --- Respuesta Normal - Generar TTS ---
            logger.info(f"🤖 Respuesta de GPT: {reply_cleaned}")
            self.conversation_history.append({"role": "assistant", "content": reply_cleaned})

            start_tts_gen = self._now()
            ts_tts_gen_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            ##logger.debug(f"⏱️ TS:[{ts_tts_gen_start}] PROCESS_GPT Calling TTS...")
            audio_para_reproducir = text_to_speech(reply_cleaned) # Guardar en variable local
            tts_gen_duration_ms = (self._now() - start_tts_gen) * 1000
            ts_tts_gen_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]

            if not audio_para_reproducir:
                 logger.error(f"🔇 TS:[{ts_tts_gen_end}] Fallo al generar audio TTS principal.")
                 # Intentar generar TTS de error como fallback
                 error_tts_msg = "Hubo un problema generando la respuesta de audio."
                 audio_para_reproducir = text_to_speech(error_tts_msg)
                 if not audio_para_reproducir:
                     logger.error("❌ Falló también la generación de TTS para el mensaje de error TTS.")


        except asyncio.CancelledError:
            ts_cancel = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.info(f"🚫 TS:[{ts_cancel}] Tarea GPT cancelada.")
            # No relanzar, dejar que el finally del wrapper maneje la reactivación
            return # Importante salir aquí
        except Exception as e_gpt_tts:
            ts_error_gpt_tts = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.error(f"❌ TS:[{ts_error_gpt_tts}] Error crítico durante GPT/TTS: {e_gpt_tts}", exc_info=True)
            # Intentar generar audio para mensaje de error genérico
            try:
                 error_message = "Lo siento, ocurrió un error técnico."
                 if not self.conversation_history or "[ERROR]" not in self.conversation_history[-1].get("content",""):
                     self.conversation_history.append({"role": "assistant", "content": f"[ERROR] {error_message}"})
                 audio_para_reproducir = text_to_speech(error_message) # Sobrescribir con audio de error
                 if not audio_para_reproducir:
                      logger.error("❌ Falló incluso la generación de TTS para el mensaje de error genérico.")
            except Exception as e_tts_error_fallback:
                 logger.error(f"❌ Error generando TTS para mensaje de error genérico: {e_tts_error_fallback}")
                 audio_para_reproducir = b"" # Asegurar que esté vacío si todo falla

        # --- Bloque de Reproducción (Fuera del try/except de GPT/TTS) ---
        # Este bloque se ejecuta siempre, incluso si hubo errores en GPT/TTS,
        # para intentar reproducir al menos el mensaje de error si se pudo generar.
        try:
            # ### NUEVO ### Check de Latencia y Mensaje de Espera
            play_hold_message = False
            if last_final_ts is not None and not self.call_ended: # Añadir check de call_ended
                current_time_before_play = self._now()
                real_latency = current_time_before_play - last_final_ts
                latency_threshold = LATENCY_THRESHOLD_FOR_HOLD_MESSAGE # Usar constante

                ts_check_delay = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                # Loguear sólo si es relevante (ej. > 3 segundos) para no saturar
                if real_latency > 4.0:
                     logger.info(f"⏱️ TS:[{ts_check_delay}] PROCESS_GPT Latency Check: Diff={real_latency:.3f}s, Threshold={latency_threshold}s")

                if real_latency > latency_threshold:
                    if self.hold_audio_mulaw_bytes:
                        logger.info(f"⚠️ TS:[{ts_check_delay}] Real latency ({real_latency:.2f}s) EXCEEDED threshold ({latency_threshold}s). Playing hold message.")
                        play_hold_message = True
                    else:
                        logger.warning(f"⚠️ TS:[{ts_check_delay}] Real latency ({real_latency:.2f}s) exceeded threshold, but hold audio is not loaded.")
            # ### FIN Check Latencia ###

            # Reproducir mensaje de espera si es necesario y la llamada sigue activa
            if play_hold_message and not self.call_ended:
                ts_hold_play_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                logger.debug(f"⏱️ TS:[{ts_hold_play_start}] PROCESS_GPT Playing hold audio...")
                await self._play_audio_bytes(self.hold_audio_mulaw_bytes)
                # No añadir sleep aquí, _play_audio_bytes ya espera
                ts_hold_play_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                logger.debug(f"⏱️ TS:[{ts_hold_play_end}] PROCESS_GPT Hold audio finished.")
            # ### FIN Reproducción Espera ###

            # Reproducir la respuesta principal (o el audio de error si se generó) si la llamada sigue activa
            if audio_para_reproducir and not self.call_ended:
                # Loguear qué se va a reproducir (respuesta normal o de error)
                if "[ERROR]" in self.conversation_history[-1].get("content", ""):
                    logger.warning(f"🔊 Procediendo a reproducir audio de ERROR ({len(audio_para_reproducir)} bytes)...")
                else:
                    logger.info(f"🔊 Procediendo a reproducir audio principal ({len(audio_para_reproducir)} bytes)...")

                await self._play_audio_bytes(audio_para_reproducir)

                # --- Corte inmediato si la frase ya es una despedida (después de reproducir) ---
                if not self.call_ended: # Verificar de nuevo por si _play_audio_bytes fue interrumpido
                    despedidas = ("hasta luego", "placer atenderle", "gracias por comunicarse")
                    # Usamos 'respuesta_gpt' porque 'audio_para_reproducir' podría ser de error
                    if isinstance(respuesta_gpt, str) and any(p in respuesta_gpt.lower() for p in despedidas):
                        logger.info("👋 Detectada despedida de la IA después de reproducir.")
                        await asyncio.sleep(0.2) # Deja salir el último chunk de audio
                        await self._shutdown(reason="Assistant farewell")
                        # No necesitamos return aquí, el wrapper se encargará
            elif not self.call_ended:
                # Esto sólo ocurriría si GPT/TTS fallaron Y el TTS de error también falló
                logger.error("🔇 No se generó audio principal ni de error para reproducir.")

        except asyncio.CancelledError:
             # Si la tarea se cancela mientras se reproduce el audio (ej. shutdown)
             logger.info("🚫 Reproducción de audio cancelada.")
        except Exception as e_play_block:
             logger.error(f"❌ Error durante el bloque de reproducción de audio: {e_play_block}", exc_info=True)
             # Considerar un shutdown si falla gravemente la reproducción?
             # await self._shutdown(reason="Audio Playback Error")

        finally:
             # El finally del wrapper se encarga de reactivar STT
             ts_process_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
             logger.debug(f"⏱️ TS:[{ts_process_end}] PROCESS_GPT END")






    # --- Funciones Auxiliares 

    async def _play_audio_bytes(self, pcm_ulaw_bytes: bytes) -> None:
            """
            Envía audio μ-law (8 kHz, mono) al <Stream> de Twilio.

            • Divide en frames de 160 bytes = 20 ms.  
            • Añade 'streamSid' a cada JSON; sin él Twilio lanza 31951.
            """
            if not pcm_ulaw_bytes or not self.websocket or not self.stream_sid:
                return

            # Ya no se arranca el feeder de silencio aquí.
            # El keepalive de Deepgram se manejará por el SDK.

            self.is_speaking = True
            CHUNK = 160                               # 20 ms @ 8 kHz μ-law
            total_sent = 0

            try:
                for i in range(0, len(pcm_ulaw_bytes), CHUNK):
                    if self.call_ended:
                        break

                    chunk = pcm_ulaw_bytes[i:i + CHUNK]

                    # LOG opcional (ayuda a depurar si vuelve a salir 31951)
                    # logger.debug("➡️ SEND → %s bytes", len(chunk))

                    await self.websocket.send_json({
                        "streamSid": self.stream_sid,          # 👈 OBLIGATORIO
                        "event": "media",
                        "media": {
                            "payload": base64.b64encode(chunk).decode("ascii")
                        }
                    })

                    total_sent += len(chunk)
                    await asyncio.sleep(0.02)                 # 20 ms

                logger.info(f"🔊 PLAY_AUDIO Fin reproducción. Enviados {total_sent} bytes.")
            finally:
                # Ya no se detiene el feeder de silencio aquí.
                self.is_speaking = False
                self.last_activity_ts = self._now()




    def _greeting(self):
        """Genera el saludo inicial."""
        try:
            now = get_cancun_time()
            h = now.hour
            if 5 <= h < 12: return "Buenos días, consultorio del Dr. Wilfrido Alarcón. ¿Cómo puedo ayudarle?"
            if 12 <= h < 19: return "Buenas tardes, consultorio del Dr. Wilfrido Alarcón. ¿Cómo puedo ayudarle?"
            return "Buenas noches, consultorio del Dr. Wilfrido Alarcón. ¿Cómo puedo ayudarle?"
        except Exception as e_greet:
             logger.error(f"Error generando saludo: {e_greet}")
             return "Consultorio del Doctor Wilfrido Alarcón, ¿Cómo puedo ayudarle?" 







    async def _monitor_call_timeout(self):
        """Tarea en segundo plano que monitoriza duración y silencio."""
        ts_monitor_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.info(f"⏱️ TS:[{ts_monitor_start}] MONITOR Iniciando...")
        while not self.call_ended:
            
            await asyncio.sleep(5) # Revisar cada 5 segundos
            
            if self.call_ended: break 

            now_pc = self._now()
            now_dt_str = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            # logger.debug(f"⏱️ TS:[{now_dt_str}] MONITOR Check...")
            
            # Timeout por duración máxima
            call_duration = now_pc - self.stream_start_time
            if call_duration >= CALL_MAX_DURATION:
                logger.warning(f"⏰ TS:[{now_dt_str}] MONITOR Duración máxima ({CALL_MAX_DURATION}s) excedida (actual: {call_duration:.1f}s).")
                await self._shutdown(reason="Max Call Duration")
                break 

            # Timeout por silencio prolongado (basado en last_activity_ts)
            # Solo si no estamos ocupados (GPT/TTS)
            if not self.ignorar_stt and not self.is_speaking:
                silence_duration = now_pc - self.last_activity_ts
                if silence_duration >= CALL_SILENCE_TIMEOUT:
                    logger.warning(f"⏳ TS:[{now_dt_str}] MONITOR Silencio prolongado ({CALL_SILENCE_TIMEOUT}s) detectado (actual: {silence_duration:.1f}s).")
                    await self._shutdown(reason="User Silence Timeout")
                    break
            # else:
                 # logger.debug(f"   MONITOR Ignorando chequeo silencio (ignorar_stt={self.ignorar_stt}, is_speaking={self.is_speaking})")

        logger.info(f"⏱️ MONITOR Finalizado (CallEnded={self.call_ended}).")


    









    async def _shutdown(self, reason: str = "Unknown"):
            """Cierra conexiones y tareas de forma ordenada, con timeouts."""
            # ts_shutdown_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3] # Movido abajo
            if self.call_ended and self.shutdown_reason != "N/A": 
                logger.info(f"Intento de shutdown múltiple ignorado. Razón original: {self.shutdown_reason}. Nueva razón: {reason}")
                return

            ts_shutdown_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            # Solo loguear "Iniciando" si no es un shutdown múltiple o si la razón es nueva y significativa
            if not self.call_ended or (self.call_ended and self.shutdown_reason == "N/A"):
                 logger.info(f"🔻 TS:[{ts_shutdown_start}] SHUTDOWN Iniciando... Razón: {reason}")
            
            self.call_ended = True 
            if self.shutdown_reason == "N/A": 
                self.shutdown_reason = reason



            # --- Cancelar otras tareas activas ---
            # (Tu código original para cancelar PausaTimer y GPTTask estaba bien,
            # solo me aseguro que se limpien las referencias y añado close_dg_task si lo usamos)
            tasks_to_cancel_map = {
                "PausaTimer": "temporizador_pausa",
                "GPTTask": "current_gpt_task",
                "CloseDGTask": "close_dg_task" # Si añades self.close_dg_task
            }

            for task_name_log, attr_name in tasks_to_cancel_map.items():
                task_instance = getattr(self, attr_name, None)
                if task_instance and not task_instance.done():
                    logger.debug(f"🔴 SHUTDOWN: Cancelando Tarea {task_name_log}...")
                    task_instance.cancel()
                    try:
                        # Espera muy breve para que la cancelación se propague
                        await asyncio.wait_for(task_instance, timeout=0.1) 
                    except asyncio.TimeoutError:
                        logger.debug(f"Timeout breve esperando cancelación de {task_name_log}.")
                    except asyncio.CancelledError:
                        logger.debug(f"Tarea {task_name_log} ya cancelada.")
                    except Exception: # Capturar cualquier otra excepción
                        pass # Ignorar para no bloquear shutdown
                setattr(self, attr_name, None) # Limpiar la referencia (ej. self.temporizador_pausa = None)

            # --- Cerrar Deepgram streamer explícitamente ---
            if self.stt_streamer:
                logger.debug("   SHUTDOWN: Llamando a stt_streamer.close() explícitamente...")
                try:
                    await self.stt_streamer.close() 
                    logger.info("✅ SHUTDOWN: stt_streamer.close() invocado (o ya estaba cerrado/en proceso).")
                except Exception as e_dg_close_final:
                    logger.error(f"❌ SHUTDOWN: Error en la llamada final a stt_streamer.close(): {e_dg_close_final}", exc_info=True)
                finally: # Asegurar que la referencia se limpia
                    self.stt_streamer = None

            # --- Cerrar WebSocket de Twilio ---
            await self._safe_close_websocket(code=1000, reason=self.shutdown_reason)

            # --- Limpiar buffers y conversación ---
            self.conversation_history.clear()
            self.finales_acumulados.clear()
            if hasattr(self, 'audio_buffer_twilio'): # Por si este código se ejecuta antes de que __init__ lo cree
                self.audio_buffer_twilio.clear() 

            ts_shutdown_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.info(f"🏁 TS:[{ts_shutdown_end}] SHUTDOWN Completado (Razón: {self.shutdown_reason}).")

    async def _safe_close_websocket(self, code: int = 1000, reason: str = "Closing"):
        """Cierra el WebSocket de forma segura."""
        ts_ws_close_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
            logger.debug(f"🚪 TS:[{ts_ws_close_start}] Cerrando WebSocket (Code: {code}, Reason: {reason[:100]})")
            try:
                await self.websocket.close(code=code, reason=reason)
                logger.info("✅ WebSocket cerrado correctamente.")
            except Exception as e_ws_close:
                logger.warning(f"⚠️ Error al cerrar WebSocket (normal si ya estaba cerrado): {e_ws_close}")
        else:
            logger.debug(f"🟢 WebSocket ya estaba cerrado o no estaba conectado.")
        self.websocket = None







# --- Función de ayuda para nivel de log ---
def set_debug(active: bool = True) -> None:
    """Establece el nivel de logging para módulos clave."""
    level = logging.DEBUG if active else logging.INFO
    modules_to_set = ["tw_utils", "aiagent", "buscarslot", "consultarinfo", "tts_utils", "deepgram_stt_streamer"]
    for name in modules_to_set:
         logging.getLogger(name).setLevel(level)
    logger.info(f"Nivel de log establecido a {'DEBUG' if active else 'INFO'} para módulos relevantes.")




# --- Inicialización del Nivel de Log ---
set_debug(True) # Descomenta para activar DEBUG por defecto