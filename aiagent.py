# aiagent.py
# -*- coding: utf-8 -*-
"""
aiagent – motor de decisión para la asistente de voz.
────────────────────────────────────────────────────
• Único modelo: gpt‑4o‑mini.
• Flujos main / edit / delete.
• Métrica de latencia en cada paso (🕒 ms).
• Logs exhaustivos nivel DEBUG (cámbialo en LOG_LEVEL).
"""

from __future__ import annotations

import json, logging, time
from typing import Dict, List
from time import perf_counter

from decouple import config
from openai import OpenAI

# ── CONFIG -----------------------------------------------------------------
LOG_LEVEL = logging.DEBUG  # cambia a INFO en producción
logging.basicConfig(level=LOG_LEVEL,
                    format="%(asctime)s | %(levelname)5s | %(name)s: %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger("aiagent")

client = OpenAI(api_key=config("CHATGPT_SECRET_KEY"))

# ── IMPORTS DE TOOLS --------------------------------------------------------
from consultarinfo import get_consultorio_data_from_cache
from buscarslot import find_next_available_slot
from crearcita import create_calendar_event
from editarcita import edit_calendar_event
from eliminarcita import delete_calendar_event
from utils import search_calendar_event_by_phone

# prompts dinámicos
from prompt import generate_openai_prompt

# ── HELPERS -----------------------------------------------------------------

def _t(start: float) -> str:
    """Devuelve tiempo transcurrido en ms para logs."""
    return f"{(perf_counter()-start)*1000:6.1f} ms"

# ── DECLARACIÓN DE TOOLS ----------------------------------------------------
MAIN_TOOLS = [
    {"type": "function", "function": {"name": "read_sheet_data", "description": "Obtener información del consultorio"}},
    {"type": "function", "function": {
        "name": "find_next_available_slot", "description": "Buscar horario disponible",
        "parameters": {"type": "object", "properties": {
            "target_date": {"type": "string", "format": "date"},
            "target_hour": {"type": "string"},
            "urgent": {"type": "boolean"}}, "required": []}}},
    {"type": "function", "function": {"name": "create_calendar_event", "description": "Crear cita médica",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"},
            "phone": {"type": "string"},
            "reason": {"type": "string"},
            "start_time": {"type": "string", "format": "date-time"},
            "end_time": {"type": "string", "format": "date-time"}},
            "required": ["name", "phone", "start_time", "end_time"]}}},
    {"type": "function", "function": {"name": "detect_intent", "description": "Detectar intención",
        "parameters": {"type": "object", "properties": {
            "intention": {"type": "string", "enum": ["create", "edit", "delete", "unknown"]}},
            "required": ["intention"]}}},
    {"type": "function", "function": {"name": "end_call", "description": "Finalizar llamada",
        "parameters": {"type": "object", "properties": {
            "reason": {"type": "string", "enum": ["user_request", "silence", "spam", "time_limit", "error"]}},
            "required": ["reason"]}}},
]

