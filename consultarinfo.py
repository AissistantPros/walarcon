#consultarinfo.py
# -*- coding: utf-8 -*-
"""
Módulo para leer datos de Google Sheets.
Utilizado para obtener información como precios, políticas y otros datos del consultorio.
"""

import yaml
import os
from fastapi import APIRouter, HTTPException

router = APIRouter()

INFO_PATH = os.path.join(os.path.dirname(__file__), 'info_negocio.yaml')

# Cache simple en memoria para evitar leer el archivo en cada request
_info_cache = None

def load_info_from_yaml(force_reload=False):
    global _info_cache
    if _info_cache is None or force_reload:
        try:
            with open(INFO_PATH, 'r', encoding='utf-8') as f:
                _info_cache = yaml.safe_load(f)
        except Exception as e:
            raise RuntimeError(f"No se pudo cargar info_negocio.yaml: {e}")
    return _info_cache

@router.get('/consultorio')
def get_consultorio_info():
    """Endpoint para obtener toda la info del consultorio"""
    try:
        return load_info_from_yaml()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Si necesitas funciones internas para el agente:
def get_consultorio_data():
    return load_info_from_yaml()