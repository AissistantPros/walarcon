# aiagent.py
# -*- coding: utf-8 -*-
"""
aiagent – motor de decisión para la asistente telefónica
────────────────────────────────────────────────────────
• Único modelo → gpt-4o-mini
• Flujos main / edit / delete con redirecciones internas
• Herramienta parse_relative_date para expresiones como “próximo martes”
• Métricas de latencia (🕒 ms) en todos los pases Chat-GPT
• Logging DEBUG uniforme (cambia LOG_LEVEL a INFO en producción)
"""

from __future__ import annotations

import json
import logging
from time import perf_counter
from typing import Dict, List

from decouple import config
from openai import OpenAI

# ────────────────────── CONFIG LOGGING ────────────────────────────
LOG_LEVEL = logging.DEBUG          # ⇢ INFO en prod.
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)5s | %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("aiagent")

# ──────────────────────── OPENAI CLIENT ───────────────────────────
client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

# ────────────────── IMPORTS DE TOOLS DE NEGOCIO ───────────────────
from utils import (
    parse_relative_date,
    search_calendar_event_by_phone,
)
from consultarinfo import get_consultorio_data_from_cache
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event

# prompt dinámico (system)
from prompt import generate_openai_prompt

# ══════════════════ HELPERS ═══════════════════════════════════════
def _t(start: float) -> str:
    """Devuelve el tiempo transcurrido desde *start* en ms formateado."""
    return f"{(perf_counter() - start) * 1_000:6.1f} ms"


# ══════════════════ TOOLS DEFINITIONS ═════════════════════════════
MAIN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "parse_relative_date",
            "description": "Convierte expresiones de fecha relativas en español a ISO YYYY-MM-DD",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {"name": "read_sheet_data", "description": "Obtener información del consultorio"},
    },
    {
        "type": "function",
        "function": {
            "name": "find_next_available_slot",
            "description": "Buscar horario disponible",
            "parameters": {
                "type": "object",
                "properties": {
                    "target_date": {"type": "string", "format": "date"},
                    "target_hour": {"type": "string"},
                    "urgent": {"type": "boolean"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": "Crear cita médica",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "phone": {"type": "string"},
                    "reason": {"type": "string"},
                    "start_time": {"type": "string", "format": "date-time"},
                    "end_time": {"type": "string", "format": "date-time"},
                },
                "required": ["name", "phone", "start_time", "end_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_intent",
            "description": "Detecta si el usuario quiere crear / editar / eliminar",
            "parameters": {
                "type": "object",
                "properties": {"intention": {"type": "string", "enum": ["create", "edit", "delete", "unknown"]}},
                "required": ["intention"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "end_call",
            "description": "Finaliza la llamada",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["user_request", "silence", "spam", "time_limit", "error"],
                    }
                },
                "required": ["reason"],
            },
        },
    },
]

EDIT_TOOLS = MAIN_TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas por teléfono",
            "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}, "required": ["phone"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_calendar_event",
            "description": "Modificar cita",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "original_start_time": {"type": "string", "format": "date-time"},
                    "new_start_time": {"type": "string", "format": "date-time"},
                    "new_end_time": {"type": "string", "format": "date-time"},
                },
                "required": ["phone", "original_start_time"],
            },
        },
    },
]

DELETE_TOOLS = MAIN_TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "search_calendar_event_by_phone",
            "description": "Buscar citas por teléfono",
            "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}, "required": ["phone"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_calendar_event",
            "description": "Eliminar cita",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {"type": "string"},
                    "original_start_time": {"type": "string", "format": "date-time"},
                },
                "required": ["phone", "original_start_time"],
            },
        },
    },
]

# ══════════════════ TOOL EXECUTOR ═════════════════════════════════
def handle_tool_execution(tc) -> Dict:
    fn = tc.function.name
    args = json.loads(tc.function.arguments or "{}")
    logger.debug("🛠️ Ejecutando %s %s", fn, args)

    try:
        if fn == "read_sheet_data":
            return {"data": get_consultorio_data_from_cache()}

        if fn == "parse_relative_date":
            try:
                return {"date": parse_relative_date(args["expression"])}
            except Exception as e:
                return {"error": str(e)}

        if fn == "find_next_available_slot":
            return {"slot": find_next_available_slot(**args)}

        if fn == "create_calendar_event":
            phone = args.get("phone", "")
            if not (phone.isdigit() and len(phone) == 10):
                return {"error": "Teléfono inválido"}
            return {"event_created": create_calendar_event(**args)}

        if fn == "edit_calendar_event":
            return {"event_edited": edit_calendar_event(**args)}

        if fn == "delete_calendar_event":
            return {"event_deleted": delete_calendar_event(**args)}

        if fn == "search_calendar_event_by_phone":
            return {"search_results": search_calendar_event_by_phone(**args)}

        if fn == "detect_intent":
            return {"intent_detected": args.get("intention")}

        if fn == "end_call":
            return {"call_ended": args["reason"]}

        return {"error": f"Función {fn} no reconocida"}

    except Exception as e:
        logger.exception("Error en tool %s", fn)
        return {"error": str(e)}


