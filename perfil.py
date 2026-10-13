# -*- coding: utf-8 -*-
"""
Carga del perfil de busqueda.

Todo lo que cambia entre una persona y otra -- el umbral de sueldo, las listas de
empresas, las palabras de rol, las busquedas de LinkedIn, el registro de ATS --
vive en perfil.json y no en el codigo. Asi el repo se puede publicar sin publicar
la busqueda de trabajo de nadie.

Orden de carga:
  1. perfil.json          (el suyo; esta en .gitignore)
  2. perfil.example.json  (fallback versionado, para que el repo corra recien clonado)

Los campos que necesitaban explicacion se escriben como {"_": "...", "lista": [...]}
en el JSON, porque JSON no tiene comentarios. `_lista()` desenvuelve las dos formas.
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PERFIL_PATH = BASE_DIR / "perfil.json"
EJEMPLO_PATH = BASE_DIR / "perfil.example.json"


# Identidad del bot en cada peticion HTTP. Se define una sola vez a proposito: si
# una fuente se identifica distinto que las otras, esa diferencia termina siendo una
# suplantacion de navegador que nadie decidio a conciencia. Verificado el 27-ago-2026:
# el endpoint guest de LinkedIn responde igual con este UA que con uno de Chrome
# (HTTP 200, mismas tarjetas), asi que ser honesto no cuesta nada.
USER_AGENT = ("alertas-empleo/1.0 "
              "(+https://github.com/talcaldeg/alertas-empleo-telegram)")


def _lista(v):
    """Acepta ["a","b"] o {"_": "doc", "lista": ["a","b"]}."""
    if isinstance(v, dict):
        return v.get("lista", [])
    return v or []


def _valor(v, defecto=None):
    """Acepta 2500 o {"_": "doc", "valor": 2500}."""
    if isinstance(v, dict):
        return v.get("valor", defecto)
    return defecto if v is None else v


def _cargar():
    ruta = PERFIL_PATH if PERFIL_PATH.exists() else EJEMPLO_PATH
    if not ruta.exists():
        raise SystemExit(
            "ERROR: falta perfil.json y perfil.example.json. Copie el ejemplo del repo."
        )
    with open(ruta, "r", encoding="utf-8") as f:
        return ruta, json.load(f)


ORIGEN, P = _cargar()
ES_EJEMPLO = ORIGEN.name == EJEMPLO_PATH.name

_pago = P["pago"]
_match = P["match"]
_fuentes = P["fuentes"]

# --- Regla de pago ------------------------------------------------------------
MIN_SALARY_USD_FULLTIME = _pago["min_salary_usd_fulltime"]
# Part time, freelance y contratos por hora tienen su propio piso. Un perfil antiguo
# sin este campo cae al de full time: es el lado conservador (llega menos, no mas).
MIN_SALARY_USD_PARTTIME = _valor(_pago.get("min_salary_usd_parttime"),
                                 MIN_SALARY_USD_FULLTIME)
# Horas al mes de un part time, para llevar una tarifa por hora a mensual.
HORAS_MES_PART_TIME = _valor(_pago.get("horas_semana_part_time"), 20) * 52 / 12
PART_TIME_TERMS = _lista(_pago["part_time_terms"])
ANALYST_TIER_TERMS = _lista(_pago["analyst_tier_terms"])
SENIOR_TITLE_TERMS = _lista(_pago["senior_title_terms"])
EMPRESAS_PAGAN_BIEN = {e.lower() for e in _lista(_pago["empresas_pagan_bien"])}
EMPRESAS_NO_CALIFICAN = {e.lower() for e in _lista(_pago["empresas_no_califican"])}

# --- Matching -----------------------------------------------------------------
CATEGORIES = _lista(_match["categorias_getonboard"])
ROLE_KEYWORDS = _lista(_match["role_keywords"])
STACK_KEYWORDS = _lista(_match["stack_keywords"])
NEGATIVE_TITLE = _lista(_match["negative_title"])
SENIORITY_BLOCK_IDS = set(_lista(_match["seniority_block_ids"]))
# Ubicacion: sin default a proposito. Un default seria volver a escribir el pais de
# alguien en el codigo, y un perfil antiguo cambiaria de filtro sin avisar.
if "ubicacion" not in _match:
    raise SystemExit(
        f"ERROR: a {ORIGEN.name} le falta match.ubicacion (pais_terms, ciudad_terms y "
        "remoto_terms). Reemplaza a location_ok_terms; copie el bloque desde "
        "perfil.example.json."
    )
_ubicacion = _match["ubicacion"]
UBICACION_PAIS_TERMS = [t.lower() for t in _lista(_ubicacion.get("pais_terms"))]
UBICACION_CIUDAD_TERMS = [t.lower() for t in _lista(_ubicacion.get("ciudad_terms"))]
UBICACION_REMOTO_TERMS = [t.lower() for t in _lista(_ubicacion.get("remoto_terms"))]
# Bonos de puntaje. Solo ordenan los avisos de una corrida; vacios = sin bono.
BONO_RUBRO_TERMS = [t.lower() for t in _lista(_match.get("bono_rubro_terms"))]
BONO_STACK_TERMS = [t.lower() for t in _lista(_match.get("bono_stack_terms"))]

# --- Fuentes ------------------------------------------------------------------
GOB = _fuentes.get("getonboard", {})
GOB_ACTIVA = GOB.get("activa", True)
PER_PAGE = GOB.get("per_page", 50)
MAX_PAGES = GOB.get("max_pages", 2)

LINKEDIN = _fuentes.get("linkedin", {})
LINKEDIN_ACTIVA = LINKEDIN.get("activa", True)
LINKEDIN_SEARCHES = [tuple(s) for s in _lista(LINKEDIN.get("searches", []))]
LINKEDIN_PAGES = LINKEDIN.get("pages_per_search", 2)
LINKEDIN_TIME_POSTED = LINKEDIN.get("time_posted", "r604800")
LINKEDIN_UBICACION_EXTRA_TERMS = [
    t.lower() for t in _lista(LINKEDIN.get("ubicacion_extra_terms"))]

EMPRESAS_CFG = _fuentes.get("empresas", {})
EMPRESAS_ACTIVA = EMPRESAS_CFG.get("activa", True)
EMPRESAS_REGISTRO = [tuple(e) for e in _lista(EMPRESAS_CFG.get("registro", []))]
EIGHTFOLD_QUERIES = _lista(EMPRESAS_CFG.get("eightfold_queries", []))
# Lugar que se le pasa a la busqueda de Eightfold. Vacio = sin filtro de lugar.
EIGHTFOLD_LOCATION = EMPRESAS_CFG.get("eightfold_location", "")

CONTRATOS = _fuentes.get("contratos", {})
CONTRATOS_ACTIVA = CONTRATOS.get("activa", False)
CONTRATOS_PAIS_ISO = CONTRATOS.get("pais_iso", "")
CONTRATOS_PAIS_NOMBRE = CONTRATOS.get("pais_nombre", "")
HIMALAYAS_QUERIES = _lista(CONTRATOS.get("himalayas_queries", []))
HIMALAYAS_TIPOS = _lista(CONTRATOS.get("himalayas_tipos", []))
ARC_SLUGS = _lista(CONTRATOS.get("arc_slugs", []))
EMPRESAS_RUIDO = [e.lower() for e in _lista(CONTRATOS.get("empresas_ruido", []))]
TITULOS_RUIDO = [t.lower() for t in _lista(CONTRATOS.get("titulos_ruido", []))]
