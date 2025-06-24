# eleven_ws_client.py
import asyncio
import base64
import contextlib
import json
import logging
import audioop # type: ignore
from typing import Callable, Awaitable, Optional

import websockets
from decouple import config

from global_state import CURRENT_CALL_MANAGER


# ────────────────────────────────────────────────────────────
# ElevenLabs ⇄ WebSocket  →  Twilio (μ-law 8 kHz, 160 B/20 ms)
# ────────────────────────────────────────────────────────────

ELEVEN_LABS_API_KEY: str = config("ELEVEN_LABS_API_KEY")
ELEVEN_LABS_VOICE_ID: str = config("ELEVEN_LABS_VOICE_ID")
MODEL_ID: str = "eleven_flash_v2_5"

logger = logging.getLogger("eleven_ws_client")
logger.setLevel(logging.INFO)


class ElevenLabsWSClient:
    """Stream real-time TTS to Twilio in 160-byte μ-law chunks."""

    CHUNK_SIZE = 160          # 160 bytes  = 20 ms @ 8 kHz μ-law
    CHUNK_INTERVAL = 0.02     # 20 ms

    def __init__(
        self,
        *,
        stream_sid: str,
        websocket_send: Callable[[str], Awaitable[None]],
    ) -> None:
        self.stream_sid = stream_sid
        self._send_to_twilio = websocket_send
        self.url = (
            f"wss://api.elevenlabs.io/v1/text-to-speech/"
            f"{ELEVEN_LABS_VOICE_ID}/stream-input"
            f"?model_id={MODEL_ID}&output_format=pcm_8000"
        )

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._last_tx = asyncio.get_event_loop().time()
        self._backup_text: str = ""
        self._authenticated = False
        self._first_chunk_logged = False








    # ───────── Conexión ───────── #
    async def connect(self) -> None:
        if self.ws and not getattr(self.ws, "closed", False):
            return
        self.ws = await websockets.connect(self.url)
        logger.info("[EL-WS] Conexión WebSocket abierta.")
        await self._init_connection_settings()
        if not self._keepalive_task or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())







    async def _init_connection_settings(self) -> None:
        await self.ws.send(json.dumps({
            "xi_api_key": ELEVEN_LABS_API_KEY,
            "text": " ",
            "voice_settings": {
                "stability": 0.3,
                "similarity_boost": 0.9,
                "use_speaker_boost": True,
                "style": 0.5,
                "speed": 1.1,
            },
            "generation_config": {
                "chunk_length_schedule": [80,100, 160],
            },
        }))







    # ─────── Enviar texto ─────── #
    async def send_text(self, text: str) -> None:
        clean = text.strip()
        if not clean:
            return
        self._backup_text = clean
        if not self.ws or getattr(self.ws, "closed", True):
            await self.connect()

        payload = {"text": clean, "flush": True}
        if not self._authenticated:
            payload["xi_api_key"] = ELEVEN_LABS_API_KEY
            self._authenticated = True

        await self.ws.send(json.dumps(payload))          # type: ignore[arg-type]
        self._last_tx = asyncio.get_event_loop().time()

        if not self._recv_task or self._recv_task.done():
            self._recv_task = asyncio.create_task(self._receive_audio_loop())







    # ─────── Recibir audio ─────── #
    async def _receive_audio_loop(self) -> None:
        try:
            async for message in self.ws:                 # type: ignore[async-for]
                await self._handle_ws_message(message)
        except Exception as exc:
            logger.error(f"[EL-WS] Error de recepción: {exc}")
            await self._fallback_http()







    async def _handle_ws_message(self, message: str) -> None:
        """
        Procesa cada mensaje que llega del WebSocket de ElevenLabs.
        1. Primero revisa si el chunk es el aviso final ("isFinal": true).
        2. Si no, convierte el audio a mu-law y lo envía a Twilio.
        """
        try:
            data = json.loads(message)

            # ——— Nuevo: imprime las claves recibidas, para diagnóstico ———
            logger.debug(f"[EL-WS] Chunk keys: {list(data.keys())}")
            logger.debug(f"[EL-WS] isFinal: {data.get('isFinal')}")

            # 1️⃣  Capturamos el aviso final ANTES de cualquier return.
            if data.get("isFinal"):
                logger.info("[EL-WS] 📤 Último chunk recibido (isFinal=True). "
                            "Finalizando TTS y reactivando STT.")

                if CURRENT_CALL_MANAGER:
                    CURRENT_CALL_MANAGER.tts_en_progreso = False
                    await CURRENT_CALL_MANAGER._reactivar_stt_despues_de_envio()
                else:
                    logger.warning("[EL-WS] ⚠️ No se encontró CURRENT_CALL_MANAGER "
                                "para reactivar STT.")
                return  # 🔚 nada más que hacer con este mensaje

            # 2️⃣  Si no es 'isFinal', revisamos si al menos trae audio.
            audio_b64 = data.get("audio")
            if not audio_b64:
                return  # Chunk sin audio ni isFinal → lo ignoramos

            # Registramos el primer chunk solo una vez
            if not self._first_chunk_logged:
                logger.info("[EL-WS] 📥 Primer chunk recibido.")
                self._first_chunk_logged = True

            # Decodificamos y preparamos audio para Twilio
            pcm = base64.b64decode(audio_b64)
            if len(pcm) % 2:           # asegúrate de que la longitud sea par
                pcm = pcm[:-1]

            pcm_amp = audioop.mul(pcm, 2, 3.0)  # pequeño aumento de volumen
            mulaw = audioop.lin2ulaw(pcm_amp, 2)

            await self._send_audio_to_twilio(mulaw)

        except Exception as exc:
            logger.warning(f"[EL-WS] Error procesando chunk: {exc}")







    # ─────── Enviar a Twilio (160 B / 20 ms) ─────── #
    async def _send_audio_to_twilio(self, mulaw: bytes) -> None:
        for i in range(0, len(mulaw), self.CHUNK_SIZE):
            chunk = mulaw[i:i + self.CHUNK_SIZE]
            msg = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(chunk).decode()},
            }
            try:
                await self._send_to_twilio(json.dumps(msg))
            except Exception as exc:
                logger.error(f"[EL-WS] Error enviando a Twilio: {exc}")
                return
            #await asyncio.sleep(self.CHUNK_INTERVAL)







    # ─────── Keep-alive cada 10 s ─────── #
    async def _keepalive_loop(self) -> None:
        try:
            while self.ws and not getattr(self.ws, "closed", False):
                await asyncio.sleep(10)
                if asyncio.get_event_loop().time() - self._last_tx >= 10:
                    try:
                        await self.ws.send(json.dumps({"text": " "}))
                        logger.debug("[EL-WS] keep-alive ping sent.")
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass






    # ─────── Cierre limpio ─────── #
    async def close(self) -> None:
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recv_task
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
        if self.ws and not getattr(self.ws, "closed", False):
            await self.ws.close()
            logger.info("[EL-WS] Conexión WebSocket cerrada.")







    # ─────── Fallback HTTP ─────── #
    async def _fallback_http(self) -> None:
        try:
            logger.error("[EL-WS] Activando fallback HTTP.")
            from eleven_http_client import send_tts_fallback_to_twilio

            await send_tts_fallback_to_twilio(
                text=self._backup_text,
                stream_sid=self.stream_sid,
                websocket_send=self._send_to_twilio,
            )
        except Exception as exc:
            logger.critical(f"[EL-WS] Fallback HTTP también falló: {exc}")
