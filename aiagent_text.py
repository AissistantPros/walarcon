import os
import json
from typing import List, Dict, Optional
from decouple import config
from openai import OpenAI

# 1. Importamos la función para generar el prompt desde tu archivo prompt_text.py
from prompt_text import generate_openai_prompt

# ----- Configuración del Cliente OpenAI y Modelo -----
CLIENT_INIT_ERROR = None
client = None
try:
    print("[aiagent_text.py] Intentando inicializar cliente OpenAI...")
    client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))
except Exception as e_client:
    CLIENT_INIT_ERROR = str(e_client)
    print(f"[aiagent_text.py] ERROR al inicializar OpenAI: {CLIENT_INIT_ERROR}")

# --- Modelo por defecto para texto ---
MODEL_TO_USE = "gpt-4.1-mini"   # tu modelo rápido, ventana grande

# -----  Librerías y utilidades de herramientas -----
from buscarslot import process_appointment_request
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from utils import search_calendar_event_by_phone
from selectevent import select_calendar_event_by_index
from consultarinfo import get_consultorio_data_from_cache  # versión con caché
from weather_utils import get_cancun_weather

def handle_detect_intent(**kwargs) -> Dict:
    return {"intent_detected": kwargs.get("intention")}

# ====== Mapeo de funciones reales (tool name → función Python) ======
tool_functions_map = {
    "read_sheet_data": get_consultorio_data_from_cache,
    "process_appointment_request": process_appointment_request,
    "create_calendar_event": create_calendar_event,
    "search_calendar_event_by_phone": search_calendar_event_by_phone,
    "select_calendar_event_by_index": select_calendar_event_by_index,
    "edit_calendar_event": edit_calendar_event,
    "delete_calendar_event": delete_calendar_event,
    "detect_intent": handle_detect_intent,
    "get_cancun_weather": get_cancun_weather,
}

# ═════════════════ UNIFIED TOOLS DEFINITION ══════════════════════
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información general del consultorio: costos, políticas de cancelación, horarios. No usar para verificar disponibilidad de citas."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cancun_weather",
            "description": "Obtener el estado del tiempo actual y la temperatura en Cancún. Útil si el usuario pregunta específicamente por el clima."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_appointment_request",
            "description": "Analizar la intención del usuario y extraer fecha, hora y motivo de la cita. Devuelve JSON con fields: {date, time, motive}."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear una cita nueva en Google Calendar con la información proporcionada.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "Fecha YYYY-MM-DD"},
                    "time": {"type": "string", "description": "Hora HH:MM"},
                    "motive": {"type": "string", "description": "Motivo de la consulta"},
                    "name": {"type": "string", "description": "Nombre del paciente"},
                    "phone": {"type": "string", "description": "Teléfono del paciente"}
                },
                "required": ["date", "time", "name", "phone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar próximas citas por número telefónico del paciente."
        }
    },
    {
        "type": "function",
        "function": {
            "name": "select_calendar_event_by_index",
            "description": "Seleccionar una cita específica por su índice en una lista.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Índice (0-based) de la cita"}
                },
                "required": ["index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_calendar_event",
            "description": "Editar la fecha u hora de una cita existente.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string"},
                    "new_date": {"type": "string"},
                    "new_time": {"type": "string"}
                },
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Eliminar una cita existente.",
            "parameters": {
                "type": "object",
                "properties": {"event_id": {"type": "string"}},
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detectar la intención general del usuario (pregunta informativa, solicitud de cita, etc.)."
        }
    }
]