# Las herramientas de EDIT y DELETE reutilizan las anteriores añadiendo las suyas
EDIT_TOOLS   = MAIN_TOOLS + [
    {"type": "function", "function": {"name": "search_calendar_event_by_phone", "description": "Buscar citas",
        "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}, "required": ["phone"]}}},
    {"type": "function", "function": {"name": "edit_calendar_event", "description": "Modificar cita",
        "parameters": {"type": "object", "properties": {
            "phone": {"type": "string"},
            "original_start_time": {"type": "string", "format": "date-time"},
            "new_start_time": {"type": "string", "format": "date-time"},
            "new_end_time": {"type": "string", "format": "date-time"}},
            "required": ["phone", "original_start_time"]}}},
]

DELETE_TOOLS = MAIN_TOOLS + [
    {"type": "function", "function": {"name": "search_calendar_event_by_phone", "description": "Buscar citas",
        "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}, "required": ["phone"]}}},
    {"type": "function", "function": {"name": "delete_calendar_event", "description": "Eliminar cita",
        "parameters": {"type": "object", "properties": {
            "phone": {"type": "string"},
            "original_start_time": {"type": "string", "format": "date-time"}},
            "required": ["phone", "original_start_time"]}}},
]

# ── DISPATCHER DE TOOLS -----------------------------------------------------

def handle_tool_execution(tc) -> Dict:
    fn, args = tc.function.name, json.loads(tc.function.arguments or "{}")
    logger.debug("🛠️ Ejecutando %s %s", fn, args)
    try:
        if fn == "read_sheet_data":
            return {"data": get_consultorio_data_from_cache()}
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

# ── CORE FLOW ---------------------------------------------------------------
async def generate_openai_response_main(history: List[Dict], model: str = "gpt-4.1-mini") -> str:
    """Primer flujo (agendar) – llamado también en cascada desde edit/delete."""
    try:
        start = perf_counter()
        # Ensure system prompt
        conversation = generate_openai_prompt(history) if not any(m.get("role") == "system" for m in history) else list(history)

        # Pase 1 ------------------------------------------------------------
        rsp1 = client.chat.completions.create(
            model=model,
            messages=conversation,
            tools=MAIN_TOOLS,
            tool_choice="auto",
            max_tokens=200,
            temperature=0.3,
            timeout=10,
        ).choices[0].message

        logger.debug("🕒 OpenAI pase 1 %s", _t(start))
        tool_calls = rsp1.tool_calls or []
        if not tool_calls:
            return rsp1.content or "Disculpe, no entendí su última frase."

        # Ejecutar tools ----------------------------------------------------
        tool_msgs = []
        for tc in tool_calls:
            res = handle_tool_execution(tc)
            if "call_ended" in res:
                return "__END_CALL__"
            if "error" in res:
                return f"Lo siento, ocurrió un problema: {res['error']}"
            tool_msgs.append({"role": "tool", "content": json.dumps(res), "tool_call_id": tc.id})

        updated = conversation + [{"role": rsp1.role, "content": rsp1.content, "tool_calls": [t.model_dump() for t in tool_calls]}] + tool_msgs

        # Pase 2 ------------------------------------------------------------
        p2_start = perf_counter()
        rsp2 = client.chat.completions.create(
            model=model,
            messages=updated,
            max_tokens=200,
            temperature=0.3,
            timeout=10,
        ).choices[0].message
        logger.debug("🕒 OpenAI pase 2 %s", _t(p2_start))

        # 6) Desvío a edit/delete si la IA lo indica ------------------------
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
        return "Lo siento, ocurrió un error técnico en el prompt principal."

# ── EDIT --------------------------------------------------------------------

async def generate_openai_response_edit(history: List[Dict], model: str = "gpt-4.1-mini") -> str:
    """Flujo para modificar citas."""
    from prompts.prompt_editar_cita import prompt_editar_cita  # lazy‑import
    return await _two_pass_flow(history, prompt_editar_cita, EDIT_TOOLS, model, "EDIT")

# ── DELETE ------------------------------------------------------------------

async def generate_openai_response_delete(history: List[Dict], model: str = "gpt-4.1-mini") -> str:
    """Flujo para cancelar citas."""
    from prompts.prompt_eliminar_cita import prompt_eliminar_cita  # lazy‑import
    return await _two_pass_flow(history, prompt_eliminar_cita, DELETE_TOOLS, model, "DELETE")

# ── REFACTORED COMMON TWO‑PASS FLOW ----------------------------------------

async def _two_pass_flow(history: List[Dict], prompt_fn, tools, model, tag: str) -> str:
    try:
        start = perf_counter()
        conversation = prompt_fn(history) if not any(m.get("role") == "system" for m in history) else list(history)
        rsp1 = client.chat.completions.create(model=model, messages=conversation, tools=tools, tool_choice="auto",
                                              max_tokens=200, temperature=0.3, timeout=10).choices[0].message
        logger.debug("🕒 %s pase 1 %s", tag, _t(start))
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
        updated = conversation + [{"role": rsp1.role, "content": rsp1.content,
                                    "tool_calls": [t.model_dump() for t in rsp1.tool_calls]}] + tool_msgs
        rsp2 = client.chat.completions.create(model=model, messages=updated, max_tokens=200, temperature=0.3,
                                              timeout=10).choices[0].message
        logger.debug("🕒 %s pase 2 %s", tag, _t(start))
        # redirecciones cruzadas
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

# alias práctico -------------------------------------------------------------
generate_openai_response = generate_openai_response_main
