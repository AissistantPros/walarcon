# call_orchestrator.py
# -*- coding: utf-8 -*-
"""
🎭 ORQUESTADOR PRINCIPAL DE LLAMADAS
=====================================
Este es el CEREBRO que coordina todos los módulos:
- TwilioHandler → recibe/envía a Twilio
- AudioManager → maneja STT/TTS
- ConversationFlow → controla el diálogo
- IntegrationManager → monitorea servicios

Reemplaza al gigantesco tw_utils.py con una arquitectura modular.
"""

import asyncio
import logging
import os
import time
from typing import Optional, Dict, Any
from dataclasses import dataclass
from datetime import datetime
from fastapi import WebSocket
import base64

# Importar nuestros módulos
from twilio_handler import TwilioHandler
from audio_manager import AudioManager
from conversation_flow import ConversationFlow
from integration_manager import IntegrationManager
from buscarslot import load_free_slots_to_cache
from consultarinfo import load_consultorio_data_to_cache
from utils import get_cancun_time, cierre_con_despedida, terminar_llamada_twilio
from state_store import session_state

logger = logging.getLogger(__name__)

# ===== CONFIGURACIÓN =====
CALL_CONFIG = {
    "MAX_DURATION": 600,           # 10 minutos máximo por llamada
    "SILENCE_TIMEOUT": 30,         # 30 segundos de silencio = colgar
    "GREETING_DELAY": 0.5,         # Delay antes del saludo
    "MONITOR_INTERVAL": 5.0,       # Intervalo de monitoreo
    "HOLD_MESSAGE_FILE": "audio/espera_1.wav",
    "LATENCY_THRESHOLD": 0.05,     # 50ms para mensaje de espera
}


@dataclass
class CallState:
    """
    📊 Estado completo de una llamada
    """
    call_sid: Optional[str] = None
    stream_sid: Optional[str] = None
    start_time: float = 0.0
    ended: bool = False
    ending_reason: Optional[str] = None
    twilio_terminated: bool = False
    

