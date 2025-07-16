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
    
    async def speak(self, text: str, on_complete: Optional[Callable] = None) -> bool:
        """
        🗣️ Convierte texto a voz y lo envía al usuario
        
        Args:
            text: Texto a convertir en voz
            on_complete: Callback cuando termina de hablar
            
        Returns:
            bool: True si se envió correctamente
            
        Proceso:
        1. Activa ignore_stt (silencia entrada)
        2. Limpia buffer de Twilio
        3. Intenta ElevenLabs WebSocket
        4. Si falla → ElevenLabs HTTP (fallback)
        5. Al terminar → reactiva STT
        """
        logger.info(f"🗣️ TTS iniciando: '{text[:50]}...'")
        
        # Guardar callback
        self.on_tts_complete = on_complete
        
        # Activar modo "IA hablando"
        self.state.ignore_stt = True
        self.state.tts_in_progress = True
        
        # Limpiar buffer de Twilio
        await self._clear_twilio_buffer()
        
        # Intentar con WebSocket primero
        success = await self._try_websocket_tts(text)
        
        # Si falla, usar HTTP fallback
        if not success:
            logger.warning("⚠️ WebSocket TTS falló, usando HTTP fallback")
            await self._http_fallback_tts(text)
        
        return True
    
    async def _try_websocket_tts(self, text: str) -> bool:
        """
        🚀 Intenta TTS con ElevenLabs WebSocket (baja latencia)
        
        Returns:
            bool: True si funcionó, False si falló
        """
        if not self.tts_client:
            # Intentar crear cliente si no existe
            await self.initialize_tts()
            
        if not self.tts_client:
            return False
        
        try:
            # Callback para enviar chunks
            async def send_chunk(chunk: bytes):
                await self._send_audio_to_twilio(chunk)
                self.last_chunk_time = time.perf_counter()
            
            # Hablar
            ok = await self.tts_client.speak(
                text,
                on_chunk=send_chunk,
                on_end=self._on_tts_complete,
                timeout_first_chunk=1.0
            )
            
            if ok:
                # Iniciar detector de stalls
                self.stall_detector_task = asyncio.create_task(
                    self._monitor_tts_stall()
                )
                return True
            else:
                return False
                
        except Exception as e:
            logger.error(f"❌ Error en WebSocket TTS: {e}")
            return False
    
    async def _http_fallback_tts(self, text: str) -> None:
        """
        🔄 Fallback a ElevenLabs HTTP (más lento pero confiable)
        """
        try:
            await send_tts_http_to_twilio(
                text=text,
                stream_sid=self.stream_sid,
                websocket_send=self.websocket_send
            )
            # Llamar callback de finalización
            await self._on_tts_complete()
            
        except Exception as e:
            logger.error(f"❌ Error en HTTP TTS fallback: {e}")
            await self._on_tts_complete()
    
    async def _send_audio_to_twilio(self, audio_chunk: bytes) -> None:
        """
        📤 Envía chunk de audio a Twilio
        
        Args:
            audio_chunk: Audio μ-law 8kHz
        """
        payload = base64.b64encode(audio_chunk).decode()
        await self.websocket_send(json.dumps({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": payload}
        }))
    
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
        4. Llama callback externo
        """
        logger.info("✅ TTS completado")
        
        # Cancelar detector de stalls
        if self.stall_detector_task:
            self.stall_detector_task.cancel()
            self.stall_detector_task = None
        
        # Reactivar STT
        await self.reactivate_stt()
        
        # Callback externo
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
        
        Si pasan 300ms sin chunks → asume que falló y reactiva STT
        """
        while self.state.tts_in_progress:
            if self.last_chunk_time:
                elapsed = time.perf_counter() - self.last_chunk_time
                if elapsed > 0.3:  # 300ms sin chunks
                    logger.warning("🚨 TTS stall detectado! Reactivando STT")
                    await self._on_tts_complete()
                    break
            await asyncio.sleep(0.05)
    
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
        """✅ Verifica si todos los servicios están listos"""
        stt_ready = self.stt_streamer and self.stt_streamer._started
        # TTS se puede crear on-demand, así que no es requisito
        return bool(stt_ready)