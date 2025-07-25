# audio_manager.py
# -*- coding: utf-8 -*-
"""
🎵 GESTOR DE AUDIO - Maneja TODO el flujo de audio de la llamada
==================================================================
Este módulo se encarga de:
- Recibir audio del usuario (desde Twilio)
- Enviarlo a Deepgram (STT - Speech to Text)
- Recibir texto de la IA
- Convertirlo a audio con ElevenLabs (TTS - Text to Speech)
- Enviarlo de vuelta a Twilio

⚡ IMPORTANTE: Los tiempos y buffers están optimizados para latencia mínima
"""

import asyncio
import base64
import json
import logging
import time
from typing import Optional, List, Callable, Awaitable
from dataclasses import dataclass
from datetime import datetime

from deepgram_stt_streamer import DeepgramSTTStreamer
from eleven_ws_tts_client import ElevenLabsWSClient
from eleven_http_client import send_tts_http_to_twilio

logger = logging.getLogger(__name__)

# ===== CONFIGURACIÓN DE AUDIO =====
AUDIO_CONFIG = {
    "CHUNK_SIZE": 160,              # bytes - 20ms @ 8kHz μ-law
    "BUFFER_MAX_SIZE": 40000,       # bytes máximos en buffer
    "SAMPLE_RATE": 8000,            # Hz
    "CHANNELS": 1,                  # Mono
    "ENCODING": "mulaw",            # μ-law para Twilio
}

# ===== CALLBACKS =====
TranscriptCallback = Callable[[str, bool], None]
AudioChunkCallback = Callable[[bytes], Awaitable[None]]


@dataclass
class AudioState:
    """
    📊 Estado del audio en un momento dado
    """
    is_speaking: bool = False           # ¿La IA está hablando?
    ignore_stt: bool = False            # ¿Ignorar entrada de voz?
    tts_in_progress: bool = False       # ¿TTS activo?
    last_audio_activity: float = 0.0    # Timestamp última actividad
    

