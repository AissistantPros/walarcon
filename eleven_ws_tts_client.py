"""
Cliente WebSocket de ElevenLabs TTS — v3 OPTIMIZADO con auto_mode
==============================================================
• Estrategia basada en auto_mode de ElevenLabs para latencia mínima
• Modelo eleven_flash_v2_5 + auto_mode + optimize_streaming_latency
• Envío directo de chunks sin buffer manual
• Reutilización de conexión WebSocket

"""

from __future__ import annotations

import asyncio
import json
import os
import base64
import websockets
import time
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
        model_id: str = "eleven_flash_v2_5",  
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

        # ✅ Configuración optimizada según RAG
        self.voice_settings = {
            "stability": 0.5,
            "style": 0.9,
            "similarity_boost": 0.4,
            "use_speaker_boost": False,
            "speed": 1.2,
        }

        # Iniciar conexión WebSocket REUTILIZABLE
        self._start_connection()

    def _start_connection(self):
        """Inicia la conexión WebSocket reutilizable"""
        self._ws_task = asyncio.create_task(self._run_websocket())

    async def _run_websocket(self):
        """Maneja la conexión WebSocket persistente"""
        # ✅ URL optimizada con parámetros de latencia máxima
        url = f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream-input?model_id={self.model_id}&output_format=ulaw_8000&optimize_streaming_latency=4"
        headers = {"xi-api-key": self.api_key}

        try:
            logger.debug(f"🔌 Conectando a ElevenLabs WebSocket optimizado: {url}")
            
            async with websockets.connect(url, additional_headers=headers) as ws:
                self._ws = ws
                logger.info("🟢 ElevenLabs WebSocket conectado (reutilizable)")
                
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

        except Exception as e:
            logger.error(f"❌ Error en WebSocket ElevenLabs: {e}")
        finally:
            # ✅ Marcar que estamos cerrando y cancelar keepalive
            self._closing = True
            
            # Cancelar tarea de keepalive si existe
            if 'keepalive_task' in locals():
                keepalive_task.cancel()
                try:
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
        """Procesa mensajes del WebSocket"""
        
        # Mensaje de audio
        if "audio" in data:
            audio_b64 = data["audio"]
            if audio_b64:  # Solo procesar si hay audio
                try:
                    audio_bytes = base64.b64decode(audio_b64)
                    
                    # 🧹 LIMPIEZA: Remover headers si es necesario
                    if audio_bytes[:3] == b"ID3":
                        logger.debug(f"🧹 Removiendo headers ID3 de audio ({len(audio_bytes)} bytes)")
                        audio_bytes = self._clean_mp3_headers(audio_bytes)
                    elif audio_bytes[:2] == b"\xff\xfb":
                        logger.debug(f"✅ MP3 sin headers ID3, usando directamente ({len(audio_bytes)} bytes)")
                    elif audio_bytes[:4] == b"RIFF":
                        logger.debug("🧹 Removiendo header WAV de 44 bytes")
                        audio_bytes = audio_bytes[44:]  # Quitar header WAV
                    else:
                        logger.debug(f"✅ Audio en formato directo: {len(audio_bytes)} bytes")
                    
                    # Marcar primer chunk si aplica
                    if self._first_chunk and not self._first_chunk.is_set():
                        first_audio_time = time.perf_counter()
                        if hasattr(self, '_send_time') and self._send_time > 0:
                            delta_ms = (first_audio_time - self._send_time) * 1000
                            logger.info(f"⏱️ [LATENCIA-4-FIRST] EL primer audio chunk: {delta_ms:.1f} ms")
                        self._loop.call_soon_threadsafe(self._first_chunk.set)
                    
                    # Enviar chunk al callback
                    if self._user_chunk:
                        if asyncio.iscoroutinefunction(self._user_chunk):
                            asyncio.run_coroutine_threadsafe(
                                self._user_chunk(audio_bytes), self._loop
                            )
                        else:
                            self._loop.call_soon_threadsafe(self._user_chunk, audio_bytes)
                    
                    logger.debug(f"🔊 Chunk μ-law enviado: {len(audio_bytes)} bytes")
                    
                except Exception as e:
                    logger.error(f"❌ Error procesando audio: {e}")

        # Fin de stream
        if data.get("isFinal", False):
            logger.info("🔚 ElevenLabs: fin de stream recibido")
            if self._user_end:
                if asyncio.iscoroutinefunction(self._user_end):
                    asyncio.run_coroutine_threadsafe(self._user_end(), self._loop)
                else:
                    self._loop.call_soon_threadsafe(self._user_end)

        # Mensajes de error
        if "error" in data:
            error_msg = data["error"]
            logger.error(f"❌ Error de ElevenLabs: {error_msg}")


    async def _keepalive_loop(self):
        """Envía espacios cada 15 segundos para mantener viva la conexión"""
        while not self._closing:
            try:
                if self._ws and not self._ws.closed:
                    await self._ws.send(json.dumps({"text": " "}))
                    logger.debug("💓 Keepalive enviado a ElevenLabs")
                await asyncio.sleep(15)
            except asyncio.CancelledError:
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
            return False

    async def finalize_stream(self) -> bool:
        """
        Finaliza el stream enviando EOS (End of Sequence).
        """
        try:
            # Enviar EOS (End of Sequence)
            await self._ws.send(json.dumps({"text": ""}))
            logger.debug("📤 EOS enviado")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Error finalizando stream: {e}")
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
        
        # Esperar conexión
        try:
            await asyncio.wait_for(self._ws_open.wait(), timeout=5.0)
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

            # Esperar primer chunk
            try:
                await asyncio.wait_for(self._first_chunk.wait(), timeout_first_chunk)
                return True
            except asyncio.TimeoutError:
                logger.warning(f"⏰ Timeout ({timeout_first_chunk}s) esperando primer chunk")
                return False

        except Exception as e:
            logger.error(f"❌ Error enviando texto a ElevenLabs: {e}")
            return False

    async def close(self):
        """Cierra la conexión WebSocket"""
        logger.info("🔒 Cerrando ElevenLabs WebSocket...")
        
        self._should_close = True
        
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

        logger.info("✅ ElevenLabs WebSocket cerrado")


# Alias para compatibilidad con código existente
DeepgramTTSSocketClient = ElevenLabsWSClient