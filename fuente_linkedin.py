# -*- coding: utf-8 -*-
"""
Fuente LinkedIn para las alertas de empleo.
Usa el endpoint publico "guest" de busqueda de empleos de LinkedIn (sin login),
el mismo que sirve la web cuando no has iniciado sesion. Solo libreria estandar.

Limitaciones conocidas:
- LinkedIn no expone salario ni seniority en la tarjeta guest -> el filtro se basa
  en el titulo (mismas listas de palabras que GetOnBoard).
- Es un endpoint no documentado: puede cambiar o responder 429 si se consulta muy
  seguido. El codigo degrada con elegancia (si falla, la fuente GetOnBoard sigue).
"""

import html as htmllib
import re
import time
import urllib.parse
import urllib.request

GUEST_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

# Busquedas, paginas y ventana de tiempo salen del perfil (perfil.json).
# f_TPR limita a lo publicado en la ultima semana para mantener las alertas frescas.
from perfil import (
    LINKEDIN_SEARCHES as SEARCHES,
    LINKEDIN_PAGES as PAGES_PER_SEARCH,
    LINKEDIN_TIME_POSTED as TIME_POSTED,
)

REQUEST_TIMEOUT = 25
SLEEP_BETWEEN = 1.5            # respetar el endpoint / evitar 429

from perfil import USER_AGENT as UA


def _strip_tags(s):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _fetch_page(keywords, location, start):
    params = urllib.parse.urlencode({
        "keywords": keywords,
        "location": location,
        "f_TPR": TIME_POSTED,
        "start": start,
    })
    url = f"{GUEST_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "es-CL,es;q=0.9"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as r:
        return r.read().decode("utf-8", "ignore")


def _parse_cards(page_html):
    jobs = []
    cards = re.split(r"<li>", page_html)
    for card in cards:
        m_id = re.search(r'urn:li:jobPosting:(\d+)', card)
        m_title = re.search(r'base-search-card__title">\s*(.*?)\s*</h3>', card, re.S)
        if not m_id or not m_title:
            continue
        m_url = re.search(r'base-card__full-link[^>]*href="([^"]+)"', card)
        m_comp = re.search(r'base-search-card__subtitle">(.*?)</h4>', card, re.S)
        m_loc = re.search(r'job-search-card__location">\s*(.*?)\s*</span>', card, re.S)
        m_date = re.search(r'datetime="([^"]+)"', card)

        url = m_url.group(1) if m_url else ""
        url = htmllib.unescape(url).split("?")[0]  # limpiar tracking params

        jobs.append({
            "id": "li:" + m_id.group(1),
            "title": _strip_tags(m_title.group(1)),
            "company": _strip_tags(m_comp.group(1)) if m_comp else "",
            "location": _strip_tags(m_loc.group(1)) if m_loc else "",
            "url": url,
            "date": m_date.group(1) if m_date else "",
            "source": "LinkedIn",
        })
    return jobs


def fetch_all(logger=print):
    seen_ids = set()
    out = []
    for keywords, location in SEARCHES:
        for page in range(PAGES_PER_SEARCH):
            try:
                html_page = _fetch_page(keywords, location, page * 10)
            except Exception as e:
                logger(f"  aviso LinkedIn ['{keywords}' / {location} pag {page}]: {e}")
                break
            cards = _parse_cards(html_page)
            if not cards:
                break
            for j in cards:
                if j["id"] not in seen_ids:
                    seen_ids.add(j["id"])
                    out.append(j)
            time.sleep(SLEEP_BETWEEN)
    logger(f"LinkedIn: {len(out)} ofertas leidas (crudas, antes de filtrar).")
    return out
