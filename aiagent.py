# aiagent.py

# -*- coding: utf-8 -*-

import logging
import time
import json
from typing import List, Dict
from decouple import config
from openai import OpenAI
from consultarinfo import get_consultorio_data_from_cache
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from eliminarcita import delete_calendar_event
from editarcita import edit_calendar_event
from utils import search_calendar_event_by_phone, get_cancun_time
from datetime import datetime, timedelta
import pytz
from prompt import generate_openai_prompt
from prompts.prompt_crear_cita import prompt_crear_cita
from prompts.prompt_editar_cita import prompt_editar_cita
from prompts.prompt_eliminar_cita import prompt_eliminar_cita



logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

#########################################################
# 🔹 Funciones para interpretar expresiones de fecha
#########################################################

def get_next_monday(reference_date: datetime) -> datetime:
    ref = reference_date
    while ref.weekday() != 0:  # 0 = lunes
        ref += timedelta(days=1)
    if ref.date() == reference_date.date():
        ref += timedelta(days=7)
    return ref





#########################################################
# 🔹 Función para generar system_message de resumen de fechas
#########################################################

def generar_system_message_resumen_fecha(original_date_str, original_hour_str, result_date_str, result_hour_str):
    dias_semana = {
        0: "lunes", 1: "martes", 2: "miércoles", 3: "jueves",
        4: "viernes", 5: "sábado", 6: "domingo"
    }
    meses = {
        1: "enero", 2: "febrero", 3: "marzo", 4: "abril",
        5: "mayo", 6: "junio", 7: "julio", 8: "agosto",
        9: "septiembre", 10: "octubre", 11: "noviembre", 12: "diciembre"
    }
    try:
        original_dt = datetime.strptime(f"{original_date_str} {original_hour_str}", "%Y-%m-%d %H:%M")
        result_dt = datetime.strptime(f"{result_date_str} {result_hour_str}", "%Y-%m-%d %H:%M")

        dia_orig = dias_semana[original_dt.weekday()]
        dia_disp = dias_semana[result_dt.weekday()]
        mes_orig = meses[original_dt.month]
        mes_disp = meses[result_dt.month]

        fecha_inicio = f"{dia_orig} {original_dt.day} de {mes_orig} del {original_dt.year} a las {original_dt.strftime('%H:%M')}"
        fecha_resultado = f"{dia_disp} {result_dt.day} de {mes_disp} del {result_dt.year} a las {result_dt.strftime('%H:%M')}"
        return {
            "role": "system",
            "content": (
                f"Se comenzó buscando desde el {fecha_inicio}. "
                f"El sistema encontró espacio el {fecha_resultado}. Explica esto al usuario con claridad, sin inventar intenciones."
            )
        }
    except Exception as e:
        logger.warning(f"No se pudo generar system_message de fechas: {e}")
        return None

































#########################################################
# 🔹 DEFINICIÓN DE TOOLS
#########################################################
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_sheet_data",
            "description": "Obtener información general del consultorio (precios, horarios, ubicación)",
        }
    },
    {
        "type": "function",
        "function": {
            "name": "find_next_available_slot",
            "description": "Buscar próximo horario disponible para citas",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {"type": "string", "format": "date"},
                    "target_hour": {"type": "string"},
                    "urgent": {"type": "boolean"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear nueva cita médica",
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
            "name": "edit_calendar_event",
            "description": "Modificar cita existente",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "original_start_time": {"type": "string", "format": "date-time"},
                    "new_start_time": {"type": "string", "format": "date-time"},
                    "new_end_time": {"type": "string", "format": "date-time"}
                },
                "required": ["phone", "original_start_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_cancun_time",
            "description": "Devuelve la fecha y hora actual en Cancún (UTC -5). Úsala como referencia para interpretar expresiones como 'hoy', 'mañana', etc.",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Eliminar una cita médica",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "original_start_time": {"type": "string", "format": "date-time"},
                    "patient_name": {"type": "string"}
                },
                "required": ["phone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas por número de teléfono",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"}
                },
                "required": ["phone"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": "Termina la llamada con el usuario y cierra la sesión actual",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["user_request", "silence", "spam", "time_limit", "error"]
                    }
                },
                "required": ["reason"]
            }
        }
    }
]

























