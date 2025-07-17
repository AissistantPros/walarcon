# integration_manager.py
# -*- coding: utf-8 -*-
"""
🔌 GESTOR DE INTEGRACIONES EXTERNAS
====================================
Maneja las conexiones con servicios externos:
- Deepgram (STT)
- ElevenLabs (TTS)
- Reconexiones automáticas
- Fallbacks

Este módulo se encarga de la RESILIENCIA de las conexiones.
"""

import asyncio
import json
import logging
import time
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)

# ===== CONFIGURACIÓN =====
INTEGRATION_CONFIG = {
    "RECONNECT_DELAY": 1.0,          # Segundos antes de reconectar
    "MAX_RECONNECT_ATTEMPTS": 3,     # Intentos máximos de reconexión
    "CONNECTION_TIMEOUT": 5.0,       # Timeout para conexiones
    "KEEPALIVE_INTERVAL": 15.0,     # Intervalo para keepalive
}


class ServiceStatus(Enum):
    """Estados posibles de un servicio"""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"


@dataclass
class ServiceHealth:
    """
    📊 Estado de salud de un servicio
    """
    status: ServiceStatus
    last_connected: Optional[float] = None
    last_error: Optional[str] = None
    reconnect_attempts: int = 0
    total_reconnects: int = 0


