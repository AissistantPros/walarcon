# aiagent.py
# -*- coding: utf-8 -*-
"""
Motor de Decisión (Versión Final de Producción para Groq/Llama 3.3)
"""
import asyncio
import json
import logging
import re
import shlex
import time
from time import perf_counter
from typing import Dict, List, Any, Optional, Callable

from decouple import config
from groq import AsyncGroq

# Importamos nuestro motor de prompts final del paso anterior
from prompt import LlamaPromptEngine

# --- Configuración ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)5s | %(name)s: %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger("aiagent")

# --- Clientes y Gestores ---
try:
    client = AsyncGroq(api_key=config("GROQ_API_KEY"))
    logger.info("Cliente AsyncGroq inicializado correctamente.")
except Exception as e:
    logger.critical(f"No se pudo inicializar el cliente Groq. Verifica GROQ_API_KEY: {e}")
    client = None

class SessionManager:
    """Gestiona el estado de la conversación para cada sesión única."""
    def __init__(self):
        self.sessions: Dict[str, Dict[str, Any]] = {}

    def get_state(self, session_id: str) -> Dict[str, Any]:
        if session_id not in self.sessions:
            self.sessions[session_id] = {"mode": None}
        return self.sessions[session_id]

    def set_mode(self, session_id: str, mode: str):
        state = self.get_state(session_id)
        state['mode'] = mode

# --- Motor de Herramientas con Parsing Seguro ---
class ToolEngine:
    """Encapsula el parseo, validación y ejecución de herramientas."""
    # Patrones para detectar diferentes formatos
    TOOL_CALL_PATTERN = re.compile(r'\[(\w+)\((.*?)\)\]', re.DOTALL)
    JSON_PATTERN = re.compile(r'\{[^{}]*"type"\s*:\s*"function"[^{}]*\}', re.DOTALL)
    XML_PATTERN = re.compile(r'<function\s*=\s*(\w+)>(.*?)</function>', re.DOTALL)
    PYTHON_TAG_PATTERN = re.compile(r'<\|python_tag\|>\s*(\w+)\.call\((.*?)\)', re.DOTALL)

    def __init__(self, tool_definitions: List[Dict]):
        self.tool_schemas = {tool['function']['name']: tool['function'] for tool in tool_definitions}
        self.tool_executors = self._map_executors()
        self._buffer = ""
        self._buffer_start_time = None
        self._buffer_timeout = 0.5  # 500ms

    def _map_executors(self) -> Dict[str, Callable]:
        """Mapea nombres de herramientas a las funciones de Python."""
        # Importaciones de tu lógica de negocio
        from consultarinfo import get_consultorio_data_from_cache
        from crearcita import create_calendar_event
        from editarcita import edit_calendar_event
        from eliminarcita import delete_calendar_event
        from utils import search_calendar_event_by_phone
        from weather_utils import get_cancun_weather
        import buscarslot

        return {
            "read_sheet_data": get_consultorio_data_from_cache,
            "process_appointment_request": buscarslot.process_appointment_request,
            "create_calendar_event": create_calendar_event,
            "edit_calendar_event": edit_calendar_event,
            "delete_calendar_event": delete_calendar_event,
            "search_calendar_event_by_phone": search_calendar_event_by_phone,
            "get_cancun_weather": get_cancun_weather,
            "end_call": self._handle_end_call,  # <<< CAMBIO 1: AGREGADO
        }

    # <<< CAMBIO 2: MÉTODO AGREGADO >>>
    def _handle_end_call(self, reason: str = "user_request") -> Dict:
        """Marca que se debe terminar la llamada."""
        return {"action": "end_call", "reason": reason}

    def parse_tool_calls(self, text: str) -> List[Dict]:
        """Parsea el texto crudo del LLM y extrae las llamadas a herramientas."""
        tool_calls = []
        
        # Intentar con todos los formatos
        # 1. Formato preferido [tool(args)]
        for match in self.TOOL_CALL_PATTERN.finditer(text):
            tool_name, args_str = match.groups()
            if tool_name in self.tool_schemas:
                try:
                    args = self._parse_arguments_with_shlex(args_str)
                    tool_calls.append({"name": tool_name, "arguments": args})
                except Exception as e:
                    logger.warning(f"Error parseando argumentos para '{tool_name}': {e}")
        
        # 2. Formato JSON (el problemático)
        for match in self.JSON_PATTERN.finditer(text):
            try:
                json_obj = json.loads(match.group(0))
                if json_obj.get("type") == "function" and json_obj.get("name") in self.tool_schemas:
                    tool_calls.append({
                        "name": json_obj["name"],
                        "arguments": json_obj.get("parameters", {})
                    })
            except Exception as e:
                logger.warning(f"Error parseando JSON de herramienta: {e}")
        
        # 3. Formato XML
        for match in self.XML_PATTERN.finditer(text):
            tool_name = match.group(1)
            if tool_name in self.tool_schemas:
                try:
                    args_json = match.group(2)
                    args = json.loads(args_json) if args_json.strip() else {}
                    tool_calls.append({"name": tool_name, "arguments": args})
                except Exception as e:
                    logger.warning(f"Error parseando XML para '{tool_name}': {e}")
        
        # 4. Formato python_tag
        for match in self.PYTHON_TAG_PATTERN.finditer(text):
            tool_name = match.group(1)
            if tool_name in self.tool_schemas:
                try:
                    args = self._parse_arguments_with_shlex(match.group(2))
                    tool_calls.append({"name": tool_name, "arguments": args})
                except Exception as e:
                    logger.warning(f"Error parseando python_tag para '{tool_name}': {e}")
        
        return tool_calls
    
    def _parse_arguments_with_shlex(self, args_str: str) -> Dict[str, Any]:
        """
        Parsea argumentos de forma segura usando shlex y limpia comas finales de los valores.
        """
        if not args_str.strip():
            return {}
            
        args = {}
        try:
            # shlex.split maneja correctamente las comillas y espacios
            parts = shlex.split(args_str)
            for part in parts:
                if '=' in part:
                    key, value = part.split('=', 1)
                    args[key.strip()] = self._convert_type(value)
        except Exception as e:
            print(f"Advertencia: Error en shlex parsing: {e}, intentando parsing simple")
            for pair in args_str.split(','):
                if '=' in pair:
                    key, value = pair.split('=', 1)
                    args[key.strip()] = self._convert_type(value.strip())

        cleaned_args = {}
        for key, value in args.items():
            if isinstance(value, str):
                value = value.rstrip(',')
            cleaned_args[key] = value
        return cleaned_args
    
    def _convert_type(self, value: str) -> Any:
        """Convierte un string a su tipo Python más probable."""
        value = value.strip().strip('"\'')
        if value.lower() == 'true': return True
        if value.lower() == 'false': return False
        if value.lower() in ('none', 'null'): return None
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value

    async def execute_tool(self, tool_call: Dict) -> Dict:
        """Ejecuta una herramienta y devuelve un resultado estructurado."""
        tool_name = tool_call["name"]
        arguments = tool_call["arguments"]
        executor = self.tool_executors.get(tool_name)
        
        if not executor:
            return {"error": f"Herramienta desconocida: {tool_name}", "details": "El ejecutor no fue encontrado."}
        
        try:
            logger.info(f"Ejecutando: {tool_name} con {arguments}")
            if asyncio.iscoroutinefunction(executor):
                result = await executor(**arguments)
            else:
                result = executor(**arguments)
            return result
        except Exception as e:
            logger.exception(f"La ejecución de la herramienta '{tool_name}' falló.")
            return {
                "error": f"Fallo en la ejecución de {tool_name}",
                "details": str(e),
                "arguments_used": arguments
            }

    def remove_tool_patterns(self, text: str) -> str:
        """Elimina TODOS los patrones de herramientas del texto."""
        text = self.TOOL_CALL_PATTERN.sub('', text)
        text = self.JSON_PATTERN.sub('', text)
        text = self.XML_PATTERN.sub('', text)
        text = self.PYTHON_TAG_PATTERN.sub('', text)
        return text.strip()

