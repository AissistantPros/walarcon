import os
import json
from typing import List, Dict, Optional
from decouple import config
from openai import OpenAI
from groq import Groq

# 1. Importamos la función para generar el prompt desde tu archivo prompt_text.py
from prompt_text import generate_openai_prompt

# ----- Configuración del Cliente OpenAI y Modelo -----
CLIENT_INIT_ERROR = None
client = None
try:
    print("[aiagent_text.py] Intentando inicializar cliente Groq...") # (cambio opcional para claridad)
    # client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))
    client = Groq(api_key=config("GROQ_API_KEY"))
    print("[aiagent_text.py] Cliente Groq inicializado aparentemente con éxito.") # (cambio opcional para claridad)
except Exception as e:
    CLIENT_INIT_ERROR = str(e)
    print(f"[aiagent_text.py] CRITICAL: No se pudo inicializar el cliente OpenAI. Verifica CHATGPT_SECRET_KEY: {e}")
    # client permanece None

MODEL_TO_USE = "llama3-70b-8192"

# ----- Herramientas y Funciones de Mapeo -----
from buscarslot import process_appointment_request
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from utils import search_calendar_event_by_phone
from selectevent import select_calendar_event_by_index
from consultarinfo import get_consultorio_data_from_cache # Usaremos la versión con caché
from weather_utils import get_cancun_weather 

def handle_detect_intent(**kwargs) -> Dict:
    return {"intent_detected": kwargs.get("intention")}

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