class IntegrationManager:
    """
    🎯 GESTOR CENTRAL de integraciones externas
    
    Responsabilidades:
    1. Inicializar servicios externos
    2. Monitorear salud de conexiones
    3. Reconectar automáticamente
    4. Proveer fallbacks
    5. Reportar estado de servicios
    """
    
    def __init__(self):
        """
        📥 Inicializa el gestor
        """
        # Estado de servicios
        self.services_health: Dict[str, ServiceHealth] = {
            "deepgram": ServiceHealth(status=ServiceStatus.DISCONNECTED),
            "elevenlabs": ServiceHealth(status=ServiceStatus.DISCONNECTED),
        }
        
        # Tareas de monitoreo
        self.monitor_tasks: Dict[str, Optional[asyncio.Task]] = {
            "deepgram": None,
            "elevenlabs": None,
        }
        
        # Callbacks de reconexión
        self.reconnect_callbacks: Dict[str, Optional[Callable]] = {
            "deepgram": None,
            "elevenlabs": None,
        }
        
        logger.info("🔌 IntegrationManager inicializado")
    
    # ========== DEEPGRAM (STT) ==========
    
    async def setup_deepgram(
        self,
        stt_client,
        on_reconnect: Optional[Callable] = None
    ) -> bool:
        logger.info("[FUNCIONALIDAD] Configurando Deepgram STT...")
        t0 = time.perf_counter()
        self.reconnect_callbacks["deepgram"] = on_reconnect
        self.services_health["deepgram"].status = ServiceStatus.CONNECTING
        
        try:
            # Intentar conectar
            await stt_client.start_streaming()
            
            if stt_client._started:
                # Conexión exitosa
                self.services_health["deepgram"].status = ServiceStatus.CONNECTED
                self.services_health["deepgram"].last_connected = time.perf_counter()
                self.services_health["deepgram"].reconnect_attempts = 0
                
                # Iniciar monitoreo
                self.monitor_tasks["deepgram"] = asyncio.create_task(
                    self._monitor_deepgram(stt_client)
                )
                
                logger.info("✅ Deepgram conectado y monitoreado")
                logger.info(f"[LATENCIA] Deepgram configurado en {1000*(time.perf_counter()-t0):.1f} ms")
                return True
            else:
                raise Exception("Deepgram no se inició correctamente")
                
        except Exception as e:
            logger.error(f"❌ Error conectando Deepgram: {e}")
            self.services_health["deepgram"].status = ServiceStatus.FAILED
            self.services_health["deepgram"].last_error = str(e)
            return False
    
    async def _monitor_deepgram(self, stt_client) -> None:
        """
        👁️ Monitorea la conexión de Deepgram
        
        Si detecta desconexión, intenta reconectar automáticamente
        """
        logger.debug("👁️ Monitor de Deepgram iniciado")
        
        while True:
            try:
                await asyncio.sleep(5.0)  # Check cada 5 segundos
                
                # Verificar estado
                if not stt_client._started:
                    logger.warning("⚠️ Deepgram desconectado detectado")
                    await self._handle_deepgram_disconnect(stt_client)
                    break
                    
            except asyncio.CancelledError:
                logger.debug("Monitor de Deepgram cancelado")
                break
            except Exception as e:
                logger.error(f"Error en monitor Deepgram: {e}")
    
    async def _handle_deepgram_disconnect(self, stt_client) -> None:
        """
        🔄 Maneja desconexión de Deepgram
        """
        health = self.services_health["deepgram"]
        health.status = ServiceStatus.RECONNECTING
        t0 = time.perf_counter()
        # Intentar reconectar
        for attempt in range(INTEGRATION_CONFIG["MAX_RECONNECT_ATTEMPTS"]):
            health.reconnect_attempts = attempt + 1
            logger.info(f"🔄 Intento de reconexión Deepgram {attempt + 1}/{INTEGRATION_CONFIG['MAX_RECONNECT_ATTEMPTS']}")
            
            # Esperar antes de reintentar
            await asyncio.sleep(INTEGRATION_CONFIG["RECONNECT_DELAY"])
            
            try:
                # Cerrar conexión anterior
                await stt_client.close()
                
                # Reconectar
                await stt_client.start_streaming()
                
                if stt_client._started:
                    # Reconexión exitosa
                    health.status = ServiceStatus.CONNECTED
                    health.last_connected = time.perf_counter()
                    health.total_reconnects += 1
                    health.reconnect_attempts = 0
                    
                    logger.info("✅ Deepgram reconectado exitosamente")
                    logger.info(f"[LATENCIA] Deepgram reconectado en {1000*(time.perf_counter()-t0):.1f} ms")
                    
                    # Llamar callback
                    if self.reconnect_callbacks["deepgram"]:
                        await self.reconnect_callbacks["deepgram"]()
                    
                    # Reiniciar monitoreo
                    self.monitor_tasks["deepgram"] = asyncio.create_task(
                        self._monitor_deepgram(stt_client)
                    )
                    return
                    
            except Exception as e:
                logger.error(f"Error en reconexión Deepgram: {e}")
                health.last_error = str(e)
        
        # Si llegamos aquí, falló la reconexión
        health.status = ServiceStatus.FAILED
        logger.error(f"❌ Deepgram reconexión falló después de {INTEGRATION_CONFIG['MAX_RECONNECT_ATTEMPTS']} intentos")
    
    # ========== ELEVENLABS (TTS) ==========
    
    async def setup_elevenlabs(
        self,
        tts_client,
        on_reconnect: Optional[Callable] = None
    ) -> bool:
        logger.info("[FUNCIONALIDAD] Configurando ElevenLabs TTS...")
        t0 = time.perf_counter()
        self.reconnect_callbacks["elevenlabs"] = on_reconnect
        self.services_health["elevenlabs"].status = ServiceStatus.CONNECTING
        
        try:
            # ElevenLabs se conecta on-demand, así que solo verificamos que existe
            if tts_client and hasattr(tts_client, '_ws_open'):
                # Esperar conexión inicial si es necesario
                try:
                    await asyncio.wait_for(
                        tts_client._ws_open.wait(), 
                        timeout=INTEGRATION_CONFIG["CONNECTION_TIMEOUT"]
                    )
                except asyncio.TimeoutError:
                    # Es OK, ElevenLabs puede conectar después
                    pass
                
                # Marcar como conectado
                self.services_health["elevenlabs"].status = ServiceStatus.CONNECTED
                self.services_health["elevenlabs"].last_connected = time.perf_counter()
                
                # Iniciar keepalive
                self.monitor_tasks["elevenlabs"] = asyncio.create_task(
                    self._monitor_elevenlabs(tts_client)
                )
                
                logger.info("✅ ElevenLabs configurado")
                logger.info(f"[LATENCIA] ElevenLabs configurado en {1000*(time.perf_counter()-t0):.1f} ms")
                return True
            else:
                raise Exception("Cliente ElevenLabs inválido")
                
        except Exception as e:
            logger.error(f"❌ Error configurando ElevenLabs: {e}")
            self.services_health["elevenlabs"].status = ServiceStatus.FAILED
            self.services_health["elevenlabs"].last_error = str(e)
            return False
    
    async def _monitor_elevenlabs(self, tts_client) -> None:
        """
        👁️ Monitorea y mantiene viva la conexión de ElevenLabs
        """
        logger.debug("👁️ Monitor de ElevenLabs iniciado")
        
        while True:
            try:
                await asyncio.sleep(INTEGRATION_CONFIG["KEEPALIVE_INTERVAL"])
                
                # Verificar si el WebSocket está conectado
                if hasattr(tts_client, '_ws') and tts_client._ws and not tts_client._ws.closed:
                    # Enviar keepalive
                    try:
                        await tts_client._ws.send(json.dumps({"text": " "}))
                        logger.debug("💓 Keepalive enviado a ElevenLabs")
                    except Exception as e:
                        logger.warning(f"⚠️ Error enviando keepalive: {e}")
                        await self._handle_elevenlabs_disconnect(tts_client)
                        break
                else:
                    logger.warning("⚠️ ElevenLabs WebSocket no conectado")
                    # No es crítico, se reconectará on-demand
                    
            except asyncio.CancelledError:
                logger.debug("Monitor de ElevenLabs cancelado")
                break
            except Exception as e:
                logger.error(f"Error en monitor ElevenLabs: {e}")
    
    async def _handle_elevenlabs_disconnect(self, tts_client) -> None:
        """
        🔄 Maneja desconexión de ElevenLabs (menos crítico que Deepgram)
        """
        health = self.services_health["elevenlabs"]
        health.status = ServiceStatus.RECONNECTING
        
        # Para ElevenLabs, simplemente marcamos el estado
        # La reconexión ocurrirá on-demand en el próximo TTS
        logger.info("🔄 ElevenLabs desconectado, se reconectará on-demand")
        
        health.status = ServiceStatus.DISCONNECTED
        
        # Llamar callback si existe
        if self.reconnect_callbacks["elevenlabs"]:
            try:
                await self.reconnect_callbacks["elevenlabs"]()
            except Exception as e:
                logger.error(f"Error en callback reconexión ElevenLabs: {e}")
    
    # ========== UTILIDADES Y ESTADO ==========
    
    def get_service_status(self, service: str) -> ServiceStatus:
        """
        📊 Obtiene el estado de un servicio
        
        Args:
            service: "deepgram" o "elevenlabs"
            
        Returns:
            Estado actual del servicio
        """
        return self.services_health.get(service, ServiceHealth(ServiceStatus.DISCONNECTED)).status
    
    def is_service_healthy(self, service: str) -> bool:
        """
        ✅ Verifica si un servicio está saludable
        
        Args:
            service: "deepgram" o "elevenlabs"
            
        Returns:
            True si está conectado
        """
        return self.get_service_status(service) == ServiceStatus.CONNECTED
    
    def get_health_report(self) -> Dict[str, Any]:
        """
        📋 Genera reporte completo de salud
        
        Returns:
            Diccionario con estado de todos los servicios
        """
        report = {}
        
        for service, health in self.services_health.items():
            report[service] = {
                "status": health.status.value,
                "healthy": health.status == ServiceStatus.CONNECTED,
                "last_connected": health.last_connected,
                "last_error": health.last_error,
                "reconnect_attempts": health.reconnect_attempts,
                "total_reconnects": health.total_reconnects,
            }
            
            # Calcular uptime si está conectado
            if health.status == ServiceStatus.CONNECTED and health.last_connected:
                uptime = time.perf_counter() - health.last_connected
                report[service]["uptime_seconds"] = round(uptime, 1)
        
        return report
    
    def get_service_health(self, service: str) -> ServiceHealth:
        """
        🏥 Obtiene información detallada de salud de un servicio
        
        Args:
            service: "deepgram" o "elevenlabs"
            
        Returns:
            Objeto ServiceHealth con todos los detalles
        """
        return self.services_health.get(service, ServiceHealth(ServiceStatus.DISCONNECTED))
    
    # ========== LIMPIEZA Y CIERRE ==========
    
    async def shutdown(self) -> None:
        logger.info("🔌 Cerrando IntegrationManager...")
        t0 = time.perf_counter()
        # Cancelar todas las tareas de monitoreo
        for service, task in self.monitor_tasks.items():
            if task and not task.done():
                logger.debug(f"Cancelando monitor de {service}")
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
        
        # Marcar todos los servicios como desconectados
        for service in self.services_health:
            self.services_health[service].status = ServiceStatus.DISCONNECTED
        
        logger.info("✅ IntegrationManager cerrado")
        logger.info(f"[LATENCIA] IntegrationManager cerrado en {1000*(time.perf_counter()-t0):.1f} ms")
    
    # ========== MÉTODOS DE CONVENIENCIA ==========
    
    async def ensure_all_connected(self) -> Dict[str, bool]:
        """
        🔗 Verifica que todos los servicios estén conectados
        
        Returns:
            Dict con el estado de cada servicio
        """
        results = {}
        
        for service in self.services_health:
            results[service] = self.is_service_healthy(service)
        
        return results
    
    def format_status_message(self) -> str:
        """
        📝 Genera mensaje de estado legible
        
        Returns:
            String con resumen del estado
        """
        lines = ["Estado de Integraciones:"]
        
        for service, health in self.services_health.items():
            status_emoji = {
                ServiceStatus.CONNECTED: "✅",
                ServiceStatus.CONNECTING: "🔄",
                ServiceStatus.RECONNECTING: "🔄",
                ServiceStatus.DISCONNECTED: "❌",
                ServiceStatus.FAILED: "💀",
            }.get(health.status, "❓")
            
            line = f"  {status_emoji} {service.capitalize()}: {health.status.value}"
            
            if health.total_reconnects > 0:
                line += f" (reconexiones: {health.total_reconnects})"
            
            lines.append(line)
        
        return "\n".join(lines)