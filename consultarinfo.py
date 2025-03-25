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
@router.get("/consultorio-info")
async def get_consultorio_info():
    """
    Endpoint para obtener información del consultorio desde Google Sheets.
    Utiliza la caché para mejorar tiempos de respuesta.
    """
    try:
        data = get_consultorio_data_from_cache()
        if not data:
            raise HTTPException(status_code=404, detail="No se encontraron datos en la hoja de cálculo.")
        return data
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"❌ Error en el endpoint de información del consultorio: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")
