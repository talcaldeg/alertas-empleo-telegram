# -*- coding: utf-8 -*-
"""
Fuente de part time y contratos por hora.
Lee dos bolsas internacionales que publican trabajo por hora, sin login. Solo
libreria estandar.

- Himalayas: API publica de busqueda, con filtro por pais (country=<ISO>) y tipo de
  empleo (part_time, contractor). Trae sueldo con su periodo (hourly, monthly, yearly).
  https://himalayas.app/jobs/api/search
- Arc.dev: la pagina de cada categoria trae los avisos en el JSON de Next.js
  (__NEXT_DATA__ -> pageProps.arcJobs), con tarifa por hora y paises exigidos.
  Solo se leen los avisos propios de Arc (arcJobs), no los agregados de otros sitios.

Cada aviso se normaliza a un dict con: id, title, company, location, url, desc,
part_time, salary_min, salary_max, salary_period ("hourly", "monthly" o None) y source.
El filtro de pago y de rol lo aplica alertas_empleos.score_contrato.

El pais, las consultas de Himalayas, las categorias de Arc y las listas de ruido salen
de fuentes.contratos en perfil.json, no del codigo.
"""

import json
import re
import time
import urllib.parse
import urllib.request

from perfil import USER_AGENT as UA
from perfil import (
    CONTRATOS_PAIS_ISO as PAIS_ISO, CONTRATOS_PAIS_NOMBRE as PAIS_NOMBRE,
    HIMALAYAS_QUERIES, HIMALAYAS_TIPOS, ARC_SLUGS, EMPRESAS_RUIDO, TITULOS_RUIDO,
)

REQUEST_TIMEOUT = 30
SLEEP_BETWEEN = 0.5

HIMALAYAS_URL = "https://himalayas.app/jobs/api/search"
ARC_URL = "https://arc.dev/remote-jobs/{slug}"


def es_ruido(company, title):
    """Reclutadores de entrenamiento de IA y anotacion: titulos de datos que no son BI."""
    c = (company or "").lower()
    t = (title or "").lower()
    return any(e in c for e in EMPRESAS_RUIDO) or any(k in t for k in TITULOS_RUIDO)


def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _periodo(p):
    p = (p or "").lower()
    if p.startswith("hour"):
        return "hourly"
    if p.startswith("month"):
        return "monthly"
    if p.startswith("year") or p.startswith("annual"):
        return "yearly"
    return None


def _himalayas(logger):
    out = {}
    for q in HIMALAYAS_QUERIES:
        for tipo in HIMALAYAS_TIPOS:
            params = {"q": q, "employment_type": tipo, "sort": "recent"}
            if PAIS_ISO:
                params["country"] = PAIS_ISO
            url = HIMALAYAS_URL + "?" + urllib.parse.urlencode(params)
            try:
                data = json.loads(_get(url))
            except Exception as e:
                logger(f"  aviso: Himalayas '{q}' {tipo} fallo: {e}")
                continue
            for j in data.get("jobs", []) or []:
                restr = j.get("locationRestrictions") or []
                if restr and PAIS_NOMBRE and PAIS_NOMBRE not in restr:
                    continue
                guid = j.get("guid") or j.get("applicationLink") or ""
                if not guid:
                    continue
                periodo = _periodo(j.get("salaryPeriod"))
                mn, mx = j.get("minSalary"), j.get("maxSalary")
                if periodo == "yearly":
                    mn = mn / 12 if isinstance(mn, (int, float)) else None
                    mx = mx / 12 if isinstance(mx, (int, float)) else None
                    periodo = "monthly"
                if (j.get("currency") or "USD").upper() != "USD":
                    mn = mx = periodo = None   # sin conversion de moneda: queda sin sueldo
                out["him:" + guid] = {
                    "id": "him:" + guid,
                    "title": j.get("title", "") or "",
                    "company": j.get("companyName", "") or "",
                    "location": ", ".join(restr) if restr else "Remoto, sin restriccion de pais",
                    "url": j.get("applicationLink") or guid,
                    "desc": j.get("excerpt", "") or "",
                    "part_time": (j.get("employmentType") or "").lower().startswith("part"),
                    "salary_min": mn,
                    "salary_max": mx,
                    "salary_period": periodo,
                    "source": "Himalayas",
                }
            time.sleep(SLEEP_BETWEEN)
    return list(out.values())


def _arc(logger):
    out = {}
    for slug in ARC_SLUGS:
        try:
            page = _get(ARC_URL.format(slug=slug))
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
            jobs = json.loads(m.group(1))["props"]["pageProps"].get("arcJobs") or []
        except Exception as e:
            logger(f"  aviso: Arc '{slug}' fallo: {e}")
            continue
        for j in jobs:
            if (j.get("jobType") or "").lower() != "contract":
                continue   # los "permanent" son full time y ya los cubren otras fuentes
            paises = j.get("requiredCountries") or []
            if isinstance(paises, str):
                try:
                    paises = json.loads(paises)
                except Exception:
                    paises = []
            if paises and PAIS_ISO and PAIS_ISO not in paises:
                continue
            key = j.get("randomKey") or ""
            if not key:
                continue
            title = j.get("title", "") or ""
            company = j.get("company") or {}
            out["arc:" + key] = {
                "id": "arc:" + key,
                "title": title,
                "company": company.get("name", "") if isinstance(company, dict) else "",
                "location": (f"{PAIS_NOMBRE or PAIS_ISO} incluido" if paises
                             else "Remoto, sin restriccion de pais"),
                "url": f"https://arc.dev/remote-jobs/details/{j.get('urlString', '')}-{key}",
                "desc": "",
                # "PT" en el titulo es la marca de Arc para part time (p. ej. "WW-PT")
                "part_time": bool(re.search(r"(?<![a-z])pt(?![a-z])", title.lower())),
                "salary_min": j.get("minHourlyRate"),
                "salary_max": j.get("maxHourlyRate"),
                "salary_period": "hourly" if (j.get("minHourlyRate") or j.get("maxHourlyRate")) else None,
                "source": "Arc",
            }
        time.sleep(SLEEP_BETWEEN)
    return list(out.values())


def fetch_all(logger=print):
    jobs = []
    for nombre, fn in (("Himalayas", _himalayas), ("Arc", _arc)):
        try:
            lote = fn(logger)
            logger(f"{nombre}: {len(lote)} avisos part time o por contrato.")
            jobs.extend(lote)
        except Exception as e:
            logger(f"  aviso: fuente {nombre} fallo por completo, se omite: {e}")
    return jobs
