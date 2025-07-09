# tw_utils.py
"""
WebSocket manager para Twilio <-> ElevenLabs <-> Llama 3.3
----------------------------------------------------------------
Maneja la lógica de acumulación de transcripciones, interacción con el Agente de IA,
TTS, y el control del flujo de la llamada.
"""

import asyncio
import base64
import json
import logging
import re
import time
import os
from datetime import datetime
from typing import Optional, List, Tuple
from decouple import config
from fastapi import WebSocket
from starlette.websockets import WebSocketState
from state_store import session_state
from eleven_http_client import send_tts_http_to_twilio
from eleven_ws_tts_client import ElevenLabsWSClient
from utils import terminar_llamada_twilio
import utils
from asyncio import run_coroutine_threadsafe
import collections.abc

# Tus importaciones de módulos locales
try:

    from aiagent import generate_ai_response
    from buscarslot import load_free_slots_to_cache
    from consultarinfo import load_consultorio_data_to_cache
    from deepgram_stt_streamer import DeepgramSTTStreamer
    from utils import get_cancun_time
except ImportError as e:
    logging.exception(f"CRÍTICO: Error importando módulos locales: {e}.")
    raise SystemExit(f"No se pudieron importar módulos necesarios: {e}")

# --- Configuración de Logging ---
logger = logging.getLogger("tw_utils")
logger.setLevel(logging.DEBUG)

# --- Formato para Timestamps ---
LOG_TS_FORMAT = "%H:%M:%S.%f"

# --- Constantes Configurables para Tiempos (en segundos) ---
PAUSA_SIN_ACTIVIDAD_TIMEOUT = .30
MAX_TIMEOUT_SIN_ACTIVIDAD = 5.0
LATENCY_THRESHOLD_FOR_HOLD_MESSAGE = 50
HOLD_MESSAGE_FILE = "audio/espera_1.wav"

# --- Otras Constantes Globales ---
CALL_MAX_DURATION = 600
CALL_SILENCE_TIMEOUT = 30
GOODBYE_PHRASE = "Fue un placer atenderle. ¡Hasta luego!"
TEST_MODE_NO_AI = False

CURRENT_CALL_MANAGER: Optional[object] = None


# --------------------------------------------------------------------------

