# tw_utils.py
"""
WebSocket manager para Twilio <-> Deepgram <-> GPT‑4.1‑mini
----------------------------------------------------------------
Maneja la lógica de acumulación de transcripciones, interacción con GPT,
TTS, y el control del flujo de la llamada, incluyendo la gestión de timeouts
y la prevención de procesamiento de STT obsoleto.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Optional, List # Asegúrate de importar List y Optional

from fastapi import WebSocket
from starlette.websockets import WebSocketState

# Tus importaciones de módulos locales
# Asegúrate de que estas rutas sean correctas para tu estructura de proyecto
try:
    from aiagent import generate_openai_response_main
    from buscarslot import load_free_slots_to_cache
    from consultarinfo import load_consultorio_data_to_cache
    from deepgram_stt_streamer import DeepgramSTTStreamer
    from prompt import generate_openai_prompt
    from tts_utils import text_to_speech
    from utils import get_cancun_time
except ImportError as e:
    logging.error(f"Error importando módulos locales: {e}. Asegúrate que están en el PYTHONPATH.")
    # Podrías levantar el error o definir valores por defecto si es apropiado
    raise e

# --- Configuración de Logging ---
logger = logging.getLogger("tw_utils") # Usar "tw_utils" como nombre del logger
# Nivel de log: DEBUG para desarrollo/pruebas, INFO para producción
logger.setLevel(logging.DEBUG) 

# --- Constantes Configurables para Tiempos (en segundos) ---

# Tiempo de espera sin NINGUNA actividad de DG (ni parcial ni final) 
# antes de considerar que el usuario ha hecho una pausa significativa.
# Si expira y lo último fue un FINAL, se envía. Si fue un PARCIAL, se espera.
# AJUSTA ESTE VALOR según tus pruebas (rango sugerido: 1.5 a 2.5 segundos).
PAUSA_SIN_ACTIVIDAD_TIMEOUT = 2.0 

# Tiempo máximo absoluto sin NINGUNA actividad de DG antes de forzar
# el envío de lo acumulado (failsafe, evita que se quede "atascado" indefinidamente). 
# Debe ser mayor que PAUSA_SIN_ACTIVIDAD_TIMEOUT. (Ej. 7-10 segundos).
MAX_TIMEOUT_SIN_ACTIVIDAD = 8.0 

# --- Otras Constantes Globales ---
CURRENT_CALL_MANAGER: Optional["TwilioWebSocketManager"] = None
CALL_MAX_DURATION = 600 # Duración máxima de la llamada en segundos (10 minutos)
CALL_SILENCE_TIMEOUT = 30 # Silencio máximo del usuario antes de colgar (detectado por _monitor_call_timeout)
GOODBYE_PHRASE = "Fue un placer atenderle. Que tenga un excelente día. ¡Hasta luego!"

# --- Variable Global para Controlar Modo Prueba ---
# ¡¡Poner en False para operación normal con GPT!!
TEST_MODE_NO_GPT = False 

# --------------------------------------------------------------------------

class TwilioWebSocketManager:
    """
    Gestiona la lógica de una llamada telefónica individual, incluyendo
    STT con Deepgram, acumulación inteligente de texto, interacción con
    GPT y reproducción de TTS.
    """
    def __init__(self) -> None:
        """Inicializa el estado para una nueva llamada."""
        self.websocket: Optional[WebSocket] = None
        self.stt_streamer: Optional[DeepgramSTTStreamer] = None
        self.current_gpt_task: Optional[asyncio.Task] = None
        self.temporizador_pausa: Optional[asyncio.Task] = None # Único timer principal

        self.call_sid: str = "" # Identificador de la llamada de Twilio
        self.stream_sid: Optional[str] = None # Identificador del stream de audio
        self.call_ended: bool = False
        self.shutdown_reason: str = "N/A" # Razón por la que terminó la llamada
        
        # Flags de estado cruciales
        self.is_speaking: bool = False # ¿Está el TTS reproduciendo audio?
        self.ignorar_stt: bool = False # ¿Ignorar eventos de DG (durante GPT/TTS)?
        self.ultimo_evento_fue_parcial: bool = False # ¿El último evento de DG fue is_final=False?

        # Timestamps (usando perf_counter para intervalos precisos)
        now = self._now()
        self.stream_start_time: float = now
        self.last_activity_ts: float = now # Timestamp de la última actividad (parcial o final)
        self.last_final_ts: float = now # Timestamp del último evento is_final=true

        # Acumulación y conversación
        self.finales_acumulados: List[str] = [] # Aquí se guardan los textos de is_final=true
        self.conversation_history: List[dict] = []

        # Locks para concurrencia segura
        self.speaking_lock = asyncio.Lock() # Protege el acceso a self.is_speaking
        
        logger.debug(f"📞 Objeto TwilioWebSocketManager inicializado (ID: {id(self)})")






    def _now(self) -> float:
        """Devuelve el timestamp actual de alta precisión."""
        return time.perf_counter()






    def _reset_state_for_new_call(self):
        """Resetea variables de estado al inicio de una llamada."""
        # Cancelar tareas si quedaron de una llamada anterior (poco probable pero seguro)
        if self.temporizador_pausa and not self.temporizador_pausa.done():
            self.temporizador_pausa.cancel()
        if self.current_gpt_task and not self.current_gpt_task.done():
            self.current_gpt_task.cancel()
            
        self.current_gpt_task = None
        self.temporizador_pausa = None
        self.call_ended = False
        self.shutdown_reason = "N/A"
        self.is_speaking = False
        self.ignorar_stt = False
        self.ultimo_evento_fue_parcial = False
        now = self._now()
        # No reiniciar stream_start_time aquí, se reinicia al crear la instancia
        self.last_activity_ts = now
        self.last_final_ts = now
        self.finales_acumulados = []
        self.conversation_history = []
        logger.debug("🧽 Estado reseteado para nueva llamada.")






    # --- Manejador Principal del WebSocket ---
    
    async def handle_twilio_websocket(self, websocket: WebSocket):
        """Punto de entrada y bucle principal para manejar la conexión WebSocket de Twilio."""
        self.websocket = websocket
        try:
            await websocket.accept()
        except Exception as e_accept:
             logger.error(f"❌ Fallo al aceptar WebSocket: {e_accept}")
             return # No podemos continuar si falla el accept

        # Podrías intentar obtener el CallSid de los headers aquí si es posible/necesario
        # Ejemplo: self.call_sid = websocket.scope.get("headers", {}).get(b'x-twilio-callsid', b'').decode('utf-8')
        
        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = self # Registrar esta instancia como la activa
        logger.info(f"📞 Llamada conectada (WS aceptado). CallSid: {self.call_sid or 'Desconocido'}")
        
        self._reset_state_for_new_call() # Asegurar estado limpio

        # --- Precarga de Datos ---
        try:
            await asyncio.gather(
                asyncio.to_thread(load_free_slots_to_cache, 90),
                asyncio.to_thread(load_consultorio_data_to_cache)
            )
            logger.info("✅ Precarga de datos externos completada.")
        except Exception as e_preload:
            logger.warning(f"⚠️ Precarga de datos falló: {e_preload}", exc_info=False) # No mostrar stack trace completo si no es crítico

        # --- Iniciar Deepgram ---
        try:
            self.stt_streamer = DeepgramSTTStreamer(self._stt_callback)
            await self.stt_streamer.start_streaming()
            logger.info("✅ Deepgram STT iniciado y conectado.")
        except Exception as e_dg_start:
            logger.critical(f"❌ CRÍTICO: Deepgram no arrancó: {e_dg_start}", exc_info=True)
            await self._safe_close_websocket(code=1011, reason="STT Initialization Failed")
            CURRENT_CALL_MANAGER = None
            return

        # --- Tarea de Monitoreo (en segundo plano) ---
        monitor_task = asyncio.create_task(self._monitor_call_timeout(), name=f"MonitorTask_{self.call_sid or id(self)}")

        # --- Bucle Principal de Recepción ---
        try:
            while not self.call_ended:
                try:
                    raw = await websocket.receive_text()
                    data = json.loads(raw)
                except Exception as e_receive:
                    # Manejar errores específicos de recepción o desconexión
                    if "1000" in str(e_receive) or "1001" in str(e_receive) or "1006" in str(e_receive) or "close code" in str(e_receive).lower():
                         logger.warning(f"🔌 WebSocket desconectado por cliente o red: {e_receive}")
                         await self._shutdown(reason="WebSocket Closed Remotely")
                    else:
                         logger.error(f"❌ Error recibiendo del WebSocket: {e_receive}", exc_info=True)
                         await self._shutdown(reason=f"WebSocket Receive Error: {type(e_receive).__name__}")
                    break # Salir del bucle si hay error de recepción grave

                event = data.get("event")

                if event == "start":
                    self.stream_sid = data.get("streamSid")
                    start_data = data.get("start", {})
                    # Actualizar CallSid si no lo teníamos o si cambia
                    received_call_sid = start_data.get("callSid")
                    if received_call_sid and self.call_sid != received_call_sid:
                         self.call_sid = received_call_sid
                         logger.info(f"📞 CallSid actualizado a: {self.call_sid}")
                    logger.info(f"▶️ Evento 'start' recibido. StreamSid: {self.stream_sid}")
                    # Enviar saludo inicial
                    await self._play_audio_bytes(text_to_speech(self._greeting()))

                elif event == "media":
                    # Reenvío de audio a Deepgram (si no estamos ocupados)
                    if not self.ignorar_stt and not self.is_speaking:
                        payload = data.get("media", {}).get("payload")
                        if payload and self.stt_streamer:
                            try:
                                await self.stt_streamer.send_audio(base64.b64decode(payload))
                            except Exception as e_send_audio:
                                logger.error(f"Error enviando audio a Deepgram: {e_send_audio}")
                    # else: # Log muy verboso, mejor omitir
                        # pass # Ignorando audio

                elif event == "stop":
                    logger.info("🛑 Evento 'stop' recibido de Twilio.")
                    await self._shutdown(reason="Twilio Stop Event")
                    # break # _shutdown ya pone call_ended=True, el bucle terminará

                elif event == "mark":
                    mark_name = data.get("mark", {}).get("name")
                    logger.debug(f"🔹 Evento 'mark' recibido: {mark_name}")
                    # Aquí podríamos reactivar STT si usamos marks para sincronizar TTS
                    # if mark_name == 'TTS_END_MARK':
                    #     await self._reactivar_stt_despues_de_envio()
                    pass
                
                else:
                    logger.warning(f"❓ Evento WebSocket desconocido: {event}, Data: {str(data)[:200]}")

        except asyncio.CancelledError:
             logger.info("🚦 Tarea principal del WebSocket cancelada (normal durante cierre).")
        except Exception as e_main_loop:
            logger.error(f"❌ Error fatal en bucle principal WebSocket: {e_main_loop}", exc_info=True)
            await self._shutdown(reason=f"Main Loop Error: {type(e_main_loop).__name__}")
        finally:
            # Asegurar limpieza final
            if monitor_task and not monitor_task.done():
                monitor_task.cancel()
                logger.debug(" Cancelando tarea de monitoreo...")
            logger.info(f"🏁 Finalizado manejo de WebSocket para CallSid: {self.call_sid or 'N/A'}")
            # Desregistrar como manager activo al finalizar
            if CURRENT_CALL_MANAGER is self:
                CURRENT_CALL_MANAGER = None













    # --- Callback de Deepgram y Lógica de Acumulación ---

    def _stt_callback(self, transcript: str, is_final: bool):
        """
        Callback para Deepgram. Ignora si flag 'ignorar_stt' está activo.
        Actualiza estado, acumula finales, y reinicia timer de pausa.
        """
        if self.ignorar_stt:
            logger.warning(f"🚫 STT Ignorado (ignorar_stt=True): final={is_final}, text='{transcript[:60]}...'")
            return 

        ahora = self._now()
        
        if transcript and transcript.strip():
            # Actualizar timestamp de última actividad y si fue parcial
            self.last_activity_ts = ahora
            self.ultimo_evento_fue_parcial = not is_final 
            
            logger.debug(f"🎤 Actividad DG: final={is_final}, flag_parcial={self.ultimo_evento_fue_parcial}, text='{transcript.strip()[:60]}...'")

            if is_final:
                # Si es final, actualizar su timestamp y acumular
                self.last_final_ts = ahora 
                logger.info(f"📥 Final recibido (DG): '{transcript.strip()}'")
                self.finales_acumulados.append(transcript.strip())
            # else: # Loguear parciales puede ser útil en DEBUG
                # logger.debug(f"📊 Parcial: '{transcript.strip()[:60]}...'") 

            # Reiniciar el temporizador con cualquier actividad válida
            if self.temporizador_pausa and not self.temporizador_pausa.done():
                self.temporizador_pausa.cancel()
                # El log de cancelación está en la excepción de la tarea
            logger.debug(f"🔄 Reiniciando timer de pausa ({PAUSA_SIN_ACTIVIDAD_TIMEOUT}s) por actividad.")
            self.temporizador_pausa = asyncio.create_task(self._intentar_enviar_si_pausa(), name=f"PausaTimer_{self.call_sid or id(self)}")
        # else:
            # logger.debug("🔇 Recibido transcript vacío de DG.")













    async def _intentar_enviar_si_pausa(self):
        """
        Tarea que espera una pausa sin actividad y decide si enviar a GPT,
        considerando si lo último fue un parcial o final, y un timeout máximo.
        """
        # Usamos las constantes globales definidas al principio
        tiempo_espera_normal = PAUSA_SIN_ACTIVIDAD_TIMEOUT 
        timeout_maximo = MAX_TIMEOUT_SIN_ACTIVIDAD

        try:
            # Esperar el tiempo normal de pausa
            logger.debug(f"⏳ Esperando {tiempo_espera_normal:.1f}s de pausa total...")
            await asyncio.sleep(tiempo_espera_normal)
            
            # --- El temporizador de pausa normal se cumplió ---
            ahora = self._now()
            elapsed_activity = ahora - self.last_activity_ts
            # Asegurar que last_final_ts exista antes de usarlo
            elapsed_final = ahora - getattr(self, 'last_final_ts', ahora) 
            
            logger.debug(f"⌛ Timer de pausa ({tiempo_espera_normal:.1f}s) completado. Tiempo real desde última act: {elapsed_activity:.2f}s / desde último final: {elapsed_final:.2f}s")

            # Si la llamada terminó mientras esperábamos, salir.
            if self.call_ended:
                logger.debug("⚠️ Llamada finalizada durante espera de pausa. No se procesa.")
                return

            # ¿Hay algo acumulado para enviar?
            if not self.finales_acumulados:
                logger.debug(" Timer de pausa cumplido, pero sin finales acumulados para enviar.")
                # Reiniciar flag por si acaso quedó en True
                self.ultimo_evento_fue_parcial = False 
                return

            # --- Lógica de Decisión para Enviar ---
            
            # CONDICIÓN 1: Timeout Máximo (Failsafe)
            # Si ha pasado DEMASIADO tiempo desde la última actividad, forzamos envío.
            # Usamos >= para asegurar que se active si es exacto o mayor.
            if elapsed_activity >= timeout_maximo:
                logger.warning(f"⚠️ Timeout máximo ({timeout_maximo:.1f}s) alcanzado desde última actividad ({elapsed_activity:.2f}s). Forzando envío.")
                await self._proceder_a_enviar() 
                return

            # CONDICIÓN 2: Pausa Normal Detectada y Confirmada con Final
            # Si ha pasado el tiempo normal de pausa Y lo último recibido fue un FINAL
            # Comparamos elapsed_activity con el tiempo de espera normal
            if elapsed_activity >= (tiempo_espera_normal - 0.1) and not self.ultimo_evento_fue_parcial:
                logger.info(f"✅ Pausa normal ({tiempo_espera_normal:.1f}s) detectada después de FINAL. Procediendo a enviar.")
                await self._proceder_a_enviar() 
                return
                
            # CONDICIÓN 3: Pausa Normal Detectada pero Último fue Parcial
            # Si ha pasado el tiempo normal de pausa PERO lo último recibido fue un PARCIAL
            if elapsed_activity >= (tiempo_espera_normal - 0.1) and self.ultimo_evento_fue_parcial:
                logger.info(f"⏸️ Pausa normal ({tiempo_espera_normal:.1f}s) detectada después de PARCIAL. Esperando 'is_final=true' correspondiente...")
                # No enviamos. Esperamos que el 'is_final=true' llegue y reinicie este timer.
                # Si nunca llega, el TIMEOUT_MAXIMO_SIN_ACTIVIDAD (Condición 1)
                # debería eventualmente activarse en una futura comprobación de esta tarea
                # (si no es reiniciada antes por otra actividad).
                # O el CALL_SILENCE_TIMEOUT general podría actuar.
                return

            # Si el timer se cumplió pero elapsed es menor (raro) o ninguna condición se activó
            logger.debug(f" Timer de pausa cumplido, pero ninguna condición de envío activa (elapsed={elapsed_activity:.2f}s, ultimo_fue_parcial={self.ultimo_evento_fue_parcial}).")


        except asyncio.CancelledError:
            logger.debug("🛑 Timer de pausa cancelado/reiniciado (normal por nueva actividad)")
        except Exception as e:
            logger.error(f"❌ Error en _intentar_enviar_si_pausa: {e}", exc_info=True)
            # Limpiar estado para evitar bucles de error?
            # self.finales_acumulados.clear()
            # self.ultimo_evento_fue_parcial = False















    async def _proceder_a_enviar(self):
        """
        Lógica centralizada para preparar el mensaje acumulado,
        activar 'ignorar_stt', y llamar a la tarea de GPT/TTS.
        """
        if not self.finales_acumulados or self.call_ended or self.ignorar_stt:
             logger.debug(f" Ignorando _proceder_a_enviar: finales_empty={not self.finales_acumulados}, call_ended={self.call_ended}, ignorar_stt={self.ignorar_stt}")
             # Si ya estamos ignorando STT, significa que un envío anterior está en proceso. No iniciar otro.
             return 

        # 1. Preparar mensaje
        mensaje_acumulado = " ".join(self.finales_acumulados).replace("\n", " ").strip()
        if not mensaje_acumulado:
             logger.warning(" Mensaje acumulado resultó vacío después de unir/limpiar. No enviando.")
             self.finales_acumulados.clear() # Asegurar limpieza
             self.ultimo_evento_fue_parcial = False
             return
             
        logger.info(f"📦 Preparado para enviar (acumulados: {len(self.finales_acumulados)}): '{mensaje_acumulado}'")
        
        # Copiar y Limpiar estado ANTES de operaciones asíncronas largas
        finales_enviados = list(self.finales_acumulados) # Opcional, para logging si falla
        self.finales_acumulados.clear()
        self.ultimo_evento_fue_parcial = False 

        # 2. Activar modo "ignorar STT" ¡¡IMPORTANTE!!
        self.ignorar_stt = True
        logger.info("🚫 Activado: Ignorando nuevos eventos STT.")
        
        # Cancelar el timer de pausa por si acaso se reactivó de forma inesperada
        if self.temporizador_pausa and not self.temporizador_pausa.done():
            self.temporizador_pausa.cancel()
            logger.debug(" Cancelado timer de pausa justo antes de enviar.")
            self.temporizador_pausa = None # Asegurar que no quede referencia

        # 3. Ejecutar envío (GPT o Log) y reactivar STT al finalizar
        try:
            if TEST_MODE_NO_GPT:
                logger.info(f"🧪 MODO PRUEBA (SIN GPT): Mensaje sería: '{mensaje_acumulado}'")
                # En modo prueba, necesitamos reactivar STT manualmente después de simular el envío
                # Usamos create_task para no bloquear aquí si _reactivar... tuviera sleeps largos
                asyncio.create_task(self._reactivar_stt_despues_de_envio(), name=f"ReactivarSTT_Test_{self.call_sid or id(self)}")
            else:
                # Cancelar tarea GPT anterior (doble check)
                if self.current_gpt_task and not self.current_gpt_task.done():
                    logger.warning("⚠️ Cancelando tarea GPT anterior activa antes de enviar nueva.")
                    self.current_gpt_task.cancel()
                    try: await self.current_gpt_task
                    except asyncio.CancelledError: logger.debug(" Tarea GPT anterior cancelada.")
                    except Exception as e_gpt_cancel: logger.error(f" Error esperando cancelación de tarea GPT: {e_gpt_cancel}")
                    self.current_gpt_task = None

                # Iniciar la nueva tarea GPT que reactivará STT en su 'finally'
                logger.info(f"🌐 Iniciando tarea para GPT...")
                self.current_gpt_task = asyncio.create_task(
                    self.process_gpt_and_reactivate_stt(mensaje_acumulado), 
                    name=f"GPTTask_{self.call_sid or id(self)}"
                )
        except Exception as e_proc_env:
             logger.error(f"❌ Error al iniciar la tarea de envío/GPT: {e_proc_env}", exc_info=True)
             # Si falla aquí, STT podría quedar bloqueado. Intentar reactivar.
             await self._reactivar_stt_despues_de_envio()












    async def process_gpt_and_reactivate_stt(self, texto_para_gpt: str):
        """Wrapper seguro que llama a process_gpt_response y asegura reactivar STT."""
        try:
            await self.process_gpt_response(texto_para_gpt) 
        except Exception as e:
             # Loguear cualquier error de la tarea principal GPT/TTS
             logger.error(f"❌ Error capturado dentro de process_gpt_and_reactivate_stt: {e}", exc_info=True)
        finally:
            # ESTO SE EJECUTA SIEMPRE: al terminar ok o si hubo error
            logger.debug("🏁 Finalizando process_gpt_and_reactivate_stt. Procediendo a reactivar STT.")
            await self._reactivar_stt_despues_de_envio()














    async def _reactivar_stt_despues_de_envio(self):
        """Desactiva el flag 'ignorar_stt'."""
        # No añadir sleep aquí, ya que process_gpt_response incluye TTS que tiene sus propios sleeps.
        # Si se llama desde modo prueba, una pequeña pausa podría tener sentido.
        if TEST_MODE_NO_GPT:
            await asyncio.sleep(0.1) 

        if not self.call_ended: 
            if self.ignorar_stt:
                 self.ignorar_stt = False
                 logger.info("✅ Desactivado: Reactivando procesamiento de eventos STT.")
            else:
                 logger.debug(" Reactivación STT no necesaria (ignorar_stt ya era False).")
            # Reiniciar el temporizador de pausa AHORA para detectar silencio POST-TTS?
            # Podría ser útil para colgar si el usuario no responde después de la IA.
            # if self.temporizador_pausa and not self.temporizador_pausa.done():
            #     self.temporizador_pausa.cancel()
            # self.temporizador_pausa = asyncio.create_task(self._intentar_enviar_si_pausa())
            # logger.debug(" Reiniciado timer de pausa después de reactivar STT.")
        else:
             logger.debug(" Llamada ya terminó, no se reactiva STT.")











    async def process_gpt_response(self, user_text: str):
        """Llama a GPT, maneja la respuesta y llama a TTS."""
        # Doble check por si se llamó directamente de algún modo
        if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            logger.warning(" Ignorando process_gpt_response: llamada terminada o WS desconectado.")
            # Asegurarse de que STT no quede bloqueado si se entra aquí por error
            self.ignorar_stt = False 
            return
        
        if not user_text:
             logger.warning(" Texto de usuario vacío para GPT, saltando.")
             # Asegurarse de que STT no quede bloqueado
             self.ignorar_stt = False
             return
             
        logger.info(f"🗣️ Mensaje para GPT: '{user_text}'")
        self.conversation_history.append({"role": "user", "content": user_text}) 

        respuesta_gpt = "Lo siento, ocurrió un problema al procesar su solicitud." # Mensaje por defecto
        try:
            start_gpt = self._now()
            model_a_usar = "gpt-4.1-mini" # O config("CHATGPT_MODEL")
            mensajes_para_gpt = generate_openai_prompt(self.conversation_history)
            
            # Ejecutar la llamada a OpenAI en un hilo separado para no bloquear el bucle de eventos
            respuesta_gpt = await generate_openai_response_main( 
                history=mensajes_para_gpt, 
                model=model_a_usar 
            )
            logger.info(f"⏱️ Tiempo de respuesta OpenAI: {(self._now() - start_gpt)*1000:.1f} ms")

            if self.call_ended: return # Verificar de nuevo después de llamada bloqueante (aunque to_thread ayuda)

            if respuesta_gpt is None or not isinstance(respuesta_gpt, str):
                 logger.error(f"❌ Respuesta inválida/nula de generate_openai_response_main: {respuesta_gpt}")
                 respuesta_gpt = "Disculpe, tuve un inconveniente interno." # Fallback

            reply_cleaned = respuesta_gpt.strip()

            # --- Manejar Respuesta de GPT (__END_CALL__ o normal) ---
            if reply_cleaned == "__END_CALL__":
                logger.info("🚪 Protocolo de cierre (__END_CALL__) activado por IA.")
                # Revisar si ya nos despedimos
                despedida_dicha = any(
                    gphrase.lower() in m.get("content", "").lower()
                    for m in self.conversation_history[-2:] 
                    if m.get("role") == "assistant"
                    for gphrase in ["gracias", "hasta luego", "placer atenderle", "excelente día"]
                ) 
                
                frase_final = ""
                if not despedida_dicha:
                    frase_final = GOODBYE_PHRASE
                    logger.info(f"💬 Añadiendo despedida estándar: '{frase_final}'")
                    self.conversation_history.append({"role": "assistant", "content": frase_final})
                    # No añadir "__END_CALL__" a la historia conversacional
                else:
                     # Si ya se despidió, no reproducir nada más, solo cerrar.
                     logger.info(" IA ya se había despedido, cerrando directamente.")

                # Reproducir despedida si es necesario (play_audio_bytes maneja si hay datos)
                await self._play_audio_bytes(text_to_speech(frase_final) if frase_final else b"")
                await asyncio.sleep(0.5) # Pausa corta
                await self._shutdown(reason="AI Request (__END_CALL__)")
                return 

            # --- Respuesta Normal ---
            logger.info(f"🤖 Respuesta de GPT: {reply_cleaned}")
            self.conversation_history.append({"role": "assistant", "content": reply_cleaned})
            audio_para_reproducir = text_to_speech(reply_cleaned)
            
            if audio_para_reproducir:
                 await self._play_audio_bytes(audio_para_reproducir)
                 await asyncio.sleep(0.2) 
                 # await self._send_silence_chunk() # Opcional: enviar silencio post-TTS
            else:
                 logger.error("🔇 Fallo al generar audio TTS para la respuesta de GPT.")

        except asyncio.CancelledError:
            logger.info("🚫 Tarea GPT cancelada.")
            # No relanzar error aquí, simplemente terminar la tarea.
            # El flag 'ignorar_stt' se manejará en el 'finally' del wrapper.
        except Exception as e_gpt:
            logger.error(f"❌ Error crítico durante la llamada a GPT o TTS: {e_gpt}", exc_info=True)
            # Intentar reproducir un mensaje de error genérico
            try:
                 error_message = "Lo siento, estoy teniendo problemas técnicos. Intente llamar más tarde."
                 # Evitar añadir múltiples errores a la historia si esto falla repetidamente
                 if not self.conversation_history or "[ERROR]" not in self.conversation_history[-1].get("content",""):
                     self.conversation_history.append({"role": "assistant", "content": f"[ERROR] {error_message}"})
                 audio_error = text_to_speech(error_message)
                 if audio_error:
                     await self._play_audio_bytes(audio_error)
            except Exception as e_tts_error:
                 logger.error(f"❌ Error incluso al reproducir mensaje de error TTS: {e_tts_error}")
            # Considerar colgar automáticamente después de un error grave
            # await self._shutdown(reason="GPT/TTS Critical Error")

















    # --- Funciones Auxiliares (Mantenidas de tu versión, con pequeñas mejoras/logs) ---

    async def _play_audio_bytes(self, audio_data: bytes):
        """Envía audio mu-law a Twilio, manejando el estado 'is_speaking'."""
        if self.call_ended or not audio_data or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
            logger.debug(" Ignorando _play_audio_bytes: condiciones no cumplidas.")
            return

        tts_start_time = self._now()
        logger.info(f"🔊 Iniciando reproducción TTS ({len(audio_data)} bytes)...")
        
        # Asegurar que is_speaking se ponga a True ANTES de empezar el loop
        acquired_lock = False
        try:
            await self.speaking_lock.acquire()
            self.is_speaking = True
            acquired_lock = True
            logger.debug(" Lock adquirido, is_speaking = True")

            chunk_size = 320 # 40ms de audio (8000 * 1 * 0.040)
            sent_bytes = 0
            start_send_loop = self._now()

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
                    # Esperar el tiempo real del chunk
                    await asyncio.sleep(chunk_size / 8000.0) 
                except Exception as e_send:
                     logger.error(f"❌ Error enviando chunk de audio a Twilio: {e_send}")
                     # Si falla el envío, probablemente debamos parar
                     await self._shutdown(reason="WebSocket Send Error during TTS")
                     break

            if not self.call_ended: # Loguear solo si no fue interrumpido
                 logger.info(f"🔇 Finalizada reproducción TTS. Enviados {sent_bytes} bytes en {(self._now() - start_send_loop):.2f}s. Tiempo total TTS: {(self._now() - tts_start_time):.2f}s")

        except Exception as e_play:
             logger.error(f"❌ Error durante _play_audio_bytes: {e_play}", exc_info=True)
        finally:
            # Liberar el lock y resetear el flag
            if acquired_lock:
                self.is_speaking = False
                self.speaking_lock.release()
                logger.debug(" Lock liberado, is_speaking = False")






    async def _send_silence_chunk(self):
        """Envía un pequeño chunk de silencio a Deepgram."""
        if self.stt_streamer and not self.call_ended and getattr(self.stt_streamer, '_started', False):
            try:
                silence_chunk = b"\xff" * 320 # ~40ms 
                await self.stt_streamer.send_audio(silence_chunk)
            except Exception: # No es crítico si falla
                pass # logger.warning(f"⚠️ No se pudo enviar chunk de silencio a DG: {e_silence}")







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
             return "Consultorio Doctor Wilfrido Alarcón, ¿cómo puedo ayudarle?" # Fallback









    async def _monitor_call_timeout(self):
        """Tarea en segundo plano que monitoriza duración y silencio."""
        logger.info("⏱️ Iniciando monitor de timeouts...")
        while not self.call_ended:
            
            # Esperar antes de la siguiente verificación
            await asyncio.sleep(5) # Revisar cada 5 segundos
            
            if self.call_ended: break 

            now = self._now()
            
            # Timeout por duración máxima
            if now - self.stream_start_time >= CALL_MAX_DURATION:
                logger.warning(f"⏰ Duración máxima ({CALL_MAX_DURATION}s) excedida.")
                await self._shutdown(reason="Max Call Duration")
                break 

            # Timeout por silencio prolongado (basado en last_activity_ts)
            # Solo si no estamos esperando a GPT o hablando
            if not self.ignorar_stt and not self.is_speaking:
                if now - self.last_activity_ts >= CALL_SILENCE_TIMEOUT:
                    logger.warning(f"⏳ Silencio prolongado ({CALL_SILENCE_TIMEOUT}s) detectado (desde última act: {self.last_activity_ts:.1f}).")
                    await self._shutdown(reason="User Silence Timeout")
                    break
            # else:
                 # logger.debug(" Monitor: Ignorando chequeo de silencio (GPT/TTS activo).")

        logger.info(f"⏱️ Monitor de timeouts finalizado (CallEnded={self.call_ended}).")









    async def _shutdown(self, reason: str = "Unknown"):
        """Cierra conexiones y tareas de forma ordenada."""
        # Prevenir ejecuciones múltiples
        if self.call_ended:
            logger.debug(f"⚠️ Intento de shutdown múltiple ignorado (Razón anterior: {self.shutdown_reason}). Nueva razón: {reason}")
            return
        
        self.call_ended = True
        self.shutdown_reason = reason 
        logger.info(f"🔻 Iniciando cierre de llamada... Razón: {reason}")

        # Cancelar tareas principales que podrían estar activas
        tasks_to_cancel = [self.temporizador_pausa, self.current_gpt_task]
        for i, task in enumerate(tasks_to_cancel):
            if task and not task.done():
                task.cancel()
                logger.debug(f" Cancelando Tarea {i+1} ({task.get_name()})...")
                # Dar oportunidad a que la cancelación se procese
                try:
                    await asyncio.wait_for(task, timeout=0.5) 
                except asyncio.CancelledError:
                    logger.debug(f" Tarea {i+1} cancelada.")
                except asyncio.TimeoutError:
                     logger.warning(f" Timeout esperando cancelación de Tarea {i+1}.")
                except Exception as e_cancel:
                     logger.error(f" Error durante cancelación de Tarea {i+1}: {e_cancel}")

        # Cerrar Deepgram STT Streamer
        if self.stt_streamer:
            logger.debug(" Cerrando conexión Deepgram STT...")
            try:
                 await self.stt_streamer.close()
                 logger.info("✅ Conexión Deepgram cerrada.")
            except Exception as e_dg_close:
                 logger.error(f"❌ Error al cerrar Deepgram: {e_dg_close}")
            self.stt_streamer = None

        # Cerrar WebSocket de Twilio
        await self._safe_close_websocket(code=1000, reason=f"Call ended: {reason}")

        # Limpiar estado final (opcional, pero bueno para liberar referencias)
        self.conversation_history.clear()
        self.finales_acumulados.clear()
        logger.info(f"🏁 Cierre de llamada completado (Razón: {self.shutdown_reason}).")










    async def _safe_close_websocket(self, code: int = 1000, reason: str = "Closing"):
         """Cierra el WebSocket de forma segura."""
         if self.websocket and self.websocket.client_state == WebSocketState.CONNECTED:
             logger.debug(f"🚪 Cerrando WebSocket (Code: {code}, Reason: {reason[:100]})")
             try:
                 await self.websocket.close(code=code, reason=reason)
                 logger.info("✅ WebSocket cerrado.")
             except Exception as e_ws_close:
                 logger.warning(f"⚠️ Error al cerrar WebSocket (normal si ya estaba cerrado): {e_ws_close}")
         else:
              logger.debug(" WebSocket no conectado o inexistente, no se intenta cerrar.")
         # Liberar referencia
         self.websocket = None

# --- Función de ayuda para habilitar/deshabilitar logs DEBUG ---
# (Mantenida de tu versión)
def set_debug(active: bool = True) -> None:
    """Establece el nivel de logging para módulos clave."""
    level = logging.DEBUG if active else logging.INFO
    modules_to_set = ["tw_utils", "aiagent", "buscarslot", "consultarinfo", "tts_utils", "deepgram_stt_streamer"]
    # Asegurarse de que los loggers existan antes de intentar configurar nivel
    for name in modules_to_set:
         logging.getLogger(name).setLevel(level)
    # Configurar el logger de este módulo también
    logger.setLevel(level) 
    logger.info(f"Nivel de log establecido a {'DEBUG' if active else 'INFO'} para módulos relevantes.")

# --- Inicialización del Nivel de Log al Cargar el Módulo ---
# Puedes decidir aquí el nivel por defecto o usar una variable de entorno
# set_debug(True) # Activa DEBUG por defecto al iniciar
# set_debug(False) # Activa INFO por defecto al iniciar