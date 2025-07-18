# conversation_flow.py
# -*- coding: utf-8 -*-
"""
🗣️ GESTOR DE FLUJO DE CONVERSACIÓN
====================================
Este módulo controla CUÁNDO y CÓMO fluye la conversación:
- Detecta pausas del usuario
- Decide cuándo enviar a la IA
- Maneja el historial
- Controla los turnos de habla

⚡ IMPORTANTE: Los tiempos de pausa están finamente calibrados
"""

import asyncio
import logging
import time
from typing import List, Dict, Optional, Callable, Awaitable
from dataclasses import dataclass, field
from datetime import datetime

from state_store import emit_latency_event
from aiagent import generate_ai_response

logger = logging.getLogger(__name__)

# ===== CONFIGURACIÓN DE TIEMPOS =====
TIMING_CONFIG = {
    # ⏱️ CRÍTICO: No cambiar sin pruebas exhaustivas
    "PAUSE_DETECTION": 0.4,        # Segundos de silencio = usuario terminó
    "MAX_WAIT_TIME": 8.0,          # Máximo espera antes de forzar envío
    "MIN_TEXT_LENGTH": 2,          # Mínimo de caracteres para procesar
    "LATENCY_THRESHOLD": 0.05,     # 50ms para mensaje de espera
}

# ===== TIPOS =====
ResponseHandler = Callable[[str, Optional[Callable]], Awaitable[None]]


@dataclass
class ConversationState:
    """
    📊 Estado de la conversación en un momento dado
    """
    # Historial
    history: List[Dict] = field(default_factory=list)
    
    # Acumuladores de texto
    pending_finals: List[str] = field(default_factory=list)
    last_partial_was_empty: bool = False
    
    # Control de tiempo
    last_activity_time: float = 0.0
    last_final_time: float = 0.0
    last_stt_timestamp: Optional[float] = None
    
    # Control de flujo
    ai_task_active: bool = False
    pause_timer: Optional[asyncio.Task] = None
    
    # Métricas
    turn_start_time: Optional[float] = None