class TwilioWebSocketManager:
    def __init__(self) -> None:
        """Inicializa el estado para una nueva llamada."""
        ts = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        logger.debug(f"⏱️ TS:[{ts}] INIT START")
        self.websocket: Optional[WebSocket] = None
        self.stt_streamer: Optional[DeepgramSTTStreamer] = None
        self.current_ai_task: Optional[asyncio.Task] = None
        self.temporizador_pausa: Optional[asyncio.Task] = None
        self.tts_timeout_task: Optional[asyncio.Task] = None
        self.audio_espera_task: Optional[asyncio.Task] = None
        self.finalizar_llamada_pendiente = False
        self.dg_tts_client = None
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
        self.last_final_stt_timestamp: Optional[float] = None
        logger.debug(f"⏱️ TS:[{ts_now_str}] INIT Timestamps set: start={self.stream_start_time:.2f}, activity={self.last_activity_ts:.2f}, final={self.last_final_ts:.2f}")

        self.twilio_terminated = False

        self.audio_buffer_twilio: List[bytes] = []
        self.audio_buffer_lock = asyncio.Lock()
        self.audio_buffer_max_bytes = 40000
        self.audio_buffer_current_bytes = 0
        self.hold_audio_task: Optional[asyncio.Task] = None

        self.finales_acumulados: List[str] = []
        self.conversation_history: List[dict] = []
        self.hold_audio_mulaw_bytes: bytes = b""


        try:
            if os.path.exists(HOLD_MESSAGE_FILE):
                with open(HOLD_MESSAGE_FILE, 'rb') as f:
                    raw = f.read()

                if raw[:4] == b"RIFF":
                    raw = raw[44:]

                self.hold_audio_mulaw_bytes = raw
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
            #logger.debug(f"⏱️ TS:[{ts}] RESET_STATE START")

            # Cancelar tareas si quedaron de una llamada anterior
            if self.temporizador_pausa and not self.temporizador_pausa.done():
                self.temporizador_pausa.cancel()
                logger.debug("   RESET_STATE: Temporizador pausa cancelado.")

            if self.current_ai_task and not self.current_ai_task.done():
                self.current_ai_task.cancel()
                logger.debug("   RESET_STATE: Tarea de IA cancelada.")

            self.current_ai_task = None

            self.temporizador_pausa = None
            self.call_ended = False
            self.shutdown_reason = "N/A"
            self.is_speaking = False
            self.ignorar_stt = False
            self.ultimo_evento_fue_parcial = False
            now = self._now()

            self.last_chunk_time = None
            self.stall_detector_task = None

            self.last_activity_ts = now
            self.last_final_ts = now
            self.finales_acumulados = []
            self.conversation_history = []
            #logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] RESET_STATE END")

    # --- Manejador Principal del WebSocket ---

    async def handle_twilio_websocket(self, websocket: WebSocket):
        """Punto de entrada y bucle principal."""
        ts_start_handle = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        #logger.info(f"⏱️ TS:[{ts_start_handle}] HANDLE_WS START")
        self.websocket = websocket
        try:
            await websocket.accept()
            ts_accept = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            #logger.info(f"⏱️ TS:[{ts_accept}] HANDLE_WS WebSocket accepted.")
        except Exception as e_accept:
             logger.error(f"❌ Fallo al aceptar WebSocket: {e_accept}")
             return

        global CURRENT_CALL_MANAGER
        CURRENT_CALL_MANAGER = self

        self._reset_state_for_new_call()


        # --- Crear el cliente Eleven Labs TTS WebSocket (una sola vez) ---
        try:
            from eleven_ws_tts_client import ElevenLabsWSClient
            self.dg_tts_client = ElevenLabsWSClient()

            # IMPORTANTE: Esperar a que el WebSocket se conecte completamente
            await asyncio.wait_for(self.dg_tts_client._ws_open.wait(), timeout=2.0)

            logger.debug("🔌 Elabs TTS WS abierto y conectado al iniciar la llamada.")
        except asyncio.TimeoutError:
            logger.error("❌ Timeout esperando conexión inicial de ElevenLabs WS")
            self.dg_tts_client = None
        except Exception as e_ws_init:
            logger.error(f"❌ No se pudo abrir el WS de Elabs TTS: {e_ws_init}")
            self.dg_tts_client = None


        # --- Precarga de Datos ---
        ts_preload_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        #logger.debug(f"⏱️ TS:[{ts_preload_start}] HANDLE_WS Preload Start")
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
        #logger.debug(f"⏱️ TS:[{ts_dg_start}] HANDLE_WS Deepgram Init Start")
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
            await self._shutdown(reason="STT Initialization Exception")
            return
       # --- Tarea de Monitoreo ---
        monitor_task = asyncio.create_task(self._monitor_call_timeout(), name=f"MonitorTask_{self.call_sid or id(self)}")
        #logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] HANDLE_WS Monitor task created.")

        # --- Bucle Principal de Recepción ---
        #logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] HANDLE_WS Entering main receive loop...")
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

                if event == "start":
                    self.stream_sid = data.get("streamSid")

                    # Generar el saludo
                    greeting_text = self._greeting()

                    # 🔇 Silenciar STT mientras hablamos
                    self.tts_en_progreso = True
                    self.ignorar_stt = True

                    # 🧹 Vacía el búfer de audio que Twilio pudiera tener
                    await self.websocket.send_text(json.dumps({
                        "event": "clear",
                        "streamSid": self.stream_sid
                    }))

                    # ▶️ Enviar TTS (ElevenLabs WS primero, ElevenLabs HTTP fallback)
                    async def _send_greet_chunk(chunk: bytes) -> None:
                        await self.websocket.send_text(json.dumps({
                            "event": "media",
                            "streamSid": self.stream_sid,
                            "media": { "payload": base64.b64encode(chunk).decode() },
                        }))
                        # ACTUALIZA EL TIMESTAMP DEL ÚLTIMO CHUNK
                        self.last_chunk_time = self._now()

                    async def _on_greet_end():
                        await self._reactivar_stt_despues_de_envio()

                    try:
                        # Si aún no existe el cliente, créalo (caso de error previo)
                        if not getattr(self, "dg_tts_client", None):
                            from eleven_ws_tts_client import ElevenLabsWSClient
                            self.dg_tts_client = ElevenLabsWSClient()
                            logger.debug("🔌 ElevenLabs TTS WS creado / recreado.")

                        ok = await self.dg_tts_client.speak(
                            greeting_text,
                            on_chunk=_send_greet_chunk,
                            on_end=_on_greet_end,
                            timeout_first_chunk=3.0,
                        )

                        if not ok:
                            raise RuntimeError("ElevenLabs WS tardó en dar el primer chunk")
                        else:
                            # INICIAR DETECTOR DE STALLS PARA EL SALUDO
                            self.stall_detector_task = asyncio.create_task(
                                self._start_stall_detector()
                            )

                    except Exception as e_dg_greet:
                        logger.error(f"ElevenLabs WS falló en saludo: {e_dg_greet}. Usando ElevenLabs HTTP.")

                        # ── 1) Cerrar con seguridad el WS de ElevenLabs si sigue abierto ─────────
                        try:
                            if getattr(self, "dg_tts_client", None):
                                await self.dg_tts_client.close()          # cierre limpio
                                self.dg_tts_client = None                 # se recreará luego
                        except Exception as e_close:
                            logger.debug(f"ElevenLabs TTS WS ya cerrado o falló al cerrar: {e_close}")

                        # ── 2) Fallback a ElevenLabs (HTTP) ────────────────────────────────────
                        logger.info("🔴 Fallback (saludo): Elabs WS no entregó audio a tiempo → se llama ElevenLabs HTTP.")
                        await send_tts_http_to_twilio(
                            text=greeting_text,
                            stream_sid=self.stream_sid,
                            websocket_send=self.websocket.send_text,
                        )

                        # ── 3) Reactivar STT tan pronto termine el envío del fallback ──────────
                        await self._reactivar_stt_despues_de_envio()

                    # Actualizar CallSid si aplica
                    start_data = data.get("start", {})
                    received_call_sid = start_data.get("callSid")
                    if received_call_sid and self.call_sid != received_call_sid:
                        self.call_sid = received_call_sid

                    logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] Saludo TTS enviado.")
                    logger.debug(f"⏱️ TS:[{datetime.now().strftime(LOG_TS_FORMAT)[:-3]}] HANDLE_WS Greeting TTS finished.")

                elif event == "media":
                    payload_b64 = data.get("media", {}).get("payload")
                    if not payload_b64:
                        continue

                    decoded_payload = base64.b64decode(payload_b64)
                    chunk_size = len(decoded_payload)

                    if self.ignorar_stt:
                        continue

                    if not self.stt_streamer or not self.stt_streamer._started:
                        async with self.audio_buffer_lock:
                            if self.audio_buffer_current_bytes + chunk_size <= self.audio_buffer_max_bytes:
                                self.audio_buffer_twilio.append(decoded_payload)
                                self.audio_buffer_current_bytes += chunk_size
                                logger.debug(
                                    f"🎙️ Audio bufferizado (STT inactivo). "
                                    f"Tamaño total: {self.audio_buffer_current_bytes} bytes."
                                )
                            else:
                                logger.warning("⚠️ Buffer de audio excedido. Chunk descartado.")
                        continue

                    try:
                        await self.stt_streamer.send_audio(decoded_payload)
                    except Exception as e_send_audio:
                        logger.error(f"❌ Error enviando audio a STT: {e_send_audio}")

                elif event == "stop":
                    logger.info(f"🛑 Evento 'stop' recibido de Twilio (TS:{datetime.now().strftime(LOG_TS_FORMAT)[:-3]})")
                    await self._shutdown(reason="Twilio Stop Event")

                elif event == "mark":
                    mark_name = data.get("mark", {}).get("name")
                    if mark_name == "end_of_tts":
                            self.ignorar_stt = False
                            logger.info("🔈 Fin de TTS, STT reactivado")

                    logger.debug(f"🔹 Evento 'mark' recibido: {mark_name} (TS:{datetime.now().strftime(LOG_TS_FORMAT)[:-3]})")

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
            logger.info(f"🏁 Iniciando bloque finally de handle_twilio_websocket. CallSid: {self.call_sid or 'N/A'}")

            if monitor_task and not monitor_task.done():
                logger.info("Cancelando tarea de monitoreo en finally...")
                monitor_task.cancel()
                try:
                    await asyncio.wait_for(monitor_task, timeout=0.5)
                except asyncio.TimeoutError:
                    logger.warning("Timeout (0.5s) esperando la cancelación de la tarea de monitoreo.")
                except asyncio.CancelledError:
                    logger.debug("Tarea de monitoreo ya estaba cancelada o se canceló a tiempo.")
                except Exception as e_cancel_mon:
                    logger.error(f"Error durante la espera de cancelación de la tarea de monitoreo: {e_cancel_mon}")

            if self.audio_espera_task and not self.audio_espera_task.done():
                logger.debug("🛑 Cancelando tarea de audio de espera en cleanup final...")
                self.audio_espera_task.cancel()
                try:
                    await asyncio.wait_for(self.audio_espera_task, timeout=0.5)
                except asyncio.TimeoutError:
                    logger.warning("⚠️ Timeout esperando cancelación de audio_espera_task.")
                except asyncio.CancelledError:
                    logger.debug("Tarea de audio de espera cancelada correctamente.")
                except Exception as e:
                    logger.error(f"❌ Error al cancelar audio_espera_task: {e}")
            self.audio_espera_task = None

            if not self.call_ended:
                logger.warning("Llamada no marcada como finalizada en finally de handle_twilio_websocket, llamando a _shutdown como precaución.")
                await self._shutdown(reason="Cleanup in handle_twilio_websocket finally")

            logger.info("📜 Historial completo de conversación enviado a IA:")
            for i, msg in enumerate(self.conversation_history):
                logger.info(f"[{i}] ({msg['role']}): {json.dumps(msg['content'], ensure_ascii=False)}")

            logger.info(f"🏁 Finalizado handle_twilio_websocket (post-finally). CallSid: {self.call_sid or 'N/A'}")

            if CURRENT_CALL_MANAGER is self:
                CURRENT_CALL_MANAGER = None

    async def _ensure_elevenlabs_connection(self):
        """
        Asegura que la conexión con ElevenLabs esté activa.
        Si está conectada, envía keepalive. Si no, la reconecta.
        """
        try:
            # Si no existe el cliente, créalo
            if not self.dg_tts_client:
                logger.info("🔌 Creando nuevo cliente ElevenLabs WS...")
                from eleven_ws_tts_client import ElevenLabsWSClient
                self.dg_tts_client = ElevenLabsWSClient()

                # Esperar conexión (máximo 500ms para no demorar mucho)
                await asyncio.wait_for(self.dg_tts_client._ws_open.wait(), timeout=0.5)
                logger.info("✅ ElevenLabs WS conectado y listo")
                return

            # Si existe, verificar si está conectado
            if self.dg_tts_client._ws and not self.dg_tts_client._ws.closed:
                # Conexión activa, enviar keepalive (texto vacío)
                try:
                    # Enviar un espacio para mantener viva la conexión
                    await self.dg_tts_client._ws.send(json.dumps({"text": " "}))
                    logger.debug("💓 Keepalive enviado a ElevenLabs")
                except Exception as e:
                    logger.warning(f"⚠️ Error enviando keepalive: {e}")
                    # Si falla el keepalive, intentar reconectar
                    await self._reconnect_elevenlabs()
            else:
                # Conexión cerrada o no establecida, reconectar
                logger.info("🔄 ElevenLabs WS cerrado, reconectando...")
                await self._reconnect_elevenlabs()

        except asyncio.TimeoutError:
            logger.error("⏰ Timeout conectando a ElevenLabs")
            self.dg_tts_client = None
        except Exception as e:
            logger.error(f"❌ Error en _ensure_elevenlabs_connection: {e}")
            self.dg_tts_client = None

    async def _reconnect_elevenlabs(self):
        """Cierra y reconecta el cliente de ElevenLabs"""
        # Cerrar conexión anterior si existe
        if self.dg_tts_client:
            try:
                await self.dg_tts_client.close()
            except Exception as e:
                logger.debug(f"Error cerrando ElevenLabs anterior: {e}")
            self.dg_tts_client = None

        # Crear nueva conexión
        from eleven_ws_tts_client import ElevenLabsWSClient
        self.dg_tts_client = ElevenLabsWSClient()

        # Esperar conexión
        try:
            await asyncio.wait_for(self.dg_tts_client._ws_open.wait(), timeout=0.5)
            logger.info("✅ ElevenLabs reconectado exitosamente")
        except asyncio.TimeoutError:
            logger.error("⏰ Timeout en reconexión de ElevenLabs")
            raise

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

        try:
            if self.stt_streamer and self.stt_streamer.dg_connection:
                logger.info("RECONEXIÓN DG: Cerrando conexión anterior de Deepgram antes de reintentar...")
                await self.stt_streamer.close()
        except Exception as e_close_old:
            logger.warning(f"RECONEXIÓN DG: Error cerrando instancia anterior de Deepgram (puede ser normal): {e_close_old}")

        self.stt_streamer = DeepgramSTTStreamer(
            callback=self._stt_callback,
            on_disconnect_callback=self._reconnect_deepgram_if_needed # ¡Importante!
        )

        await self.stt_streamer.start_streaming()

        if self.stt_streamer._started:
            logger.info("RECONEXIÓN DG: ✅ Reconexión a Deepgram exitosa.")

            async with self.audio_buffer_lock:
                buffered_audio = list(self.audio_buffer_twilio)
                self.audio_buffer_twilio.clear()
                self.audio_buffer_current_bytes = 0

            if buffered_audio:
                logger.info(f"RECONEXIÓN DG: Enviando {len(buffered_audio)} chunks de audio bufferizado…")
                for chunk in buffered_audio:
                    if self.stt_streamer and self.stt_streamer._started:
                        await self.stt_streamer.send_audio(chunk)
                    else:
                        logger.warning(
                            "RECONEXIÓN DG: Deepgram se desconectó durante el vaciado "
                            "del buffer. Re-bufferizando audio restante."
                        )
                        self.audio_buffer_twilio.extend(buffered_audio[buffered_audio.index(chunk):])
                        self.audio_buffer_current_bytes = sum(len(c) for c in self.audio_buffer_twilio)
                        break
                else:
                    logger.info("RECONEXIÓN DG: Buffer de audio enviado por completo.")
        else:
            logger.error("RECONEXIÓN DG: ❌ Falló la reconexión a Deepgram.")


    def _stt_callback(self, transcript: str, is_final: bool):
        """Callback de Deepgram con Timestamps y Lógica Mejorada."""
        ts_callback_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        #logger.debug(f"⏱️ TS:[{ts_callback_start}] STT_CALLBACK START (final={is_final})")

        if self.ignorar_stt:
            logger.debug(f"🚫 STT Ignorado (ignorar_stt=True): final={is_final}, text='{transcript[:60]}...' (TS:{ts_callback_start})")
            return

        ahora_pc = self._now()
        ahora_dt = datetime.now()

        if transcript and transcript.strip():
            self.last_activity_ts = ahora_pc
            self.ultimo_evento_fue_parcial = not is_final

            log_text_brief = transcript.strip()[:60] + ('...' if len(transcript.strip()) > 60 else '')
            #logger.debug(f"🎤 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Activity: final={is_final}, flag_parcial={self.ultimo_evento_fue_parcial}, text='{log_text_brief}'")

            if is_final:
                self.last_final_ts = ahora_pc
                self.last_final_stt_timestamp = ahora_pc
                #logger.info(f"📥 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Final Recibido: '{transcript.strip()}'")
                self.finales_acumulados.append(transcript.strip())
            else:
                 #logger.debug(f"📊 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Parcial: '{log_text_brief}'")
                 pass

            if self.temporizador_pausa and not self.temporizador_pausa.done():
                logger.debug(f"   STT_CALLBACK Cancelling existing pause timer...")
                self.temporizador_pausa.cancel()

            #logger.debug(f"⏱️ TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Reiniciando timer de pausa ({PAUSA_SIN_ACTIVIDAD_TIMEOUT}s).")
            self.temporizador_pausa = asyncio.create_task(self._intentar_enviar_si_pausa(), name=f"PausaTimer_{self.call_sid or id(self)}")
        else:
             logger.debug(f"🔇 TS:[{ahora_dt.strftime(LOG_TS_FORMAT)[:-3]}] STT_CALLBACK Recibido transcript vacío.")


    async def _intentar_enviar_si_pausa(self):
        """Tarea que espera pausa y decide si enviar, con Timestamps."""
        ts_intento_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        #logger.debug(f"⏱️ TS:[{ts_intento_start}] INTENTAR_ENVIAR START")

        tiempo_espera = PAUSA_SIN_ACTIVIDAD_TIMEOUT
        timeout_maximo = MAX_TIMEOUT_SIN_ACTIVIDAD

        try:
            #logger.debug(f"⏳ Esperando {tiempo_espera:.1f}s de pausa total...")
            await asyncio.sleep(tiempo_espera)

            ts_sleep_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            ahora = self._now()
            elapsed_activity = ahora - self.last_activity_ts
            elapsed_final = ahora - getattr(self, 'last_final_ts', ahora)

            #logger.debug(f"⌛ TS:[{ts_sleep_end}] INTENTAR_ENVIAR Timer completado. Tiempo real desde últ_act: {elapsed_activity:.2f}s / desde últ_final: {elapsed_final:.2f}s")

            if self.call_ended:
                logger.debug("⚠️ INTENTAR_ENVIAR: Llamada finalizada durante espera. Abortando.")
                return

            if not self.finales_acumulados:
                #logger.debug("⏸️ INTENTAR_ENVIAR: Timer cumplido, pero sin finales acumulados.")
                self.ultimo_evento_fue_parcial = False
                return

            if elapsed_activity >= timeout_maximo:
                logger.warning(f"⚠️ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Timeout máximo ({timeout_maximo:.1f}s) alcanzado (elapsed={elapsed_activity:.2f}s). Forzando envío.")
                await self._proceder_a_enviar()
                return

            if elapsed_activity >= (tiempo_espera - 0.1) and not self.ultimo_evento_fue_parcial:
                #logger.info(f"✅ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Pausa normal ({tiempo_espera:.1f}s) detectada después de FINAL. Procediendo.")
                await self._proceder_a_enviar()
                return

            if elapsed_activity >= (tiempo_espera - 0.1) and self.ultimo_evento_fue_parcial:
                #logger.info(f"⏸️ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Pausa normal ({tiempo_espera:.1f}s) detectada después de PARCIAL. Esperando 'is_final=true' correspondiente...")
                return

            logger.debug(f"❔ TS:[{ts_sleep_end}] INTENTAR_ENVIAR: Timer cumplido, pero ninguna condición de envío activa.")

        except asyncio.CancelledError:
            ts_cancel = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.debug(f"🛑 TS:[{ts_cancel}] INTENTAR_ENVIAR: Timer de pausa cancelado/reiniciado (normal).")
        except Exception as e:
            ts_error = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.error(f"❌ TS:[{ts_error}] Error en _intentar_enviar_si_pausa: {e}", exc_info=True)


    async def _proceder_a_enviar(self):
        """Prepara y envía acumulados, activa 'ignorar_stt' y lanza la tarea de IA."""

        mensaje = await self._preparar_mensaje_para_ia()
        if not mensaje:
            return

        # ⏱️ Medición: cuánto tiempo pasó desde el último is_final
        ahora_pc = self._now()
        if hasattr(self, "last_final_stt_timestamp"):
            delta_ms = (ahora_pc - self.last_final_stt_timestamp) * 1000
            logger.info(f"⏱️ [LATENCIA-1] STT final → AI call: {delta_ms:.1f} ms")

        await self._activar_modo_ignorar_stt()

        # 🆕 PREPARAR CONEXIÓN ELEVENLABS EN PARALELO CON LA IA
        elevenlabs_task = asyncio.create_task(self._ensure_elevenlabs_connection())

        await self._iniciar_tarea_ia(mensaje, self.last_final_stt_timestamp)

        # Esperar que ElevenLabs esté listo (máximo 100ms adicionales)
        try:
            await asyncio.wait_for(elevenlabs_task, timeout=0.1)
        except asyncio.TimeoutError:
            logger.warning("⚠️ Timeout esperando preparación de ElevenLabs")
        except Exception as e:
            logger.error(f"❌ Error preparando ElevenLabs: {e}")


    async def _preparar_mensaje_para_ia(self) -> Optional[str]:
        """Valida si hay finales, construye mensaje y limpia buffers si es inválido."""

        if not self.finales_acumulados or self.call_ended or self.ignorar_stt:
            logger.warning(
                f"⚠️ PROCEDER_ENVIAR Abortado: finales_empty={not self.finales_acumulados}, "
                f"call_ended={self.call_ended}, ignorar_stt={self.ignorar_stt}"
            )
            if not self.finales_acumulados:
                self.last_final_stt_timestamp = None
            return None

        mensaje = " ".join(self.finales_acumulados).replace("\n", " ").strip()
        if not mensaje:
            logger.warning("⚠️ PROCEDER_ENVIAR: Mensaje acumulado vacío. Abortando.")
            self.finales_acumulados.clear()
            self.ultimo_evento_fue_parcial = False
            self.last_final_stt_timestamp = None
            return None

        # Limpiar buffers de texto antes de enviar
        self.finales_acumulados.clear()
        self.ultimo_evento_fue_parcial = False
        return mensaje


    async def _activar_modo_ignorar_stt(self):
        """Activa ignorar_stt y cancela temporizador de pausa si existe."""

        self.ignorar_stt = True
        logger.info("🚫 PROCEDER_ENVIAR: Activado ignorar_stt=True")

        if self.temporizador_pausa and not self.temporizador_pausa.done():
            self.temporizador_pausa.cancel()
            logger.debug("🕒 PROCEDER_ENVIAR: Temporizador de pausa cancelado.")
            self.temporizador_pausa = None


    async def _iniciar_tarea_ia(self, mensaje: str, ts_final: Optional[float]):
        """Cancela tarea de IA anterior (si aplica), programa el audio-espera y lanza la nueva tarea de IA."""

        if self.current_ai_task and not self.current_ai_task.done():
            logger.warning("⚠️ Tarea de IA anterior aún activa. Cancelando…")
            self.current_ai_task.cancel()
            try:
                await asyncio.wait_for(self.current_ai_task, timeout=0.5)
            except asyncio.CancelledError:
                logger.debug("🧹 Tarea de IA cancelada exitosamente.")
            except Exception as e:
                logger.error(f"❌ Error al cancelar tarea de IA previa: {e}")
            self.current_ai_task = None

        if self.audio_espera_task and not self.audio_espera_task.done():
            self.audio_espera_task.cancel()

        if ts_final is not None:
            self.audio_espera_task = asyncio.create_task(
                self._iniciar_temporizador_audio_espera(ts_final),
                name=f"HoldAudioTimer_{self.call_sid or id(self)}"
            )

        logger.debug("🚀 Iniciando nueva tarea de IA…")
        self.current_ai_task = asyncio.create_task(
            self.process_ai_response_wrapper(mensaje, ts_final),
            name=f"AITask_{self.call_sid or id(self)}"
        )


    async def process_ai_response_wrapper(self, texto_para_ia: str, last_final_ts: Optional[float]):
        """Wrapper seguro que llama a process_ai_response y asegura reactivar STT."""
        ts_wrapper_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
        try:
            await self.process_ai_response(texto_para_ia, last_final_ts)
        except Exception as e:
            logger.error(f"❌ Error capturado dentro de process_ai_response_wrapper: {e}", exc_info=True)
        finally:
            ts_wrapper_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.debug(f"🏁 TS:[{ts_wrapper_end}] PROCESS_AI_WRAPPER Finalizado. STT seguirá desactivado hasta isFinal de TTS")

    async def _timeout_reactivar_stt(self, segundos: float):
        """
        Espera 'segundos'.
        Si al terminar todavía estamos en modo TTS (self.tts_en_progreso),
        asume que ElevenLabs nunca envió isFinal y reactiva el STT.
        """
        try:
            await asyncio.sleep(segundos)
            if self.call_ended:
                return

            if self.tts_en_progreso:
                logger.warning(f"[TTS-TIMEOUT] Pasaron {segundos:.1f}s sin isFinal; "
                               "reactivando STT por seguridad.")
                self.tts_en_progreso = False
                await self._reactivar_stt_despues_de_envio()
        except asyncio.CancelledError:
            pass


    async def _send_mark_end_of_tts(self) -> None:
        """Envía explícitamente el evento mark end_of_tts a Twilio."""
        if self.websocket.application_state == WebSocketState.CONNECTED:
            await self.websocket.send_text(json.dumps({
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": "end_of_tts"}
            }))


    async def _reactivar_stt_despues_de_envio(self):
        """Limpia buffers, informa a Twilio que terminó el TTS y re‑habilita STT."""
        log_prefix = f"ReactivarSTT_{self.call_sid}"

        if self.stall_detector_task:
            self.stall_detector_task.cancel()
            try:
                await self.stall_detector_task
            except asyncio.CancelledError:
                pass
            self.stall_detector_task = None
        self.last_chunk_time = None

        try:
            await self._send_mark_end_of_tts()
        except Exception as e:
            logger.debug(f"[{log_prefix}] No se pudo enviar mark end_of_tts: {e}")

        async with self.audio_buffer_lock:
            bytes_descartados = self.audio_buffer_current_bytes
            self.audio_buffer_twilio.clear()
            self.audio_buffer_current_bytes = 0

        self.finales_acumulados.clear()

        logger.info(f"🧹 Buffer de audio vaciado: {bytes_descartados} bytes descartados.")

        self.last_final_stt_timestamp = None

        self.ignorar_stt = False
        self.tts_en_progreso = False
        logger.info(f" 🟢 STT reactivado (ignorar_stt=False).")

        if self.tts_timeout_task and not self.tts_timeout_task.done():
            self.tts_timeout_task.cancel()
        self.tts_timeout_task = None


    async def _start_stall_detector(self):
        """Monitorea si el envío de chunks se detiene"""
        #logger.debug("🚦 Iniciando detector de stalls TTS...")
        while not self.call_ended and self.tts_en_progreso:
            ahora = self._now()
            if self.last_chunk_time and (ahora - self.last_chunk_time) > 0.3:  # 300ms
                logger.warning("🚨 STALL DETECTED! No hay chunks recientes, reactivando STT")
                await self._reactivar_stt_despues_de_envio()
                break
            await asyncio.sleep(0.05)


    async def _reactivar_stt_si_posible(self, log_prefix: str):
        """Reactiva STT si no está activo, y Deepgram está operativo."""
        if not self.stt_streamer or not self.stt_streamer._started or self.stt_streamer._is_closing:
            logger.warning(f"[{log_prefix}] ⚠️ No se puede reactivar STT: Deepgram no operativo.")
            return

        if self.ignorar_stt:
            self.ignorar_stt = False
            self.tts_en_progreso = False
            logger.info(f"[{log_prefix}] 🟢 STT reactivado (ignorar_stt=False).")
        else:
            logger.debug(f"[{log_prefix}] STT ya estaba activo (ignorar_stt=False).")


    async def process_ai_response(
            self,
            user_text: str,
            last_final_ts: Optional[float],
        ):
            """
            Orquestador principal: Llama al nuevo motor de IA nativo para Llama 3.3.
            """
            if self.call_ended or not self.websocket or self.websocket.client_state != WebSocketState.CONNECTED:
                logger.warning("⚠️ PROCESS_AI ignorado: llamada terminada / WS cerrado")
                return
            if not user_text:
                logger.warning("⚠️ PROCESS_AI texto vacío; abortando")
                return

            logger.info("🗣️ Usuario → IA: “%s”", user_text)
            self.conversation_history.append({"role": "user", "content": user_text})

            session_id = self.call_sid

            try:
                respuesta_texto = await generate_ai_response(
                    session_id=session_id,
                    history=self.conversation_history
                )
            except Exception as e:
                logger.error(f"❌ Error en generate_ai_response: {e}", exc_info=True)
                respuesta_texto = "Disculpe, tuve un problema técnico. ¿Podría repetir?"

            if respuesta_texto == "__END_CALL__":
                logger.info("🔚 IA solicitó colgar")
                await utils.cierre_con_despedida(self, reason="assistant_request", delay=5.0)
                return

            self.conversation_history.append(
                {"role": "assistant", "content": respuesta_texto}
            )

            await self.handle_tts_response(respuesta_texto, last_final_ts)


    async def handle_tts_response(self, texto: str, last_final_ts: Optional[float]):
        """Convierte respuesta IA a TTS con ElevenLabs WS + fallback a ElevenLabs HTTP"""
        if self.call_ended:
            logger.warning("🔇 handle_tts_response abortado: llamada terminada.")
            return

        try:
            if self.audio_espera_task and not self.audio_espera_task.done():
                self.audio_espera_task.cancel()
                self.audio_espera_task = None

            self.tts_en_progreso = True
            self.ignorar_stt     = True

            duracion_max = estimar_duracion_tts(texto)
            if self.tts_timeout_task and not self.tts_timeout_task.done():
                self.tts_timeout_task.cancel()
            self.tts_timeout_task = asyncio.create_task(
                self._timeout_reactivar_stt(duracion_max),
                name=f"TTS_TO_{self.call_sid or id(self)}"
            )

            await self.websocket.send_text(json.dumps({
                "event": "clear",
                "streamSid": self.stream_sid
            }))

            logger.info(f"⏱️ [LATENCIA-3-START] TTS request iniciado para: '{texto[:30]}...'")
            ts_tts_start = self._now()

            async def _send_chunk(chunk: bytes):
                await self.websocket.send_text(json.dumps({
                    "event": "media",
                    "streamSid": self.stream_sid,
                    "media": {
                        "payload": base64.b64encode(chunk).decode()
                    },
                }))
                self.last_chunk_time = self._now()

            try:
                ok = await self.dg_tts_client.speak(
                    texto,
                    on_chunk=_send_chunk,
                    on_end=self._reactivar_stt_despues_de_envio,
                    timeout_first_chunk=3.0,
                )

                if not ok:
                    raise RuntimeError("ElevenLabs WS tardó demasiado en dar el primer chunk")

                self.stall_detector_task = asyncio.create_task(
                    self._start_stall_detector()
                )

                ts_tts_end = self._now()
                logger.info(
                    f"📦 ElevenLabs WS TTS→Twilio emitido en {(ts_tts_end - ts_tts_start) * 1000:.1f} ms"
                )

            except Exception as e_dg:
                logger.error(f"ElevenLabs WS falló: {e_dg}. Cambiando a ElevenLabs HTTP.")
                logger.info("🔴 Fallback (respuesta): ElevenLabs WS no entregó audio a tiempo → se llama ElevenLabs HTTP.")
                ts_tts_start = self._now()
                await send_tts_http_to_twilio(
                    text=texto,
                    stream_sid=self.stream_sid,
                    websocket_send=self.websocket.send_text,
                )
                ts_tts_end = self._now()
                logger.info(
                    f"📦 ElevenLabs HTTP TTS→Twilio emitido en {(ts_tts_end - ts_tts_start) * 1000:.1f} ms"
                )
                await self._reactivar_stt_despues_de_envio()

        except asyncio.CancelledError:
            logger.info("🚫 handle_tts_response cancelado (normal en shutdown).")
        except Exception as e:
            logger.error(f"❌ Error en handle_tts_response: {e}", exc_info=True)
            await self._reactivar_stt_despues_de_envio()


    async def should_play_hold_audio(self, last_final_ts: Optional[float]) -> bool:
        """Devuelve True si la latencia excede el umbral y hay audio de espera cargado."""
        if last_final_ts is None or self.call_ended:
            return False

        now = self._now()
        real_latency = now - last_final_ts
        threshold = LATENCY_THRESHOLD_FOR_HOLD_MESSAGE

        if real_latency > threshold:
            if self.hold_audio_mulaw_bytes:
                logger.info(f"⏱️ Latencia {real_latency:.2f}s > umbral {threshold}s → se usará mensaje de espera.")
                return True
            else:
                logger.warning("⚠️ Latencia alta, pero no hay mensaje de espera cargado.")
        return False


    async def _iniciar_temporizador_audio_espera(self, ts_final: Optional[float]):
        """
        Inicia un temporizador para reproducir el audio de espera si IA tarda mucho.
        Se cancela automáticamente si TTS comienza antes del umbral.
        """
        if ts_final is None:
            return

        threshold = LATENCY_THRESHOLD_FOR_HOLD_MESSAGE
        try:
            await asyncio.sleep(threshold)
            if self.call_ended or self.tts_en_progreso:
                return
            if await self.should_play_hold_audio(ts_final):
                await self._play_audio_bytes(self.hold_audio_mulaw_bytes)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Error en temporizador de audio de espera: {e}", exc_info=True)


    async def _play_audio_bytes(self, pcm_ulaw_bytes: bytes) -> None:
            """
            Envía audio μ-law (8 kHz, mono) al <Stream> de Twilio.
            """
            if not pcm_ulaw_bytes or not self.websocket or not self.stream_sid:
                return

            self.is_speaking = True
            CHUNK = 160
            total_sent = 0

            try:
                for i in range(0, len(pcm_ulaw_bytes), CHUNK):
                    if self.call_ended:
                        break

                    chunk = pcm_ulaw_bytes[i:i + CHUNK]

                    await self.websocket.send_json({
                        "streamSid": self.stream_sid,
                        "event": "media",
                        "media": {
                            "payload": base64.b64encode(chunk).decode("ascii")
                        }
                    })

                    total_sent += len(chunk)
                    await asyncio.sleep(0.02)

                logger.info(f"🔊 PLAY_AUDIO Fin reproducción. Enviados {total_sent} bytes.")
            finally:
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

            await asyncio.sleep(5)

            if self.call_ended: break

            now_pc = self._now()
            now_dt_str = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            #logger.debug(f"⏱️ TS:[{now_dt_str}] MONITOR Check...")

            call_duration = now_pc - self.stream_start_time
            if call_duration >= CALL_MAX_DURATION:
                logger.warning(f"⏰ TS:[{now_dt_str}] MONITOR Duración máxima ({CALL_MAX_DURATION}s) excedida (actual: {call_duration:.1f}s).")
                await self._shutdown(reason="Max Call Duration")
                break

            if not self.ignorar_stt and not self.is_speaking:
                silence_duration = now_pc - self.last_activity_ts
                if silence_duration >= CALL_SILENCE_TIMEOUT:
                    logger.warning(f"⏳ TS:[{now_dt_str}] MONITOR Silencio prolongado ({CALL_SILENCE_TIMEOUT}s) detectado (actual: {silence_duration:.1f}s).")
                    await self._shutdown(reason="User Silence Timeout")
                    break

        logger.info(f"⏱️ MONITOR Finalizado (CallEnded={self.call_ended}).")


    async def _shutdown(self, reason: str = "Unknown"):
            """Cierra conexiones y tareas de forma ordenada, con timeouts."""
            if self.call_ended and self.shutdown_reason != "N/A":
                logger.info(f"Intento de shutdown múltiple ignorado. Razón original: {self.shutdown_reason}. Nueva razón: {reason}")
                return

            ts_shutdown_start = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            if not self.call_ended or (self.call_ended and self.shutdown_reason == "N/A"):
                 logger.info(f"🔻 TS:[{ts_shutdown_start}] SHUTDOWN Iniciando... Razón: {reason}")
                 self.call_ended = True

            if self.shutdown_reason == "N/A":
                self.shutdown_reason = reason

            tasks_to_cancel_map = {
                "PausaTimer": "temporizador_pausa",
                "AITask": "current_ai_task"
            }

            for task_name_log, attr_name in tasks_to_cancel_map.items():
                task_instance = getattr(self, attr_name, None)
                if task_instance and not task_instance.done():
                    logger.debug(f"🔴 SHUTDOWN: Cancelando Tarea {task_name_log}...")
                    task_instance.cancel()
                    try:
                        await asyncio.wait_for(task_instance, timeout=0.1)
                    except asyncio.TimeoutError:
                        logger.debug(f"Timeout breve esperando cancelación de {task_name_log}.")
                    except asyncio.CancelledError:
                        logger.debug(f"Tarea {task_name_log} ya cancelada.")
                    except Exception:
                        pass
                setattr(self, attr_name, None)

            if self.stt_streamer:
                logger.debug("   SHUTDOWN: Llamando a stt_streamer.close() explícitamente...")
                try:
                    await self.stt_streamer.close()
                    logger.info("✅ SHUTDOWN: stt_streamer.close() invocado (o ya estaba cerrado/en proceso).")
                except Exception as e:
                    logger.error(f"❌ SHUTDOWN: Error en la llamada final a stt_streamer.close(): {e}", exc_info=True)
                finally:
                    self.stt_streamer = None

            if getattr(self, "dg_tts_client", None):
                try:
                    await self.dg_tts_client.close()
                except Exception:
                    pass

            await self._safe_close_websocket(code=1000, reason=self.shutdown_reason)

            self.conversation_history.clear()
            self.finales_acumulados.clear()
            if hasattr(self, 'audio_buffer_twilio'):
                async with self.audio_buffer_lock:
                    self.audio_buffer_twilio.clear()
                    self.audio_buffer_current_bytes = 0

            ts_shutdown_end = datetime.now().strftime(LOG_TS_FORMAT)[:-3]
            logger.info(f"🏁 TS:[{ts_shutdown_end}] SHUTDOWN Completado (Razón: {self.shutdown_reason}).")

            if self.call_sid and not self.twilio_terminated:
                try:
                    await terminar_llamada_twilio(self.call_sid)
                    self.twilio_terminated = True
                except Exception as e:
                    logger.error(f"❌ Error al finalizar llamada con Twilio: {e}")
            elif not self.call_sid:
                logger.warning("⚠️ SHUTDOWN: No se encontró call_sid para finalizar la llamada en Twilio.")
            else:
                logger.info("ℹ️ SHUTDOWN: La llamada ya había sido finalizada en Twilio.")


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


def estimar_duracion_tts(texto: str) -> float:
    # 13 caracteres ≈ 1 seg a 1.2 × (empírico)
    secs = len(texto) / 13 / 1.2
    return secs * 1.5 + 5


# --- Función de ayuda para nivel de log ---
def set_debug(active: bool = True) -> None:
    """Establece el nivel de logging para módulos clave."""
    level = logging.DEBUG if active else logging.INFO
    modules_to_set = ["tw_utils", "aiagent", "buscarslot", "consultarinfo", "deepgram_stt_streamer"]
    for name in modules_to_set:
         logging.getLogger(name).setLevel(level)
    logger.info(f"Nivel de log establecido a {'DEBUG' if active else 'INFO'} para módulos relevantes.")


# --- Inicialización del Nivel de Log ---
set_debug(True)