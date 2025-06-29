"""
Cliente WebSocket de Deepgram TTS — v3
=====================================
• Maneja correctamente hilos: los callbacks del SDK llegan en un *thread*
  diferente; ahora usamos `asyncio.run_coroutine_threadsafe` para
  despachar las corrutinas al *event loop* principal.
• Evita el error «no running event loop» y los `send_raw() failed`.

"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Awaitable, Callable, Optional

from deepgram import DeepgramClient, SpeakOptions, SpeakWebSocketEvents
from fastapi import logger

ChunkCallback = Callable[[bytes], Awaitable[None]]
EndCallback = Callable[[], Awaitable[None]]


class DeepgramTTSSocketClient:
    """Cliente único para TTS en streaming vía WebSocket."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        encoding: str = "mulaw",
        sample_rate: int = 8000,
        
    ) -> None:
        # API‑key: DEEPGRAM_API_KEY > DEEPGRAM_KEY > parámetro
        api_key = api_key or os.getenv("DEEPGRAM_API_KEY") or os.getenv("DEEPGRAM_KEY")
        if not api_key:
            raise RuntimeError("Deepgram API-key no encontrada (DEEPGRAM_API_KEY/DEEPGRAM_KEY)")

        # Modelo
        model = model or os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-estrella-es")

        # Loop principal donde despacharemos callbacks
        self._loop = asyncio.get_running_loop()

        # SDK
        self._dg = DeepgramClient(api_key)
        self._conn = self._dg.speak.websocket.v("1")

        # Eventos de coordinación
        self._ws_open = asyncio.Event()
        self._ws_close = asyncio.Event()
        self._first_chunk: Optional[asyncio.Event] = None

        # Callbacks usuario
        self._user_chunk: Optional[ChunkCallback] = None
        self._user_end: Optional[EndCallback] = None

    

        # Opciones (dict para SDK 3.10+)
        self._options = SpeakOptions(
            model=model,
            encoding=encoding,
            sample_rate=sample_rate,
        ).to_dict()

        # Registrar handlers (ejecutan en *thread* del SDK)
        self._conn.on(SpeakWebSocketEvents.Open, self._on_open)
        self._conn.on(SpeakWebSocketEvents.AudioData, self._on_audio)
        self._conn.on(SpeakWebSocketEvents.Close, self._on_close)
        self._conn.on(SpeakWebSocketEvents.Metadata, self._on_metadata) # Nuevo: metadata   
        # Arrancar
        if not self._conn.start(self._options):
            raise RuntimeError("No se pudo iniciar conexión WebSocket con Deepgram")

    # ──────────────────────────── Handlers internos (en thread) ────────────────────────────

    

    def _on_metadata(self, *args, **kwargs):
        """Maneja el evento de metadatos (fin de TTS)"""
        logger.info("🔚 Evento de metadatos recibido (fin de TTS)")
        # Log detallado de los argumentos recibidos
        logger.debug(f"Metadata args: {args} | kwargs: {kwargs}") 
        
        if self._user_end:
            logger.debug("Ejecutando callback de fin de usuario")
            try:
                asyncio.run_coroutine_threadsafe(self._user_end(), self._loop)
            except Exception as e:
                logger.error(f"Error ejecutando user_end: {e}")


    def _on_open(self, *_):
        self._loop.call_soon_threadsafe(self._ws_open.set)


    def _on_audio(self, *args, **kwargs):
        """
        Recibe audio de Deepgram (bytes) y lo re-envía al callback del usuario.
        También marca la llegada del primer chunk para desactivar el fallback.
        """
        data = None

        # ① Buscamos bytes en los argumentos posicionales
        for arg in args:
            if isinstance(arg, (bytes, bytearray)):
                data = arg
                break

        # ② Si no apareció, revisamos kwargs (clave 'data')
        if data is None:
            maybe = kwargs.get("data")
            if isinstance(maybe, (bytes, bytearray)):
                data = maybe

        if not data:
            return  # nada que hacer

        # ── NUEVO: llegó el primer chunk → avisa al semáforo ────────────────
        if self._first_chunk and not self._first_chunk.is_set():
            self._loop.call_soon_threadsafe(self._first_chunk.set)
            logger.info("🟢 Deepgram: primer chunk recibido; fallback desactivado.")

        # Reenvía el audio al callback del usuario (puede ser async o normal)
        if self._user_chunk:
            if asyncio.iscoroutinefunction(self._user_chunk):
                asyncio.run_coroutine_threadsafe(self._user_chunk(data), self._loop)
            else:
                self._loop.call_soon_threadsafe(self._user_chunk, data)

        logger.debug(f"🔊 Audio chunk recibido: {len(data)} bytes")



    def _on_close(self, *_):
        if self._user_end:
            asyncio.run_coroutine_threadsafe(self._user_end(), self._loop)
        self._loop.call_soon_threadsafe(self._ws_close.set)
 


    # ─────────────────────────────────── API pública ────────────────────────────────────
    async def speak(
        self,
        text: str,
        on_chunk: ChunkCallback,
        *,
        on_end: Optional[EndCallback] = None,
        timeout_first_chunk: float = 1.0,
    ) -> bool:
        await self._ws_open.wait()

        self._first_chunk = asyncio.Event()
        self._user_chunk = on_chunk
        self._user_end = on_end

        self._conn.send_text(text)
        self._conn.flush()

        try:
            await asyncio.wait_for(self._first_chunk.wait(), timeout_first_chunk)
            return True
        except asyncio.TimeoutError:
            return False



    async def close(self):
        try:
            if not self._ws_close.is_set():
                self._conn.finish()
                await asyncio.wait_for(self._ws_close.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.warning("Timeout al cerrar conexión TTS")
        except Exception as e:
            logger.error(f"Error al cerrar TTS: {e}")