class ConversationFlow:
    """
    🎯 CONTROLADOR PRINCIPAL del flujo de conversación
    
    Responsabilidades:
    1. Detectar cuándo el usuario terminó de hablar
    2. Acumular transcripciones parciales/finales
    3. Decidir cuándo enviar a la IA
    4. Mantener el historial
    5. Medir latencias
    """
    
    def __init__(self, session_id: str, response_handler: ResponseHandler, audio_manager=None):
        """
        📥 Inicializa el gestor de conversación
        
        Args:
            session_id: ID único de la sesión/llamada
            response_handler: Función que maneja respuestas de IA
            audio_manager: Referencia al AudioManager para preparar TTS
        """
        self.session_id = session_id
        self.response_handler = response_handler
        self.state = ConversationState()
        self.current_ai_task: Optional[asyncio.Task] = None
        self.audio_manager = audio_manager
        
        logger.info(f"🗣️ ConversationFlow creado para sesión: {session_id}")
    
    # ========== PROCESAMIENTO DE TRANSCRIPCIONES ==========
    
    def process_transcript(self, transcript: str, is_final: bool) -> None:
        """
        📝 Procesa una transcripción de Deepgram
        
        Args:
            transcript: Texto transcrito
            is_final: True = transcripción final, False = parcial
            
        Este es el PUNTO DE ENTRADA principal desde AudioManager
        """
        now = time.perf_counter()
        
        # Actualizar tiempos
        self.state.last_activity_time = now
        
        # Si hay texto significativo
        if transcript and transcript.strip():
            logger.debug(f"📝 Transcript: final={is_final}, text='{transcript[:60]}...'")
            
            if is_final:
                # Transcripción final → acumular
                self.state.last_final_time = now
                self.state.last_stt_timestamp = now
                self.state.pending_finals.append(transcript.strip())
                logger.info(f"📥 Final recibido: '{transcript.strip()}'")
                
            # Reiniciar timer de pausa
            self._restart_pause_timer()
        
        # Marcar si el último parcial fue vacío (para lógica de pausa)
        self.state.last_partial_was_empty = not is_final and not transcript.strip()
    
    def _restart_pause_timer(self) -> None:
        """
        ⏲️ Reinicia el temporizador de detección de pausa
        
        Cada vez que el usuario habla, cancelamos el timer anterior
        y empezamos uno nuevo. Si el timer completa → usuario pausó.
        """
        # Cancelar timer existente
        if self.state.pause_timer and not self.state.pause_timer.done():
            self.state.pause_timer.cancel()
            logger.debug("⏲️ Timer de pausa cancelado")
        
        # Crear nuevo timer
        self.state.pause_timer = asyncio.create_task(
            self._wait_for_pause(),
            name=f"PauseTimer_{self.session_id}"
        )
        logger.debug(f"⏲️ Timer de pausa iniciado ({TIMING_CONFIG['PAUSE_DETECTION']}s)")
    
    async def _wait_for_pause(self) -> None:
        """
        ⏳ Espera a que el usuario haga pausa
        
        Si completa → el usuario terminó de hablar → enviar a IA
        Si se cancela → el usuario sigue hablando
        """
        try:
            # Esperar el tiempo de pausa configurado
            await asyncio.sleep(TIMING_CONFIG["PAUSE_DETECTION"])
            
            # Si llegamos aquí, hubo pausa
            logger.debug("⏸️ Pausa detectada - procesando mensaje")
            t0 = time.perf_counter()
            logger.debug("[FUNCIONALIDAD] Pausa detectada, preparando TTS y procesando con LLM...")
            await self._process_accumulated_text()
            logger.debug(f"[LATENCIA] Proceso de pausa (preparar TTS + LLM) completado en {1000*(time.perf_counter()-t0):.1f} ms")
            
        except asyncio.CancelledError:
            # Normal - usuario siguió hablando
            logger.debug("⏲️ Timer cancelado (usuario sigue hablando)")
    
    async def prepare_tts_ws(self):
        """
        Prepara el WebSocket de ElevenLabs antes de enviar el texto al LLM.
        """
        if self.audio_manager:
            t0 = time.perf_counter()
            logger.info("[FUNCIONALIDAD] Preparando TTS (WS ElevenLabs) desde ConversationFlow...")
            await self.audio_manager.on_user_pause_prepare_tts()
            logger.info(f"[LATENCIA] Preparación de TTS (WS ElevenLabs) desde ConversationFlow completada en {1000*(time.perf_counter()-t0):.1f} ms")
    
    async def _process_accumulated_text(self) -> None:
        """
        📤 Procesa el texto acumulado y lo envía a la IA
        
        Pasos:
        1. Valida que hay texto para enviar
        2. Construye el mensaje completo
        3. Marca inicio de turno
        4. Llama a la IA
        5. Maneja la respuesta
        """
        # Validar que hay texto
        if not self.state.pending_finals:
            logger.debug("📭 No hay texto acumulado para procesar")
            return
        
        # Si ya hay una tarea de IA activa, no iniciar otra
        if self.state.ai_task_active:
            logger.warning("⚠️ IA ya está procesando, ignorando")
            return
        
        # Construir mensaje completo
        full_message = " ".join(self.state.pending_finals).strip()
        
        # Validar longitud mínima
        if len(full_message) < TIMING_CONFIG["MIN_TEXT_LENGTH"]:
            logger.debug(f"📏 Mensaje muy corto ({len(full_message)} chars), ignorando")
            self.state.pending_finals.clear()
            return
        
        # Limpiar acumuladores
        self.state.pending_finals.clear()
        
        # Marcar inicio de turno
        self.state.turn_start_time = time.perf_counter()
        logger.info(f"🎯 [PERF] INICIO DE TURNO - Usuario dijo: '{full_message}'")
        
        # Preparar TTS antes de enviar a la IA
        await self.prepare_tts_ws()
        t_llm = time.perf_counter()
        # Iniciar procesamiento con IA
        self.state.ai_task_active = True
        self.current_ai_task = asyncio.create_task(
            self._handle_ai_response(full_message),
            name=f"AITask_{self.session_id}"
        )
        logger.info(f"[LATENCIA] Preparación de TTS + lanzamiento de LLM en {1000*(t_llm-self.state.turn_start_time):.1f} ms")
    
    async def _handle_ai_response(self, user_message: str, on_complete=None) -> None:
        """
        🤖 Maneja la interacción con la IA
        Args:
            user_message: Mensaje del usuario para la IA
            on_complete: Callback opcional para ejecutar al terminar el TTS (solo para despedida)
        """
        try:
            t0 = time.perf_counter()
            self.state.history.append({
                "role": "user",
                "content": user_message
            })
            logger.info(f"[HISTORIAL] Usuario: '{user_message}'")
            emit_latency_event(self.session_id, "ai_request_start")
            try:
                ai_response = await generate_ai_response(
                    session_id=self.session_id,
                    history=self.state.history
                )
            except Exception as e:
                logger.error(f"❌ Error llamando a IA: {e}", exc_info=True)
                ai_response = "Disculpe, tuve un problema técnico. ¿Podría repetir?"
            if ai_response == "__END_CALL__":
                logger.info("🔚 IA solicitó terminar la llamada")
                if hasattr(self, 'response_handler') and self.response_handler:
                    try:
                        await self._execute_end_call()
                    except Exception as e:
                        logger.error(f"❌ Error ejecutando terminación de llamada: {e}")
                return
            self.state.history.append({
                "role": "assistant",
                "content": ai_response
            })
            logger.info(f"[HISTORIAL] Asistente: '{ai_response}'")
            # Enviar respuesta como audio, pasando on_complete si está presente
            if on_complete:
                await self.response_handler(ai_response, on_complete)
            else:
                await self.response_handler(ai_response, None)
            logger.info(f"[LATENCIA] Turno completo (LLM + respuesta TTS) en {1000*(time.perf_counter()-t0):.1f} ms")
            if self.state.turn_start_time:
                total_latency = (time.perf_counter() - self.state.turn_start_time) * 1000
                logger.info(f"⏱️ [PERF] FIN DE TURNO - Latencia total: {total_latency:.1f}ms")
                self.state.turn_start_time = None
        except Exception as e:
            logger.error(f"❌ Error en _handle_ai_response: {e}", exc_info=True)
        finally:
            self.state.ai_task_active = False
            emit_latency_event(self.session_id, "ai_response_complete")
    
    async def _execute_end_call(self) -> None:
        """
        🔚 Ejecuta la terminación de llamada cuando la IA lo solicita
        
        Este método busca el manager y ejecuta la terminación
        """
        logger.info("🔚 Ejecutando terminación de llamada solicitada por IA")
        
        # Buscar el manager a través del response_handler
        # El response_handler es _handle_ai_response del CallOrchestrator
        # Necesitamos acceder al manager desde el contexto del response_handler
        
        # Intentar obtener el manager desde el contexto del response_handler
        try:
            # El response_handler es un método del CallOrchestrator
            # Podemos intentar acceder al manager a través del response_handler
            if hasattr(self.response_handler, '__self__'):
                manager = self.response_handler.__self__
                if hasattr(manager, '_handle_ai_end_call'):
                    await manager._handle_ai_end_call()
                    logger.info("✅ Terminación de llamada ejecutada correctamente")
                else:
                    logger.error("❌ Manager no tiene método _handle_ai_end_call")
            else:
                logger.error("❌ No se pudo acceder al manager desde response_handler")
        except Exception as e:
            logger.error(f"❌ Error ejecutando terminación de llamada: {e}")
            # Fallback: intentar shutdown directo
            try:
                await self.shutdown()
            except Exception as shutdown_error:
                logger.error(f"❌ Error en shutdown de emergencia: {shutdown_error}")
    
    # ========== CONTROL DE TIMEOUTS ==========
    
    async def check_silence_timeout(self, timeout_seconds: float = 30.0) -> bool:
        """
        🔇 Verifica si ha habido silencio prolongado
        
        Args:
            timeout_seconds: Segundos de silencio antes de timeout
            
        Returns:
            bool: True si se excedió el timeout
        """
        if not self.state.last_activity_time:
            return False
            
        silence_duration = time.perf_counter() - self.state.last_activity_time
        return silence_duration >= timeout_seconds
    
    def reset_silence_timer(self) -> None:
        """
        🔄 Reinicia el contador de silencio
        """
        self.state.last_activity_time = time.perf_counter()
    
    # ========== GESTIÓN DEL HISTORIAL ==========
    
    def add_to_history(self, role: str, content: str) -> None:
        """
        📚 Agrega una entrada al historial
        
        Args:
            role: "user", "assistant", "system", "tool"
            content: Contenido del mensaje
        """
        self.state.history.append({
            "role": role,
            "content": content
        })
        logger.debug(f"[HISTORIAL] {role}: '{content[:100]}...'")
    
    def get_history(self) -> List[Dict]:
        """
        📖 Obtiene el historial completo
        
        Returns:
            Lista de mensajes del historial
        """
        return self.state.history.copy()
    
    def clear_history(self) -> None:
        """
        🗑️ Limpia el historial (útil para nuevas conversaciones)
        """
        self.state.history.clear()
        logger.info("🗑️ Historial limpiado")
    
    def get_history_summary(self) -> str:
        """
        📊 Obtiene un resumen del historial para logs
        
        Returns:
            Resumen en formato string
        """
        total_messages = len(self.state.history)
        user_messages = sum(1 for m in self.state.history if m["role"] == "user")
        assistant_messages = sum(1 for m in self.state.history if m["role"] == "assistant")
        
        return (f"Total: {total_messages} mensajes "
                f"(Usuario: {user_messages}, Asistente: {assistant_messages})")
    
    # ========== LIMPIEZA Y CIERRE ==========
    
    async def shutdown(self) -> None:
        """
        🔌 Cierra el gestor de conversación
        
        Cancela tareas pendientes y limpia recursos
        """
        logger.info("🔌 Cerrando ConversationFlow...")
        
        # Cancelar timer de pausa
        if self.state.pause_timer and not self.state.pause_timer.done():
            self.state.pause_timer.cancel()
            try:
                await self.state.pause_timer
            except asyncio.CancelledError:
                pass
        
        # Cancelar tarea de IA si está activa
        if self.current_ai_task and not self.current_ai_task.done():
            logger.warning("⚠️ Cancelando tarea de IA activa...")
            self.current_ai_task.cancel()
            try:
                await asyncio.wait_for(self.current_ai_task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        
        # Log final del historial
        logger.info(f"📊 Conversación finalizada - {self.get_history_summary()}")
        
        # Imprimir historial completo para debugging
        logger.info("📜 HISTORIAL COMPLETO:")
        for i, msg in enumerate(self.state.history):
            logger.info(f"  [{i}] {msg['role']}: {msg['content']}")
        
        logger.info("✅ ConversationFlow cerrado")
    
    # ========== UTILIDADES Y ESTADO ==========
    
    def is_processing(self) -> bool:
        """
        🔄 Verifica si hay procesamiento activo
        
        Returns:
            True si la IA está procesando
        """
        return self.state.ai_task_active
    
    def get_pending_text(self) -> str:
        """
        📝 Obtiene el texto pendiente de procesar
        
        Returns:
            Texto acumulado que no se ha enviado
        """
        return " ".join(self.state.pending_finals)
    
    def force_process_now(self) -> asyncio.Task:
        """
        ⚡ Fuerza el procesamiento inmediato del texto acumulado
        
        Returns:
            Task del procesamiento
            
        Útil para casos especiales donde no queremos esperar la pausa
        """
        logger.info("⚡ Forzando procesamiento inmediato")
        
        # Cancelar timer si existe
        if self.state.pause_timer and not self.state.pause_timer.done():
            self.state.pause_timer.cancel()
        
        # Procesar ahora
        return asyncio.create_task(self._process_accumulated_text())
    
    def get_metrics(self) -> Dict:
        """
        📈 Obtiene métricas de la conversación
        
        Returns:
            Diccionario con métricas útiles
        """
        return {
            "total_messages": len(self.state.history),
            "user_messages": sum(1 for m in self.state.history if m["role"] == "user"),
            "assistant_messages": sum(1 for m in self.state.history if m["role"] == "assistant"),
            "pending_text_length": len(self.get_pending_text()),
            "is_processing": self.is_processing(),
            "session_id": self.session_id
        }