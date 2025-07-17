"""
Cliente WebSocket de ElevenLabs TTS — v3 OPTIMIZADO con auto_mode
==============================================================
• Estrategia basada en auto_mode de ElevenLabs para latencia mínima
• Modelo eleven_flash_v2_5 + auto_mode + optimize_streaming_latency
• Envío directo de chunks sin buffer manual
• Reutilización de conexión WebSocket
• Reconexión automática con backoff exponencial
• Logs detallados de diagnóstico y métricas

"""

from __future__ import annotations

import asyncio
import json
import os
import base64
import websockets
import time
import random
from typing import Awaitable, Callable, Optional
import logging

logger = logging.getLogger(__name__)

ChunkCallback = Callable[[bytes], Awaitable[None]]
EndCallback = Callable[[], Awaitable[None]]


class ElevenLabsWSClient:
    """Cliente optimizado para TTS streaming con latencia mínima usando auto_mode."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        voice_id: str | None = None,
        model_id: str = "eleven_multilingual_v2",  
    ) -> None:
        # API key: ELEVEN_LABS_API_KEY > parámetro
        self.api_key = api_key or os.getenv("ELEVEN_LABS_API_KEY")
        if not self.api_key:
            raise RuntimeError("ElevenLabs API key no encontrada (ELEVEN_LABS_API_KEY)")

        # Voice ID: ELEVEN_LABS_VOICE_ID > parámetro
        self.voice_id = voice_id or os.getenv("ELEVEN_LABS_VOICE_ID")
        if not self.voice_id:
            raise RuntimeError("ElevenLabs Voice ID no encontrado (ELEVEN_LABS_VOICE_ID)")

        self.model_id = model_id

        # Loop principal donde despacharemos callbacks
        self._loop = asyncio.get_running_loop()

        # WebSocket connection - REUTILIZABLE
        self._ws = None
        self._ws_task = None

        # Eventos de coordinación
        self._ws_open = asyncio.Event()
        self._ws_close = asyncio.Event()
        self._first_chunk: Optional[asyncio.Event] = None

        # Callbacks usuario
        self._user_chunk: Optional[ChunkCallback] = None
        self._user_end: Optional[EndCallback] = None

        # Control de estado
        self._is_speaking = False
        self._should_close = False
        self._chunk_counter = 0
        self._send_time = 0.0

        # Nueva bandera para control de cierre
        self._closing = False

        # Métricas de robustez
        self._connection_attempts = 0
        self._max_reconnect_attempts = 3
        self._reconnect_delay = 1.0
        self._last_error = None
        self._connection_start_time = 0.0
        self._total_audio_chunks = 0
        self._total_errors = 0
       
        self.voice_settings = {
            "stability": 0.5,
            "style": 0.2,
            #"similarity_boost": 0.4,
            "use_speaker_boost": False,
            "speed": 1.2,
        }

        # Iniciar conexión WebSocket REUTILIZABLE
        self._start_connection()

    def _start_connection(self):
        """Inicia la conexión WebSocket reutilizable"""
        logger.info("[FUNCIONALIDAD] Iniciando conexión WebSocket ElevenLabs...")
        self._ws_open_start = time.perf_counter()
        self._connection_start_time = time.perf_counter()
        self._ws_task = asyncio.create_task(self._run_websocket())

    async def _run_websocket(self):
        """Maneja la conexión WebSocket persistente con reconexión automática"""
        while self._connection_attempts < self._max_reconnect_attempts and not self._closing:
            try:
                await self._attempt_connection()
                break  # Si llegamos aquí, la conexión fue exitosa
            except Exception as e:
                self._connection_attempts += 1
                self._last_error = str(e)
                self._total_errors += 1
                
                logger.error(f"❌ Intento {self._connection_attempts}/{self._max_reconnect_attempts} falló: {e}")
                
                if self._connection_attempts < self._max_reconnect_attempts:
                    # Backoff exponencial con jitter
                    delay = self._reconnect_delay * (2 ** (self._connection_attempts - 1))
                    jitter = random.uniform(0.1, 0.3) * delay
                    total_delay = delay + jitter
                    
                    logger.info(f"🔄 Reintentando en {total_delay:.1f}s (backoff exponencial)")
                    await asyncio.sleep(total_delay)
                else:
                    logger.error("❌ Máximo de intentos de reconexión alcanzado")
                    break

    async def _attempt_connection(self):
        """Intenta establecer una conexión WebSocket individual"""
        # ✅ URL optimizada con parámetros de latencia máxima
        url = f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream-input?model_id={self.model_id}&output_format=ulaw_8000&optimize_streaming_latency=4"
        headers = {"xi-api-key": self.api_key}

        try:
            logger.debug(f"🔌 Conectando a ElevenLabs WebSocket optimizado: {url}")
            t0 = time.perf_counter()
            # Asegurar que headers no contenga None
            clean_headers = {k: v for k, v in headers.items() if v is not None}
            
            # Timeout de conexión más agresivo
            async with websockets.connect(url, additional_headers=clean_headers, close_timeout=5.0) as ws:
                self._ws = ws
                self._connection_attempts = 0  # Resetear contador en éxito
                logger.info("🟢 ElevenLabs WebSocket conectado (reutilizable)")
                logger.info(f"[LATENCIA] WebSocket ElevenLabs abierto en {1000*(time.perf_counter()-t0):.1f} ms")
                
                # ✅ Configuración inicial con auto_mode (EL maneja chunks automáticamente)
                config_message = {
                    "text": " ",  # Texto inicial vacío
                    "voice_settings": self.voice_settings,
                    "generation_config": {
                        "auto_mode": True  # EL decide cuándo enviar audio
                    }
                }
                
                await ws.send(json.dumps(config_message))
                logger.debug("⚙️ Configuración auto_mode enviada")
                
                # Marcar como conectado
                self._loop.call_soon_threadsafe(self._ws_open.set)

                # ✅ Iniciar tarea de keepalive
                keepalive_task = asyncio.create_task(self._keepalive_loop())
                
                # Bucle de recepción
                async for message in ws:
                    if self._closing:  # Verificar si estamos en proceso de cierre
                        break
                        
                    try:
                        data = json.loads(message)
                        await self._handle_message(data)
                    except json.JSONDecodeError:
                        logger.warning(f"⚠️ Mensaje no JSON recibido: {message[:100]}")
                    except Exception as e:
                        logger.error(f"❌ Error procesando mensaje: {e}")
                        self._total_errors += 1

        except websockets.exceptions.ConnectionClosed as e:
            logger.warning(f"🔌 WebSocket cerrado por el servidor: {e}")
            raise
        except Exception as e:
            if "401" in str(e) or "403" in str(e) or "authentication" in str(e).lower():
                logger.error(f"❌ Error de autenticación/API: {e}")
            elif "timeout" in str(e).lower():
                logger.error("⏰ Timeout en conexión WebSocket")
            else:
                logger.error(f"❌ Error inesperado en WebSocket ElevenLabs: {e}")
            raise
        finally:
            # ✅ Marcar que estamos cerrando y cancelar keepalive
            self._closing = True
            logger.info("[FUNCIONALIDAD] Cerrando WebSocket ElevenLabs...")
            if hasattr(self, '_ws_open_start'):
                duration = time.perf_counter() - self._ws_open_start
                logger.info(f"[LATENCIA] WebSocket ElevenLabs estuvo abierto durante {1000*duration:.1f} ms")
                logger.info(f"[DIAGNÓSTICO] Chunks de audio procesados: {self._total_audio_chunks}")
                logger.info(f"[DIAGNÓSTICO] Errores totales: {self._total_errors}")
            
            # Cancelar tarea de keepalive si existe
            if 'keepalive_task' in locals():
                try:
                    if isinstance(keepalive_task, asyncio.Task) and not keepalive_task.done():
                        keepalive_task.cancel()
                        await keepalive_task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    logger.debug(f"Error al cancelar keepalive: {e}")

            self._ws = None
            self._loop.call_soon_threadsafe(self._ws_close.set)
            logger.info("🔒 ElevenLabs WebSocket cerrado")

    def _clean_mp3_headers(self, audio_bytes: bytes) -> bytes:
        """Remueve headers ID3 del MP3, manteniendo solo datos de audio"""
        if audio_bytes[:3] == b"ID3":
            # Header ID3v2: ID3 + version(2) + flags(1) + size(4)
            if len(audio_bytes) >= 10:
                # Los bytes 6-9 contienen el tamaño del header ID3
                size_bytes = audio_bytes[6:10]
                # Decodificar tamaño syncsafe (7 bits por byte)
                header_size = 0
                for b in size_bytes:
                    header_size = (header_size << 7) | (b & 0x7F)
                header_size += 10  # Añadir los 10 bytes del header básico
                
                if len(audio_bytes) > header_size:
                    logger.debug(f"🧹 Removiendo header ID3 de {header_size} bytes")
                    return audio_bytes[header_size:]
                else:
                    logger.warning(f"⚠️ Header ID3 más grande que el audio ({header_size} vs {len(audio_bytes)})")
                    return audio_bytes
            else:
                logger.warning("⚠️ Header ID3 incompleto")
                return audio_bytes
        
        # Si no hay header ID3, devolver como está
        return audio_bytes

    async def _handle_message(self, data: dict):
        """Procesa mensajes del WebSocket con logs detallados"""
        
        # Mensaje de audio
        if "audio" in data:
            audio_b64 = data["audio"]
            try:
                # Decodificar audio
                audio_bytes = base64.b64decode(audio_b64)
                self._total_audio_chunks += 1
                
                # Limpiar headers MP3 si es necesario
                audio_bytes = self._clean_mp3_headers(audio_bytes)
                
                # Log del primer chunk con latencia detallada
                if self._first_chunk and not self._first_chunk.is_set():
                    first_audio_time = time.perf_counter()
                    if hasattr(self, '_send_time') and self._send_time > 0:
                        delta_ms = (first_audio_time - self._send_time) * 1000
                        logger.info(f"⏱️ [LATENCIA-4-FIRST] EL primer audio chunk: {delta_ms:.1f} ms")
                        logger.info(f"[DIAGNÓSTICO] Primer chunk recibido tras {self._total_audio_chunks} intentos")
                    self._loop.call_soon_threadsafe(self._first_chunk.set)
                
                # Enviar chunk al callback
                if self._user_chunk:
                    if asyncio.iscoroutinefunction(self._user_chunk):
                        asyncio.run_coroutine_threadsafe(
                            self._user_chunk(audio_bytes), self._loop
                        )
                    else:
                        self._loop.call_soon_threadsafe(self._user_chunk, audio_bytes)
                
                logger.debug(f"🔊 Chunk μ-law enviado: {len(audio_bytes)} bytes (total: {self._total_audio_chunks})")
                
            except Exception as e:
                logger.error(f"❌ Error procesando audio: {e}")
                self._total_errors += 1

        # Fin de stream
        if data.get("isFinal", False):
            logger.info("🔚 ElevenLabs: fin de stream recibido")
            if hasattr(self, '_send_time') and self._send_time > 0:
                total_time = time.perf_counter() - self._send_time
                logger.info(f"[FUNCIONALIDAD] Fin de stream ElevenLabs recibido tras {1000*total_time:.1f} ms desde envío de texto.")
                logger.info(f"[DIAGNÓSTICO] Chunks procesados en esta sesión: {self._total_audio_chunks}")
            if self._user_end:
                if asyncio.iscoroutinefunction(self._user_end):
                    asyncio.run_coroutine_threadsafe(self._user_end(), self._loop)
                else:
                    self._loop.call_soon_threadsafe(self._user_end)

        # Mensajes de error
        if "error" in data:
            error_msg = data["error"]
            logger.error(f"❌ Error de ElevenLabs: {error_msg}")
            self._total_errors += 1

    async def _keepalive_loop(self):
        """Envía espacios cada 15 segundos para mantener viva la conexión"""
        keepalive_count = 0
        while not self._closing:
            try:
                if self._ws:
                    try:
                        # Verificar si el WebSocket está cerrado de forma segura
                        if not getattr(self._ws, 'closed', True):
                            await self._ws.send(json.dumps({"text": " "}))
                            keepalive_count += 1
                            logger.debug(f"💓 Keepalive #{keepalive_count} enviado a ElevenLabs")
                        else:
                            logger.warning("⚠️ WebSocket cerrado durante keepalive")
                            break
                    except Exception as e:
                        logger.warning(f"⚠️ Error en keepalive: {e}")
                        break
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                logger.debug("🔄 Keepalive cancelado")
                break
            except Exception as e:
                logger.debug(f"Error en keepalive: {e}")
                break

    # ─────────────────────────────────── API pública ────────────────────────────────────

    async def add_text_chunk(self, text_chunk: str) -> bool:
        """
        Envía chunks directamente a EL con auto_mode (sin buffer manual).
        """
        if not self._ws:
            logger.error("❌ WebSocket no disponible para chunk")
            return False

        if not text_chunk.strip():
            return False
            
        try:
            message = {"text": text_chunk.strip()}
            
            logger.info(f"📤 Chunk directo a EL: '{text_chunk.strip()[:40]}...' ({len(text_chunk.strip())} chars)")
            
            self._send_time = time.perf_counter()
            await self._ws.send(json.dumps(message))
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Error enviando chunk directo: {e}")
            self._total_errors += 1
            return False

    async def finalize_stream(self) -> bool:
        """
        Finaliza el stream enviando EOS (End of Sequence).
        """
        if not self._ws:
            logger.error("❌ WebSocket no disponible para finalizar stream")
            return False
            
        try:
            # Enviar EOS (End of Sequence)
            await self._ws.send(json.dumps({"text": ""}))
            logger.debug("📤 EOS enviado")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Error finalizando stream: {e}")
            self._total_errors += 1
            return False

    async def speak(
        self,
        text: str,
        on_chunk: ChunkCallback,
        *,
        on_end: Optional[EndCallback] = None,
        timeout_first_chunk: float = 1.0,
    ) -> bool:
        """
        API compatible con versión anterior para texto completo.
        Para streaming real usar add_text_chunk() + finalize_stream()
        """
        t0 = time.perf_counter()
        # Esperar conexión con timeout más agresivo
        try:
            await asyncio.wait_for(self._ws_open.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            logger.error("❌ Timeout esperando conexión ElevenLabs")
            return False

        if not self._ws:
            logger.error("❌ WebSocket no disponible")
            return False

        # Configurar callbacks
        self._first_chunk = asyncio.Event()
        self._user_chunk = on_chunk
        self._user_end = on_end
        self._is_speaking = True

        try:
            # Mensaje completo sin auto_mode (usando chunk_length_schedule)
            message = {
                "text": text,
                "voice_settings": self.voice_settings
            }
            
            self._send_time = time.perf_counter()
            await self._ws.send(json.dumps(message))
            logger.info(f"⏱️ [LATENCIA-4-START] EL WS texto enviado: {len(text)} chars (modo legacy)")

            # Enviar EOS
            await self._ws.send(json.dumps({"text": ""}))

            # Esperar primer chunk con timeout más agresivo
            try:
                await asyncio.wait_for(self._first_chunk.wait(), timeout_first_chunk)
                logger.info(f"[LATENCIA] Primer chunk de audio recibido en {1000*(time.perf_counter()-t0):.1f} ms")
                return True
            except asyncio.TimeoutError:
                logger.warning(f"⏰ Timeout ({timeout_first_chunk}s) esperando primer chunk")
                logger.error(f"[DIAGNÓSTICO] Timeout en primer chunk - errores totales: {self._total_errors}")
                return False

        except Exception as e:
            logger.error(f"❌ Error enviando texto a ElevenLabs: {e}")
            self._total_errors += 1
            return False

    async def close(self):
        """Cierra la conexión WebSocket"""
        logger.info("🔒 Cerrando ElevenLabs WebSocket...")
        
        self._should_close = True
        self._closing = True
        
        # Cerrar WebSocket si está abierto
        if self._ws:
            try:
                await self._ws.close()
            except Exception as e:
                logger.debug(f"Error cerrando WebSocket: {e}")

        # Cancelar tarea de WebSocket
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await asyncio.wait_for(self._ws_task, timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("⏰ Timeout cancelando tarea WebSocket")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"❌ Error cancelando tarea: {e}")

        # Esperar cierre final
        try:
            await asyncio.wait_for(self._ws_close.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("⏰ Timeout esperando cierre de WebSocket")

        # Log final de métricas
        if hasattr(self, '_connection_start_time'):
            total_duration = time.perf_counter() - self._connection_start_time
            logger.info(f"[DIAGNÓSTICO] Sesión total: {total_duration:.1f}s, chunks: {self._total_audio_chunks}, errores: {self._total_errors}")

        logger.info("✅ ElevenLabs WebSocket cerrado")

    def get_diagnostics(self) -> dict:
        """Retorna métricas de diagnóstico del cliente"""
        return {
            "connection_attempts": self._connection_attempts,
            "total_audio_chunks": self._total_audio_chunks,
            "total_errors": self._total_errors,
            "last_error": self._last_error,
            "is_connected": self._ws is not None and not getattr(self._ws, 'closed', True),
            "is_speaking": self._is_speaking,
            "closing": self._closing
        }


# Alias para compatibilidad con código existente
DeepgramTTSSocketClient = ElevenLabsWSClient