# aiagent.py
# ────────────────────────────────────────────────────────────────────────────────
# Modificación profunda que:
#   • Añade parser multiformato con buffer (500 ms)
#   • Implementa flujo “una pasada” (ejecuta herramienta → respuesta sintética)
#   • Emite telemetría a session_state['events']
#   • Mantiene intactos SessionManager, session_state y lógica existente de STT/TTS
# Copia / pega este archivo completo y ajusta SOLO las rutas de import si algún
# módulo real tiene nombre diferente en tu proyecto.
# ────────────────────────────────────────────────────────────────────────────────

import re
import json
import asyncio
import time
import logging
from typing import Dict, Any, List, Callable, Awaitable

from state_store import session_state  # ya existe
from state_store import emit_latency_event
from synthetic_responses import generate_synthetic_response

# ── Importa funciones REALES de tus módulos de herramientas ───────────────────
# Ajusta nombres de módulo si difieren.
from buscarslot import process_appointment_request            # noqa: F401
from crearcita import create_calendar_event                   # noqa: F401
from selectevent import edit_calendar_event, delete_calendar_event  # noqa: F401
from buscarslot import read_sheet_data                        # noqa: F401
from buscarslot import get_cancun_weather                     # noqa: F401
from buscarslot import detect_intent                          # noqa: F401
from selectevent import search_calendar_event_by_phone        # noqa: F401

logger = logging.getLogger(__name__)

# ╭─ REGISTRO DE HERRAMIENTAS ───────────────────────────────────────────────╮
TOOL_REGISTRY: Dict[str, Callable[..., Awaitable[Dict[str, Any]]]] = {}


def register_tool(name: str, func: Callable[..., Awaitable[Dict[str, Any]]]):
    """Se invoca abajo para cada herramienta importada."""
    TOOL_REGISTRY[name] = func


# ── Registrar todo (mantén en sincronía con ALL_TOOLS del prompt) ─────────────
register_tool("process_appointment_request", process_appointment_request)
register_tool("create_calendar_event", create_calendar_event)
register_tool("edit_calendar_event", edit_calendar_event)
register_tool("delete_calendar_event", delete_calendar_event)
register_tool("read_sheet_data", read_sheet_data)
register_tool("get_cancun_weather", get_cancun_weather)
register_tool("detect_intent", detect_intent)
register_tool("search_calendar_event_by_phone", search_calendar_event_by_phone)


# ╭─ TOOL ENGINE (parser con buffer) ──────────────────────────────────────────╮
class ToolEngine:
    """Detecta [tool()], JSON, <function>, <|python_tag|> y nunca deja filtrar texto."""
    LIST_RE = re.compile(r'\[(\w+)\s*\((.*?)\)\]', re.S)
    JSON_RE = re.compile(r'^\s*\{.*?"type"\s*:\s*"function".*?\}\s*$', re.S)
    XML_RE = re.compile(r'<function\s*=\s*(\w+)>\s*(\{.*?\})\s*</function>', re.S)
    PYTAG_RE = re.compile(r'<\|python_tag\|>\s*(\w+)\.call\((.*?)\)', re.S)

    def __init__(self):
        self._buf = ""
        self._buf_start = 0.0
        self._open = False

    # ── Buffer helpers ──
    def _start(self):
        self._buf = ""
        self._buf_start = time.perf_counter()
        self._open = True

    def _timeout(self) -> bool:
        return (time.perf_counter() - self._buf_start) * 1000 > 500

    # ── Public API ──
    def parse_with_buffer(self, chunk: str) -> List[Dict[str, Any]]:
        """Devuelve llamadas detectadas o [] si patrón no ha cerrado."""
        if any(sym in chunk for sym in ('[', '{', '<')):
            if not self._open:
                self._start()
            self._buf += chunk

            if not self._pattern_complete(self._buf):
                if self._timeout():
                    logger.warning("Parser timeout, descarto buffer para evitar JSON al TTS")
                    self._open = False
                    self._buf = ""
                return []

            # patrón completo
            calls = self._extract_calls(self._buf)
            self._open = False
            self._buf = ""
            return calls

        # chunk sin ningún inicio de patrón
        return []

    # ── Internos ──
    @staticmethod
    def _pattern_complete(text: str) -> bool:
        return (text.count('{') == text.count('}')) or \
               (text.count('[') == text.count(']')) or \
               (text.count('<') == text.count('>'))

    def _extract_calls(self, txt: str) -> List[Dict[str, Any]]:
        calls: List[Dict[str, Any]] = []

        # JSON formato
        if self.JSON_RE.match(txt.strip()):
            try:
                payload = json.loads(txt)
                payloads = payload if isinstance(payload, list) else [payload]
                for p in payloads:
                    if p.get("type") == "function":
                        calls.append({"name": p["name"],
                                      "arguments": p.get("parameters", {})})
            except json.JSONDecodeError:
                pass

        # XML formato
        for m in self.XML_RE.finditer(txt):
            try:
                args = json.loads(m.group(2))
            except json.JSONDecodeError:
                args = {}
            calls.append({"name": m.group(1), "arguments": args})

        # Llama python_tag
        for m in self.PYTAG_RE.finditer(txt):
            calls.append({"name": m.group(1),
                          "arguments": self._parse_kv_pairs(m.group(2))})

        # Lista clásica
        for m in self.LIST_RE.finditer(txt):
            calls.append({"name": m.group(1),
                          "arguments": self._parse_kv_pairs(m.group(2))})

        return calls

    @staticmethod
    def _parse_kv_pairs(argstr: str) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for pair in re.split(r'\s*,\s*', argstr.strip()):
            if '=' in pair:
                k, v = pair.split('=', 1)
                try:
                    out[k.strip()] = json.loads(v)
                except json.JSONDecodeError:
                    out[k.strip()] = v.strip().strip('"\'')
        return out


