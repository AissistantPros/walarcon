#consultarinfo.py
# -*- coding: utf-8 -*-
"""
Módulo para leer datos de Google Sheets.
Utilizado para obtener información como precios, políticas y otros datos del consultorio.
"""

from fastapi import APIRouter, HTTPException
import logging
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

@router.get("/consultorio-info")
async def get_consultorio_info():
    """
    Endpoint para obtener información del consultorio desde Google Sheets.
    """
    try:
        data = read_sheet_data()
        if not data:
            raise HTTPException(status_code=404, detail="No se encontraron datos en la hoja de cálculo.")
        return data
    except HTTPException as e:
        raise e
    except Exception as e:
        logger.error(f"❌ Error en el endpoint de información del consultorio: {str(e)}")
        raise HTTPException(status_code=500, detail="Error interno del servidor")