# --- Agente Principal de IA (Orquestador) ---
class AIAgent:
    def __init__(self, tool_definitions: List[Dict]):
        self.groq_client = client
        self.prompt_engine = LlamaPromptEngine(tool_definitions=tool_definitions)
        self.tool_engine = ToolEngine(tool_definitions)
        self.session_manager = SessionManager()
        self.model = "llama-3.3-70b-versatile"

    def _detect_intent(self, history: List[Dict], current_mode: Optional[str]) -> Optional[str]:
        if not history: return None
        last_message = history[-1]['content'].lower()
        mode_keywords = {
            "crear_cita": ["agendar", "cita", "reservar", "espacio"],
            "editar_cita": ["cambiar", "modificar", "reprogramar"],
            "eliminar_cita": ["cancelar", "borrar", "anular"]
        }
        for mode, keywords in mode_keywords.items():
            if any(keyword in last_message for keyword in keywords):
                return mode
        return current_mode

    async def process_stream(self, session_id: str, history: List[Dict]) -> str:
        """Orquesta el flujo completo en un solo pase de streaming."""
        from state_store import emit_latency_event
        
        session_state = self.session_manager.get_state(session_id)
        
        detected_intent = self._detect_intent(history, session_state.get("mode"))
        if detected_intent:
            self.session_manager.set_mode(session_id, detected_intent)
        
        full_prompt = self.prompt_engine.generate_prompt(history, detected_intent)
        
        emit_latency_event(session_id, "chunk_received")
        
        try:
            stream = await self.groq_client.chat.completions.create(
                model=self.model, 
                messages=[{"role": "user", "content": full_prompt}],
                temperature=0.1, 
                stream=True
            )
            full_response_text = ""
            async for chunk in stream:
                full_response_text += chunk.choices[0].delta.content or ""
        except Exception as e:
            logger.error(f"Error en la llamada a Groq: {e}")
            return "Lo siento, hay un problema con la conexión al asistente. Por favor, intente de nuevo."

        emit_latency_event(session_id, "parse_start")
        
        user_facing_text = self.tool_engine.remove_tool_patterns(full_response_text).strip()
        
        tool_calls = self.tool_engine.parse_tool_calls(full_response_text)
        
        if tool_calls:
            emit_latency_event(session_id, "tool_detected", {"count": len(tool_calls)})
            
            emit_latency_event(session_id, "tool_exec_start")
            tool_tasks = [self.tool_engine.execute_tool(tc) for tc in tool_calls]
            results = await asyncio.gather(*tool_tasks)
            emit_latency_event(session_id, "tool_exec_end")
            
            history.append({"role": "assistant", "content": full_response_text})
            for tool_call, result in zip(tool_calls, results):
                history.append({
                    "role": "tool", 
                    "name": tool_call["name"],
                    "content": json.dumps(result, ensure_ascii=False)
                })
            
            if not user_facing_text:
                from synthetic_responses import generate_synthetic_response
                if tool_calls and results:
                    user_facing_text = generate_synthetic_response(
                        tool_calls[0]["name"], 
                        results[0]
                    )
                else:
                    user_facing_text = "He procesado su solicitud."
        else:
            history.append({"role": "assistant", "content": user_facing_text})
        
        emit_latency_event(session_id, "response_complete")
        return user_facing_text

