# twilio_handler.py
# -*- coding: utf-8 -*-
"""
📞 MANEJADOR DE WEBSOCKET DE TWILIO
====================================
Este módulo SOLO maneja la comunicación con Twilio:
- Recibe eventos del WebSocket
- Envía respuestas a Twilio
- NO contiene lógica de negocio

Es el punto de entrada/salida con Twilio, nada más.
"""

import asyncio
import base64
import json
import logging
import time
from typing import Optional, Dict, Any, Callable, Awaitable
from dataclasses import dataclass
from datetime import datetime
from fastapi import WebSocket
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

# ===== TIPOS =====
EventHandler = Callable[[str, Dict[str, Any]], Awaitable[None]]
AudioHandler = Callable[[bytes], Awaitable[None]]


@dataclass
class TwilioConnection:
    """
    📊 Información de la conexión con Twilio
    """
    websocket: WebSocket
    stream_sid: Optional[str] = None
    call_sid: Optional[str] = None
    connection_start: float = 0.0
    is_connected: bool = False


class TwilioHandler:
    """
    🎯 MANEJADOR DE WEBSOCKET con Twilio
    
    Responsabilidades:
    1. Aceptar conexión WebSocket
    2. Recibir eventos de Twilio
    3. Decodificar y validar datos
    4. Delegar a handlers específicos
    5. Enviar respuestas a Twilio
    """
    
    def __init__(self):
        """
        📥 Inicializa el manejador
        """
        self.connection: Optional[TwilioConnection] = None
        
        # Handlers externos
        self.on_start: Optional[EventHandler] = None
        self.on_media: Optional[AudioHandler] = None
        self.on_stop: Optional[EventHandler] = None
        self.on_mark: Optional[EventHandler] = None
        
        # Control
        self.running = False
        
        logger.info("📞 TwilioHandler inicializado")
    
    # ========== CONFIGURACIÓN DE HANDLERS ==========
    
    def set_handlers(
        self,
        on_start: Optional[EventHandler] = None,
        on_media: Optional[AudioHandler] = None,
        on_stop: Optional[EventHandler] = None,
        on_mark: Optional[EventHandler] = None
    ) -> None:
        """
        🔧 Configura los handlers para cada tipo de evento
        
        Args:
            on_start: Handler cuando inicia el stream
            on_media: Handler para chunks de audio
            on_stop: Handler cuando termina el stream
            on_mark: Handler para eventos mark
        """
        self.on_start = on_start
        self.on_media = on_media
        self.on_stop = on_stop
        self.on_mark = on_mark
        
        logger.info("🔧 Handlers configurados")
    
    # ========== MANEJO DE CONEXIÓN ==========
    
    async def handle_websocket(self, websocket: WebSocket) -> None:
        logger.info("[FUNCIONALIDAD] Nueva conexión WebSocket entrante de Twilio")
        t0 = time.perf_counter()
        try:
            await websocket.accept()
            logger.info("✅ WebSocket aceptado")
        except Exception as e:
            logger.error(f"❌ Error aceptando WebSocket: {e}")
            return
        self.connection = TwilioConnection(
            websocket=websocket,
            connection_start=time.perf_counter()
        )
        self.running = True
        try:
            await self._receive_loop()
        except Exception as e:
            logger.error(f"❌ Error en loop principal: {e}", exc_info=True)
        finally:
            await self._cleanup()
            logger.info(f"[LATENCIA] Conexión WebSocket Twilio finalizada en {1000*(time.perf_counter()-t0):.1f} ms")
    
    async def _receive_loop(self) -> None:
        """
        🔄 Loop principal de recepción de mensajes
        """
        logger.info("🔄 Iniciando loop de recepción")
        
        while self.running and self.connection:
            try:
                # Recibir mensaje
                raw_data = await self.connection.websocket.receive_text()
                
                # Parsear JSON
                try:
                    data = json.loads(raw_data)
                except json.JSONDecodeError as e:
                    logger.error(f"❌ JSON inválido: {e}")
                    continue
                
                # Procesar evento
                event_type = data.get("event")
                if event_type:
                    await self._handle_event(event_type, data)
                else:
                    logger.warning(f"⚠️ Mensaje sin tipo de evento: {data}")
                    
            except Exception as e:
                # Detectar desconexión
                if "close" in str(e).lower() or "disconnect" in str(e).lower():
                    logger.info("🔌 WebSocket desconectado por el cliente")
                    self.running = False
                    break
                else:
                    logger.error(f"❌ Error recibiendo mensaje: {e}")
                    if self.connection.websocket.client_state != WebSocketState.CONNECTED:
                        logger.info("🔌 WebSocket no está conectado, terminando loop")
                        self.running = False
                        break
    
    async def _handle_event(self, event_type: str, data: Dict[str, Any]) -> None:
        logger.debug(f"📨 Evento recibido: {event_type}")
        t0 = time.perf_counter()
        try:
            if event_type == "start":
                await self._handle_start(data)
                
            elif event_type == "media":
                await self._handle_media(data)
                
            elif event_type == "stop":
                await self._handle_stop(data)
                
            elif event_type == "mark":
                await self._handle_mark(data)
                
            elif event_type == "connected":
                # Evento informativo, solo loguear
                logger.info("✅ Twilio confirmó conexión establecida")
                
            else:
                logger.warning(f"❓ Evento desconocido: {event_type}")
            if event_type != "media":
                logger.info(f"[LATENCIA] Evento '{event_type}' procesado en {1000*(time.perf_counter()-t0):.1f} ms")
                
        except Exception as e:
            logger.error(f"❌ Error procesando evento {event_type}: {e}", exc_info=True)
    
    async def _handle_start(self, data: Dict[str, Any]) -> None:
        """
        🏁 Maneja el evento START (inicio del stream)
        
        Extrae información importante como stream_sid y call_sid
        """
        if not self.connection:
            logger.warning("⚠️ No hay conexión establecida en _handle_start")
            return
        assert self.connection is not None
        # Extraer IDs
        self.connection.stream_sid = data.get("streamSid")
        
        start_data = data.get("start", {})
        self.connection.call_sid = start_data.get("callSid")
        
        # Marcar como conectado
        self.connection.is_connected = True
        
        logger.info(
            f"🏁 Stream iniciado - "
            f"StreamSID: {self.connection.stream_sid}, "
            f"CallSID: {self.connection.call_sid}"
        )
        
        # Llamar handler externo
        if self.on_start:
            await self.on_start("start", data)
    
    async def _handle_media(self, data: Dict[str, Any]) -> None:
        """
        🎵 Maneja el evento MEDIA (chunk de audio)
        
        Decodifica el audio base64 y lo pasa al handler
        """
        media_data = data.get("media", {})
        payload_b64 = media_data.get("payload")
        
        if not payload_b64:
            return
        
        # Decodificar audio
        try:
            import base64
            audio_bytes = base64.b64decode(payload_b64)
            
            # Llamar handler externo
            if self.on_media:
                await self.on_media(audio_bytes)
                
        except Exception as e:
            logger.error(f"❌ Error decodificando audio: {e}")
    
    async def _handle_stop(self, data: Dict[str, Any]) -> None:
        """
        🛑 Maneja el evento STOP (fin del stream)
        """
        logger.info("🛑 Evento STOP recibido")
        self.running = False
        
        # Llamar handler externo
        if self.on_stop:
            await self.on_stop("stop", data)
    
    async def _handle_mark(self, data: Dict[str, Any]) -> None:
        """
        🏷️ Maneja el evento MARK (marcadores custom)
        """
        mark_data = data.get("mark", {})
        mark_name = mark_data.get("name", "unknown")
        
        logger.debug(f"🏷️ Mark recibido: {mark_name}")
        
        # Llamar handler externo
        if self.on_mark:
            await self.on_mark("mark", data)
    
    # ========== ENVÍO DE DATOS A TWILIO ==========
    
    async def send_audio(self, audio_base64: str) -> bool:
        if not self._can_send():
            return False
        assert self.connection is not None
        t0 = time.perf_counter()
        try:
            await self.connection.websocket.send_text(json.dumps({
                "event": "media",
                "streamSid": self.connection.stream_sid,
                "media": {
                    "payload": audio_base64
                }
            }))
            logger.info(f"[LATENCIA] Audio enviado a Twilio en {1000*(time.perf_counter()-t0):.1f} ms")
            return True
        except Exception as e:
            logger.error(f"❌ Error enviando audio: {e}")
            return False
    
    async def send_mark(self, name: str) -> bool:
        if not self._can_send():
            return False
        assert self.connection is not None
        t0 = time.perf_counter()
        try:
            await self.connection.websocket.send_text(json.dumps({
                "event": "mark",
                "streamSid": self.connection.stream_sid,
                "mark": {
                    "name": name
                }
            }))
            logger.debug(f"🏷️ Mark enviado: {name}")
            logger.info(f"[LATENCIA] Mark '{name}' enviado a Twilio en {1000*(time.perf_counter()-t0):.1f} ms")
            return True
        except Exception as e:
            logger.error(f"❌ Error enviando mark: {e}")
            return False
    
    async def clear_buffer(self) -> bool:
        if not self._can_send():
            return False
        assert self.connection is not None
        t0 = time.perf_counter()
        try:
            await self.connection.websocket.send_text(json.dumps({
                "event": "clear",
                "streamSid": self.connection.stream_sid
            }))
            logger.debug("🧹 Buffer limpiado")
            logger.info(f"[LATENCIA] Buffer limpiado en Twilio en {1000*(time.perf_counter()-t0):.1f} ms")
            return True
        except Exception as e:
            logger.error(f"❌ Error limpiando buffer: {e}")
            return False
    
    async def send_json(self, data: Dict[str, Any]) -> bool:
        t0 = time.perf_counter()
        try:
            # Si recibe string, asume que ya es JSON
            if isinstance(data, str):
                assert self.connection is not None
                await self.connection.websocket.send_text(data)
                logger.debug(f"[LATENCIA] JSON (str) enviado a Twilio en {1000*(time.perf_counter()-t0):.1f} ms")
                return True
            # ---- INICIO DEL BLOQUE DE DIAGNÓSTICO ----
            if data.get("event") == "media":
                payload_b64 = data.get("media", {}).get("payload", "")
                
                # 1. Validar que el payload se puede decodificar desde Base64
                try:
                    base64.b64decode(payload_b64)
                    logger.info(f"[DIAGNÓSTICO] ✓ Payload Base64 válido (longitud: {len(payload_b64)})")
                except Exception as e:
                    logger.error(f"[DIAGNÓSTICO] ✗ Payload Base64 INVÁLIDO: {e}")
                    return False # No enviar si está malformado
                
                # 2. Validar que el streamSid es el que esperamos
                if data.get("streamSid") != self.get_stream_sid():
                    logger.error(f"[DIAGNÓSTICO] ✗ Inconsistencia de StreamSID! Esperado: {self.get_stream_sid()}, Encontrado: {data.get('streamSid')}")
                    return False
            
            # 3. Imprimir el JSON que se va a enviar
            json_str = json.dumps(data)
            logger.debug(f"[DIAGNÓSTICO] Enviando a Twilio: {json_str[:250]}...") # Imprime los primeros 250 caracteres
            # ---- FIN DEL BLOQUE DE DIAGNÓSTICO ----
            assert self.connection is not None
            # Envío final (usando el método que sabemos que funciona en tu otro proyecto)
            await self.connection.websocket.send_text(json_str)
            logger.info(f"[LATENCIA] JSON enviado a Twilio en {1000*(time.perf_counter()-t0):.1f} ms")
            return True
        
        except Exception as e:
            logger.error(f"❌ Error en la capa final de envío a Twilio: {e}")
            return False
    
    def _can_send(self) -> bool:
        """
        ✅ Verifica si se puede envia.r datos
        
        Returns:
            bool: True si la conexión está lista
        """
        if not self.connection:
            logger.warning("⚠️ No hay conexión establecida")
            return False
            
        if not self.connection.is_connected:
            logger.warning("⚠️ Conexión no está marcada como activa")
            return False
            
        if not self.connection.stream_sid:
            logger.warning("⚠️ No hay stream_sid disponible")
            return False
            
        if self.connection.websocket.client_state != WebSocketState.CONNECTED:
            logger.warning("⚠️ WebSocket no está conectado")
            return False
            
        return True
    
    # ========== LIMPIEZA Y CIERRE ==========
    
    async def _cleanup(self) -> None:
        """
        🧹 Limpia recursos al cerrar
        """
        logger.info("🧹 Limpiando TwilioHandler...")
        t0 = time.perf_counter()
        self.running = False
        
        # Cerrar WebSocket si está abierto
        if self.connection and self.connection.websocket:
            if self.connection.websocket.client_state == WebSocketState.CONNECTED:
                try:
                    await self.connection.websocket.close(code=1000, reason="Normal closure")
                    logger.info("✅ WebSocket cerrado correctamente")
                except Exception as e:
                    logger.warning(f"⚠️ Error cerrando WebSocket: {e}")
        
        # Calcular duración de la llamada
        if self.connection and self.connection.connection_start:
            duration = time.perf_counter() - self.connection.connection_start
            logger.info(f"📊 Duración de la conexión: {duration:.1f} segundos")
        
        self.connection = None
        logger.info("✅ TwilioHandler limpiado")
        logger.info(f"[LATENCIA] Cleanup de TwilioHandler completado en {1000*(time.perf_counter()-t0):.1f} ms")
    
    async def close(self) -> None:
        """
        🔌 Cierra la conexión manualmente
        """
        logger.info("🔌 Cerrando TwilioHandler manualmente...")
        self.running = False
        await self._cleanup()
    
    # ========== UTILIDADES ==========
    
    def get_stream_sid(self) -> Optional[str]:
        """Obtiene el Stream SID actual"""
        return self.connection.stream_sid if self.connection else None
    
    def get_call_sid(self) -> Optional[str]:
        """Obtiene el Call SID actual"""
        return self.connection.call_sid if self.connection else None
    
    def is_connected(self) -> bool:
        """Verifica si está conectado"""
        return bool(
            self.connection 
            and self.connection.is_connected 
            and self.connection.websocket.client_state == WebSocketState.CONNECTED
        )
    
    def get_connection_info(self) -> Dict[str, Any]:
        """
        📊 Obtiene información de la conexión
        
        Returns:
            Diccionario con info de la conexión
        """
        if not self.connection:
            return {"status": "no_connection"}
        
        duration = time.perf_counter() - self.connection.connection_start
        
        return {
            "status": "connected" if self.is_connected() else "disconnected",
            "stream_sid": self.connection.stream_sid,
            "call_sid": self.connection.call_sid,
            "duration_seconds": round(duration, 1),
            "websocket_state": str(self.connection.websocket.client_state)
        }