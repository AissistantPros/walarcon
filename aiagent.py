# aiagent.py
# -*- coding: utf-8 -*-
"""
Motor de Decisión (Versión Final de Producción para Groq/Llama 3.3)
"""
import asyncio
import json
import logging
import re
import shlex  # <-- Usamos shlex para un parsing más seguro
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
    TOOL_CALL_PATTERN = re.compile(r'\[(\w+)\((.*?)\)\]', re.DOTALL)

    def __init__(self, tool_definitions: List[Dict]):
        self.tool_schemas = {tool['function']['name']: tool['function'] for tool in tool_definitions}
        self.tool_executors = self._map_executors()

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
            # Agrega aquí cualquier otra herramienta que falte
        }

    def parse_tool_calls(self, text: str) -> List[Dict]:
        """Parsea el texto crudo del LLM y extrae las llamadas a herramientas."""
        tool_calls = []
        for match in self.TOOL_CALL_PATTERN.finditer(text):
            tool_name, args_str = match.groups()
            if tool_name in self.tool_schemas:
                try:
                    args = self._parse_arguments_with_shlex(args_str)
                    tool_calls.append({"name": tool_name, "arguments": args})
                except Exception as e:
                    logger.warning(f"Error parseando argumentos para '{tool_name}' con shlex: {e}")
        return tool_calls
    
    def _parse_arguments_with_shlex(self, args_str: str) -> Dict[str, Any]:
        """Parsea argumentos de forma segura usando shlex, ideal para producción."""
        if not args_str.strip(): return {}
        args = {}
        # shlex.split maneja correctamente las comillas y espacios
        parts = shlex.split(args_str)
        for part in parts:
            if '=' in part:
                key, value = part.split('=', 1)
                args[key.strip()] = self._convert_type(value)
        return args
    
    def _convert_type(self, value: str) -> Any:
        """Convierte un string a su tipo Python más probable de forma segura."""
        value = value.strip()
        if value.lower() == 'true': return True
        if value.lower() == 'false': return False
        if value.lower() == 'none' or value.lower() == 'null': return None
        try:
            return int(value)
        except ValueError:
            try:
                return float(value)
            except ValueError:
                return value # Dejar como string

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
                return await executor(**arguments)
            else:
                return executor(**arguments)
        except Exception as e:
            logger.exception(f"La ejecución de la herramienta '{tool_name}' falló.")
            return {
                "error": f"Fallo en la ejecución de {tool_name}",
                "details": str(e),
                "arguments_used": arguments
            }

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
        session_state = self.session_manager.get_state(session_id)
        
        detected_intent = self._detect_intent(history, session_state.get("mode"))
        if detected_intent:
            self.session_manager.set_mode(session_id, detected_intent)
        
        full_prompt = self.prompt_engine.generate_prompt(history, detected_intent)
        
        try:
            stream = await self.groq_client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": full_prompt}],
                temperature=0.1, stream=True
            )
            full_response_text = ""
            async for chunk in stream:
                full_response_text += chunk.choices[0].delta.content or ""
        except Exception as e:
            logger.error(f"Error en la llamada a Groq: {e}")
            return "Lo siento, hay un problema con la conexión al asistente. Por favor, intente de nuevo."

        user_facing_text = re.sub(self.tool_engine.TOOL_CALL_PATTERN, "", full_response_text).strip()
        tool_calls = self.tool_engine.parse_tool_calls(full_response_text)
        
        if tool_calls:
            tool_tasks = [self.tool_engine.execute_tool(tc) for tc in tool_calls]
            results = await asyncio.gather(*tool_tasks)
            
            history.append({"role": "assistant", "content": full_response_text})
            for tool_call, result in zip(tool_calls, results):
                history.append({
                    "role": "tool", "name": tool_call["name"],
                    "content": json.dumps(result, ensure_ascii=False)
                })
        else:
             history.append({"role": "assistant", "content": user_facing_text})
        
        return user_facing_text


# --- Definiciones Completas de Herramientas ---
# Esta lista maestra contiene todas las herramientas disponibles para el agente.
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


ai_agent = AIAgent(tool_definitions=ALL_TOOLS)

async def generate_ai_response(session_id: str, history: List[Dict]) -> str:
    """Función pública que será llamada desde tw_utils.py."""
    return await ai_agent.process_stream(session_id, history)