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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

#########################################################
# 🔹 NUEVO: Funciones para interpretar expresiones de fecha
#########################################################


def get_next_monday(reference_date: datetime) -> datetime:
    """
    Retorna el lunes de la próxima semana a partir de 'reference_date'.
    Si hoy es lunes, se va al lunes de la semana siguiente.
    """
    ref = reference_date
    while ref.weekday() != 0:  # 0 = lunes
        ref += timedelta(days=1)
    # Si hoy es lunes, saltamos 7 días
    if ref.date() == reference_date.date():
        ref += timedelta(days=7)
    return ref

def interpret_date_expression(date_expr: str, now: datetime):
    """
    Convierte expresiones como 'lo antes posible', 'mañana', 
    'la próxima semana', 'de hoy en 8', 'el próximo mes', etc.,
    en una (target_date_str, urgent_bool).

    Retorna (target_date_str, urgent_bool).
    target_date_str es YYYY-MM-DD o None.
    urgent_bool es True o False.
    """
    date_expr_lower = date_expr.strip().lower()
    cancun = pytz.timezone("America/Cancun")

    # Valor por defecto
    date_str = None
    urgent = False

    if date_expr_lower in ["lo antes posible", "urgente", "hoy"]:
        # Significa urgent = True
        urgent = True

    elif date_expr_lower == "mañana":
        tomorrow = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        date_str = tomorrow

    elif date_expr_lower == "la próxima semana":
        next_mon = get_next_monday(now)
        date_str = next_mon.strftime("%Y-%m-%d")

    elif date_expr_lower in ["de hoy en 8", "hoy en 8"]:
        future = now + timedelta(days=7)
        date_str = future.strftime("%Y-%m-%d")

    elif date_expr_lower in ["de mañana en 8", "mañana en 8"]:
        future = now + timedelta(days=8)
        date_str = future.strftime("%Y-%m-%d")

    elif date_expr_lower in ["en 15 días", "15 dias", "en quince dias"]:
        future = now + timedelta(days=14)
        date_str = future.strftime("%Y-%m-%d")

    elif date_expr_lower == "el próximo mes":
        # Primer día del mes siguiente
        y = now.year
        m = now.month
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
        next_month_first = datetime(y, m, 1, tzinfo=cancun)
        date_str = next_month_first.strftime("%Y-%m-%d")

    else:
        # Si GPT pasa algo tipo '2025-03-31', interpretamos
        # si luce como YYYY-MM-DD:
        if len(date_expr_lower) == 10 and date_expr_lower[4] == '-' and date_expr_lower[7] == '-':
            date_str = date_expr_lower

    return date_str, urgent


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
                    "target_date": {"type": "string", "format": "date", "description": "Fecha objetivo en formato YYYY-MM-DD"},
                    "target_hour": {"type": "string", "description": "Hora objetivo en formato HH:MM"},
                    "urgent": {"type": "boolean", "description": "Si es una solicitud urgente"}
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
                    "name": {"type": "string", "description": "Nombre completo del paciente"},
                    "phone": {"type": "string", "description": "Número de teléfono con WhatsApp (10 dígitos)"},
                    "reason": {"type": "string", "description": "Motivo de la consulta"},
                    "start_time": {"type": "string", "format": "date-time", "description": "Fecha y hora de inicio en ISO8601"},
                    "end_time": {"type": "string", "format": "date-time", "description": "Fecha y hora de fin en ISO8601"}
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
                    "phone": {"type": "string", "description": "Número de teléfono registrado"},
                    "original_start_time": {"type": "string", "format": "date-time", "description": "Fecha/hora original de la cita"},
                    "new_start_time": {"type": "string", "format": "date-time", "description": "Nueva fecha/hora de inicio"},
                    "new_end_time": {"type": "string", "format": "date-time", "description": "Nueva fecha/hora de fin"}
                },
                "required": ["phone", "original_start_time"]
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
                    "phone": {"type": "string", "description": "Número de teléfono registrado"},
                    "patient_name": {"type": "string", "description": "Nombre del paciente (opcional para confirmación)"}
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
                    "phone": {"type": "string", "description": "Número de teléfono a buscar"}
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
                        "description": "Razón para finalizar la llamada",
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

            logger.info(f"🧠 IA → aiagent.py → find_next_available_slot: argumentos crudos recibidos: {args}")

            now = get_cancun_time()
            raw_date_expr = args.get("target_date", "").strip()
            real_date_str, real_urgent = interpret_date_expression(raw_date_expr, now)
            combined_urgent = args.get("urgent", False) or real_urgent

            if real_date_str:
                args["target_date"] = real_date_str
            else:
                args["target_date"] = None

            args["urgent"] = combined_urgent

            if args.get("target_hour") in [None, False, ""]:
                args["target_hour"] = "09:30"

            logger.info(f"📤 aiagent.py → buscarslot.py: llamando con target_date={args.get('target_date')}, target_hour={args.get('target_hour')}, urgent={args.get('urgent')}")

            slot_info = find_next_available_slot(
                target_date=args.get("target_date"),
                target_hour=args.get("target_hour"),
                urgent=args.get("urgent", False)
            )

            logger.info(f"📩 aiagent.py ← buscarslot.py: respuesta recibida: {slot_info}")

            # 🧠 Formateo final para la IA (para que diga la fecha con claridad)
            if "start_time" in slot_info:
                try:
                    start_iso = slot_info["start_time"][:19]  # Quitar zona horaria
                    start_dt = datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%S")
                    tz = pytz.timezone("America/Cancun")
                    start_dt = tz.localize(start_dt)

                    dias_semana = {
                        "Monday": "lunes",
                        "Tuesday": "martes",
                        "Wednesday": "miércoles",
                        "Thursday": "jueves",
                        "Friday": "viernes",
                        "Saturday": "sábado",
                        "Sunday": "domingo"
                    }
                    meses = {
                        "January": "enero",
                        "February": "febrero",
                        "March": "marzo",
                        "April": "abril",
                        "May": "mayo",
                        "June": "junio",
                        "July": "julio",
                        "August": "agosto",
                        "September": "septiembre",
                        "October": "octubre",
                        "November": "noviembre",
                        "December": "diciembre"
                    }

                    dia_ingles = start_dt.strftime("%A")
                    dia_semana = dias_semana.get(dia_ingles, dia_ingles).capitalize()

                    dia_num = start_dt.strftime("%d")
                    mes_ingles = start_dt.strftime("%B")
                    mes = meses.get(mes_ingles, mes_ingles)
                    anio = start_dt.strftime("%Y")
                    hora = start_dt.strftime("%I:%M %p").lstrip("0").lower().replace("am", "a.m.").replace("pm", "p.m.")

                    formatted_text = f"Slot disponible: {dia_semana} {dia_num} de {mes} del {anio} a las {hora}"
                    slot_info["formatted_description"] = formatted_text
                except Exception as e:
                    logger.warning(f"⚠️ No se pudo formatear la fecha para la IA: {e}")

            logger.info(f"📦 aiagent.py → sistema: enviando slot final a la IA: {slot_info}")
            return {"slot": slot_info}

        elif function_name == "create_calendar_event":
            start_time = args["start_time"]
            end_time = args["end_time"]

            if not start_time.endswith("-05:00"):
                start_time += "-05:00"
            if not end_time.endswith("-05:00"):
                end_time += "-05:00"

            logger.info(f"Zonas horarias ajustadas: start_time={start_time}, end_time={end_time}")

            return {"event": create_calendar_event(
                args["name"],
                args["phone"],
                args.get("reason", "Consulta general"),
                start_time,
                end_time
            )}

        elif function_name == "edit_calendar_event":
            return {"event": edit_calendar_event(
                args["phone"],
                args["original_start_time"],
                args.get("new_start_time"),
                args.get("new_end_time")
            )}

        elif function_name == "delete_calendar_event":
            return {"status": delete_calendar_event(
                args["phone"],
                args.get("patient_name")
            )}

        elif function_name == "search_calendar_event_by_phone":
            return {"events": search_calendar_event_by_phone(args["phone"])}

        elif function_name == "end_call":
            try:
                from tw_utils import CURRENT_CALL_MANAGER
                if CURRENT_CALL_MANAGER is not None:
                    import asyncio
                    asyncio.create_task(CURRENT_CALL_MANAGER._shutdown())
            except Exception as e:
                logger.error("❌ Error al intentar finalizar la llamada desde end_call", exc_info=True)

            return {"status": "__END_CALL__", "reason": args.get("reason", "user_request")}

        else:
            logger.error(f"❌ Función no reconocida: {function_name}")
            return {"error": "Función no implementada"}

    except Exception as e:
        logger.error(f"💥 Error en {function_name}: {str(e)}")
        return {"error": f"No se pudo ejecutar {function_name}"}