# ╭─ Limpia patrones para que nada “JSON” llegue al TTS ────────────────────────
_CLEAN_RE = re.compile(
    r'(<\|python_tag\|>.*?|\[.*?\]|\{.*?"type"\s*:\s*"function".*?\}|' +
    r'<function=.*?</function>)',
    re.S
)


def remove_all_tool_patterns(text: str) -> str:
    return _CLEAN_RE.sub('', text).strip()


# ╭─ AIAgent ────────────────────────────────────────────────────────────────────
class AIAgent:
    """
    Orquesta: respuesta LLM → detecta herramienta → ejecuta → respuesta sintética
    Mantiene compatibilidad con SessionManager y session_state existentes.
    """
    def __init__(self, call_sid: str):
        self.call_sid = call_sid
        self.tool_engine = ToolEngine()

    # Este método se llama desde tu manejador de streaming
    async def process_stream(
        self,
        llm_chunk: str,
        history: List[Dict[str, str]]
    ) -> str:
        """
        Retorna texto limpio para TTS.
        """
        emit_latency_event(self.call_sid, "chunk_received")

        tool_calls = self.tool_engine.parse_with_buffer(llm_chunk)
        emit_latency_event(self.call_sid, "parse_start")

        clean_text = remove_all_tool_patterns(llm_chunk)

        # ─── Caso 1: Solo herramientas ───────────────────────────────────────
        if tool_calls and not clean_text:
            emit_latency_event(self.call_sid, "tool_detected", {"count": len(tool_calls)})
            return await self._execute_tools_and_respond(tool_calls)

        # ─── Caso 2: Texto + herramientas ───────────────────────────────────
        if tool_calls and clean_text:
            emit_latency_event(self.call_sid, "tool_detected", {"count": len(tool_calls)})

            transition_words = {"un momento", "verificando", "por favor espere"}
            if clean_text.lower().strip() in transition_words:
                # Respuesta transicional → TTS inmediato, herramientas en background
                asyncio.create_task(self._execute_tools_and_respond(tool_calls))
                return clean_text

            synthetic = await self._execute_tools_and_respond(tool_calls)
            return f"{clean_text} {synthetic}"

        # ─── Caso 3: Solo texto ──────────────────────────────────────────────
        return clean_text

    # ── Helpers internos ────────────────────────────────────────────────────
    async def _execute_tools_and_respond(self, calls: List[Dict[str, Any]]) -> str:
        emit_latency_event(self.call_sid, "tool_exec_start")
        try:
            results = await asyncio.wait_for(self._execute_tools(calls), timeout=4.0)
        except asyncio.TimeoutError:
            emit_latency_event(self.call_sid, "tool_timeout")
            return "Un momento por favor…"
        emit_latency_event(self.call_sid, "tool_exec_end")

        # Solo usamos la primera llamada para generar respuesta sintética
        first = calls[0]["name"]
        return generate_synthetic_response(first, results.get(first, {}))

    async def _execute_tools(self, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        for call in calls:
            name = call["name"]
            func = TOOL_REGISTRY.get(name)
            if func is None:
                logger.error(f"Herramienta no registrada: {name}")
                continue
            try:
                out[name] = await asyncio.wait_for(
                    func(**call["arguments"]), timeout=2.0
                )
            except asyncio.TimeoutError:
                logger.error(f"Timeout ejecutando herramienta: {name}")
        return out
