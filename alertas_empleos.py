# -*- coding: utf-8 -*-
"""
Alertas de empleo -> Telegram, filtradas al perfil definido en perfil.json.
Fuente: API publica de GetOnBoard (getonbrd.com). Solo libreria estandar.

Uso:
    python alertas_empleos.py           # busca y envia solo ofertas NUEVAS
    python alertas_empleos.py --seed    # marca lo actual como visto SIN enviar (primera vez)
    python alertas_empleos.py --test     # envia un mensaje de prueba a Telegram
    python alertas_empleos.py --resumen  # muestra en consola lo que enviaria, sin marcar visto
    python alertas_empleos.py --autotest # verifica la regla de pago (sin red); falla con exit 1
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import fuente_linkedin
    LINKEDIN_ENABLED = True
except Exception:
    LINKEDIN_ENABLED = False

try:
    import fuente_empresas
    EMPRESAS_ENABLED = True
except Exception:
    EMPRESAS_ENABLED = False

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
SEEN_PATH = BASE_DIR / "seen_jobs.json"
LOG_PATH = BASE_DIR / "alertas.log"

API_BASE = "https://www.getonbrd.com/api/v0"

# ------------------------------------------------------------------------------
# Perfil de busqueda. Nada de esto se hardcodea: vive en perfil.json (o, si no
# existe, en perfil.example.json). Ver perfil.py y el README.
# ------------------------------------------------------------------------------
import perfil
from perfil import (                                            # noqa: E402
    CATEGORIES, ROLE_KEYWORDS, STACK_KEYWORDS, NEGATIVE_TITLE,
    SENIORITY_BLOCK_IDS, LOCATION_OK_TERMS,
    MIN_SALARY_USD_FULLTIME, PART_TIME_PASA_SIEMPRE, PART_TIME_TERMS,
    ANALYST_TIER_TERMS, SENIOR_TITLE_TERMS,
    EMPRESAS_PAGAN_BIEN, EMPRESAS_NO_CALIFICAN,
    GOB_ACTIVA, PER_PAGE, MAX_PAGES,
    ORIGEN as PERFIL_ORIGEN, ES_EJEMPLO as PERFIL_ES_EJEMPLO,
)

# Se comparan por PALABRA COMPLETA (no substring) para evitar falsos positivos
# como "ios" dentro de "Envios" o "ui" dentro de "Continuous".
_NEG_RE = re.compile(
    r"(?<![\wáéíóúñ])(?:" +
    "|".join(re.escape(n.strip()) for n in NEGATIVE_TITLE) +
    r")(?![\wáéíóúñ])", re.IGNORECASE)


def tiene_negativo(titulo):
    return bool(_NEG_RE.search(titulo or ""))


# Mapa de IDs de seniority de GetOnBoard (los bloqueados salen del perfil)
SENIORITY_MAP = {
    1: "sin experiencia",
    2: "junior",
    3: "semi senior",
    4: "senior",
    5: "expert / lead",
}

# IDs de modalidad de GetOnBoard (ver /api/v0/modalities)
MODALITY_FULL_TIME = 1
MODALITY_PART_TIME = 2
MODALITY_FREELANCE = 3
MODALITY_INTERNSHIP = 4

REQUEST_TIMEOUT = 25


# ------------------------------------------------------------------------------

def log(msg):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_config():
    # 1) Variables de entorno (las usa GitHub Actions via Secrets). Tienen prioridad.
    env_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    env_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if env_token and env_chat:
        return {"telegram_bot_token": env_token, "telegram_chat_id": env_chat}
    # 2) config.json local (para correr en el PC).
    if not CONFIG_PATH.exists():
        log("ERROR: falta config.json o las variables TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID.")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    if "PON_AQUI" in cfg.get("telegram_bot_token", "") or not cfg.get("telegram_chat_id"):
        log("ERROR: config.json sin completar (token / chat_id). Revisa el README.")
        sys.exit(1)
    return cfg


def load_seen():
    if SEEN_PATH.exists():
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, ensure_ascii=False, indent=0)


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "alertas-empleo/1.0"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_category(category):
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        params = urllib.parse.urlencode({
            "per_page": PER_PAGE,
            "page": page,
            "expand": '["company"]',
        })
        url = f"{API_BASE}/categories/{category}/jobs?{params}"
        try:
            payload = http_get_json(url)
        except Exception as e:
            log(f"  aviso: fallo al leer {category} pag {page}: {e}")
            break
        batch = payload.get("data", [])
        jobs.extend(batch)
        meta = payload.get("meta", {})
        if page >= meta.get("total_pages", 1):
            break
        time.sleep(0.4)
    return jobs


def text_of(job):
    # tags y otros vienen como IDs numericos; usamos los campos de texto ricos
    a = job.get("attributes", {})
    parts = [
        a.get("title", ""),
        a.get("description_headline", "") or "",
        a.get("description", "") or "",
        a.get("functions", "") or "",
        a.get("desirable", "") or "",
        a.get("projects", "") or "",
        a.get("category_name", "") or "",
    ]
    return " ".join(p for p in parts if p).lower()


def seniority_id(job):
    s = job.get("attributes", {}).get("seniority")
    if isinstance(s, dict):
        v = s.get("data", {}).get("id")
        if isinstance(v, int):
            return v
    return None


def seniority_label(job):
    sid = seniority_id(job)
    return SENIORITY_MAP.get(sid, "n/d")


def location_ok(job, blob):
    a = job.get("attributes", {})
    if a.get("remote"):
        return True
    for country in a.get("countries", []) or []:
        c = str(country.get("id", "") if isinstance(country, dict) else country).lower()
        if "chile" in c or "remote" in c:
            return True
    cities = a.get("location_cities", {})
    city_list = cities.get("data", []) if isinstance(cities, dict) else (cities or [])
    for city in city_list:
        c = str(city.get("id", "") if isinstance(city, dict) else city).lower()
        if "santiago" in c or "chile" in c:
            return True
    return any(term in blob for term in ["chile", "santiago"])


def modality_id(job):
    m = job.get("attributes", {}).get("modality")
    if isinstance(m, dict):
        v = m.get("data", {}).get("id")
        if isinstance(v, str) and v.isdigit():
            return int(v)
        if isinstance(v, int):
            return v
    return None


def es_part_time(texto):
    """True si el texto declara jornada parcial."""
    t = (texto or "").lower()
    return any(term in t for term in PART_TIME_TERMS)


def empresa_paga_bien(company):
    c = (company or "").lower()
    if not c:
        return False
    # La lista de exclusion manda: si la empresa esta ahi, no califica a nivel analyst
    # aunque alguien la haya agregado tambien a EMPRESAS_PAGAN_BIEN.
    if any(e in c for e in EMPRESAS_NO_CALIFICAN):
        return False
    return any(e in c for e in EMPRESAS_PAGAN_BIEN)


def es_nivel_analyst(title):
    t = (title or "").lower()
    if any(k in t for k in SENIOR_TITLE_TERMS):
        return False
    return any(k in t for k in ANALYST_TIER_TERMS)


def filtro_pago(title, company, min_salary, max_salary, part_time):
    """Regla unica de pago para todas las fuentes. Devuelve (ok, motivo).

    - Part time: pasa siempre si el perfil lo permite (media jornada a cualquier sueldo).
    - Full time con sueldo publicado: el techo del rango debe llegar al umbral.
    - Full time sin sueldo publicado: pasa salvo que sea cargo nivel analyst
      en una empresa que no esta en la lista de las que pagan bien.
    """
    if part_time and PART_TIME_PASA_SIEMPRE:
        return True, "part time"

    techo = None
    for v in (max_salary, min_salary):
        if isinstance(v, (int, float)) and v > 0:
            techo = v
            break
    if techo is not None:
        if techo < MIN_SALARY_USD_FULLTIME:
            return False, f"full time bajo umbral ({int(techo)} USD)"
        return True, "sueldo sobre umbral"

    if es_nivel_analyst(title) and not empresa_paga_bien(company):
        return False, "analyst sin sueldo publicado y empresa fuera de la lista"
    return True, "sueldo no publicado"


def company_name(job):
    comp = job.get("attributes", {}).get("company")
    if isinstance(comp, dict):
        return comp.get("data", {}).get("attributes", {}).get("name", "") or ""
    return ""


def score_job(job):
    """Devuelve (match: bool, score: int, motivo). Filtra segun el perfil cargado."""
    a = job.get("attributes", {})
    title = (a.get("title", "") or "").lower()
    blob = text_of(job)

    # Descartes duros
    if tiene_negativo(title):
        return False, 0, "titulo no objetivo"
    sid = seniority_id(job)
    if sid in SENIORITY_BLOCK_IDS:
        return False, 0, f"seniority {SENIORITY_MAP.get(sid)}"
    if not location_ok(job, blob):
        return False, 0, "fuera de Chile/remoto"

    # Debe haber senal de rol en el titulo. Va antes del filtro de pago: es el descarte
    # mas selectivo y asi el motivo que queda en el log es el que de verdad manda.
    role_hit = any(kw in title for kw in ROLE_KEYWORDS)
    if not role_hit:
        return False, 0, "sin rol objetivo en titulo"

    mod = modality_id(job)
    if mod == MODALITY_INTERNSHIP:
        return False, 0, "practica/internship"
    part_time = (mod == MODALITY_PART_TIME) or es_part_time(title)
    ok_pago, motivo_pago = filtro_pago(
        title, company_name(job), a.get("min_salary"), a.get("max_salary"), part_time)
    if not ok_pago:
        return False, 0, motivo_pago

    score = 5
    if part_time:
        score += 3
    score += sum(2 for kw in STACK_KEYWORDS if kw in blob)
    if any(k in title for k in ["lead", "manager", "head", "senior"]):
        score += 4
    if "retail" in blob or "consumo masivo" in blob or "ecommerce" in blob:
        score += 4
    if any(k in blob for k in ["bigquery", "dbt", "looker", "gcp", "power bi"]):
        score += 3

    return True, score, "match"


def fmt_salary(a):
    lo, hi = a.get("min_salary"), a.get("max_salary")
    if lo and hi:
        return f"{lo:,}-{hi:,} USD"
    if lo:
        return f"desde {lo:,} USD"
    if hi:
        return f"hasta {hi:,} USD"
    return "salario no publicado"


def esc(s):
    # Escape para Telegram HTML parse mode
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_message(job):
    a = job.get("attributes", {})
    company = company_name(job)
    title = a.get("title", "Sin titulo")
    url = job.get("links", {}).get("public_url", "https://www.getonbrd.com")
    remote = "Remoto" if a.get("remote") else "Presencial/Hibrido"
    zone = a.get("remote_zone") or ""
    sal = fmt_salary(a)
    sr = seniority_label(job)
    cat = a.get("category_name", "")

    mod = modality_id(job)
    if mod == MODALITY_PART_TIME or es_part_time(title):
        sal = "⏱ PART TIME · " + sal
    elif mod == MODALITY_FREELANCE:
        sal = "Freelance · " + sal

    loc = remote + (f" ({esc(zone)})" if zone else "")
    header = f"🎯 <b>{esc(title)}</b>"
    if company:
        header += f"\n🏢 {esc(company)}"
    body = (
        f"{header}\n"
        f"💰 {esc(sal)}\n"
        f"📍 {esc(loc)} · 🧭 {esc(sr)} · 🗂 {esc(cat)}\n"
        f"🔗 <a href=\"{esc(url)}\">Ver y postular</a>"
    )
    return body


def send_telegram(cfg, text, disable_preview=True):
    token = cfg["telegram_bot_token"]
    chat_id = cfg["telegram_chat_id"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true" if disable_preview else "false",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload)
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            r = json.loads(resp.read().decode("utf-8"))
            return r.get("ok", False)
    except Exception as e:
        log(f"  ERROR enviando a Telegram: {e}")
        return False


# ------------------------------------------------------------------------------
# Scoring y mensaje para LinkedIn (tarjeta guest: solo titulo/empresa/ubicacion)
# ------------------------------------------------------------------------------

def score_linkedin(job):
    title = (job.get("title", "") or "").lower()
    loc = (job.get("location", "") or "").lower()
    blob = f"{title} {job.get('company','')} {loc}".lower()

    if tiene_negativo(title):
        return False, 0
    # Solo Chile o remoto/LATAM (evita presencial en otros paises)
    if "chile" not in loc and not any(k in blob for k in ["remote", "remoto", "hybrid", "híbrido", "latam", "latin america"]):
        return False, 0
    if not any(kw in title for kw in ROLE_KEYWORDS):
        return False, 0

    # LinkedIn guest no trae sueldo: manda la regla por empresa/nivel del cargo
    part_time = es_part_time(f"{title} {loc}")
    ok_pago, _ = filtro_pago(title, job.get("company", ""), None, None, part_time)
    if not ok_pago:
        return False, 0

    score = 5
    if part_time:
        score += 3
    score += sum(2 for kw in STACK_KEYWORDS if kw in blob)
    if any(k in title for k in ["lead", "manager", "head", "senior", "senior", "ssr", "sr "]):
        score += 4
    if any(k in blob for k in ["retail", "consumo masivo", "ecommerce", "e-commerce"]):
        score += 4
    return True, score


def build_message_linkedin(job):
    title = job.get("title", "Sin titulo")
    company = job.get("company", "")
    loc = job.get("location", "")
    url = job.get("url", "https://www.linkedin.com/jobs")
    body = f"🎯 <b>{esc(title)}</b>"
    if company:
        body += f"\n🏢 {esc(company)}"
    if loc:
        body += f"\n📍 {esc(loc)}"
    body += "\n💰 " + ("⏱ PART TIME" if es_part_time(f"{title} {loc}") else "sueldo no publicado")
    body += f"\n🗂 LinkedIn\n🔗 <a href=\"{esc(url)}\">Ver y postular</a>"
    return body


# ------------------------------------------------------------------------------
# Scoring y mensaje para paginas de empresa (ATS)
# ------------------------------------------------------------------------------

def score_empresa(job):
    title = (job.get("title", "") or "").lower()
    loc = (job.get("location", "") or "").lower()
    blob = f"{title} {job.get('company','')} {loc} {job.get('desc','')}".lower()

    if tiene_negativo(title):
        return False, 0
    en_chile = "chile" in loc or "santiago" in loc
    remoto = job.get("remote") or any(k in blob for k in ["remote", "remoto", "latam", "latin america"])
    if not (en_chile or remoto):
        return False, 0
    if not any(kw in title for kw in ROLE_KEYWORDS):
        return False, 0

    # Solo el titulo: la descripcion menciona jornadas de pasada y daria falsos positivos
    part_time = es_part_time(title)
    ok_pago, _ = filtro_pago(title, job.get("company", ""), None, None, part_time)
    if not ok_pago:
        return False, 0

    score = 5
    if part_time:
        score += 3
    score += sum(2 for kw in STACK_KEYWORDS if kw in blob)
    if any(k in title for k in ["lead", "manager", "head", "senior", "sr "]):
        score += 4
    if en_chile:
        score += 3
    if any(k in blob for k in ["retail", "consumo masivo", "ecommerce", "e-commerce"]):
        score += 4
    return True, score


def build_message_empresa(job):
    title = job.get("title", "Sin titulo")
    company = job.get("company", "")
    loc = job.get("location", "") or ("Remoto" if job.get("remote") else "")
    url = job.get("url", "")
    body = f"🎯 <b>{esc(title)}</b>"
    if company:
        body += f"\n🏢 {esc(company)}"
    if loc:
        body += f"\n📍 {esc(loc)}"
    body += "\n💰 " + ("⏱ PART TIME" if es_part_time(title) else "sueldo no publicado")
    body += f"\n🗂 Sitio de la empresa\n🔗 <a href=\"{esc(url)}\">Ver y postular</a>"
    return body


def gather_matches():
    """Devuelve lista de dicts normalizados: {score,id,title,url,message,source}."""
    entries = {}

    # --- GetOnBoard ---
    all_jobs = {}
    for cat in (CATEGORIES if GOB_ACTIVA else []):
        log(f"Leyendo categoria: {cat}")
        for job in fetch_category(cat):
            all_jobs[job.get("id")] = job  # dedup entre categorias
    gob_ok = 0
    for job in all_jobs.values():
        ok, score, _ = score_job(job)
        if ok:
            jid = job.get("id")
            entries[jid] = {
                "score": score,
                "id": jid,
                "title": job.get("attributes", {}).get("title", ""),
                "url": job.get("links", {}).get("public_url", ""),
                "message": build_message(job),
                "source": "GetOnBoard",
            }
            gob_ok += 1
    log(f"GetOnBoard: {len(all_jobs)} revisadas, {gob_ok} calzan.")

    # --- LinkedIn ---
    if LINKEDIN_ENABLED and perfil.LINKEDIN_ACTIVA:
        try:
            ln_jobs = fuente_linkedin.fetch_all(logger=log)
            ln_ok = 0
            for job in ln_jobs:
                ok, score = score_linkedin(job)
                if ok:
                    entries[job["id"]] = {
                        "score": score,
                        "id": job["id"],
                        "title": job.get("title", ""),
                        "url": job.get("url", ""),
                        "message": build_message_linkedin(job),
                        "source": "LinkedIn",
                    }
                    ln_ok += 1
            log(f"LinkedIn: {ln_ok} calzan con el perfil.")
        except Exception as e:
            log(f"  aviso: fuente LinkedIn fallo por completo, se omite: {e}")

    # --- Paginas de empresa (ATS) ---
    if EMPRESAS_ENABLED and perfil.EMPRESAS_ACTIVA:
        try:
            emp_jobs = fuente_empresas.fetch_all(logger=log)
            emp_ok = 0
            for job in emp_jobs:
                ok, score = score_empresa(job)
                if ok:
                    entries[job["id"]] = {
                        "score": score,
                        "id": job["id"],
                        "title": job.get("title", ""),
                        "url": job.get("url", ""),
                        "message": build_message_empresa(job),
                        "source": "Empresa",
                    }
                    emp_ok += 1
            log(f"Empresas: {emp_ok} calzan con el perfil.")
        except Exception as e:
            log(f"  aviso: fuente Empresas fallo por completo, se omite: {e}")

    matches = sorted(entries.values(), key=lambda x: x["score"], reverse=True)
    log(f"TOTAL: {len(matches)} ofertas calzan con el perfil (todas las fuentes).")
    return matches


def _job_gob(title, company="Acme", mn=None, mx=None, modality=MODALITY_FULL_TIME,
             seniority=4, remote=True, desc=""):
    """Arma una oferta sintetica con la forma que devuelve la API de GetOnBoard."""
    return {
        "id": "test-1",
        "attributes": {
            "title": title,
            "description": desc,
            "min_salary": mn,
            "max_salary": mx,
            "remote": remote,
            "modality": {"data": {"id": str(modality), "type": "modality"}},
            "seniority": {"data": {"id": seniority, "type": "seniority"}},
            "company": {"data": {"attributes": {"name": company}}},
            "category_name": "Data Science / Analytics",
        },
        "links": {"public_url": "https://example.com"},
    }


def autotest():
    """Guardia mecanica de la regla de pago. Corre sin red, antes de cada ejecucion en
    GitHub Actions y en cada push: si alguien afloja el umbral por accidente, falla aca.

    Cubre los tres errores que ya se cometieron una vez:
      1. meter en EMPRESAS_PAGAN_BIEN empresas que no pagan eso a nivel analyst,
      2. detectar part time leyendo la descripcion (falso positivo que salta el umbral),
      3. sacarle el filtro de pago a alguna de las tres fuentes.
    """
    fallas = []

    # Las empresas de prueba salen del perfil cargado, no van escritas aca: el
    # autotest tiene que valer para cualquiera que clone el repo y ponga las suyas.
    # Se toma la primera de cada lista; si una lista esta vacia, sus casos se omiten.
    paga_bien = sorted(EMPRESAS_PAGAN_BIEN)[0] if EMPRESAS_PAGAN_BIEN else None
    no_califica = sorted(EMPRESAS_NO_CALIFICAN)[0] if EMPRESAS_NO_CALIFICAN else None
    omitidos = 0

    # --- 1. Integridad de las listas de empresas -------------------------------
    solapadas = sorted(
        a for a in EMPRESAS_PAGAN_BIEN
        for b in EMPRESAS_NO_CALIFICAN
        if a == b or a in b or b in a
    )
    if solapadas:
        fallas.append("  empresas en EMPRESAS_PAGAN_BIEN y EMPRESAS_NO_CALIFICAN a la "
                      f"vez: {solapadas}")
    for nombre, lista in (("EMPRESAS_PAGAN_BIEN", EMPRESAS_PAGAN_BIEN),
                          ("EMPRESAS_NO_CALIFICAN", EMPRESAS_NO_CALIFICAN)):
        malas = sorted(e for e in lista if e != e.strip().lower() or not e)
        if malas:
            fallas.append(f"  {nombre} tiene entradas sin normalizar a minusculas: {malas}")

    # --- 2. Regla de pago aislada ---------------------------------------------
    # Los montos son RELATIVOS al umbral del perfil. Escribirlos fijos haria que
    # bajar el umbral a proposito rompiera el autotest, y lo que la guardia cuida es
    # la regla ("manda el techo del rango"), no la cifra.
    U = MIN_SALARY_USD_FULLTIME
    bajo, justo, alto = U - 800, U, U + 700

    casos = [
        # Full time con sueldo publicado: manda el techo del rango
        ("Data Engineer", "Acme", bajo - 300, bajo, False, False),
        ("Data Analyst", "Acme", None, U - 1, False, False),
        ("BI Lead", "Acme", bajo, justo, False, True),
        ("Data Engineer", "Acme", justo, alto, False, True),
        ("Data Engineer", "Acme", bajo, None, False, False),   # solo minimo, bajo umbral
        # Part time: entra a cualquier sueldo
        ("Data Analyst (Part Time)", "Acme", bajo - 500, bajo, True, True),
        ("Analista BI media jornada", "Acme", None, None, True, True),
        # Sin sueldo publicado: decide el nivel del cargo y la empresa
        ("Senior Data Analyst", "Empresa Desconocida", None, None, False, True),
        ("BI Manager", "Empresa Desconocida", None, None, False, True),
        ("Analista de datos", "Empresa Desconocida", None, None, False, False),
    ]
    if paga_bien:
        # Un analyst sin sueldo pasa solo si la empresa esta en la lista.
        casos.append(("Data Analyst", paga_bien, None, None, False, True))
    else:
        omitidos += 1
    if no_califica:
        # Y la exclusion gana, incluso con la razon social completa alrededor del
        # nombre. Tener una cadena de retail entre las que pagan bien fue justo lo
        # que dejo pasar los Data Analyst que se querian cortar.
        casos.append(("Data Analyst", no_califica, None, None, False, False))
        casos.append(("Data Analyst", f"Grupo {no_califica.title()} S.A.",
                      None, None, False, False))
    else:
        omitidos += 2
    for titulo, empresa, mn, mx, pt, esperado in casos:
        obtenido, motivo = filtro_pago(titulo, empresa, mn, mx, pt)
        if obtenido != esperado:
            fallas.append(f"  {titulo!r} @ {empresa!r} [{mn},{mx}] pt={pt}: "
                          f"esperaba {esperado}, obtuvo {obtenido} ({motivo})")
    # --- 3. Deteccion de part time --------------------------------------------
    if not es_part_time("Data Analyst (Part Time)"):
        fallas.append("  es_part_time no reconoce '(Part Time)' en el titulo")
    if es_part_time("Senior Data Engineer"):
        fallas.append("  es_part_time marca part time un titulo full time")
    # El filtro de part time NO puede alimentarse de la descripcion: los avisos full
    # time mencionan jornadas de pasada y eso saltaria el umbral. Se revisa el codigo.
    try:
        fuentes = [Path(__file__).read_text(encoding="utf-8")]
        for extra in ("fuente_linkedin.py", "fuente_empresas.py"):
            p = BASE_DIR / extra
            if p.exists():
                fuentes.append(p.read_text(encoding="utf-8"))
        mal_uso = re.search(r"es_part_time\([^)]*\b(desc|description|descripcion)\b",
                            "\n".join(fuentes))
        if mal_uso:
            fallas.append("  es_part_time se esta llamando sobre una descripcion "
                          f"({mal_uso.group(0)!r}); debe recibir solo el titulo")
    except Exception as e:
        fallas.append(f"  no se pudo revisar el uso de es_part_time: {e}")

    # --- 4. Las TRES fuentes deben aplicar la regla, no solo GetOnBoard ---------
    # (job sintetico, empresa fuera de la lista, sueldo bajo o ausente)
    e2e = [
        ("GetOnBoard sueldo bajo",
         lambda: score_job(_job_gob("Data Engineer", mn=bajo - 300, mx=bajo))[0], False),
        ("GetOnBoard sobre umbral",
         lambda: score_job(_job_gob("Data Engineer", mn=justo, mx=alto))[0], True),
        ("GetOnBoard part time por modality",
         lambda: score_job(_job_gob("Data Analyst", mn=bajo - 600, mx=bajo - 300,
                                    modality=MODALITY_PART_TIME))[0], True),
        ("GetOnBoard internship",
         lambda: score_job(_job_gob("Data Analyst", mn=justo, mx=alto,
                                    modality=MODALITY_INTERNSHIP))[0], False),
        ("GetOnBoard descripcion que menciona media jornada no lo vuelve part time",
         lambda: score_job(_job_gob("Data Engineer", mn=bajo - 800, mx=bajo - 300,
                                    desc="Horario flexible, media jornada los viernes"))[0],
         False),
        ("LinkedIn analyst empresa fuera de la lista",
         lambda: score_linkedin({"title": "Data Analyst", "company": "Retail Cualquiera",
                                 "location": "Santiago, Chile"})[0], False),
        ("LinkedIn senior sin sueldo",
         lambda: score_linkedin({"title": "Senior Data Engineer", "company": "Acme",
                                 "location": "Santiago, Chile"})[0], True),
        ("Empresas analyst fuera de la lista",
         lambda: score_empresa({"title": "Data Analyst", "company": "Retail Cualquiera",
                                "location": "Santiago, Chile", "desc": ""})[0], False),
    ]
    if paga_bien:
        e2e.append(("LinkedIn analyst empresa que paga bien",
                    lambda: score_linkedin({"title": "Data Analyst", "company": paga_bien,
                                            "location": "Santiago, Chile"})[0], True))
        e2e.append(("Empresas analyst que paga bien",
                    lambda: score_empresa({"title": "Data Analyst", "company": paga_bien,
                                           "location": "Santiago, Chile", "desc": ""})[0], True))
    else:
        omitidos += 2

    for nombre, fn, esperado in e2e:
        try:
            obtenido = fn()
        except Exception as e:
            fallas.append(f"  {nombre}: reviento con {e!r}")
            continue
        if obtenido != esperado:
            fallas.append(f"  {nombre}: esperaba {esperado}, obtuvo {obtenido}")

    if fallas:
        log("AUTOTEST FALLIDO: la regla de pago no se esta cumpliendo:")
        for f in fallas:
            log(f)
        sys.exit(1)
    resumen = (f"Autotest ok ({PERFIL_ORIGEN.name}): {len(casos)} casos de la regla de "
               f"pago + {len(e2e)} de las tres fuentes + integridad de las listas.")
    if omitidos:
        resumen += (f" {omitidos} casos omitidos: el perfil trae vacia alguna de las dos "
                    "listas de empresas.")
    log(resumen)


def main():
    args = set(sys.argv[1:])

    if "--autotest" in args:
        autotest()
        return

    if "--test" in args:
        cfg = load_config()
        ok = send_telegram(cfg, "✅ <b>Alertas de empleo conectadas</b>\nRecibiras aqui las ofertas que calzan con tu perfil.")
        log("Mensaje de prueba enviado." if ok else "Fallo el mensaje de prueba.")
        return

    matches = gather_matches()

    if "--resumen" in args:
        for m in matches:
            print(f"[{m['score']:>3}] {m['source']:<10} {m['title']}  |  {m['url']}")
        return

    seen = load_seen()

    if "--seed" in args or not SEEN_PATH.exists():
        for m in matches:
            seen.add(m["id"])
        save_seen(seen)
        log(f"Modo seed: {len(seen)} ofertas marcadas como vistas. No se envio nada.")
        if "--seed" in args:
            return

    cfg = load_config()
    nuevas = [m for m in matches if m["id"] not in seen]
    log(f"Ofertas NUEVAS a enviar: {len(nuevas)}")

    enviados = 0
    for m in nuevas:
        if send_telegram(cfg, m["message"]):
            enviados += 1
            seen.add(m["id"])
            time.sleep(1.2)  # respetar rate limit de Telegram
        else:
            time.sleep(3)

    save_seen(seen)
    log(f"Listo. Enviadas {enviados} ofertas nuevas a Telegram.")


if __name__ == "__main__":
    main()