# ---------------- FUNCIÓN PRINCIPAL ----------------
def process_text_message(
    user_id: str,
    current_user_message: str,
    history: List[Dict],
    client_info: Optional[Dict] = None,
) -> Dict:
    """
    Procesa un mensaje de texto usando GPT-4.1-mini + tool-calling nativo.
    Retorna un dict {reply_text:str, status:str}
    """

    conv_id_for_logs = f"conv:{user_id[:4]}…"  # para logs cortos

    if CLIENT_INIT_ERROR:
        print(f"[{conv_id_for_logs}] Cliente OpenAI no iniciado: {CLIENT_INIT_ERROR}")
        return {
            "reply_text": "Ups, el asistente de texto no está disponible en este momento 😕",
            "status": "error_init_openai",
        }

    # 1) Construimos el prompt
    messages_for_api = generate_openai_prompt(history) + [
        {"role": "user", "content": current_user_message}
    ]

    try:
        print(
            f"[{conv_id_for_logs}] 1ª llamada a GPT con modelo {MODEL_TO_USE}. "
            f"Mensajes: {len(messages_for_api)}"
        )

        chat_completion = client.chat.completions.create(
            model=MODEL_TO_USE,
            messages=messages_for_api,
            tools=TOOLS,
            tool_choice="auto",

            # ← AQUÍ pones tus ajustes
            temperature=0.4,        # 0-1 (0 = ultra-determinista)
            max_tokens=512,         # tope de la respuesta
            top_p=0.9,              # nucleus sampling
            presence_penalty=0.3,   # incentiva temas nuevos
            frequency_penalty=0.2,  # evita repeticiones
        )


        response_message = chat_completion.choices[0].message
        tool_calls = response_message.tool_calls

        # 2) ¿Invocó alguna tool?
        if tool_calls:
            print(
                f"[{conv_id_for_logs}] GPT solicitó {len(tool_calls)} tool_call(s): {tool_calls}"
            )

            messages_for_api.append(response_message)  # tool_call en historial
            tool_call = tool_calls[0]
            func_name = tool_call.function.name
            func_args = json.loads(tool_call.function.arguments or "{}")

            
            # Ejecutamos la función real con timeout
            if func_name in tool_functions_map:
                try:
                    # Timeout de 10 segundos para herramientas
                    import asyncio
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    
                    tool_result = loop.run_until_complete(
                        asyncio.wait_for(
                            asyncio.to_thread(tool_functions_map[func_name], **func_args),
                            timeout=10.0
                        )
                    )
                except asyncio.TimeoutError:
                    tool_result = {
                        "error": f"timeout_exceeded",
                        "message": f"La operación {func_name} tardó más de 10 segundos"
                    }
                    print(f"[{conv_id_for_logs}] ⏰ TIMEOUT en tool {func_name}")
                except Exception as e_tool:
                    tool_result = {
                        "error": f"tool_execution_error",
                        "message": f"Error ejecutando {func_name}: {str(e_tool)}"
                    }
                    print(f"[{conv_id_for_logs}] ❌ ERROR en tool {func_name}: {e_tool}")
                finally:
                    loop.close()
            else:
                tool_result = {"error": f"Función {func_name} no registrada."}
            
            print(f"[{conv_id_for_logs}] Resultado tool {func_name}: {tool_result}")

            # System message con la respuesta de la tool
            messages_for_api.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": func_name,
                    "content": json.dumps(tool_result),
                }
            )

            # 3) Segunda pasada para respuesta final
            second_chat_completion = client.chat.completions.create(
                model=MODEL_TO_USE,
                messages=messages_for_api,
                temperature=0.4,
                max_tokens=512,
                top_p=0.9,
            )

            ai_final_response_content = (
                second_chat_completion.choices[0].message.content.strip()
            )
            status_message = "success_with_tool"
        else:
            ai_final_response_content = response_message.content.strip()
            status_message = "success_no_tool"

        print(
            f"[{conv_id_for_logs}] Respuesta final: '{ai_final_response_content}'"
        )

        return {
            "reply_text": ai_final_response_content,
            "status": status_message,
        }

    except Exception as e_main_process:
        import traceback
        traceback.print_exc()
        
        # Mensaje más específico según el tipo de error
        error_str = str(e_main_process).lower()
        
        if "rate_limit" in error_str or "429" in error_str:
            return {
                "reply_text": "El sistema está muy ocupado ahora. Por favor intenta en unos segundos 😊",
                "status": "error_rate_limit"
            }
        elif "api_key" in error_str or "authentication" in error_str or "401" in error_str:
            return {
                "reply_text": "Hay un problema de configuración. Por favor contacta al administrador 🔧",
                "status": "error_api_key"
            }
        elif "timeout" in error_str or "timed out" in error_str:
            return {
                "reply_text": "La respuesta tardó demasiado. Por favor intenta de nuevo 🕐",
                "status": "error_timeout"
            }
        elif "connection" in error_str or "network" in error_str:
            return {
                "reply_text": "Parece que hay problemas de conexión. Intenta en un momento 📡",
                "status": "error_connection"
            }
        else:
            return {
                "reply_text": "¡Caramba! 😅 Hubo un problema procesando tu mensaje. "
                "¿Podrías intentar de nuevo?",
                "status": "error_processing_message",
            }