"""
Cliente WebSocket de Deepgram TTS (v2)
=====================================

• **Conexión temprana**: se abre al instanciarse para que ya esté viva
  cuando Twilio empiece la llamada.
• **Método `speak(text, on_chunk, on_end=None)`**
  - Reenvía cada trozo de audio a `on_chunk(bytes)`.
  - Llama a `on_end()` (si lo proporcionas) cuando Deepgram cierra el
    socket → indicador fiable de que ya terminó de “hablar”.
  - Devuelve `True` si llegó audio a tiempo, `False` si hubo timeout.
• **Ping Keep‑Alive**: mientras la conexión permanezca abierta, se envía
  cada `keepalive_interval` seg. un JSON `{"type": "KeepAlive"}` para
  evitar que el WS se cierre por inactividad.    
• **Cierre limpio** con `close()`.

Ejemplo de uso
--------------
```python
client = DeepgramTTSSocketClient()

async def handle_chunk(chunk: bytes):
    twilio_ws.send(chunk)

async def handle_end():
    stt_streamer.resume()  # Vuelve a escuchar

ok = await client.speak("Buenos días", handle_chunk, on_end=handle_end)
if not ok:
    eleven_labs_tts("Buenos días", twilio_ws)
```
"""

from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable, Optional

from deepgram import DeepgramClient, SpeakWebSocketEvents, SpeakOptions

# Tipo para el callback de datos y fin
ChunkCallback = Callable[[bytes], Awaitable[None]]
EndCallback = Callable[[], Awaitable[None]]


class DeepgramTTSSocketClient:
    """Gestiona una única conexión WebSocket de Deepgram TTS."""

    def __init__(
        self,
        model: str = "aura-2-estrella-es",
        encoding: str = "mulaw",
        sample_rate: int = 8000,
        keepalive_interval: float = 3.0,
    ) -> None:
        self._dg = DeepgramClient()
        self._conn = self._dg.speak.websocket.v("1")

        # Eventos de control
        self._ws_open = asyncio.Event()
        self._ws_close = asyncio.Event()
        self._first_chunk: Optional[asyncio.Event] = None

        # Callbacks de usuario
        self._user_chunk: Optional[ChunkCallback] = None
        self._user_end: Optional[EndCallback] = None

        # Keep‑alive
        self._keepalive_interval = keepalive_interval
        self._keepalive_task: Optional[asyncio.Task] = None

        # Opciones de síntesis
        self._options = SpeakOptions(
            model=model,
            encoding=encoding,
            sample_rate=sample_rate,
        )

        # Registrar manejadores internos
        self._conn.on(SpeakWebSocketEvents.Open, self._on_open)
        self._conn.on(SpeakWebSocketEvents.AudioData, self._on_audio)
        self._conn.on(SpeakWebSocketEvents.Close, self._on_close)

        # Arrancar conexión
        if not self._conn.start(self._options):
            raise RuntimeError("No se pudo iniciar conexión WebSocket con Deepgram")

    # ────────────────────────────── Handlers internos ─────────────────────────────
    def _on_open(self, *_):  # tipo: ignore[no-self-use]
        self._ws_open.set()
        # Lanzar ping keep‑alive en segundo plano
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())

    def _on_audio(self, data: bytes, **_):
        if self._user_chunk:
            asyncio.create_task(self._user_chunk(data))
        if self._first_chunk is not None and not self._first_chunk.is_set():
            self._first_chunk.set()

    def _on_close(self, *_):  # tipo: ignore[no-self-use]
        if self._user_end:
            asyncio.create_task(self._user_end())
        self._ws_close.set()
        # Detener keep‑alive
        if self._keepalive_task:
            self._keepalive_task.cancel()

    async def _keepalive_loop(self) -> None:
        """Envía un JSON {"type": "KeepAlive"} cada N segundos."""
        try:
            while True:
                await asyncio.sleep(self._keepalive_interval)
                try:
                    msg = json.dumps({"type": "KeepAlive"})
                    # El SDK no expone directamente send_json, así que usamos send_text.
                    # Deepgram ignora el contenido si sólo es un ping.
                    self._conn.send_text(msg)
                    self._conn.flush()
                except Exception:
                    # Si falla, cortamos el bucle para que el WS se reinicie fuera.
                    break
        except asyncio.CancelledError:
            pass

    # ─────────────────────────────── API pública ────────────────────────────────
    async def speak(
        self,
        text: str,
        on_chunk: ChunkCallback,
        *,
        on_end: Optional[EndCallback] = None,
        timeout_first_chunk: float = 1.0,
    ) -> bool:
        """Envía *text* y reenvía audio a `on_chunk`.

        Llama `on_end()` al concluir. Devuelve *True* si llegó audio
        antes de `timeout_first_chunk`, o *False* para activar el
        respaldo.
        """
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

    async def keepalive(self) -> None:
        """Envía un ping inmediato (por si no quieres esperar al bucle)."""
        if self._ws_open.is_set() and not self._ws_close.is_set():
            msg = json.dumps({"type": "KeepAlive"})
            self._conn.send_text(msg)
            self._conn.flush()

    async def close(self) -> None:
        """Cierra la conexión de forma limpia."""
        self._conn.finish()
        await self._ws_close.wait()
