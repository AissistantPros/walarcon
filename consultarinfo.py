#consultarinfo.py
# -*- coding: utf-8 -*-
"""
Módulo para leer datos de Google Sheets.
Utilizado para obtener información como precios, políticas y otros datos del consultorio.
"""

from fastapi import APIRouter, HTTPException
import logging
from datetime import datetime
from utils import initialize_google_sheets

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter()

def read_sheet_data(sheet_range="Generales!A:B"):
    """
    Lee datos de Google Sheets en un rango específico.

    Parámetros:
        sheet_range (str): Rango a leer (por defecto: claves en columna A, valores en B).

    Retorna:
        dict: Diccionario con claves y valores obtenidos de Google Sheets.
    """
    try:
        service = initialize_google_sheets()
        sheet = service.spreadsheets()
        result = sheet.values().get(
            spreadsheetId=service.sheet_id,
            range=sheet_range
        ).execute()

        rows = result.get("values", [])
        if not rows:
            logger.warning("⚠️ La hoja de cálculo está vacía o no se encontraron datos en el rango especificado.")
            return {}

        data = {}
        for row in rows:
            if len(row) >= 2 and row[0] and row[1]:
                key = row[0].strip()
                value = row[1].strip()
                data[key] = value

        if not data:
            logger.warning("⚠️ No se encontraron valores válidos en la hoja de cálculo.")

        return data

    except Exception as e:
        logger.error(f"❌ Error inesperado al leer datos de Google Sheets: {str(e)}")
        raise HTTPException(status_code=500, detail="GOOGLE_SHEETS_UNAVAILABLE")

# =========================================
# CACHE PARA DATOS DEL CONSULTORIO
# =========================================
consultorio_data_cache = {}
consultorio_data_last_update = None

def load_consultorio_data_to_cache(sheet_range="Generales!A:B"):
    """
    Carga datos desde Google Sheets a la caché en memoria.
    """
    global consultorio_data_cache, consultorio_data_last_update
    try:
        logger.info("⏳ Cargando datos del consultorio desde Google Sheets...")
        data = read_sheet_data(sheet_range)
        consultorio_data_cache = data
        consultorio_data_last_update = datetime.now()
        logger.info("✅ Datos del consultorio cargados en caché.")
    except Exception as e:
        logger.error(f"❌ Error al cargar datos del consultorio: {str(e)}")

def clear_consultorio_data_cache():
    """
    Limpia la caché de datos del consultorio.
    """
    global consultorio_data_cache, consultorio_data_last_update
    consultorio_data_cache = {}
    consultorio_data_last_update = None
    logger.info("🗑️ Caché de datos del consultorio limpiada.")

def get_consultorio_data_from_cache(sheet_range="Generales!A:B"):
    """
    Devuelve los datos del consultorio desde la caché.
    Si la caché está vacía, se cargan los datos.
    """
    if not consultorio_data_cache:
        load_consultorio_data_to_cache(sheet_range)
    return consultorio_data_cache

# =========================================
# ENDPOINT PARA CONSULTAR INFORMACIÓN DEL CONSULTORIO
# =========================================
@router.get("/consultorio-info") # La URL puede ser esta o "/n8n/consultorio-info" si prefieres
async def n8n_get_consultorio_info(): # Cambié el nombre de la función para claridad
    """
    Endpoint para que n8n (u otros) obtengan información del consultorio.
    Utiliza la caché para mejorar tiempos de respuesta y devuelve un JSON específico.
    """
    logger.info("ℹ️ Solicitud para /consultorio-info")
    try:
        # Asegurarnos que la caché esté cargada al menos una vez al inicio o si está vacía
        # global consultorio_data_cache # Necesario si vas a reasignar, pero aquí solo leemos
        if not consultorio_data_cache: # consultorio_data_cache se define globalmente en este archivo
            logger.info("Caché de datos del consultorio vacía en endpoint, cargando ahora...")
            load_consultorio_data_to_cache()

        data = get_consultorio_data_from_cache()

        if not data:
            logger.warning("⚠️ No se encontraron datos en la hoja de cálculo para /consultorio-info.")
            # Devolvemos un JSON con error, adecuado para una API que consume n8n
            return {"error": "No se encontraron datos del consultorio."}

        # Devolvemos directamente el diccionario de datos envuelto como lo espera la IA/n8n
        return {"data_consultorio": data}
    except Exception as e:
        logger.error(f"❌ Error en endpoint /consultorio-info: {str(e)}")
        # Devolvemos un JSON con error
        return {"error": f"Error interno del servidor al obtener datos del consultorio: {str(e)}"}