#########################################################
# 🔹 Generación de Respuestas
#########################################################
def generate_openai_response(conversation_history: List[Dict]) -> str:
    from prompt import generate_openai_prompt

    try:
        # Insertamos el system_prompt si no existe
        if not any(msg["role"] == "system" for msg in conversation_history):
            conversation_history = generate_openai_prompt(conversation_history)

        # --- Instrucción de idioma ---
        last_user_msg = next((msg for msg in reversed(conversation_history) if msg["role"] == "user"), None)
        if last_user_msg and "[EN]" in last_user_msg.get("content", ""):
            lang_instruction = " Respond in English only. Keep responses under 50 words."
        else:
            lang_instruction = " Responde en español. Máximo 50 palabras."
        if conversation_history and conversation_history[0]["role"] == "system":
            conversation_history[0]["content"] += lang_instruction
        # -------------------------------------



        # Primer request a GPT, con tool_choice auto
        first_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=conversation_history,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=150,
            temperature=0.3,
            timeout=10
        )

        tool_calls = first_response.choices[0].message.tool_calls
        if not tool_calls:
            # No llamó tools, devuelvo directamente
            return first_response.choices[0].message.content

        # Procesamos tools
        tool_messages = []
        for tool_call in tool_calls:
            result = handle_tool_execution(tool_call)
            tool_messages.append({
                "role": "tool",
                "content": json.dumps(result),
                "tool_call_id": tool_call.id
            })

            if result.get("status") == "__END_CALL__":
                return "__END_CALL__"

        # Combinamos la historia
        updated_messages = conversation_history + [first_response.choices[0].message] + tool_messages

        # Segunda request, sin tool_choice
        second_response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=updated_messages,
        )

        if second_response.choices[0].message.tool_calls:
            logger.warning("⚠️ Segunda respuesta incluyó tool_calls no deseadas. Ignorando.")

        return second_response.choices[0].message.content

    except Exception as e:
        logger.error(f"💣 Error crítico: {str(e)}")
        return "Disculpe, estoy teniendo dificultades técnicas. Por favor intente nuevamente."

