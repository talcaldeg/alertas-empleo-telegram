# -*- coding: utf-8 -*-
"""
Fuente "paginas de empleo directo de empresas".
La mayoria de las empresas no tiene backend propio: usan un ATS (sistema de
reclutamiento) con API publica JSON. Este modulo lee 4 ATS comunes y normaliza
todo a un formato unico. Solo libreria estandar.

ATS soportados:
- Greenhouse: https://boards-api.greenhouse.io/v1/boards/{token}/jobs
- Lever:      https://api.lever.co/v0/postings/{token}?mode=json
- Ashby:      https://api.ashbyhq.com/posting-api/job-board/{token}
- Teamtailor: API publica con la api-key del widget (por empresa)

Para AGREGAR una empresa: averigua su ATS y token (mira el codigo de su pagina de
empleos, o prueba los endpoints de arriba con distintos slugs) y agrega una linea
a fuentes.empresas.registro en perfil.json.
"""

import json
import time
import urllib.request
from html import unescape
import re

REQUEST_TIMEOUT = 15
SLEEP_BETWEEN = 0.4
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) alertas-empleo/1.0"

# El registro de empresas (nombre, ats, token) y las queries de Eightfold salen
# del perfil, no del codigo: ver perfil.json y el README.
from perfil import EMPRESAS_REGISTRO as EMPRESAS, EIGHTFOLD_QUERIES


def _get(url, headers=None):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return r.read().decode("utf-8", "ignore")


def _strip(s):
    return unescape(re.sub(r"<[^>]+>", " ", s or "")).strip()


def _norm(company, jid, title, location, url, remote, desc):
    return {
        "id": f"emp:{company.lower()}:{jid}",
        "title": title or "",
        "company": company,
        "location": location or "",
        "url": url or "",
        "remote": bool(remote),
        "desc": (desc or "")[:1500],
        "source": "Empresa",
    }


def _greenhouse(company, token):
    data = json.loads(_get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"))
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(_norm(company, j.get("id"), j.get("title"), loc,
                         j.get("absolute_url"), "remote" in loc.lower(), ""))
    return out


def _lever(company, token):
    data = json.loads(_get(f"https://api.lever.co/v0/postings/{token}?mode=json"))
    out = []
    for j in data:
        cat = j.get("categories") or {}
        loc = cat.get("location") or ", ".join(cat.get("allLocations", []) or [])
        wt = (j.get("workplaceType") or "").lower()
        out.append(_norm(company, j.get("id"), j.get("text"), loc,
                         j.get("hostedUrl"), wt == "remote" or "remote" in loc.lower(),
                         j.get("descriptionPlain")))
    return out


def _ashby(company, token):
    data = json.loads(_get(f"https://api.ashbyhq.com/posting-api/job-board/{token}"))
    out = []
    for j in data.get("jobs", []):
        out.append(_norm(company, j.get("id"), j.get("title"), j.get("location"),
                         j.get("jobUrl"), j.get("isRemote"), j.get("descriptionPlain")))
    return out


def _teamtailor(company, token):
    host, apikey = token.split("|", 1)
    hdr = {"Authorization": f"Token token={apikey}", "X-Api-Version": "20210218"}
    data = json.loads(_get(f"https://api.teamtailor.com/v1/jobs?page%5Bsize%5D=30", hdr))
    out = []
    for j in data.get("data", []):
        a = j.get("attributes", {})
        jid = j.get("id")
        rs = (a.get("remote-status") or "").lower()
        url = f"https://{host}/jobs/{jid}"
        out.append(_norm(company, jid, a.get("title"), "",
                         url, rs in ("fully", "hybrid"), _strip(a.get("body"))))
    return out


def _eightfold(company, token):
    import urllib.parse
    tenant, domain = token.split("|", 1)
    hdr = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CL,es;q=0.9",
        "Referer": f"https://{tenant}/careers",
        "Origin": f"https://{tenant}",
    }
    seen, out = set(), []
    for q in EIGHTFOLD_QUERIES:
        params = urllib.parse.urlencode({
            "domain": domain, "query": q, "location": "Chile",
            "start": 0, "num": 25, "sort_by": "relevance",
        })
        try:
            data = json.loads(_get(f"https://{tenant}/api/pcsx/search?{params}", hdr))
        except Exception:
            continue
        for p in data.get("data", {}).get("positions", []):
            pid = p.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            loc = (p.get("locations") or [""])[0]
            wlo = (p.get("workLocationOption") or "").lower()
            url = f"https://{tenant}{p.get('positionUrl', '')}"
            out.append(_norm(company, pid, p.get("name"), loc, url,
                             wlo == "remote" or "remote" in loc.lower(), p.get("department", "")))
        time.sleep(SLEEP_BETWEEN)
    return out


_READERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "teamtailor": _teamtailor,
    "eightfold": _eightfold,
}


def fetch_all(logger=print):
    out = []
    for nombre, ats, token in EMPRESAS:
        reader = _READERS.get(ats)
        if not reader:
            continue
        try:
            jobs = reader(nombre, token)
            out.extend(jobs)
        except Exception as e:
            logger(f"  aviso Empresa [{nombre}/{ats}]: {e}")
        time.sleep(SLEEP_BETWEEN)
    logger(f"Empresas: {len(out)} ofertas leidas (crudas, {len(EMPRESAS)} empresas).")
    return out
