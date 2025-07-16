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
        logger.info("📞 Nueva llamada entrante")
        
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
        self.twilio_handler.set_handlers(
            on_start=self._handle_stream_start,
            on_media=self._handle_audio_chunk,
            on_stop=self._handle_stream_stop,
            on_mark=self._handle_mark
        )
    
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
        session_state[self.call_state.call_sid] = {
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
        await self._shutdown("twilio_stop_event")
    
    async def _handle_mark(self, event: str, data: Dict[str, Any]) -> None:
        """
        🏷️ Maneja eventos mark de Twilio
        """
        mark_name = data.get("mark", {}).get("name")
        logger.debug(f"🏷️ Mark recibido: {mark_name}")
        
        # Por ahora solo logging, pero se puede extender
    
    # ========== INICIALIZACIÓN DE COMPONENTES ==========
    
    async def _initialize_components(self) -> None:
        """
        🚀 Inicializa todos los componentes necesarios
        """
        logger.info("🚀 Inicializando componentes...")
        
        # 1. Audio Manager
        self.audio_manager = AudioManager(
            stream_sid=self.call_state.stream_sid,
            websocket_send=self.twilio_handler.send_json
        )
        
        # 2. Conversation Flow
        self.conversation_flow = ConversationFlow(
            session_id=self.call_state.call_sid,
            response_handler=self._handle_ai_response
        )
        
        # 3. Inicializar STT (Deepgram)
        stt_success = await self.audio_manager.initialize_stt(
            on_transcript=self.conversation_flow.process_transcript,
            on_disconnect=self._handle_deepgram_disconnect
        )
        
        if not stt_success:
            logger.error("❌ No se pudo inicializar STT")
            await self._shutdown("stt_init_failed")
            return
        
        # 4. Configurar monitoreo de integraciones
        await self.integration_manager.setup_deepgram(
            self.audio_manager.stt_streamer,
            on_reconnect=self._handle_deepgram_reconnect
        )
        
        # 5. TTS se inicializa on-demand
        
        logger.info("✅ Componentes inicializados")
    
    # ========== FLUJO DE CONVERSACIÓN ==========
    
    async def _send_greeting(self) -> None:
        """
        👋 Envía el saludo inicial
        """
        await asyncio.sleep(CALL_CONFIG["GREETING_DELAY"])
        
        greeting = self._generate_greeting()
        logger.info(f"👋 Enviando saludo: '{greeting}'")
        
        await self.audio_manager.speak(
            greeting,
            on_complete=self._on_greeting_complete
        )
    
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
    
    async def _handle_ai_response(self, response_text: str) -> None:
        """
        🤖 Maneja la respuesta de la IA
        
        Args:
            response_text: Texto que la IA quiere decir
        """
        if response_text == "__END_CALL__":
            logger.info("🔚 IA solicitó terminar llamada")
            await cierre_con_despedida(self, "assistant_request", delay=5.0)
            return
        
        # Convertir texto a voz
        await self.audio_manager.speak(response_text)
    
    # ========== MONITOREO Y TIMEOUTS ==========
    
    async def _monitor_call_health(self) -> None:
        """
        👁️ Monitorea la salud de la llamada
        
        Verifica:
        - Duración máxima
        - Silencio prolongado
        - Estado de servicios
        """
        logger.info("👁️ Monitor de llamada iniciado")
        
        while not self.call_state.ended:
            try:
                await asyncio.sleep(CALL_CONFIG["MONITOR_INTERVAL"])
                
                # Verificar duración máxima
                duration = time.perf_counter() - self.call_state.start_time
                if duration > CALL_CONFIG["MAX_DURATION"]:
                    logger.warning(f"⏰ Duración máxima excedida ({duration:.1f}s)")
                    await self._shutdown("max_duration_exceeded")
                    break
                
                # Verificar silencio prolongado
                if self.conversation_flow and self.audio_manager:
                    if not self.audio_manager.get_state().ignore_stt:
                        silence_timeout = await self.conversation_flow.check_silence_timeout(
                            CALL_CONFIG["SILENCE_TIMEOUT"]
                        )
                        if silence_timeout:
                            logger.warning("🔇 Silencio prolongado detectado")
                            await self._shutdown("silence_timeout")
                            break
                
                # Log estado de servicios
                if self.integration_manager:
                    health = self.integration_manager.get_health_report()
                    logger.debug(f"📊 Salud de servicios: {health}")
                    
            except asyncio.CancelledError:
                logger.info("Monitor cancelado")
                break
            except Exception as e:
                logger.error(f"Error en monitor: {e}")
        
        logger.info("👁️ Monitor finalizado")
    
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
        🔌 Cierra todo ordenadamente
        
        Args:
            reason: Razón del cierre
        """
        if self.call_state.ended:
            return
            
        logger.info(f"🔌 Iniciando shutdown - Razón: {reason}")
        self.call_state.ended = True
        self.call_state.ending_reason = reason
        
        # Cancelar tareas
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            
        # Cerrar componentes en orden
        if self.conversation_flow:
            await self.conversation_flow.shutdown()
            
        if self.audio_manager:
            await self.audio_manager.shutdown()
            
        if self.integration_manager:
            await self.integration_manager.shutdown()
        
        # Terminar llamada en Twilio si es necesario
        if self.call_state.call_sid and not self.call_state.twilio_terminated:
            try:
                await terminar_llamada_twilio(self.call_state.call_sid)
                self.call_state.twilio_terminated = True
            except Exception as e:
                logger.error(f"Error terminando llamada en Twilio: {e}")
        
        # El TwilioHandler se cerrará automáticamente
        
        logger.info(f"✅ Shutdown completado - Razón: {reason}")
    
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