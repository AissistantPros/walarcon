"""
Cliente WebSocket de ElevenLabs TTS — v1
========================================
• Reemplazo directo para DeepgramTTSSocketClient
• Maneja threading correctamente usando asyncio.run_coroutine_threadsafe
• Formato μ-law 8kHz nativo para Twilio
• Misma interfaz que el cliente de Deepgram

"""

from __future__ import annotations

import asyncio
import json
import os
import base64
import websockets
from typing import Awaitable, Callable, Optional
import logging

logger = logging.getLogger(__name__)

ChunkCallback = Callable[[bytes], Awaitable[None]]
EndCallback = Callable[[], Awaitable[None]]


class ElevenLabsWSClient:
    """Cliente único para TTS en streaming vía WebSocket con ElevenLabs."""

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

        # WebSocket connection
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

        # Configuración de voz (misma que HTTP)
        self.voice_settings = {
            "stability": 0.75,
            "style": 0.45,
            "use_speaker_boost": True,
            "speed": 1.2,
        }

        # Iniciar conexión WebSocket
        self._start_connection()

    def _start_connection(self):
        """Inicia la conexión WebSocket en background"""
        self._ws_task = asyncio.create_task(self._run_websocket())

    async def _run_websocket(self):
        """Maneja la conexión WebSocket"""
        url = f"wss://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream-input?model_id={self.model_id}"
        headers = {"xi-api-key": self.api_key}

        try:
            logger.debug(f"🔌 Conectando a ElevenLabs WebSocket: {url}")
            
            async with websockets.connect(url, additional_headers=headers) as ws:
                self._ws = ws
                logger.info("🟢 ElevenLabs WebSocket conectado")
                
                # Configurar formato de salida para μ-law 8kHz
                config_message = {
                    "xi_api_key": self.api_key,
                    "voice_settings": self.voice_settings,
                    "generation_config": {
                        "chunk_length_schedule": [120, 160, 250, 290]
                    },
                    "output_format": {
                        "container": "raw",
                        "encoding": "ulaw_8000"
                    }
                }
                
                await ws.send(json.dumps(config_message))
                
                # Marcar como conectado
                self._loop.call_soon_threadsafe(self._ws_open.set)

                # Bucle de recepción
                async for message in ws:
                    if self._should_close:
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
            self._ws = None
            self._loop.call_soon_threadsafe(self._ws_close.set)
            logger.info("🔒 ElevenLabs WebSocket cerrado")

    async def _handle_message(self, data: dict):
        """Procesa mensajes del WebSocket"""
        
        # Mensaje de audio
        if "audio" in data:
            audio_b64 = data["audio"]
            if audio_b64:  # Solo procesar si hay audio
                try:
                    audio_bytes = base64.b64decode(audio_b64)
                    
                    # Marcar primer chunk si aplica
                    if self._first_chunk and not self._first_chunk.is_set():
                        self._loop.call_soon_threadsafe(self._first_chunk.set)
                        logger.info("🟢 ElevenLabs: primer chunk recibido")
                    
                    # Enviar chunk al callback
                    if self._user_chunk:
                        if asyncio.iscoroutinefunction(self._user_chunk):
                            asyncio.run_coroutine_threadsafe(
                                self._user_chunk(audio_bytes), self._loop
                            )
                        else:
                            self._loop.call_soon_threadsafe(self._user_chunk, audio_bytes)
                    
                    logger.debug(f"🔊 Chunk de audio: {len(audio_bytes)} bytes")
                    
                except Exception as e:
                    logger.error(f"❌ Error decodificando audio: {e}")

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

    # ─────────────────────────────────── API pública ────────────────────────────────────

    async def speak(
        self,
        text: str,
        on_chunk: ChunkCallback,
        *,
        on_end: Optional[EndCallback] = None,
        timeout_first_chunk: float = 1.0,
    ) -> bool:
        """
        Envía texto para convertir a voz.
        
        Args:
            text: Texto a convertir
            on_chunk: Callback para cada chunk de audio
            on_end: Callback al finalizar (opcional)
            timeout_first_chunk: Timeout para el primer chunk
            
        Returns:
            True si el primer chunk llegó a tiempo, False si timeout
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
            # Enviar texto
            message = {
                "text": text,
                "voice_settings": self.voice_settings,
                "generation_config": {
                    "chunk_length_schedule": [120, 160, 250, 290]
                }
            }
            
            await self._ws.send(json.dumps(message))
            logger.debug(f"📤 Texto enviado a ElevenLabs: {text[:50]}...")

            # Enviar fin de input para iniciar generación
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