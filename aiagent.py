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
# 🔹 Funciones para interpretar expresiones de fecha
#########################################################

def get_next_monday(reference_date: datetime) -> datetime:
    ref = reference_date
    while ref.weekday() != 0:  # 0 = lunes
        ref += timedelta(days=1)
    if ref.date() == reference_date.date():
        ref += timedelta(days=7)
    return ref

def interpret_date_expression(date_expr: str, now: datetime):
    """
    Maneja expresiones como:
    - 'lo antes posible', 'mañana', 'la próxima semana'
    - 'el martes por la mañana', 'próximo viernes por la tarde'
    - '2025-04-10', etc.

    Retorna (target_date_str, target_hour_str, urgent_bool)
    """
    cancun = pytz.timezone("America/Cancun")
    date_expr_lower = date_expr.strip().lower()

    # Valores por defecto
    target_date = None
    target_hour = "09:30"
    urgent = False

    # Urgencia
    if any(word in date_expr_lower for word in ["urgente", "lo antes posible", "hoy"]):
        urgent = True

    # Hora del día
    if "tarde" in date_expr_lower:
        target_hour = "12:30"
    elif "mañana" in date_expr_lower:
        target_hour = "09:30"
    else:
        target_hour = "09:30"  # default siempre 9:30 si no dice nada

    # Fechas relativas
    if "mañana" in date_expr_lower:
        target_date = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    elif "de hoy en 8" in date_expr_lower or "hoy en 8" in date_expr_lower:
        target_date = (now + timedelta(days=7)).strftime("%Y-%m-%d")
    elif "de mañana en 8" in date_expr_lower or "mañana en 8" in date_expr_lower:
        target_date = (now + timedelta(days=8)).strftime("%Y-%m-%d")
    elif any(x in date_expr_lower for x in ["en 15 días", "en quince días", "15 dias"]):
        target_date = (now + timedelta(days=14)).strftime("%Y-%m-%d")
    elif "el próximo mes" in date_expr_lower:
        y, m = now.year, now.month
        m = m + 1 if m < 12 else 1
        y = y if m > 1 else y + 1
        target_date = datetime(y, m, 1, tzinfo=cancun).strftime("%Y-%m-%d")
    elif "la próxima semana" in date_expr_lower:
        next_monday = now + timedelta(days=(7 - now.weekday()) % 7 or 7)
        target_date = next_monday.strftime("%Y-%m-%d")

    # Días específicos
    days = {
        "lunes": 0, "martes": 1, "miércoles": 2, "miercoles": 2,
        "jueves": 3, "viernes": 4, "sábado": 5, "sabado": 5
    }
    for day_name, weekday in days.items():
        if f"el próximo {day_name}" in date_expr_lower or f"la próxima semana, {day_name}" in date_expr_lower:
            delta_days = (weekday - now.weekday() + 7) % 7 or 7
            target_date = (now + timedelta(days=delta_days)).strftime("%Y-%m-%d")
            break
        elif f"el {day_name}" in date_expr_lower:
            delta_days = (weekday - now.weekday() + 7) % 7 or 7
            target_date = (now + timedelta(days=delta_days)).strftime("%Y-%m-%d")
            break

    # Fecha directa YYYY-MM-DD
    if len(date_expr_lower) == 10 and date_expr_lower[4] == "-" and date_expr_lower[7] == "-":
        target_date = date_expr_lower

    return target_date, target_hour, urgent

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
                f"Se comenzó buscando desde el {fecha_inicio}, pero no había disponibilidad. "
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
            logger.info(f"🧠 IA → aiagent.py → find_next_available_slot: argumentos crudos recibidos: {args}")

            now = get_cancun_time()
            raw_date_expr = args.get("target_date", "").strip()

            # 1) Interpretar fecha/hora
            real_date_str, real_hour_str, real_urgent = interpret_date_expression(raw_date_expr, now)
            combined_urgent = args.get("urgent", False) or real_urgent
            final_hour = args.get("target_hour") or real_hour_str or "09:30"

            args["target_date"] = real_date_str
            args["target_hour"] = final_hour
            args["urgent"] = combined_urgent

            logger.info(f"📤 aiagent.py → buscarslot.py: llamando con target_date={args['target_date']}, "
                        f"target_hour={args['target_hour']}, urgent={args['urgent']}")

            # 2) Llamar al backend de slots
            slot_info = find_next_available_slot(
                target_date=args["target_date"],
                target_hour=args["target_hour"],
                urgent=args["urgent"]
            )

            logger.info(f"📩 aiagent.py ← buscarslot.py: respuesta recibida: {slot_info}")

            # 2.1 Manejar errores “NO_MORNING_AVAILABLE” o “NO_TARDE_AVAILABLE”
            if slot_info.get("error") == "NO_MORNING_AVAILABLE":
                return {
                    "slot": {
                        "error": "NO_MORNING_AVAILABLE",
                        "date": slot_info["date"],
                        "message": (
                            "Mmm, no tengo horarios por la mañana ese día. "
                            "¿Desea que busque en la tarde o en otro día por la mañana?"
                        )
                    }
                }

            if slot_info.get("error") == "NO_TARDE_AVAILABLE":
                return {
                    "slot": {
                        "error": "NO_TARDE_AVAILABLE",
                        "date": slot_info["date"],
                        "message": (
                            "Mmm, no tengo horarios por la tarde ese día. "
                            "¿Le gustaría que busque otro día por la tarde o puedo revisar la mañana de ese mismo día?"
                        )
                    }
                }

            # 2.2 Manejar otros errores normales
            if "error" in slot_info:
                return {"slot": slot_info}

            # 3) Formatear si ya hay “start_time”
            if "start_time" in slot_info:
                try:
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

                    dia_semana = dias_semana.get(start_dt.strftime("%A"), "")
                    mes = meses.get(start_dt.strftime("%B"), "")
                    dia_num = start_dt.strftime("%d")
                    anio = start_dt.strftime("%Y")
                    hora = start_dt.strftime("%I:%M %p").lstrip("0").lower().replace("am", "a.m.").replace("pm", "p.m.")

                    formatted_text = (
                        f"Slot disponible: {dia_semana.capitalize()} {dia_num} "
                        f"de {mes} del {anio} a las {hora}"
                    )
                    slot_info["formatted_description"] = formatted_text

                    # Comparar si la fecha/hora encontrada es distinta a la solicitada originalmente
                    if real_date_str and final_hour:
                        # Extraer la fecha y hora del slot encontrado
                        result_date_str = datetime.strptime(slot_info["start_time"][:10], "%Y-%m-%d").strftime("%Y-%m-%d")
                        result_hour_str = datetime.strptime(slot_info["start_time"][11:16], "%H:%M").strftime("%H:%M")
                        if result_date_str != real_date_str or result_hour_str != final_hour:
                            resumen_msg = generar_system_message_resumen_fecha(
                                original_date_str=real_date_str,
                                original_hour_str=final_hour,
                                result_date_str=result_date_str,
                                result_hour_str=result_hour_str
                            )
                            if resumen_msg:
                                slot_info["system_context_message"] = resumen_msg
                except Exception as e:
                    logger.warning(f"⚠️ No se pudo formatear la fecha para la IA: {e}")

            logger.info(f"📦 aiagent.py → sistema: enviando slot final a la IA: {slot_info}")
            return {"slot": slot_info}


        elif function_name == "get_cancun_time":
            from utils import get_cancun_time
            now = get_cancun_time()
            return {
                "datetime": now.isoformat(),
                "formatted": now.strftime("Hoy es %A %d de %B del %Y, son las %I:%M %p en Cancún.")
            }


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
        last_user_msg = next(
            (msg for msg in reversed(conversation_history) if msg["role"] == "user"),
            None
        )
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