class AudioManager:
    """
    🎯 CLASE PRINCIPAL - Gestiona todo el audio de una llamada
    
    Flujo de audio:
    1. Usuario habla → Twilio → buffer → Deepgram → texto
    2. IA responde → texto → ElevenLabs → audio → Twilio → Usuario
    """
    
    def __init__(self, stream_sid: str, websocket_send: Callable):
        """
        📥 Inicializa el gestor de audio
        
        Args:
            stream_sid: ID del stream de Twilio
            websocket_send: Función para enviar datos a Twilio
        """
        self.stream_sid = stream_sid
        self.websocket_send = websocket_send
        
        # === Estado ===
        self.state = AudioState()
        
        # === Servicios de audio ===
        self.stt_streamer: Optional[DeepgramSTTStreamer] = None
        self.tts_client: Optional[ElevenLabsWSClient] = None
        
        # === Buffers ===
        self.audio_buffer: List[bytes] = []
        self.audio_buffer_lock = asyncio.Lock()
        self.buffer_size = 0
        
        # === Callbacks ===
        self.on_transcript: Optional[TranscriptCallback] = None
        self.on_tts_complete: Optional[Callable] = None
        
        # === Control de tiempo ===
        self.last_chunk_time: Optional[float] = None
        self.stall_detector_task: Optional[asyncio.Task] = None
        
        # === NUEVO: Lock para evitar duplicación de TTS ===
        self.tts_lock = asyncio.Lock()
        self.current_tts_text: Optional[str] = None
        
        logger.info(f"🎵 AudioManager creado para stream: {stream_sid}")
    
    # ========== INICIALIZACIÓN DE SERVICIOS ==========
    
    async def initialize_stt(self, on_transcript: TranscriptCallback, 
                           on_disconnect: Optional[Callable] = None) -> bool:
        """
        🎤 Inicializa Deepgram STT (Speech-to-Text)
        
        Args:
            on_transcript: Callback cuando hay transcripción
            on_disconnect: Callback si Deepgram se desconecta
            
        Returns:
            bool: True si se inició correctamente
        """
        try:
            logger.info("🎤 Iniciando Deepgram STT...")
            
            self.on_transcript = on_transcript
            self.stt_streamer = DeepgramSTTStreamer(
                callback=self._handle_transcript,
                on_disconnect_callback=on_disconnect
            )
            
            await self.stt_streamer.start_streaming()
            
            if self.stt_streamer._started:
                logger.info("✅ Deepgram STT iniciado correctamente")
                # Vaciar buffer acumulado
                await self._flush_audio_buffer()
                return True
            else:
                logger.error("❌ Deepgram no pudo iniciarse")
                return False
                
        except Exception as e:
            logger.error(f"❌ Error iniciando Deepgram: {e}", exc_info=True)
            return False
    
    async def initialize_tts(self) -> bool:
        """
        🔊 Inicializa ElevenLabs TTS (Text-to-Speech)
        
        Returns:
            bool: True si se inició correctamente
        """
        try:
            logger.info("🔊 Iniciando ElevenLabs TTS...")
            
            self.tts_client = ElevenLabsWSClient()
            # Esperar conexión (máximo 2 segundos)
            await asyncio.wait_for(self.tts_client._ws_open.wait(), timeout=2.0)
            
            logger.info("✅ ElevenLabs TTS conectado")
            return True
            
        except asyncio.TimeoutError:
            logger.error("⏰ Timeout conectando a ElevenLabs")
            self.tts_client = None
            return False
        except Exception as e:
            logger.error(f"❌ Error iniciando ElevenLabs: {e}", exc_info=True)
            self.tts_client = None
            return False
    
    # ========== MANEJO DE AUDIO ENTRANTE (Usuario → IA) ==========
    
    async def process_audio_chunk(self, audio_bytes: bytes) -> None:
        """
        📥 Procesa un chunk de audio del usuario
        
        Args:
            audio_bytes: Audio en formato μ-law 8kHz
            
        Este método:
        1. Si STT activo y no ignorando → envía a Deepgram
        2. Si STT inactivo → guarda en buffer
        3. Si ignorando (IA hablando) → descarta
        """
        # Si estamos ignorando (IA está hablando), descartar
        if self.state.ignore_stt:
            return
        
        # Actualizar timestamp de actividad
        self.state.last_audio_activity = time.perf_counter()
        
        # Si Deepgram no está listo, bufferar
        if not self.stt_streamer or not self.stt_streamer._started:
            await self._buffer_audio(audio_bytes)
            return
        
        # Enviar a Deepgram
        try:
            await self.stt_streamer.send_audio(audio_bytes)
        except Exception as e:
            logger.error(f"❌ Error enviando audio a Deepgram: {e}")
            await self._buffer_audio(audio_bytes)
    
    async def _buffer_audio(self, audio_bytes: bytes) -> None:
        """
        💾 Guarda audio en buffer cuando STT no está disponible
        
        Args:
            audio_bytes: Chunk de audio a guardar
        """
        async with self.audio_buffer_lock:
            chunk_size = len(audio_bytes)
            
            # Verificar límite del buffer
            if self.buffer_size + chunk_size <= AUDIO_CONFIG["BUFFER_MAX_SIZE"]:
                self.audio_buffer.append(audio_bytes)
                self.buffer_size += chunk_size
                logger.debug(f"🎙️ Audio buffereado. Total: {self.buffer_size} bytes")
            else:
                logger.warning("⚠️ Buffer de audio lleno. Descartando chunk.")
    
    async def _flush_audio_buffer(self) -> None:
        """
        🚿 Vacía el buffer enviando todo a Deepgram
        """
        if not self.stt_streamer or not self.stt_streamer._started:
            return
            
        async with self.audio_buffer_lock:
            if not self.audio_buffer:
                return
                
            logger.info(f"🚿 Vaciando buffer de audio: {len(self.audio_buffer)} chunks")
            
            for chunk in self.audio_buffer:
                try:
                    await self.stt_streamer.send_audio(chunk)
                except Exception as e:
                    logger.error(f"❌ Error vaciando buffer: {e}")
                    break
            
            self.audio_buffer.clear()
            self.buffer_size = 0
    
    def _handle_transcript(self, transcript: str, is_final: bool) -> None:
        """
        📝 Callback interno cuando Deepgram devuelve transcripción
        
        Args:
            transcript: Texto transcrito
            is_final: True si es transcripción final, False si es parcial
        """
        # Ignorar si estamos en modo silencio
        if self.state.ignore_stt:
            logger.debug(f"🚫 Transcripción ignorada (IA hablando): '{transcript[:50]}...'")
            return
        
        # Pasar al callback externo
        if self.on_transcript:
            self.on_transcript(transcript, is_final)
    
    # ========== MANEJO DE AUDIO SALIENTE (IA → Usuario) ==========
    
    async def prepare_tts_ws(self) -> bool:
        """
        Prepara el WebSocket de ElevenLabs para TTS:
        - Si no está abierto, lo abre y espera.
        - Si está abierto pero inactivo, envía un keepalive.
        - Devuelve True si el WS está listo, False si no se pudo abrir.
        """
        t0 = time.perf_counter()
        logger.info("[LATENCIA] Iniciando preparación de WebSocket ElevenLabs para TTS...")
        if not self.tts_client or not hasattr(self.tts_client, '_ws') or not self.tts_client._ws or getattr(self.tts_client._ws, "closed", False):
            logger.info("🔄 WebSocket de ElevenLabs no disponible, intentando abrir...")
            ok = await self.initialize_tts()
            if not ok:
                logger.error("❌ No se pudo abrir el WebSocket de ElevenLabs")
                logger.info(f"[LATENCIA] Preparación de WS ElevenLabs FALLÓ en {1000*(time.perf_counter()-t0):.1f} ms")
                return False
            logger.info(f"[LATENCIA] WebSocket ElevenLabs abierto en {1000*(time.perf_counter()-t0):.1f} ms")
            return True
        else:
            # Si está abierto, enviar keepalive si han pasado >10s desde el último uso
            now = time.perf_counter()
            if self.last_chunk_time and (now - self.last_chunk_time) > 10:
                try:
                    await self.tts_client._ws.send(json.dumps({"text": " "}))
                    logger.debug("💓 Keepalive enviado a ElevenLabs (por inactividad)")
                except Exception as e:
                    logger.warning(f"⚠️ Error enviando keepalive a ElevenLabs: {e}")
            logger.info(f"[LATENCIA] WebSocket ElevenLabs ya estaba abierto, preparación en {1000*(time.perf_counter()-t0):.1f} ms")
            return True

    async def on_user_pause_prepare_tts(self):
        """
        Apaga STT y prepara el WebSocket de ElevenLabs antes de procesar con LLM.
        Llamar esto justo antes de enviar el texto al LLM.
        """
        logger.info("[FUNCIONALIDAD] Apagando STT y preparando TTS antes de LLM...")
        self.state.ignore_stt = True  # Apaga STT
        self.state.tts_in_progress = False
        ws_ready = await self.prepare_tts_ws()
        if not ws_ready:
            logger.warning("⚠️ No se pudo preparar el WebSocket de ElevenLabs antes del TTS")
        return ws_ready

    async def speak(self, text: str, on_complete: Optional[Callable] = None) -> bool:
        """
        🔊 Convierte texto a audio y lo envía a Twilio
        
        Args:
            text: Texto a convertir
            on_complete: Callback cuando termina
            
        Returns:
            bool: True si se inició correctamente
        """
        if not text.strip():
            logger.warning("⚠️ Texto vacío para TTS")
            return False
        
        # NUEVO: Usar lock para evitar duplicación de TTS
        async with self.tts_lock:
            # Si ya estamos procesando este texto, ignorar
            if self.current_tts_text == text:
                logger.warning(f"⚠️ TTS duplicado ignorado: '{text[:30]}...'")
                return False
            
            # Si hay otro TTS en progreso, esperar
            if self.state.tts_in_progress:
                logger.warning("⚠️ TTS en progreso, ignorando nuevo texto")
                return False
            
            # Marcar este texto como en progreso
            self.current_tts_text = text
        
        logger.info(f"🔊 Iniciando TTS: '{text[:50]}...' ({len(text)} chars)")
        t0 = time.perf_counter()
        
        # Configurar callback
        self.on_tts_complete = on_complete
        self.state.tts_in_progress = True
        self.state.is_speaking = True
        self.state.ignore_stt = True  # Ignorar entrada mientras habla
        
        # FIX: Limpiar buffer de Twilio ANTES de cualquier intento
        await self._clear_twilio_buffer()
        
        # Intentar WebSocket primero (baja latencia)
        ws_success = await self._try_websocket_tts(text)
        
        if ws_success:
            logger.info(f"[LATENCIA] WebSocket TTS iniciado en {1000*(time.perf_counter()-t0):.1f} ms")
            return True
        else:
            logger.warning("⚠️ WebSocket TTS falló, usando fallback HTTP")
            # FIX: Limpiar buffer de nuevo antes del fallback HTTP para evitar duplicación
            await self._clear_twilio_buffer()
            # Fallback a HTTP
            await self._http_fallback_tts(text)
            logger.info(f"[LATENCIA] HTTP fallback TTS iniciado en {1000*(time.perf_counter()-t0):.1f} ms")
            return True
    
    async def _try_websocket_tts(self, text: str) -> bool:
        """
        🚀 Intenta TTS con ElevenLabs WebSocket (baja latencia)
        
        Returns:
            bool: True si funcionó, False si falló
        """
        if not self.tts_client:
            # Intentar crear cliente si no existe
            logger.info("🔄 Intentando inicializar TTS WebSocket...")
            await self.initialize_tts()
            
        if not self.tts_client:
            logger.error("❌ No se pudo inicializar TTS WebSocket")
            return False
        
        try:
            # Verificar diagnóstico del cliente
            diagnostics = self.tts_client.get_diagnostics()
            logger.info(f"[DIAGNÓSTICO] TTS WebSocket - Intentos: {diagnostics['connection_attempts']}, "
                       f"Errores: {diagnostics['total_errors']}, Conectado: {diagnostics['is_connected']}")
            
            # Callback para enviar chunks
            async def send_chunk(chunk: bytes):
                await self._send_audio_to_twilio(chunk)
                self.last_chunk_time = time.perf_counter()
            
            # Hablar con timeout más generoso para el primer chunk
            ok = await self.tts_client.speak(
                text,
                on_chunk=send_chunk,
                on_end=self._on_tts_complete,
                timeout_first_chunk=2.0  # Aumentar a 2.0s para mayor estabilidad
            )
            
            if ok:
                logger.info("✅ WebSocket TTS iniciado correctamente")
                # Iniciar detector de stalls
                self.stall_detector_task = asyncio.create_task(
                    self._monitor_tts_stall()
                )
                return True
            else:
                logger.error("❌ WebSocket TTS falló en speak()")
                # FIX: Cancelar cualquier tarea de stall que se haya iniciado
                if self.stall_detector_task:
                    self.stall_detector_task.cancel()
                    self.stall_detector_task = None
                return False
                
        except Exception as e:
            logger.error(f"❌ Error en WebSocket TTS: {e}")
            # Log diagnóstico adicional
            if self.tts_client:
                diagnostics = self.tts_client.get_diagnostics()
                logger.error(f"[DIAGNÓSTICO] Error TTS - Último error: {diagnostics['last_error']}")
            return False
    
    async def _http_fallback_tts(self, text: str) -> None:
        """
        🔄 Fallback a ElevenLabs HTTP (más lento pero confiable)
        """
        t0 = time.perf_counter()
        logger.info("🔄 Usando fallback HTTP TTS...")
        
        try:
            await send_tts_http_to_twilio(
                text=text,
                stream_sid=self.stream_sid,
                websocket_send=self.websocket_send
            )
            # Llamar callback de finalización
            await self._on_tts_complete()
            logger.info(f"[LATENCIA] HTTP fallback TTS completado en {1000*(time.perf_counter()-t0):.1f} ms")
        except Exception as e:
            logger.error(f"❌ Error en HTTP TTS fallback: {e}")
            # Aún así llamar callback para reactivar STT
            await self._on_tts_complete()
    
    async def _send_audio_to_twilio(self, audio_chunk: bytes) -> None:
        """
        📤 Envía chunk de audio a Twilio
        
        Args:
            audio_chunk: Audio μ-law 8kHz
        """
        try:
            payload = base64.b64encode(audio_chunk).decode("ascii")
            await self.websocket_send(json.dumps({
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": payload}
            }))
        except Exception as e:
            logger.error(f"❌ Error enviando audio a Twilio: {e}")
    
    async def _clear_twilio_buffer(self) -> None:
        """
        🧹 Limpia el buffer de Twilio antes de hablar
        """
        try:
            await self.websocket_send(json.dumps({
                "event": "clear",
                "streamSid": self.stream_sid
            }))
            logger.debug("🧹 Buffer de Twilio limpiado")
        except Exception as e:
            logger.error(f"❌ Error limpiando buffer Twilio: {e}")
    
    async def _on_tts_complete(self) -> None:
        """
        ✅ Se ejecuta cuando TTS termina de hablar
        
        Acciones:
        1. Cancela detector de stalls
        2. Reactiva STT
        3. Limpia buffers
        4. Llama callback externo si existe
        5. Limpia el texto actual del lock
        """
        logger.info("[FUNCIONALIDAD] TTS completado, reactivando STT...")
        logger.info("✅ TTS completado")
        
        # Limpiar el texto actual del lock
        async with self.tts_lock:
            self.current_tts_text = None
        
        # Cancelar detector de stalls
        if self.stall_detector_task:
            self.stall_detector_task.cancel()
            self.stall_detector_task = None
        
        # Reactivar STT
        await self.reactivate_stt()
        
        # Callback externo - ejecutar SIEMPRE si existe
        if self.on_tts_complete:
            try:
                if asyncio.iscoroutinefunction(self.on_tts_complete):
                    await self.on_tts_complete()
                else:
                    self.on_tts_complete()
            except Exception as e:
                logger.error(f"❌ Error en callback TTS complete: {e}")
    
    async def _monitor_tts_stall(self) -> None:
        """
        🚨 Detecta si TTS se congela (no envía chunks)
        Si pasan 1.5s sin chunks → asume que falló y reactiva STT
        Solo marca error si no se recibe ningún chunk después del primero y no se recibe fin de stream.
        """
        stall_count = 0
        stall_threshold = 3.0  
        max_stalls = 1  
        
        while self.state.tts_in_progress:
            if self.last_chunk_time:
                elapsed = time.perf_counter() - self.last_chunk_time
                if elapsed > stall_threshold:
                    stall_count += 1
                    logger.warning(f"🚨 TTS stall #{stall_count} detectado! ({elapsed*1000:.1f}ms sin chunks)")
                    if stall_count >= max_stalls:  # Cambiar de 2 a 3
                        logger.error("🚨 TTS stall persistente! Reactivando STT")
                        await self._on_tts_complete()
                        break
                else:
                    stall_count = 0  # Reset si recibimos chunks
            await asyncio.sleep(0.3)  # Aumentar de 0.2 a 0.3
    
    async def reactivate_stt(self) -> None:
        """
        🟢 Reactiva el STT después de que IA termina de hablar
        
        Pasos:
        1. Limpia buffers acumulados
        2. Marca estado como "escuchando"
        3. Envía marca a Twilio
        """
        logger.info("🟢 Reactivando STT")
        
        # Limpiar buffer de audio acumulado
        async with self.audio_buffer_lock:
            if self.buffer_size > 0:
                logger.info(f"🧹 Descartando {self.buffer_size} bytes de audio buffereado")
                self.audio_buffer.clear()
                self.buffer_size = 0
        
        # Cambiar estado
        self.state.ignore_stt = False
        self.state.tts_in_progress = False
        self.state.is_speaking = False
        
        # Notificar a Twilio
        try:
            await self.websocket_send(json.dumps({
                "event": "mark",
                "streamSid": self.stream_sid,
                "mark": {"name": "end_of_tts"}
            }))
        except Exception as e:
            logger.debug(f"No se pudo enviar mark end_of_tts: {e}")
    
    # ========== LIMPIEZA Y CIERRE ==========
    
    async def shutdown(self) -> None:
        """
        🔌 Cierra todas las conexiones de audio
        """
        logger.info("🔌 Cerrando AudioManager...")
        
        # Cerrar STT
        if self.stt_streamer:
            try:
                await self.stt_streamer.close()
                logger.info("✅ Deepgram STT cerrado")
            except Exception as e:
                logger.error(f"❌ Error cerrando Deepgram: {e}")
            finally:
                self.stt_streamer = None
        
        # Cerrar TTS
        if self.tts_client:
            try:
                await self.tts_client.close()
                logger.info("✅ ElevenLabs TTS cerrado")
            except Exception as e:
                logger.error(f"❌ Error cerrando ElevenLabs: {e}")
            finally:
                self.tts_client = None
        
        # Limpiar buffers
        async with self.audio_buffer_lock:
            self.audio_buffer.clear()
            self.buffer_size = 0
        
        logger.info("✅ AudioManager cerrado completamente")
    
    # ========== UTILIDADES ==========
    
    def get_state(self) -> AudioState:
        """📊 Obtiene el estado actual del audio"""
        return self.state
    
    def is_ready(self) -> bool:
        """Verifica si el AudioManager está listo para procesar audio"""
        return (
            self.stt_streamer is not None and 
            self.stt_streamer._started
        )
    
    def get_diagnostics(self) -> dict:
        """Retorna métricas de diagnóstico del AudioManager"""
        diagnostics = {
            "stream_sid": self.stream_sid,
            "state": {
                "is_speaking": self.state.is_speaking,
                "ignore_stt": self.state.ignore_stt,
                "tts_in_progress": self.state.tts_in_progress,
                "last_audio_activity": self.state.last_audio_activity
            },
            "buffers": {
                "buffer_size": self.buffer_size,
                "buffer_chunks": len(self.audio_buffer)
            },
            "stt_ready": self.stt_streamer is not None and self.stt_streamer._started if self.stt_streamer else False,
            "tts_ready": self.tts_client is not None
        }
        
        # Agregar diagnóstico del TTS si está disponible
        if self.tts_client:
            tts_diagnostics = self.tts_client.get_diagnostics()
            diagnostics["tts_diagnostics"] = tts_diagnostics
        
        return diagnostics