# El resto del archivo no cambia, pero lo incluyo para que sea completo
# --- Definiciones Completas de Herramientas ---
ALL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información general del consultorio como dirección, horarios de atención general, servicios principales, o políticas de cancelación. No usar para verificar disponibilidad de citas."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cancun_weather",
            "description": "Obtener el estado del tiempo actual en Cancún, como temperatura, descripción (soleado, nublado, lluvia), y sensación térmica. Útil si el usuario pregunta específicamente por el clima."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detecta la intención del usuario cuando no está claro si quiere agendar en un horario 'más tarde' (more_late) o 'más temprano' (more_early) de la hora que le propusimos.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intention": {
                        "type": "string",
                        "enum": ["more_late", "more_early"],
                        "description": "La intención detectada del usuario."
                    }
                },
                "required": ["intention"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_mode",
            "description": "Cambia el modo de operación del asistente. Úsala cuando detectes una intención clara del usuario de agendar, editar o eliminar una cita.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["crear", "editar", "eliminar", "None"],
                        "description": "'crear' para agendar, 'editar' para modificar, 'eliminar' para cancelar, 'None' para modo general."
                    }
                },
                "required": ["mode"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": "Cierra la llamada de manera definitiva. Úsala cuando ya se haya despedido al paciente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo del cierre. Ej: 'user_request', 'task_completed'."
                    }
                },
                "required": ["reason"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_appointment_request",
            "description": "Procesa la solicitud de agendamiento o consulta de disponibilidad de citas. Interpreta la petición de fecha/hora del usuario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "user_query_for_date_time": {"type": "string"},
                    "day_param": {"type": "integer"},
                    "month_param": {"type": ["string", "integer"]},
                    "year_param": {"type": "integer"},
                    "fixed_weekday_param": {"type": "string"},
                    "explicit_time_preference_param": {"type": "string", "enum": ["mañana", "tarde", "mediodia"]},
                    "is_urgent_param": {"type": "boolean"},
                    "more_late_param": {"type": "boolean"},
                    "more_early_param": {"type": "boolean"}
                },
                "required": ["user_query_for_date_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear una nueva cita médica en el calendario después de que el usuario haya confirmado todo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "phone": {"type": "string"},
                    "reason": {"type": "string"},
                    "start_time": {"type": "string", "format": "date-time"},
                    "end_time": {"type": "string", "format": "date-time"}
                },
                "required": ["name", "phone", "start_time", "end_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas existentes de un paciente por su número de teléfono."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_calendar_event",
            "description": "Modificar una cita existente en el calendario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "new_start_time_iso": {"type": "string", "format": "date-time"},
                    "new_end_time_iso": {"type": "string", "format": "date-time"},
                    "new_name": {"type": "string"},
                    "new_reason": {"type": "string"},
                    "new_phone_for_description": {"type": "string"}
                },
                "required": ["event_id", "new_start_time_iso", "new_end_time_iso"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Eliminar/Cancelar una cita existente del calendario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "original_start_time_iso": {"type": "string", "format": "date-time"}
                },
                "required": ["event_id", "original_start_time_iso"]
            }
        }
    }
]

# Instancia global del agente
ai_agent = AIAgent(tool_definitions=ALL_TOOLS)

async def generate_ai_response(session_id: str, history: List[Dict]) -> str:
    """Función pública que será llamada desde tw_utils.py."""
    return await ai_agent.process_stream(session_id, history)