# ══════════════════ CORE – MAIN FLOW (AGENDAR) ═══════════════════
async def generate_openai_response_main(history: List[Dict], model: str = "gpt-4.1-mini") -> str:
    try:
        # Asegura prompt system
        conv = generate_openai_prompt(history) if not any(m.get("role") == "system" for m in history) else list(history)

        # Pase 1 ──────────────────────────────────────────────────────
        t1 = perf_counter()
        rsp1 = client.chat.completions.create(
            model=model,
            messages=conv,
            tools=MAIN_TOOLS,
            tool_choice="auto",
            max_tokens=200,
            temperature=0.3,
            timeout=10,
        ).choices[0].message
        logger.debug("🕒 OpenAI pase 1 %s", _t(t1))

        if not rsp1.tool_calls:
            return rsp1.content or "Disculpe, no entendí su última frase."

        # Ejecuta tools del pase 1
        tool_msgs = []
        for tc in rsp1.tool_calls:
            res = handle_tool_execution(tc)
            if "call_ended" in res:
                return "__END_CALL__"
            if "error" in res:
                return f"Lo siento, ocurrió un problema: {res['error']}"
            tool_msgs.append({"role": "tool", "content": json.dumps(res), "tool_call_id": tc.id})

        # Construye mensajes para pase 2
        updated = conv + [
            {"role": rsp1.role, "content": rsp1.content, "tool_calls": [t.model_dump() for t in rsp1.tool_calls]}
        ] + tool_msgs

        # Pase 2 ──────────────────────────────────────────────────────
        t2 = perf_counter()
        rsp2 = client.chat.completions.create(
            model=model,
            messages=updated,
            max_tokens=200,
            temperature=0.3,
            timeout=10,
        ).choices[0].message
        logger.debug("🕒 OpenAI pase 2 %s", _t(t2))

        # Revisa si la IA quiere desviar a edit / delete
        for tc in rsp2.tool_calls or []:
            if tc.function.name == "detect_intent":
                intention = json.loads(tc.function.arguments or "{}").get("intention")
                if intention == "edit":
                    return await generate_openai_response_edit(updated, model)
                if intention == "delete":
                    return await generate_openai_response_delete(updated, model)

        return rsp2.content

    except Exception:
        logger.exception("generate_openai_response_main falló")
        return "Lo siento, ocurrió un error técnico."

# ══════════════════ TWO-PASS FLOW GENÉRICO (EDIT / DELETE) ════════
async def _two_pass_flow(
    history: List[Dict],
    prompt_fn,
    tools,
    model: str,
    tag: str,
) -> str:
    try:
        # Pase 1
        t1 = perf_counter()
        conv = prompt_fn(history) if not any(m.get("role") == "system" for m in history) else list(history)
        rsp1 = client.chat.completions.create(
            model=model,
            messages=conv,
            tools=tools,
            tool_choice="auto",
            max_tokens=200,
            temperature=0.3,
            timeout=10,
        ).choices[0].message
        logger.debug("🕒 %s pase 1 %s", tag, _t(t1))

        # Tools del pase 1
        tool_msgs = []
        for tc in rsp1.tool_calls or []:
            res = handle_tool_execution(tc)
            if "call_ended" in res:
                return "__END_CALL__"
            if "error" in res:
                return f"Error en {tag.lower()}: {res['error']}"
            tool_msgs.append({"role": "tool", "content": json.dumps(res), "tool_call_id": tc.id})

        if not tool_msgs:
            return rsp1.content

        updated = conv + [
            {"role": rsp1.role, "content": rsp1.content, "tool_calls": [t.model_dump() for t in rsp1.tool_calls]}
        ] + tool_msgs

        # Pase 2
        t2 = perf_counter()
        rsp2 = client.chat.completions.create(
            model=model,
            messages=updated,
            max_tokens=200,
            temperature=0.3,
            timeout=10,
        ).choices[0].message
        logger.debug("🕒 %s pase 2 %s", tag, _t(t2))

        # Redirecciones cruzadas
        for tc in rsp2.tool_calls or []:
            if tc.function.name == "detect_intent":
                intention = json.loads(tc.function.arguments or "{}").get("intention")
                if intention == "create":
                    return await generate_openai_response_main(updated, model)
                if tag == "EDIT" and intention == "delete":
                    return await generate_openai_response_delete(updated, model)
                if tag == "DELETE" and intention == "edit":
                    return await generate_openai_response_edit(updated, model)

        return rsp2.content

    except Exception:
        logger.exception("%s flow falló", tag)
        return f"Lo siento, ocurrió un error al {tag.lower()} la cita."


# ══════════════════ PUBLIC HELPERS ════════════════════════════════
async def generate_openai_response_edit(history: List[Dict], model: str = "gpt-4.1-mini") -> str:
    from prompts.prompt_editar_cita import prompt_editar_cita
    return await _two_pass_flow(history, prompt_editar_cita, EDIT_TOOLS, model, "EDIT")


async def generate_openai_response_delete(history: List[Dict], model: str = "gpt-4.1-mini") -> str:
    from prompts.prompt_eliminar_cita import prompt_eliminar_cita
    return await _two_pass_flow(history, prompt_eliminar_cita, DELETE_TOOLS, model, "DELETE")


# alias corto
generate_openai_response = generate_openai_response_main
