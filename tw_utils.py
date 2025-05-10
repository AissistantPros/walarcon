# tw_utils.py
"""
WebSocket manager para Twilio <-> Deepgram <-> GPT
----------------------------------------------------------------
Maneja la lógica de acumulación de transcripciones, interacción con GPT,
TTS, y el control del flujo de la llamada, incluyendo la gestión de timeouts
y la prevención de procesamiento de STT obsoleto.
CON LOGGING DETALLADO DE TIEMPOS.
"""

import asyncio
import base64
import json
import logging
import time
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
PAUSA_SIN_ACTIVIDAD_TIMEOUT = .35
MAX_TIMEOUT_SIN_ACTIVIDAD = 5.0

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
        self.shutdown_reason: str = "N/A" 
        
        self.is_speaking: bool = False 
        self.ignorar_stt: bool = False 
        self.ultimo_evento_fue_parcial: bool = False 

        now = self._now()
        ts_now_str = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        self.stream_start_time: float = now
        self.last_activity_ts: float = now 
        self.last_final_ts: float = now 
        logger.debug(f"⏱️ TS:[{ts_now_str}] INIT Timestamps set: start={self.stream_start_time:.2f}, activity={self.last_activity_ts:.2f}, final={self.last_final_ts:.2f}")

        self.finales_acumulados: List[str] = []
        self.conversation_history: List[dict] = []
        self.speaking_lock = asyncio.Lock() 
        
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
        try:
            dg_start_pc = self._now()
            self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
            await self.stt_streamer.start_streaming()
            dg_duration = (self._now() - dg_start_pc) * 1000
            logger.info(f"✅ Deepgram STT iniciado. ⏱️ DUR:[{dg_duration:.1f}ms]")
        except Exception as e_dg_start:
            logger.critical(f"❌ CRÍTICO: Deepgram no arrancó: {e_dg_start}", exc_info=True)
            await self._safe_close_websocket(code=1011, reason="STT Initialization Failed")
            CURRENT_CALL_MANAGER = None
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
                         logger.info(f"📞 CallSid actualizado a: {self.call_sid}")
                    logger.info(f"▶️ Evento 'start'. StreamSid: {self.stream_sid}. CallSid: {self.call_sid or 'N/A'}")
                    
                    ts_greet_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                    logger.debug(f"⏱️ TS:[{ts_greet_start}] HANDLE_WS Calling greeting TTS...")
                    greeting_text = self._greeting()
                    logger.info(f"👋 Saludo: '{greeting_text}'")
                    audio_saludo = text_to_speech(greeting_text)
                    await self._play_audio_bytes(audio_saludo)
                    logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] HANDLE_WS Greeting TTS finished.")


                elif event == "media":
                    ts_media_start = self._now()
                    # logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] HANDLE_WS Media event START")
                    if not self.ignorar_stt and not self.is_speaking:
                        payload = data.get("media", {}).get("payload")
                        if payload and self.stt_streamer:
                            try:
                                # No añadir log aquí por verbosidad, el streamer lo hará si es necesario
                                await self.stt_streamer.send_audio(base64.b64decode(payload))
                            except Exception as e_send_audio:
                                logger.error(f"Error enviando audio a Deepgram: {e_send_audio}")
                    # logger.debug(f"⏱️ DUR:[{(self._now() - ts_media_start)*1000:.1f}ms] HANDLE_WS Media event END")


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
            if monitor_task and not monitor_task.done():
                monitor_task.cancel()
                logger.debug(" Cancelando tarea de monitoreo en finally...")
            logger.info(f"🏁 Finalizado handle_twilio_websocket. CallSid: {self.call_sid or 'N/A'}")
            if CURRENT_CALL_MANAGER is self:
                CURRENT_CALL_MANAGER = None

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
            logger.debug(f"🎤 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Activity: final={is_final}, flag_parcial={self.ultimo_evento_fue_parcial}, text='{log_text_brief}'")

            if is_final:
                self.last_final_ts = ahora_pc # Actualizar TS del último final
                logger.info(f"📥 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Final Recibido: '{transcript.strip()}'")
                self.finales_acumulados.append(transcript.strip())
            else:
                 # Loguear parciales solo si el nivel de log es TRACE o similar (si lo implementas)
                 # logger.trace(f"📊 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Parcial: '{log_text_brief}'")
                 pass

            # Reiniciar el temporizador principal
            if self.temporizador_pausa and not self.temporizador_pausa.done():
                # logger.debug(f"   STT_CALLBACK Cancelling existing pause timer...") # Log de cancelación está en la tarea
                self.temporizador_pausa.cancel()
                
            logger.debug(f"⏱️ TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Reiniciando timer de pausa ({PAUSA_SIN_ACTIVIDAD_TIMEOUT}s).")
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
            logger.debug(f"⏳ Esperando {tiempo_espera:.1f}s de pausa total...")
            await asyncio.sleep(tiempo_espera)
            
            ts_sleep_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            ahora = self._now()
            elapsed_activity = ahora - self.last_activity_ts
            # Usar getattr para evitar error si last_final_ts no se inicializó bien
            elapsed_final = ahora - getattr(self, 'last_final_ts', ahora) 
            
            logger.debug(f"⌛ TS:[{ts_sleep_end}] INTENTAR_ENVIAR Timer completado. Tiempo real desde últ_act: {elapsed_activity:.2f}s / desde últ_final: {elapsed_final:.2f}s")

            if self.call_ended:
                logger.debug("⚠️ INTENTAR_ENVIAR: Llamada finalizada durante espera. Abortando.")
                return

            if not self.finales_acumulados:
                logger.debug("⏸️ INTENTAR_ENVIAR: Timer cumplido, pero sin finales acumulados.")
                self.ultimo_evento_fue_parcial = False # Resetear por si acaso
                return

            # --- Lógica de Decisión para Enviar ---
            
            # CONDICIÓN 1: Timeout Máximo (Failsafe)
            if elapsed_activity >= timeout_maximo:
                logger.warning(f"⚠️ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Timeout máximo ({timeout_maximo:.1f}s) alcanzado (elapsed={elapsed_activity:.2f}s). Forzando envío.")
                await self._proceder_a_enviar() 
                return

            # CONDICIÓN 2: Pausa Normal y Último Evento fue FINAL
            # Comparamos con umbral ligeramente menor para evitar problemas de precisión flotante
            if elapsed_activity >= (tiempo_espera - 0.1) and not self.ultimo_evento_fue_parcial:
                logger.info(f"✅ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Pausa normal ({tiempo_espera:.1f}s) detectada después de FINAL. Procediendo.")
                await self._proceder_a_enviar() 
                return
                
            # CONDICIÓN 3: Pausa Normal pero Último Evento fue PARCIAL
            if elapsed_activity >= (tiempo_espera - 0.1) and self.ultimo_evento_fue_parcial:
                logger.info(f"⏸️ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Pausa normal ({tiempo_espera:.1f}s) detectada después de PARCIAL. Esperando 'is_final=true' correspondiente...")
                # No enviamos, esperamos que el final reinicie el timer.
                # El failsafe (Condición 1) eventualmente actuará si el final nunca llega.
                return

            logger.debug(f"❔ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Timer cumplido, pero ninguna condición de envío activa.")

        except asyncio.CancelledError:
            ts_cancel = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.debug(f"🛑 TS:[{ts_cancel}] INTENTAR_ENVIAR: Timer de pausa cancelado/reiniciado (normal).")
        except Exception as e:
            ts_error = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.error(f"❌ TS:[{ts_error}] Error en _intentar_enviar_si_pausa: {e}", exc_info=True)


    async def _proceder_a_enviar(self):
        """Prepara y envía acumulados, activa 'ignorar_stt', con Timestamps."""
        ts_proceder_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.debug(f"⏱️ TS:[{ts_proceder_start}] PROCEDER_ENVIAR START")

        if not self.finales_acumulados or self.call_ended or self.ignorar_stt:
             logger.warning(f"⚠️ PROCEDER_ENVIAR Abortado: finales_empty={not self.finales_acumulados}, call_ended={self.call_ended}, ignorar_stt={self.ignorar_stt}")
             return 

        # 1. Preparar mensaje
        mensaje_acumulado = " ".join(self.finales_acumulados).replace("\n", " ").strip()
        num_finales = len(self.finales_acumulados)
        
        if not mensaje_acumulado:
             logger.warning(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] PROCEDER_ENVIAR Mensaje acumulado vacío. Limpiando y abortando.")
             self.finales_acumulados.clear() 
             self.ultimo_evento_fue_parcial = False
             return
             
        logger.info(f"📦 TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] PROCEDER_ENVIAR Preparado (acumulados: {num_finales}): '{mensaje_acumulado}'")
        
        # Limpiar estado ANTES de operaciones asíncronas
        self.finales_acumulados.clear()
        self.ultimo_evento_fue_parcial = False 

        # 2. Activar modo "ignorar STT"
        self.ignorar_stt = True
        logger.info(f"🚫 TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] PROCEDER_ENVIAR Activado: Ignorando STT.")
        
        # Cancelar timer de pausa por si acaso
        if self.temporizador_pausa and not self.temporizador_pausa.done():
            self.temporizador_pausa.cancel()
            logger.debug("   PROCEDER_ENVIAR: Cancelado timer de pausa residual.")
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
                logger.info(f"🌐 TS:[{ts_gpt_start_task}] PROCEDER_ENVIAR Iniciando tarea para GPT...")
                self.current_gpt_task = asyncio.create_task(
                    self.process_gpt_and_reactivate_stt(mensaje_acumulado), 
                    name=f"GPTTask_{self.call_sid or id(self)}"
                )
        except Exception as e_proc_env:
             ts_error = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
             logger.error(f"❌ TS:[{ts_error}] Error al iniciar tarea de envío/GPT: {e_proc_env}", exc_info=True)
             # Intentar reactivar STT si falla el inicio de la tarea
             await self._reactivar_stt_despues_de_envio()


    async def process_gpt_and_reactivate_stt(self, texto_para_gpt: str):
        """Wrapper seguro que llama a process_gpt_response y asegura reactivar STT."""
        ts_wrapper_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.debug(f"⏱️ TS:[{ts_wrapper_start}] PROCESS_GPT_WRAPPER START")
        try:
            await self.process_gpt_response(texto_para_gpt) 
        except Exception as e:
             logger.error(f"❌ Error capturado dentro de process_gpt_and_reactivate_stt: {e}", exc_info=True)
        finally:
            ts_wrapper_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.debug(f"🏁 TS:[{ts_wrapper_end}] PROCESS_GPT_WRAPPER Finalizando. Reactivando STT...")
            await self._reactivar_stt_despues_de_envio()


    async def _reactivar_stt_despues_de_envio(self):
        """Desactiva el flag 'ignorar_stt'."""
        ts_reactivar_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        # logger.debug(f"⏱️ TS:[{ts_reactivar_start}] REACTIVAR_STT START")
        # No necesitamos sleep aquí si la llamada a GPT/TTS ya tuvo sus pausas.
        # await asyncio.sleep(0.1) 

        if not self.call_ended: 
            if self.ignorar_stt:
                 self.ignorar_stt = False
                 logger.info(f"✅ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] REACTIVAR_STT: Flag 'ignorar_stt' puesto a False.")
            else:
                 logger.debug("   REACTIVAR_STT: No necesario (ignorar_stt ya era False).")
            
            # Considerar si reiniciar el timer de pausa aquí es bueno o no.
            # Reiniciarlo podría colgar si el usuario no habla después del TTS.
            # No reiniciarlo significa que la próxima actividad del usuario lo iniciará.
            # Vamos a NO reiniciarlo explícitamente aquí.
        else:
             logger.debug("   REACTIVAR_STT: Llamada ya terminó, no se reactiva.")


    async def process_gpt_response(self, user_text: str):
        """Llama a GPT, maneja respuesta y TTS, con Timestamps."""
        ts_process_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.debug(f"⏱️ TS:[{ts_process_start}] PROCESS_GPT START")

        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            logger.warning("⚠️ PROCESS_GPT Ignorado: llamada terminada o WS desconectado.")
            self.ignorar_stt = False # Asegurar reactivación si se aborta aquí
            return
        
        if not user_text:
             logger.warning("⚠️ PROCESS_GPT Texto de usuario vacío, saltando.")
             self.ignorar_stt = False # Asegurar reactivación
             return
             
        logger.info(f"🗣️ Mensaje para GPT: '{user_text}'")
        # Añadir a historial ANTES de la llamada
        self.conversation_history.append({"role": "user", "content": user_text}) 

        respuesta_gpt = "Lo siento, ocurrió un problema interno." # Default
        try:
            # --- Llamada a OpenAI ---
            start_gpt_call = self._now()
            ts_gpt_call_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.debug(f"⏱️ TS:[{ts_gpt_call_start}] PROCESS_GPT Calling generate_openai_response_main...")
            
            model_a_usar = config("CHATGPT_MODEL", default="gpt-4.1-mini") # Usar config con fallback
            mensajes_para_gpt = generate_openai_prompt(self.conversation_history)
            
            respuesta_gpt = await generate_openai_response_main( 
                history=mensajes_para_gpt, 
                model=model_a_usar 
            )
            
            gpt_duration_ms = (self._now() - start_gpt_call) * 1000
            ts_gpt_call_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.info(f"⏱️ TS:[{ts_gpt_call_end}] PROCESS_GPT Respuesta OpenAI recibida. ⏱️ DUR:[{gpt_duration_ms:.1f} ms]")
            # --- Fin Llamada a OpenAI ---

            if self.call_ended: return # Verificar después de llamada potencialmente larga

            if respuesta_gpt is None or not isinstance(respuesta_gpt, str):
                 logger.error(f"❌ PROCESS_GPT Respuesta inválida/nula de IA: {respuesta_gpt}")
                 respuesta_gpt = "Disculpe, no pude procesar eso." # Fallback específico

            reply_cleaned = respuesta_gpt.strip()

            # --- Manejar Respuesta (__END_CALL__ o Normal) ---
            if reply_cleaned == "__END_CALL__":
                ts_end_call = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
                logger.info(f"🚪 TS:[{ts_end_call}] PROCESS_GPT Protocolo cierre (__END_CALL__) por IA.")
                despedida_dicha = any(
                    gphrase.lower() in m.get("content", "").lower()
                    for m in self.conversation_history[-2:] 
                    if m.get("role") == "assistant"
                    for gphrase in ["gracias", "hasta luego", "placer atenderle", "excelente día"]
                ) 
                
                frase_final = ""
                if not despedida_dicha:
                    frase_final = GOODBYE_PHRASE
                    logger.info(f"💬 PROCESS_GPT Añadiendo despedida: '{frase_final}'")
                    self.conversation_history.append({"role": "assistant", "content": frase_final})
                else:
                     logger.info("   PROCESS_GPT IA ya se despidió, cerrando.")

                await self._play_audio_bytes(text_to_speech(frase_final) if frase_final else b"")
                await asyncio.sleep(0.5) 
                await self._shutdown(reason="AI Request (__END_CALL__)")
                return 

            # --- Respuesta Normal ---
            logger.info(f"🤖 Respuesta de GPT: {reply_cleaned}")
            self.conversation_history.append({"role": "assistant", "content": reply_cleaned})
            
            # --- Llamada a TTS ---
            start_tts_gen = self._now()
            ts_tts_gen_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.debug(f"⏱️ TS:[{ts_tts_gen_start}] PROCESS_GPT Calling TTS...")
            audio_para_reproducir = text_to_speech(reply_cleaned)
            tts_gen_duration_ms = (self._now() - start_tts_gen) * 1000
            ts_tts_gen_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            # --- Fin Llamada a TTS ---

            if audio_para_reproducir:
                 logger.info(f"🔊 TTS Generado OK. ⏱️ DUR:[{tts_gen_duration_ms:.1f}ms]. Procediendo a reproducir...")
                 await self._play_audio_bytes(audio_para_reproducir)
                 # Pausa post-TTS opcional
                 # await asyncio.sleep(0.2) 
                 # Enviar silencio post-TTS opcional
                 # await self._send_silence_chunk() 
            else:
                 logger.error(f"🔇 TS:[{ts_tts_gen_end}] Fallo al generar audio TTS.")
                 # No hay audio que reproducir, la función terminará y reactivará STT

        except asyncio.CancelledError:
            ts_cancel = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.info(f"🚫 TS:[{ts_cancel}] Tarea GPT cancelada.")
            # No relanzar, dejar que el finally del wrapper maneje la reactivación
        except Exception as e_gpt:
            ts_error = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.error(f"❌ TS:[{ts_error}] Error crítico durante GPT/TTS: {e_gpt}", exc_info=True)
            # Intentar reproducir mensaje de error
            try:
                 error_message = "Lo siento, ocurrió un error técnico."
                 if not self.conversation_history or "[ERROR]" not in self.conversation_history[-1].get("content",""):
                     self.conversation_history.append({"role": "assistant", "content": f"[ERROR] {error_message}"})
                 audio_error = text_to_speech(error_message)
                 if audio_error:
                     await self._play_audio_bytes(audio_error)
            except Exception as e_tts_error:
                 logger.error(f"❌ Error reproduciendo mensaje de error TTS: {e_tts_error}")
        finally:
             ts_process_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
             logger.debug(f"⏱️ TS:[{ts_process_end}] PROCESS_GPT END")


    # --- Funciones Auxiliares (Sin cambios mayores, solo logs con TS) ---

    async def _play_audio_bytes(self, audio_data: bytes):
        """Envía audio mu-law a Twilio, manejando estado y logs con TS."""
        ts_play_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        # logger.debug(f"⏱️ TS:[{ts_play_start}] PLAY_AUDIO START")
        
        if self.call_ended or not audio_data or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            logger.debug(f"   PLAY_AUDIO Ignorando: call_ended={self.call_ended}, no_data={not audio_data}, ws_bad_state={not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED}")
            return

        tts_start_pc = self._now() # Para medir duración total
        logger.info(f"🔊 Iniciando reproducción TTS ({len(audio_data)} bytes)...")
        
        acquired_lock = False
        try:
            ts_lock_acq_start = self._now()
            await self.speaking_lock.acquire()
            self.is_speaking = True
            acquired_lock = True
            # logger.debug(f"   PLAY_AUDIO Lock adquirido ({(self._now()-ts_lock_acq_start)*1000:.1f}ms), is_speaking = True")

            chunk_size = 320 
            sent_bytes = 0
            start_send_loop_pc = self._now()
            ts_send_loop_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.debug(f"⏱️ TS:[{ts_send_loop_start}] PLAY_AUDIO Loop de envío iniciado.")

            for offset in range(0, len(audio_data), chunk_size):
                if self.call_ended: 
                    logger.warning("🛑 Reproducción TTS interrumpida por fin de llamada.")
                    break 
                
                chunk = audio_data[offset : offset + chunk_size]
                media_message = json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode("utf-8")},
                })

                try:
                    await self.websocket.send_text(media_message)
                    sent_bytes += len(chunk)
                    await asyncio.sleep(chunk_size / 8000.0) # Espera tiempo real del chunk
                except Exception as e_send:
                     logger.error(f"❌ Error enviando chunk de audio a Twilio: {e_send}")
                     await self._shutdown(reason="WebSocket Send Error during TTS")
                     break

            loop_duration_pc = self._now() - start_send_loop_pc
            ts_send_loop_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            if not self.call_ended: 
                 logger.info(f"🔇 TS:[{ts_send_loop_end}] PLAY_AUDIO Fin reproducción. Enviados {sent_bytes} bytes. ⏱️ DUR_SEND_LOOP:[{loop_duration_pc*1000:.1f}ms]")

        except Exception as e_play:
             logger.error(f"❌ Error durante _play_audio_bytes: {e_play}", exc_info=True)
        finally:
            # Liberar el lock y resetear el flag
            if acquired_lock:
                self.is_speaking = False
                self.speaking_lock.release()
                # logger.debug("   PLAY_AUDIO Lock liberado, is_speaking = False")
            ts_play_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            total_play_dur_pc = self._now() - tts_start_pc
            logger.debug(f"⏱️ TS:[{ts_play_end}] PLAY_AUDIO END. ⏱️ DUR_TOTAL:[{total_play_dur_pc*1000:.1f}ms]")


    async def _send_silence_chunk(self):
        """Envía un pequeño chunk de silencio a Deepgram."""
        if self.stt_streamer and not self.call_ended and getattr(self.stt_streamer, '_started', False):
            try:
                silence_chunk = b"\xff" * 320 
                await self.stt_streamer.send_audio(silence_chunk)
            except Exception: 
                pass


    def _greeting(self):
        """Genera el saludo inicial."""
        try:
            now = get_cancun_time()
            h = now.hour
            if 5 <= h < 12: return "Buenos días, consultorio Dr. Alarcón. ¿Cómo puedo ayudarle?"
            if 12 <= h < 19: return "Buenas tardes, consultorio Dr. Alarcón. ¿Cómo le ayudo?"
            return "Buenas noches, consultorio Dr. Alarcón. ¿En qué le puedo servir?"
        except Exception as e_greet:
             logger.error(f"Error generando saludo: {e_greet}")
             return "Consultorio Doctor Wilfrido Alarcón, ¿cómo puedo ayudarle?" 


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
        """Cierra conexiones y tareas de forma ordenada."""
        ts_shutdown_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        if self.call_ended:
            logger.debug(f"⚠️ Intento de shutdown múltiple ignorado (Razón original: {self.shutdown_reason}). Nueva razón: {reason}")
            return
        
        self.call_ended = True
        self.shutdown_reason = reason 
        logger.info(f"🔻 TS:[{ts_shutdown_start}] SHUTDOWN Iniciando... Razón: {reason}")

        # Cancelar tareas
        tasks_to_cancel = {
            "PausaTimer": self.temporizador_pausa, 
            "GPTTask": self.current_gpt_task
        }
        for name, task in tasks_to_cancel.items():
            if task and not task.done():
                task.cancel()
                logger.debug(f"   SHUTDOWN Cancelando Tarea {name} ({task.get_name()})...")
                try:
                    # Esperar un poco para que la cancelación se propague
                    await asyncio.wait_for(task, timeout=0.2) 
                except asyncio.CancelledError:
                    logger.debug(f"   SHUTDOWN Tarea {name} cancelada exitosamente.")
                except asyncio.TimeoutError:
                     logger.warning(f"   SHUTDOWN Timeout esperando cancelación de Tarea {name}.")
                except Exception as e_cancel:
                     logger.error(f"   SHUTDOWN Error durante cancelación de Tarea {name}: {e_cancel}")
            # Limpiar referencia
            if name == "PausaTimer": self.temporizador_pausa = None
            if name == "GPTTask": self.current_gpt_task = None


        # Cerrar Deepgram
        if self.stt_streamer:
            logger.debug("   SHUTDOWN Cerrando Deepgram...")
            try:
                 await self.stt_streamer.close()
                 logger.info("✅ SHUTDOWN Conexión Deepgram cerrada.")
            except Exception as e_dg_close:
                 logger.error(f"❌ SHUTDOWN Error al cerrar Deepgram: {e_dg_close}")
            self.stt_streamer = None

        # Cerrar WebSocket
        await self._safe_close_websocket(code=1000, reason=f"Call ended: {reason}")

        # Limpiar estado
        self.conversation_history.clear()
        self.finales_acumulados.clear()
        ts_shutdown_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.info(f"🏁 TS:[{ts_shutdown_end}] SHUTDOWN Completado (Razón: {self.shutdown_reason}).")


    async def _safe_close_websocket(self, code: int = 1000, reason: str = "Closing"):
         """Cierra el WebSocket de forma segura."""
         ts_ws_close_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
         if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
             logger.debug(f"🚪 TS:[{ts_ws_close_start}] Cerrando WebSocket (Code: {code}, Reason: {reason[:100]})")
             try:
                 await self.websocket.close(code=code, reason=reason)
                 logger.info("✅ WebSocket cerrado.")
             except Exception as e_ws_close:
                 logger.warning(f"⚠️ Error al cerrar WebSocket (normal si ya estaba cerrado): {e_ws_close}")
         else:
              logger.debug(f"   WebSocket no conectado o inexistente al intentar cerrar.")
         self.websocket = None # Liberar referencia

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