# ══════════════════ UNIFIED TOOLS DEFINITION ══════════════════════
TOOLS = [
    # ... (Tu lista TOOLS completa va aquí, no la modifico para brevedad)
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
            # No necesita parámetros ya que la ciudad está fija en la función.
        }
    },
    {
        "type": "function",
        "function": {
            "name": "process_appointment_request",
            "description": (
                "Procesa la solicitud de agendamiento o consulta de disponibilidad de citas. "
                "Interpreta la petición de fecha/hora del usuario (ej. 'próxima semana', 'el 15 a las 10', 'esta semana en la tarde', 'lo más pronto posible') "
                "y busca un slot disponible en el calendario que cumpla con los criterios. "
                "Devuelve un slot encontrado, un mensaje si no hay disponibilidad, o pide aclaración si la fecha es ambigua o conflictiva."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "user_query_for_date_time": {
                        "type": "string",
                        "description": "La frase textual completa del usuario referente a la fecha y/o hora deseada. Ej: 'quiero una cita para el próximo martes por la tarde', '¿tienes algo para el 15 de mayo a las 10 am?', 'lo más pronto posible'."
                    },
                    "day_param": {"type": "integer", "description": "Día numérico del mes si el usuario lo menciona explícitamente (ej. 15 para 'el 15 de mayo'). Opcional."},
                    "month_param": {"type": ["string", "integer"], "description": "Mes, como nombre (ej. 'mayo', 'enero') o número (ej. 5, 1) si el usuario lo menciona. Opcional."},
                    "year_param": {"type": "integer", "description": "Año si el usuario lo especifica (ej. 2025). Opcional, si no se da, se asume el actual o el siguiente si la fecha es pasada."},
                    "fixed_weekday_param": {"type": "string", "description": "Día de la semana solicitado por el usuario (ej. 'lunes', 'martes'). Opcional."},
                    "explicit_time_preference_param": {"type": "string", "description": "Preferencia explícita de franja horaria como 'mañana', 'tarde' o 'mediodia', si el usuario la indica claramente. Opcional.", "enum": ["mañana", "tarde", "mediodia"]},
                    "is_urgent_param": {"type": "boolean", "description": "Poner a True si el usuario indica urgencia o quiere la cita 'lo más pronto posible', 'cuanto antes', etc. Esto priorizará la búsqueda inmediata. Opcional, default False."},
                    "more_late_param": {"type": "boolean", "description": "Cuando el usuario pide ‘más tarde’ después de ofrecerle un horario. Opcional."},
                    "more_early_param": {"type": "boolean", "description": "Cuando el usuario pide ‘más temprano’ después de ofrecerle un horario. Opcional."}
                },
                "required": ["user_query_for_date_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear una nueva cita médica en el calendario DESPUÉS de que el usuario haya confirmado un slot específico, nombre, teléfono y motivo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Nombre completo del paciente."},
                    "phone": {"type": "string", "description": "Número de teléfono del paciente (10 dígitos)."},
                    "reason": {"type": "string", "description": "Motivo de la consulta."},
                    "start_time": {"type": "string", "format": "date-time", "description": "Hora de inicio de la cita en formato ISO8601 con offset (ej. markup-MM-DDTHH:MM:SS-05:00). Obtenido de 'process_appointment_request'."},
                    "end_time": {"type": "string", "format": "date-time", "description": "Hora de fin de la cita en formato ISO8601 con offset. Obtenido de 'process_appointment_request'."}
                },
                "required": ["name", "phone", "start_time", "end_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas existentes de un paciente por su número de teléfono para poder modificarlas o cancelarlas.",
            "parameters": {
                "type": "object",
                "properties": {"phone": {"type": "string", "description": "Número de teléfono del paciente (10 dígitos)."}},
                "required": ["phone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "select_calendar_event_by_index",
            "description": (
                "Marca cuál de las citas encontradas (events_found) "
                "es la que el paciente quiere modificar o cancelar. "
                "Úsalo después de enumerar las citas y recibir la confirmación "
                "del paciente. selected_index = 0 para la primera cita listada."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "selected_index": {
                        "type": "integer",
                        "description": "Índice de la cita (0, 1, 2…)."
                    }
                },
                "required": ["selected_index"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_calendar_event",
            "description": "Modificar una cita existente en el calendario. Requiere el ID del evento y los nuevos detalles de fecha/hora. Opcionalmente puede actualizar nombre, motivo o teléfono en la descripción.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "El ID del evento de calendario a modificar. Obtenido de 'search_calendar_event_by_phone'."},
                    "new_start_time_iso": {"type": "string", "format": "date-time", "description": "Nueva hora de inicio para la cita en formato ISO8601 con offset (ej. 2025-MM-DDTHH:MM:SS-05:00). Obtenida de 'process_appointment_request'."},
                    "new_end_time_iso": {"type": "string", "format": "date-time", "description": "Nueva hora de fin para la cita en formato ISO8601 con offset. Obtenida de 'process_appointment_request'."},
                    "new_name": {"type": "string", "description": "Opcional. Nuevo nombre del paciente si el usuario desea cambiarlo."},
                    "new_reason": {"type": "string", "description": "Opcional. Nuevo motivo de la consulta si el usuario desea cambiarlo."},
                    "new_phone_for_description": {"type": "string", "description": "Opcional. Nuevo teléfono para la descripción de la cita si el usuario desea cambiarlo."}
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
                    "event_id": {"type": "string", "description": "El ID del evento de calendario a eliminar. Obtenido de 'search_calendar_event_by_phone'."},
                    "original_start_time_iso": {"type": "string", "format": "date-time", "description": "Hora de inicio original de la cita a eliminar en formato ISO8601 con offset (ej. 2025-MM-DDTHH:MM:SS-05:00), para confirmación."}
                },
                "required": ["event_id", "original_start_time_iso"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detecta la intención principal del usuario cuando no está claro si quiere agendar una nueva cita, o si cambia de opinión hacia modificar o cancelar una cita existente, o si pide 'más tarde' o 'más temprano' un horario.",
            "parameters": {
                "type": "object",
                "properties": {
                    "intention": {
                        "type": "string",
                        "enum": ["create", "edit", "delete", "informational", "unknown", "more_late", "more_early"],
                        "description": "La intención detectada del usuario."
                    }
                },
                "required": ["intention"]
            }
        }
    }
]

def process_text_message(user_id: str, current_user_message: str, conversation_history: List[Dict]) -> Dict:
    """
    Procesa un mensaje de texto entrante, llama a la IA, maneja herramientas y devuelve la respuesta final.
    """
    # Extraer conversation_id del historial si está disponible, o usar user_id
    # Esto es para que los logs sean más fáciles de seguir si tienes múltiples usuarios/conversaciones
    conv_id_for_logs = user_id # Valor por defecto
    if conversation_history and isinstance(conversation_history[0], dict) and "conversation_id_for_logs" in conversation_history[0]:
        conv_id_for_logs = conversation_history[0].get("conversation_id_for_logs")


    print(f"[{conv_id_for_logs}][aiagent_text.py] INICIO process_text_message. User: {user_id}, Mensaje: '{current_user_message}'")
    print(f"[{conv_id_for_logs}][aiagent_text.py] Historial de conversación recibido (longitud {len(conversation_history)}): {json.dumps(conversation_history, indent=2)}")

    if CLIENT_INIT_ERROR: # Si hubo un error al inicializar el cliente globalmente
        print(f"[{conv_id_for_logs}][aiagent_text.py] Error PREVIO en inicialización de cliente OpenAI: {CLIENT_INIT_ERROR}")
        return {
            "reply_text": "Lo siento, estoy teniendo problemas técnicos (configuración del asistente). Por favor, intenta más tarde.",
            "status": "error_openai_client_initialization_failed"
        }

    if not client: # Chequeo por si client es None después del try-except de inicialización
        print(f"[{conv_id_for_logs}][aiagent_text.py] Error CRÍTICO - Cliente OpenAI es None. No se puede proceder.")
        return {
            "reply_text": "Lo siento, estoy teniendo problemas técnicos graves (asistente no disponible). Por favor, intenta más tarde.",
            "status": "error_openai_client_is_none"
        }

    messages_for_api = []
    try:
        print(f"[{conv_id_for_logs}][aiagent_text.py] Generando prompt completo con generate_openai_prompt...")
        # Pasamos una copia del historial para no modificar el original accidentalmente si generate_openai_prompt lo hiciera
        messages_for_api = generate_openai_prompt(list(conversation_history))
        # print(f"[{conv_id_for_logs}][aiagent_text.py] Prompt completo para API (últimos 2 mensajes): {json.dumps(messages_for_api[-2:], indent=2)}")
    except Exception as e_prompt:
        print(f"[{conv_id_for_logs}][aiagent_text.py] ERROR generando prompt con generate_openai_prompt: {str(e_prompt)}")
        import traceback
        traceback.print_exc()
        return {
            "reply_text": "¡Ups! Tuve un problema preparando mi respuesta. ¿Podrías intentarlo de nuevo?",
            "status": "error_generating_prompt"
        }

    try:
        print(f"[{conv_id_for_logs}][aiagent_text.py] 1ª Llamada a OpenAI con modelo {MODEL_TO_USE}. Mensajes: {len(messages_for_api)}")
        
        chat_completion = client.chat.completions.create(
            model=MODEL_TO_USE,
            messages=messages_for_api,
            tools=TOOLS,
            tool_choice="auto" 
        )
        print(f"[{conv_id_for_logs}][aiagent_text.py] 1ª Llamada a OpenAI completada.")

        response_message = chat_completion.choices[0].message
        tool_calls = response_message.tool_calls

        if tool_calls:
            print(f"[{conv_id_for_logs}][aiagent_text.py] La IA solicitó {len(tool_calls)} llamada(s) a herramientas: {tool_calls}")
            
            # Añadimos el mensaje original de la IA (que contiene las tool_calls) al historial
            # messages_for_api.append(response_message) # Esto añade un objeto Pydantic, mejor el dict
            messages_for_api.append(response_message.model_dump())


            for tool_call in tool_calls:
                function_name = tool_call.function.name
                function_args_json = tool_call.function.arguments
                
                print(f"[{conv_id_for_logs}][aiagent_text.py] Ejecutando herramienta: {function_name} con args: {function_args_json}")
                
                tool_result_str = "" # Inicializar
                if function_name in tool_functions_map:
                    try:
                        function_to_call = tool_functions_map[function_name]
                        function_args_dict = json.loads(function_args_json)
                        
                        tool_result = function_to_call(**function_args_dict)
                        
                        if not isinstance(tool_result, str):
                            tool_result_str = json.dumps(tool_result)
                        else:
                            tool_result_str = tool_result

                        print(f"[{conv_id_for_logs}][aiagent_text.py] Resultado de {function_name}: {tool_result_str[:500]}...") # Loguear solo una parte si es muy largo
                        
                        messages_for_api.append({
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "name": function_name,
                            "content": tool_result_str,
                        })
                    except Exception as e_tool:
                        print(f"[{conv_id_for_logs}][aiagent_text.py] ERROR ejecutando la herramienta {function_name}: {str(e_tool)}")
                        import traceback
                        traceback.print_exc()
                        messages_for_api.append({
                            "tool_call_id": tool_call.id,
                            "role": "tool",
                            "name": function_name,
                            "content": json.dumps({"error": f"Error al ejecutar la herramienta {function_name}: {str(e_tool)}"}),
                        })
                else:
                    print(f"[{conv_id_for_logs}][aiagent_text.py] Error: Herramienta desconocida '{function_name}' solicitada por la IA.")
                    messages_for_api.append({
                        "tool_call_id": tool_call.id,
                        "role": "tool",
                        "name": function_name,
                        "content": json.dumps({"error": f"Herramienta '{function_name}' no encontrada/mapeada."}),
                    })

            print(f"[{conv_id_for_logs}][aiagent_text.py] 2ª Llamada a OpenAI con resultados de herramientas. Mensajes: {len(messages_for_api)}")
            
            second_chat_completion = client.chat.completions.create(
                model=MODEL_TO_USE,
                messages=messages_for_api 
            )
            print(f"[{conv_id_for_logs}][aiagent_text.py] 2ª Llamada a OpenAI completada.")
            
            ai_final_response_content = second_chat_completion.choices[0].message.content
            status_message = "success_with_tool_execution"

        else: # No tool_calls
            print(f"[{conv_id_for_logs}][aiagent_text.py] No se solicitaron herramientas. Respuesta directa de la IA.")
            ai_final_response_content = response_message.content
            status_message = "success_text_only"
            if not ai_final_response_content:
                 print(f"[{conv_id_for_logs}][aiagent_text.py] Respuesta directa de la IA fue vacía. Usando fallback.")
                 ai_final_response_content = "No he podido generar una respuesta en este momento. 🤔"

        print(f"[{conv_id_for_logs}][aiagent_text.py] Respuesta final para el usuario: '{ai_final_response_content}'")
        
        return {
            "reply_text": ai_final_response_content,
            "status": status_message 
        }

    except Exception as e_main_process:
        print(f"[{conv_id_for_logs}][aiagent_text.py] ERROR general en process_text_message: {str(e_main_process)}")
        import traceback
        traceback.print_exc() # Esto es crucial para ver el error exacto en los logs de Render
        return {
            "reply_text": "¡Caramba! 😅 Algo inesperado ocurrió al procesar tu mensaje. ¿Podrías intentarlo de nuevo?",
            "status": "error_processing_message"
        }