class CallOrchestrator:
    """
    🎯 ORQUESTADOR PRINCIPAL - Coordina toda la llamada
    
    Este es el punto central que:
    1. Recibe la llamada de Twilio
    2. Inicializa todos los servicios
    3. Coordina el flujo de audio y conversación
    4. Maneja errores y reconexiones
    5. Cierra todo limpiamente
    """
    
    def __init__(self):
        """
        📥 Inicializa el orquestador
        """
        # Componentes principales
        self.twilio_handler = TwilioHandler()
        self.audio_manager: Optional[AudioManager] = None
        self.conversation_flow: Optional[ConversationFlow] = None
        self.integration_manager = IntegrationManager()
        
        # Estado
        self.call_state = CallState()
        
        # Tareas de monitoreo
        self.monitor_task: Optional[asyncio.Task] = None
        self.hold_message_task: Optional[asyncio.Task] = None
        
        # Audio de espera
        self.hold_audio_bytes = self._load_hold_audio()
        
        logger.info("🎭 CallOrchestrator inicializado")
    
    # ========== PUNTO DE ENTRADA PRINCIPAL ==========
    
    async def handle_call(self, websocket: WebSocket) -> None:
        """
        📞 Maneja una llamada completa de principio a fin
        
        Args:
            websocket: WebSocket de FastAPI conectado a Twilio
            
        Este es el método principal que se llama desde main.py
        """
        logger.info("[FUNCIONALIDAD] Nueva llamada entrante")
        t0 = time.perf_counter()
        try:
            # Configurar estado inicial
            self.call_state = CallState(start_time=time.perf_counter())
            
            # Pre-cargar datos necesarios
            await self._preload_data()
            
            # Configurar handlers de Twilio
            self._setup_twilio_handlers()
            
            # Iniciar el manejo del WebSocket
            await self.twilio_handler.handle_websocket(websocket)
            
        except Exception as e:
            logger.error(f"❌ Error fatal en llamada: {e}", exc_info=True)
        finally:
            # Asegurar limpieza completa
            await self._shutdown("call_complete")
            logger.info(f"[LATENCIA] Llamada completa finalizada en {1000*(time.perf_counter()-t0):.1f} ms")
    
    # ========== INICIALIZACIÓN Y CONFIGURACIÓN ==========
    
    async def _preload_data(self) -> None:
        """
        📦 Pre-carga datos necesarios para la llamada
        """
        logger.info("📦 Pre-cargando datos...")
        
        try:
            await asyncio.gather(
                asyncio.to_thread(load_free_slots_to_cache, 90),
                asyncio.to_thread(load_consultorio_data_to_cache),
                return_exceptions=True
            )
            logger.info("✅ Datos pre-cargados")
        except Exception as e:
            logger.warning(f"⚠️ Error pre-cargando datos: {e}")
    
    def _setup_twilio_handlers(self) -> None:
        """
        🔧 Configura los handlers para eventos de Twilio
        """
        # Definir un response_handler que acepte on_complete y lo pase a _handle_ai_response
        async def response_handler(response_text, on_complete=None):
            await self._handle_ai_response(response_text, on_complete=on_complete)
        self.twilio_handler.set_handlers(
            on_start=self._handle_stream_start,
            on_media=self._handle_audio_chunk,
            on_stop=self._handle_stream_stop,
            on_mark=self._handle_mark
        )
        # Inicializar ConversationFlow con el nuevo response_handler
        if self.conversation_flow:
            self.conversation_flow.response_handler = response_handler
    
    # ========== HANDLERS DE EVENTOS DE TWILIO ==========
    
    async def _handle_stream_start(self, event: str, data: Dict[str, Any]) -> None:
        """
        🏁 Maneja el inicio del stream de audio
        """
        # Actualizar estado
        self.call_state.stream_sid = self.twilio_handler.get_stream_sid()
        self.call_state.call_sid = self.twilio_handler.get_call_sid()
        
        logger.info(
            f"🏁 Stream iniciado - "
            f"CallSID: {self.call_state.call_sid}, "
            f"StreamSID: {self.call_state.stream_sid}"
        )
        
        # Inicializar sesión en state_store
        session_state[self.call_state.call_sid or "unknown_call_sid"] = {
            "start_time": datetime.now().isoformat(),
            "events": []
        }
        
        # Inicializar componentes de audio y conversación
        await self._initialize_components()
        
        # Iniciar monitoreo
        self.monitor_task = asyncio.create_task(
            self._monitor_call_health(),
            name=f"Monitor_{self.call_state.call_sid}"
        )
        
        # Enviar saludo inicial
        await self._send_greeting()
    
    async def _handle_audio_chunk(self, audio_bytes: bytes) -> None:
        """
        🎵 Maneja chunks de audio entrantes del usuario
        """
        if self.audio_manager and not self.call_state.ended:
            await self.audio_manager.process_audio_chunk(audio_bytes)
    
    async def _handle_stream_stop(self, event: str, data: Dict[str, Any]) -> None:
        """
        🛑 Maneja el evento de parada del stream
        """
        logger.info("🛑 Stream detenido por Twilio")
        await self._shutdown("stream_stopped")
    
    async def _handle_mark(self, event: str, data: Dict[str, Any]) -> None:
        """
        🏷️ Maneja eventos mark de Twilio
        """
        mark_data = data.get("mark", {})
        mark_name = mark_data.get("name", "unknown")
        logger.debug(f"🏷️ Mark recibido: {mark_name}")
    
    def _handle_transcript(self, transcript: str, is_final: bool) -> None:
        """
        📝 Maneja transcripciones de Deepgram
        
        Args:
            transcript: Texto transcrito
            is_final: True si es transcripción final
        """
        if self.conversation_flow and not self.call_state.ended:
            self.conversation_flow.process_transcript(transcript, is_final)
    
    async def _initialize_components(self) -> None:
        """
        🔧 Inicializa todos los componentes de la llamada
        
        Orden de inicialización:
        1. AudioManager (STT + TTS)
        2. ConversationFlow (control de diálogo)
        3. IntegrationManager (monitoreo)
        """
        logger.info("🔧 Inicializando componentes de la llamada...")
        
        try:
            # === PASO 1: AUDIO MANAGER ===
            logger.info("🎵 Inicializando AudioManager...")
            
            # Crear AudioManager
            self.audio_manager = AudioManager(
                stream_sid=self.call_state.stream_sid or "unknown",
                websocket_send=self.twilio_handler.send_json
            )
            
            # === PASO 2: CONVERSATION FLOW ===
            logger.info("🗣️ Inicializando ConversationFlow...")
            
            # Crear ConversationFlow
            self.conversation_flow = ConversationFlow(
                session_id=self.call_state.call_sid or "unknown_call",
                response_handler=self._handle_ai_response,
                audio_manager=self.audio_manager
            )
            
            # NUEVO: Establecer referencia al manager en ConversationFlow
            setattr(self.conversation_flow, '_manager_reference', self)
            
            logger.info("✅ ConversationFlow inicializado")
            
            # === PASO 3: INICIALIZAR STT ===
            logger.info("🎤 Inicializando STT...")
            
            stt_success = await self.audio_manager.initialize_stt(
                on_transcript=self._handle_transcript,
                on_disconnect=self._handle_deepgram_disconnect
            )
            
            if not stt_success:
                logger.error("❌ No se pudo inicializar STT")
                return
            
            # === PASO 4: INICIALIZAR TTS ===
            logger.info("🔊 Inicializando TTS...")
            
            tts_success = await self.audio_manager.initialize_tts()
            
            if not tts_success:
                logger.warning("⚠️ No se pudo inicializar TTS WebSocket, usará fallback HTTP")
            
            logger.info("✅ AudioManager inicializado")
            
            # === PASO 5: CONFIGURAR INTEGRATION MANAGER ===
            logger.info("🔗 Configurando IntegrationManager...")
            
            # Configurar monitoreo de integraciones
            await self.integration_manager.setup_deepgram(
                self.audio_manager.stt_streamer,
                on_reconnect=self._handle_deepgram_reconnect
            )
            
            logger.info("✅ IntegrationManager configurado")
            
            logger.info("✅ Todos los componentes inicializados correctamente")
            
        except Exception as e:
            logger.error(f"❌ Error inicializando componentes: {e}", exc_info=True)
            raise
    
    # ========== FLUJO DE CONVERSACIÓN ==========
    
    async def _send_greeting(self) -> None:
        """
        👋 Envía el saludo inicial
        """
        await asyncio.sleep(CALL_CONFIG["GREETING_DELAY"])
        
        greeting = self._generate_greeting()
        logger.info(f"👋 Enviando saludo: '{greeting}'")
        t0 = time.perf_counter()
        if self.audio_manager:
            await self.audio_manager.speak(
                greeting,
                on_complete=self._on_greeting_complete
            )
        logger.info(f"[LATENCIA] Saludo enviado y TTS completado en {1000*(time.perf_counter()-t0):.1f} ms")
    
    def _generate_greeting(self) -> str:
        """
        🎨 Genera el saludo según la hora
        """
        try:
            now = get_cancun_time()
            hour = now.hour
            
            if 5 <= hour < 12:
                return "¡Buenos días! Soy Dany, Asistente de Inteligencia Artificial del doctor Wilfrido Alarcón. ¿Cómo puedo ayudarle hoy?"
            elif 12 <= hour < 19:
                return "¡Buenas tardes! Soy Dany, Asistente de Inteligencia Artificial del doctor Wilfrido Alarcón. ¿Cómo puedo ayudarle hoy?"
            else:
                return "¡Buenas noches! Soy Dany, Asistente de Inteligencia Artificial del doctor Wilfrido Alarcón. ¿Cómo puedo ayudarle hoy?"
                
        except Exception as e:
            logger.error(f"Error generando saludo: {e}")
            return "Consultorio del Doctor Wilfrido Alarcón, Soy Dany, asistente de Inteligencia Artificial. ¿Cómo puedo ayudarle?"
    
    async def _on_greeting_complete(self) -> None:
        """
        ✅ Se ejecuta cuando termina el saludo
        """
        logger.info("✅ Saludo completado, escuchando al usuario...")
    
    async def _handle_ai_response(self, response_text: str, on_complete=None) -> None:
        """
        🤖 Maneja la respuesta de la IA
        Args:
            response_text: Texto que debe decir la IA
            on_complete: Callback opcional para ejecutar al terminar el TTS (solo para despedida)
        """
        if self.call_state.ended:
            return
        # Caso especial: IA solicita terminar llamada
        if response_text == "__END_CALL__":
            logger.info("🔚 IA solicitó terminar llamada")
            await self._handle_ai_end_call()
            return
        logger.info(f"🤖 IA responde: '{response_text[:50]}...'")
        # Preparar TTS antes de enviar texto (optimización)
        if self.audio_manager:
            await self.audio_manager.prepare_tts_ws()
        # Enviar respuesta a través del TTS
        if self.audio_manager:
            # Log diagnóstico antes de TTS
            if self.audio_manager.tts_client:
                diagnostics = self.audio_manager.tts_client.get_diagnostics()
                logger.info(f"[DIAGNÓSTICO] Pre-TTS - Conectado: {diagnostics['is_connected']}, "
                           f"Errores: {diagnostics['total_errors']}, Intentos: {diagnostics['connection_attempts']}")
            # Solo pasar on_complete si está presente (despedida)
            success = await self.audio_manager.speak(
                response_text,
                on_complete=on_complete if on_complete else self._on_tts_complete
            )
            if not success:
                logger.error("❌ TTS falló completamente")
                if self.audio_manager.tts_client:
                    diagnostics = self.audio_manager.tts_client.get_diagnostics()
                    logger.error(f"[DIAGNÓSTICO] Post-fallo TTS - Último error: {diagnostics['last_error']}")
        else:
            logger.error("❌ AudioManager no disponible para TTS")
    
    async def _handle_ai_end_call(self) -> None:
        """
        🔚 Maneja la terminación de llamada solicitada por la IA
        
        Usa el flujo elegante de cierre con despedida
        """
        logger.info("🔚 Iniciando terminación de llamada solicitada por IA")
        
        try:
            # Verificar que no esté ya terminada
            if self.call_state.ended:
                logger.info("🔚 Llamada ya está en proceso de terminación")
                return
            
            # Marcar como terminando
            self.call_state.ended = True
            self.call_state.ending_reason = "assistant_request"
            
            # Usar el flujo elegante de cierre
            await cierre_con_despedida(self, "assistant_request", delay=5.0)
            logger.info("✅ Terminación de llamada completada exitosamente")
        except Exception as e:
            logger.error(f"❌ Error en terminación de llamada: {e}")
            # Fallback a shutdown de emergencia
            try:
                await self._shutdown("emergency_shutdown")
            except Exception as emergency_error:
                logger.error(f"❌ Error en shutdown de emergencia: {emergency_error}")
    
    async def _on_tts_complete(self) -> None:
        """
        ✅ Se ejecuta cuando TTS termina de hablar
        """
        logger.info("✅ TTS completado, continuando conversación")
        
        # Log diagnóstico post-TTS
        if self.audio_manager:
            diagnostics = self.audio_manager.get_diagnostics()
            logger.info(f"[DIAGNÓSTICO] Post-TTS - Estado: {diagnostics['state']}, "
                       f"STT listo: {diagnostics['stt_ready']}, TTS listo: {diagnostics['tts_ready']}")
        
        # La conversación continúa automáticamente cuando STT se reactiva
        # No necesitamos llamar ningún método específico
    
    async def _monitor_call_health(self) -> None:
        """
        🏥 Monitorea la salud de la llamada
        
        Verifica:
        - Duración máxima
        - Silencio prolongado
        - Estado de servicios
        """
        logger.info("🏥 Iniciando monitoreo de salud de llamada")
        
        while not self.call_state.ended:
            try:
                # Verificar duración máxima
                duration = time.perf_counter() - self.call_state.start_time
                if duration > CALL_CONFIG["MAX_DURATION"]:
                    logger.warning(f"⏰ Llamada excedió duración máxima ({CALL_CONFIG['MAX_DURATION']}s)")
                    await self._shutdown("max_duration")
                    break
                
                # Verificar silencio
                if self.audio_manager:
                    last_activity = self.audio_manager.state.last_audio_activity
                    if last_activity > 0:
                        silence_duration = time.perf_counter() - last_activity
                        if silence_duration > CALL_CONFIG["SILENCE_TIMEOUT"]:
                            logger.warning(f"🔇 Silencio prolongado ({silence_duration:.1f}s)")
                            await self._shutdown("silence_timeout")
                            break
                
                # Log diagnóstico periódico
                if self.audio_manager and duration % 30 < 1:  # Cada ~30 segundos
                    diagnostics = self.audio_manager.get_diagnostics()
                    logger.info(f"[DIAGNÓSTICO] Salud llamada - Duración: {duration:.1f}s, "
                               f"STT: {diagnostics['stt_ready']}, TTS: {diagnostics['tts_ready']}, "
                               f"Hablando: {diagnostics['state']['is_speaking']}")
                
                await asyncio.sleep(CALL_CONFIG["MONITOR_INTERVAL"])
                
            except Exception as e:
                logger.error(f"❌ Error en monitoreo de salud: {e}")
                await asyncio.sleep(CALL_CONFIG["MONITOR_INTERVAL"])
        
        logger.info("🏥 Monitoreo de salud terminado")
    
    # ========== MANEJO DE RECONEXIONES ==========
    
    async def _handle_deepgram_disconnect(self) -> None:
        """
        🔌 Maneja desconexión de Deepgram
        """
        logger.warning("🔌 Deepgram desconectado")
        
        # El IntegrationManager manejará la reconexión
        # Mientras tanto, el audio se buferea en AudioManager
    
    async def _handle_deepgram_reconnect(self) -> None:
        """
        🔄 Maneja reconexión exitosa de Deepgram
        """
        logger.info("🔄 Deepgram reconectado")
        
        # El AudioManager automáticamente vaciará su buffer
    
    # ========== LIMPIEZA Y CIERRE ==========
    
    async def _shutdown(self, reason: str) -> None:
        """
        🔌 Cierra todo ordenadamente con manejo robusto de errores
        
        Args:
            reason: Razón del cierre
        """
        t0 = time.perf_counter()
        if self.call_state.ended:
            logger.info("🔌 Shutdown ya en progreso, ignorando")
            return
            
        logger.info(f"🔌 Iniciando shutdown - Razón: {reason}")
        self.call_state.ended = True
        self.call_state.ending_reason = reason
        
        try:
            # === PASO 1: CANCELAR TAREAS ===
            logger.info("🔄 Cancelando tareas activas...")
            
            if self.monitor_task and not self.monitor_task.done():
                try:
                    self.monitor_task.cancel()
                    logger.info("✅ Tarea de monitoreo cancelada")
                except Exception as e:
                    logger.error(f"❌ Error cancelando tarea de monitoreo: {e}")
            
            if self.hold_message_task and not self.hold_message_task.done():
                try:
                    self.hold_message_task.cancel()
                    logger.info("✅ Tarea de mensaje de espera cancelada")
                except Exception as e:
                    logger.error(f"❌ Error cancelando tarea de mensaje de espera: {e}")
            
            # === PASO 2: CERRAR COMPONENTES EN ORDEN ===
            logger.info("🔌 Cerrando componentes...")
            
            # ConversationFlow primero (puede estar procesando)
            if self.conversation_flow:
                try:
                    await self.conversation_flow.shutdown()
                    logger.info("✅ ConversationFlow cerrado")
                except Exception as e:
                    logger.error(f"❌ Error cerrando ConversationFlow: {e}")
            
            # AudioManager (Deepgram + ElevenLabs)
            if self.audio_manager:
                try:
                    await self.audio_manager.shutdown()
                    logger.info("✅ AudioManager cerrado")
                except Exception as e:
                    logger.error(f"❌ Error cerrando AudioManager: {e}")
            
            # IntegrationManager
            if self.integration_manager:
                try:
                    await self.integration_manager.shutdown()
                    logger.info("✅ IntegrationManager cerrado")
                except Exception as e:
                    logger.error(f"❌ Error cerrando IntegrationManager: {e}")
            
            # === PASO 3: TERMINAR LLAMADA EN TWILIO ===
            if self.call_state.call_sid and not self.call_state.twilio_terminated:
                try:
                    success = await terminar_llamada_twilio(self.call_state.call_sid)
                    if success:
                        self.call_state.twilio_terminated = True
                        logger.info("✅ Llamada terminada en Twilio")
                    else:
                        logger.warning("⚠️ No se pudo terminar llamada en Twilio")
                except Exception as e:
                    logger.error(f"❌ Error terminando llamada en Twilio: {e}")
            else:
                logger.info("ℹ️ Llamada ya terminada en Twilio o sin call_sid")
            
            # === PASO 4: LIMPIEZA FINAL ===
            logger.info("🧹 Limpieza final...")
            
            # Limpiar session_state
            try:
                session_state.clear()
                logger.info("✅ Session state limpiado")
            except Exception as e:
                logger.error(f"❌ Error limpiando session state: {e}")
            
            # El TwilioHandler se cerrará automáticamente cuando se cierre el WebSocket
            
            logger.info(f"✅ Shutdown completado exitosamente - Razón: {reason}")
            logger.info(f"[LATENCIA] Shutdown completado en {1000*(time.perf_counter()-t0):.1f} ms")
            
        except Exception as e:
            logger.error(f"❌ Error crítico en shutdown: {e}")
            logger.error(f"[LATENCIA] Shutdown con errores tras {1000*(time.perf_counter()-t0):.1f} ms")
        finally:
            # Asegurar que el estado esté marcado como terminado
            self.call_state.ended = True
            logger.info(f"🔚 Shutdown finalizado - Razón: {reason}")
    
    # ========== UTILIDADES ==========
    
    def _load_hold_audio(self) -> bytes:
        """
        📁 Carga el audio de espera
        """
        try:
            file_path = CALL_CONFIG["HOLD_MESSAGE_FILE"]
            if os.path.exists(file_path):
                with open(file_path, 'rb') as f:
                    raw = f.read()
                    # Si es WAV, quitar header
                    if raw[:4] == b"RIFF":
                        raw = raw[44:]
                    logger.info(f"✅ Audio de espera cargado ({len(raw)} bytes)")
                    return raw
        except Exception as e:
            logger.error(f"Error cargando audio de espera: {e}")
        
        return b""
    
    def get_call_info(self) -> Dict[str, Any]:
        """
        📊 Obtiene información de la llamada actual
        """
        duration = time.perf_counter() - self.call_state.start_time
        
        info = {
            "call_sid": self.call_state.call_sid,
            "stream_sid": self.call_state.stream_sid,
            "duration_seconds": round(duration, 1),
            "ended": self.call_state.ended,
            "ending_reason": self.call_state.ending_reason,
        }
        
        # Agregar métricas de conversación
        if self.conversation_flow:
            info["conversation"] = self.conversation_flow.get_metrics()
        
        # Agregar estado de audio
        if self.audio_manager:
            info["audio_state"] = {
                "is_speaking": self.audio_manager.get_state().is_speaking,
                "ignore_stt": self.audio_manager.get_state().ignore_stt,
                "tts_in_progress": self.audio_manager.get_state().tts_in_progress,
            }
        
        # Agregar salud de servicios
        if self.integration_manager:
            info["services"] = self.integration_manager.get_health_report()
        
        return info