#########################################################
# 🔹 handle_tool_execution
#########################################################
def handle_tool_execution(tool_call) -> Dict:
    function_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)

    logger.info(f"🛠️ Ejecutando {function_name} con: {args}")

    try:
        if function_name == "read_sheet_data":
            return {"data": get_consultorio_data_from_cache()}

        elif function_name == "find_next_available_slot":
            # GPT-4o ya interpreta fecha claramente.
            target_date = args.get("target_date")
            target_hour = args.get("target_hour", "09:30")
            urgent = args.get("urgent", False)

            slot_info = find_next_available_slot(
                target_date=target_date,
                target_hour=target_hour,
                urgent=urgent
            )

            if "error" in slot_info:
                return {"slot": slot_info}

            if "start_time" in slot_info:
                start_iso = slot_info["start_time"][:19]
                start_dt = datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%S")
                tz = pytz.timezone("America/Cancun")
                start_dt = tz.localize(start_dt)

                dias_semana = {
                    "Monday": "lunes", "Tuesday": "martes", "Wednesday": "miércoles",
                    "Thursday": "jueves", "Friday": "viernes", "Saturday": "sábado", "Sunday": "domingo"
                }
                meses = {
                    "January": "enero", "February": "febrero", "March": "marzo", "April": "abril",
                    "May": "mayo", "June": "junio", "July": "julio", "August": "agosto",
                    "September": "septiembre", "October": "octubre", "November": "noviembre", "December": "diciembre"
                }

                fecha_formateada = f"{dias_semana[start_dt.strftime('%A')]} {start_dt.day} de {meses[start_dt.strftime('%B')]} del {start_dt.year} a las {start_dt.strftime('%H:%M')}"
                slot_info["formatted_description"] = f"Slot disponible: {fecha_formateada}"

            return {"slot": slot_info}

        elif function_name == "create_calendar_event":
            created_event = create_calendar_event(**args)
            return {"event_created": created_event}

        elif function_name == "edit_calendar_event":
            edited_event = edit_calendar_event(**args)
            return {"event_edited": edited_event}

        elif function_name == "delete_calendar_event":
            deleted_event = delete_calendar_event(**args)
            return {"event_deleted": deleted_event}

        elif function_name == "search_calendar_event_by_phone":
            search_results = search_calendar_event_by_phone(args["phone"])
            return {"search_results": search_results}

        elif function_name == "get_cancun_time":
            current_time = get_cancun_time()
            return {"cancun_time": current_time.isoformat()}

        elif function_name == "end_call":
            reason = args["reason"]
            return {"call_ended": reason}

    except Exception as e:
        logger.error(f"Error en ejecución de herramienta {function_name}: {e}")
        return {"error": str(e)}








































#########################################################
# 🔹 Generación de Respuestas
#########################################################
async def generate_openai_response(conversation_history: List[Dict], model="gpt-4o-mini") -> str:
    """
    Genera una respuesta del modelo GPT-4o o GPT-4o-mini (según 'model'),
    usando un prompt específico si detecta que el usuario quiere crear/editar/eliminar cita.
    Integra:
      - System prompt si no existe (con generate_openai_prompt)
      - Detección de idioma (lang_instruction)
      - Primer request con tool_choice auto (para usar las Tools)
      - Manejo de tool_calls y segundo request
      - Devolución final, o __END_CALL__ si la IA pide terminar llamada
    """

    try:
        # 1) Detectar intención para usar sub-prompt
        last_user_msg = conversation_history[-1]["content"].lower()

        # Sub-prompts
        if "crear cita" in last_user_msg:
            conversation = prompt_crear_cita(conversation_history)
        elif "editar cita" in last_user_msg or "modificar cita" in last_user_msg:
            conversation = prompt_editar_cita(conversation_history)
        elif "eliminar cita" in last_user_msg or "cancelar cita" in last_user_msg:
            conversation = prompt_eliminar_cita(conversation_history)
        else:
            # Prompt general por defecto (o reasignar tu 'conversation_history' directamente)
            conversation = conversation_history

        # 2) Insertamos system_prompt si no existe
        #    (Esto añade reglas base de "prompt.py" que tú tengas: 
        #     estilo, funciones permitidas, etc.)
        if not any(msg["role"] == "system" for msg in conversation):
            conversation = generate_openai_prompt(conversation)

        # 3) Ajuste de idioma (EN vs ES)
        last_user_msg_dict = next(
            (msg for msg in reversed(conversation) if msg["role"] == "user"),
            None
        )
        if last_user_msg_dict and "[EN]" in last_user_msg_dict.get("content", ""):
            lang_instruction = " Respond in English only. Keep responses under 50 words."
        else:
            lang_instruction = " Responde en español. Máximo 50 palabras."

        # Añadimos la instrucción de idioma al primer system_message
        if conversation and conversation[0]["role"] == "system":
            conversation[0]["content"] += lang_instruction

        # 4) PRIMER REQUEST a GPT con tool_choice="auto"
        first_response = client.chat.completions.create(
            model=model,
            messages=conversation,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=150,
            temperature=0.3,
            timeout=10
        )

        # 5) Revisar si GPT usó Tool Calls
        tool_calls = first_response.choices[0].message.tool_calls
        if not tool_calls:
            # Sin tools → devolvemos directamente
            return first_response.choices[0].message.content

        # 6) Procesar Tools → generamos messages "role=tool"
        tool_messages = []
        for tool_call in tool_calls:
            result = handle_tool_execution(tool_call)

            # Ajuste: si la tool pide terminar la llamada → devolvemos "__END_CALL__"
            if result.get("status") == "__END_CALL__":
                return "__END_CALL__"

            tool_messages.append({
                "role": "tool",
                "content": json.dumps(result),
                "tool_call_id": tool_call.id
            })

        # 7) Combinamos la historia (primer response + tool_messages)
        updated_messages = conversation + [first_response.choices[0].message] + tool_messages

        # 8) SEGUNDO REQUEST a GPT (sin tool_choice), para dar la respuesta final
        second_response = client.chat.completions.create(
            model=model,
            messages=updated_messages,
            max_tokens=150,
            temperature=0.3,
            timeout=10
        )

        # 9) Si GPT vuelve a pedir tool_calls en la segunda respuesta, ignoramos
        if second_response.choices[0].message.tool_calls:
            logger.warning("⚠️ Segunda respuesta incluyó tool_calls no deseadas. Ignorando.")

        return second_response.choices[0].message.content

    except Exception as e:
        logger.error(f"💣 Error crítico en generate_openai_response: {str(e)}")
        return "Disculpe, estoy teniendo dificultades técnicas. Por favor intente nuevamente."
