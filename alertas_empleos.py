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

Ojo con --resumen seguido: la API de GetOnBoard limita si se la consulta muchas veces y la
corrida sale corta (se auto-sana en la siguiente).
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

try:
    import fuente_contratos
    CONTRATOS_ENABLED = True
except Exception:
    CONTRATOS_ENABLED = False

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
    SENIORITY_BLOCK_IDS,
    UBICACION_PAIS_TERMS, UBICACION_CIUDAD_TERMS, UBICACION_REMOTO_TERMS,
    LINKEDIN_UBICACION_EXTRA_TERMS,
    BONO_RUBRO_TERMS, BONO_STACK_TERMS,
    MIN_SALARY_USD_FULLTIME, MIN_SALARY_USD_PARTTIME, HORAS_MES_PART_TIME,
    PART_TIME_TERMS,
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
    req = urllib.request.Request(url, headers={"User-Agent": perfil.USER_AGENT})
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


def es_pais(texto):
    """True si el texto nombra un pais de match.ubicacion.pais_terms."""
    t = (texto or "").lower()
    return any(term in t for term in UBICACION_PAIS_TERMS)


def es_local(texto):
    """True si el texto nombra el pais o la ciudad del perfil. La ciudad sola es mas
    debil (su nombre se repite en otros paises): donde la fuente casi siempre escribe
    el pais se usa es_pais()."""
    t = (texto or "").lower()
    return es_pais(t) or any(term in t for term in UBICACION_CIUDAD_TERMS)


def es_remoto(texto, extra=()):
    """True si el texto trae un termino de match.ubicacion.remoto_terms."""
    t = (texto or "").lower()
    return any(term in t for term in list(UBICACION_REMOTO_TERMS) + list(extra))


def location_ok(job, blob):
    a = job.get("attributes", {})
    if a.get("remote"):
        return True
    for country in a.get("countries", []) or []:
        c = str(country.get("id", "") if isinstance(country, dict) else country).lower()
        # "remote" aca es vocabulario de la API de GetOnBoard, no del perfil
        if es_pais(c) or "remote" in c:
            return True
    cities = a.get("location_cities", {})
    city_list = cities.get("data", []) if isinstance(cities, dict) else (cities or [])
    for city in city_list:
        c = str(city.get("id", "") if isinstance(city, dict) else city).lower()
        if es_local(c):
            return True
    return es_local(blob)


def bono_rubro(blob):
    return 4 if any(k in blob for k in BONO_RUBRO_TERMS) else 0


def bono_stack(blob):
    return 3 if any(k in blob for k in BONO_STACK_TERMS) else 0


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

    - Con sueldo publicado: el techo del rango debe llegar al umbral de su jornada
      (MIN_SALARY_USD_FULLTIME o MIN_SALARY_USD_PARTTIME).
    - Sin sueldo publicado: pasa salvo que sea cargo nivel analyst en una empresa
      que no esta en la lista de las que pagan bien. Igual para las dos jornadas.
    """
    jornada = "part time" if part_time else "full time"
    umbral = MIN_SALARY_USD_PARTTIME if part_time else MIN_SALARY_USD_FULLTIME

    techo = None
    for v in (max_salary, min_salary):
        if isinstance(v, (int, float)) and v > 0:
            techo = v
            break
    if techo is not None:
        if techo < umbral:
            return False, f"{jornada} bajo umbral ({int(techo)} USD)"
        return True, f"{jornada} sobre umbral"

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
        return False, 0, "fuera de la ubicacion del perfil"

    # Debe haber senal de rol en el titulo. Va antes del filtro de pago: es el descarte
    # mas selectivo y asi el motivo que queda en el log es el que de verdad manda.
    role_hit = any(kw in title for kw in ROLE_KEYWORDS)
    if not role_hit:
        return False, 0, "sin rol objetivo en titulo"

    mod = modality_id(job)
    if mod == MODALITY_INTERNSHIP:
        return False, 0, "practica/internship"
    # Freelance se mide con el umbral de part time: se puede tomar sin dejar otro
    # trabajo. El mensaje igual lo rotula "Freelance".
    part_time = (mod in (MODALITY_PART_TIME, MODALITY_FREELANCE)) or es_part_time(title)
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
    score += bono_rubro(blob) + bono_stack(blob)

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
    # Solo el pais del perfil o remoto (evita presencial en otros paises). LinkedIn
    # casi siempre escribe el pais, asi que la ciudad sola no basta; un rotulo local
    # sin pais va entero en pais_terms.
    if not es_pais(loc) and not es_remoto(blob, LINKEDIN_UBICACION_EXTRA_TERMS):
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
    score += bono_rubro(blob)
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
    en_local = es_local(loc)
    remoto = job.get("remote") or es_remoto(blob)
    if not (en_local or remoto):
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
    if en_local:
        score += 3
    score += bono_rubro(blob)
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


# ------------------------------------------------------------------------------
# Scoring y mensaje para part time y contratos por hora (Himalayas, Arc)
# ------------------------------------------------------------------------------

def sueldo_mensual_contrato(job):
    """(min, max) en USD al mes. La tarifa por hora se lleva a mensual con
    HORAS_MES_PART_TIME (horas por semana del perfil x 52 / 12)."""
    mn, mx = job.get("salary_min"), job.get("salary_max")
    if job.get("salary_period") == "hourly":
        mn = mn * HORAS_MES_PART_TIME if isinstance(mn, (int, float)) else None
        mx = mx * HORAS_MES_PART_TIME if isinstance(mx, (int, float)) else None
    elif job.get("salary_period") != "monthly":
        mn = mx = None
    return mn, mx


def score_contrato(job):
    title = (job.get("title", "") or "").lower()
    blob = f"{title} {job.get('company','')} {job.get('desc','')}".lower()

    if tiene_negativo(title):
        return False, 0
    if fuente_contratos.es_ruido(job.get("company", ""), title):
        return False, 0
    if not any(kw in title for kw in ROLE_KEYWORDS):
        return False, 0

    # Por hora o part time declarado: umbral de part time. Un contrato con sueldo
    # mensual o anual es jornada completa y se mide con el de full time.
    part_time = bool(job.get("part_time")) or job.get("salary_period") == "hourly"
    mn, mx = sueldo_mensual_contrato(job)
    ok_pago, _ = filtro_pago(title, job.get("company", ""), mn, mx, part_time)
    if not ok_pago:
        return False, 0

    score = 5
    if part_time:
        score += 3
    score += sum(2 for kw in STACK_KEYWORDS if kw in blob)
    if any(k in title for k in ["lead", "manager", "head", "senior", "sr "]):
        score += 4
    return True, score


def build_message_contrato(job):
    title = job.get("title", "Sin titulo")
    company = job.get("company", "")
    body = f"🎯 <b>{esc(title)}</b>"
    if company:
        body += f"\n🏢 {esc(company)}"
    if job.get("location"):
        body += f"\n📍 {esc(job['location'])}"
    mn, mx = job.get("salary_min"), job.get("salary_max")
    if job.get("salary_period") == "hourly" and (mn or mx):
        rango = f"{mn or '?'}-{mx or '?'} USD/h"
        mmn, mmx = sueldo_mensual_contrato(job)
        horas_sem = round(HORAS_MES_PART_TIME * 12 / 52)
        sal = (f"{rango} (~{int(mmx or mmn):,} USD/mes a {horas_sem} h/sem)"
               .replace(",", "."))
    elif mn or mx:
        sal = f"{int(mn or 0):,}-{int(mx or 0):,} USD/mes".replace(",", ".")
    else:
        sal = "sueldo no publicado"
    etiqueta = "⏱ PART TIME" if job.get("part_time") else "Contrato"
    body += f"\n💰 {etiqueta} · {esc(sal)}"
    body += (f"\n🗂 {esc(job.get('source', ''))}\n🔗 "
             f"<a href=\"{esc(job.get('url', ''))}\">Ver y postular</a>")
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

    # --- Part time y contratos por hora (Himalayas, Arc) ---
    if CONTRATOS_ENABLED and perfil.CONTRATOS_ACTIVA:
        try:
            ct_jobs = fuente_contratos.fetch_all(logger=log)
            ct_ok = 0
            for job in ct_jobs:
                ok, score = score_contrato(job)
                if ok:
                    entries[job["id"]] = {
                        "score": score,
                        "id": job["id"],
                        "title": job.get("title", ""),
                        "url": job.get("url", ""),
                        "message": build_message_contrato(job),
                        "source": job.get("source", "Contratos"),
                    }
                    ct_ok += 1
            log(f"Contratos: {ct_ok} calzan con el perfil.")
        except Exception as e:
            log(f"  aviso: fuente Contratos fallo por completo, se omite: {e}")

    matches = sorted(entries.values(), key=lambda x: x["score"], reverse=True)
    log(f"TOTAL: {len(matches)} ofertas calzan con el perfil (todas las fuentes).")
    return matches


def _job_gob(title, company="Acme", mn=None, mx=None, modality=MODALITY_FULL_TIME,
             seniority=None, remote=True, desc=""):
    """Arma una oferta sintetica con la forma que devuelve la API de GetOnBoard."""
    if seniority is None:
        # El primer seniority que el perfil no bloquea, del mas alto al mas bajo
        seniority = next((s for s in (4, 5, 3, 2, 1) if s not in SENIORITY_BLOCK_IDS), 4)
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
            "category_name": "Categoria de prueba",
        },
        "links": {"public_url": "https://example.com"},
    }


def _titulos_de_prueba():
    """Titulos sinteticos armados con las palabras del perfil cargado.

    Escritos a mano ("Data Engineer", "BI Lead") el autotest solo valia para un
    perfil de datos: con otras role_keywords fallaba sin que la regla estuviera rota.
    Cada candidato se valida con las mismas funciones del filtro, asi que lo que sale
    de aca es un titulo que el perfil de verdad dejaria pasar.

      base     trae palabra de rol; sin negativo, sin part time, sin ruido de contratos
      analyst  base de nivel analyst (None si el perfil no define ese nivel)
      senior   base con termino senior, o sea fuera del nivel analyst (None si no hay)
      pt       termino de part time que se le puede colgar a base (None si no hay)
      sin_rol  titulo sin ninguna palabra de rol
    """
    def ruido(t):
        return CONTRATOS_ENABLED and fuente_contratos.es_ruido("", t)

    def limpio(t):
        return (any(kw in t for kw in ROLE_KEYWORDS) and not tiene_negativo(t)
                and not es_part_time(t) and not ruido(t))

    roles = [kw for kw in ROLE_KEYWORDS if limpio(kw)]
    base = roles[0] if roles else None

    # Primero un termino que sirva solo; si ninguno calza, analyst + rol
    candidatos = (list(ANALYST_TIER_TERMS) + roles
                  + [f"{a} {r}" for a in ANALYST_TIER_TERMS for r in roles])
    analyst = next((t for t in candidatos if limpio(t) and es_nivel_analyst(t)), None)

    bases = ([analyst] if analyst else []) + roles
    senior = next((t for t in (f"{s.strip()} {b}" for b in bases
                               for s in SENIOR_TITLE_TERMS)
                   if limpio(t) and not es_nivel_analyst(t)), None)

    pt = next((p for p in PART_TIME_TERMS
               if base and es_part_time(f"{base} ({p})")
               and not tiene_negativo(f"{base} ({p})")), None)

    sin_rol = next((t for t in ("cargo sin relacion", "zzqx wvk", "qqq")
                    if not any(kw in t for kw in ROLE_KEYWORDS)
                    and not tiene_negativo(t) and not ruido(t)), None)
    return {"base": base, "analyst": analyst, "senior": senior, "pt": pt,
            "sin_rol": sin_rol}


def autotest():
    """Guardia mecanica de la regla de pago. Corre sin red, antes de cada ejecucion en
    GitHub Actions y en cada push: si alguien afloja el umbral por accidente, falla aca.

    Cubre los tres errores que ya se cometieron una vez:
      1. meter en EMPRESAS_PAGAN_BIEN empresas que no pagan eso a nivel analyst,
      2. detectar part time leyendo la descripcion (falso positivo que salta el umbral),
      3. sacarle el filtro de pago a alguna de las cuatro fuentes.

    Nada de lo que se prueba va escrito aca: empresas, montos, titulos y lugares salen
    del perfil cargado. Lo que el perfil no permite armar se omite y se informa.
    """
    fallas = []

    # Las empresas de prueba salen del perfil cargado, no van escritas aca: el
    # autotest tiene que valer para cualquiera que clone el repo y ponga las suyas.
    # Se toma la primera de cada lista; si una lista esta vacia, sus casos se omiten.
    paga_bien = sorted(EMPRESAS_PAGAN_BIEN)[0] if EMPRESAS_PAGAN_BIEN else None
    no_califica = sorted(EMPRESAS_NO_CALIFICAN)[0] if EMPRESAS_NO_CALIFICAN else None
    desconocida = next((n for n in ("Empresa Desconocida", "Acme")
                        if not empresa_paga_bien(n)), "Zzqx Wvk")
    omitidos = 0

    # Los titulos tambien. Sin un titulo base no hay nada que probar, y tampoco hay
    # bot: con ese perfil ninguna oferta pasaria el filtro de rol.
    T = _titulos_de_prueba()
    base, analyst, senior = T["base"], T["analyst"], T["senior"]
    if not base:
        log(f"AUTOTEST FALLIDO ({PERFIL_ORIGEN.name}): ninguna palabra de "
            "match.role_keywords sirve de titulo (lista vacia, o todas caen en "
            "negative_title, part_time_terms o titulos_ruido). Con este perfil no "
            "pasaria ninguna oferta.")
        sys.exit(1)
    base_pt = f"{base} ({T['pt']})" if T["pt"] else None

    # Lugares de prueba: uno que el perfil acepta y otro que no.
    todos_lugares = (UBICACION_PAIS_TERMS + UBICACION_CIUDAD_TERMS
                     + UBICACION_REMOTO_TERMS + LINKEDIN_UBICACION_EXTRA_TERMS)
    lugar = (UBICACION_PAIS_TERMS[0].title() if UBICACION_PAIS_TERMS
             else (UBICACION_REMOTO_TERMS[0] if UBICACION_REMOTO_TERMS else None))
    # Una ciudad del perfil que no arrastre el pais ni un termino de remoto
    ciudad_sola = next((c for c in UBICACION_CIUDAD_TERMS
                        if not es_pais(c)
                        and not es_remoto(c, LINKEDIN_UBICACION_EXTRA_TERMS)), None)
    fuera = next((l for l in ("ciudad inexistente", "zzqx wvk")
                  if not any(t in l for t in todos_lugares)
                  and not any(t in f"{base} {senior}" for t in todos_lugares)), None)

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
    # Part time, freelance y contratos: su propio piso, tambien relativo. Los casos
    # que distinguen un piso del otro solo tienen sentido si el de part time es mas
    # bajo; si el perfil los iguala, se omiten.
    UP = MIN_SALARY_USD_PARTTIME
    pt_bajo, pt_alto = int(UP * 0.6), UP + 300
    entre_pisos = (UP + U) // 2 if UP < U else None   # pasa part time, no full time

    casos = [
        # Full time con sueldo publicado: manda el techo del rango
        (base, "Acme", bajo - 300, bajo, False, False),
        (base, "Acme", None, U - 1, False, False),
        (base, "Acme", bajo, justo, False, True),
        (base, "Acme", justo, alto, False, True),
        (base, "Acme", bajo, None, False, False),   # solo minimo, bajo umbral
        # Part time: umbral propio, con la misma regla del techo
        (base_pt or base, "Acme", pt_bajo - 300, pt_bajo, True, False),
        (base_pt or base, "Acme", UP - 500, UP, True, True),
        (base_pt or base, "Acme", pt_alto, None, True, True),
    ]
    # Sin sueldo publicado: decide el nivel del cargo y la empresa, igual para las
    # dos jornadas.
    if analyst:
        casos.append((analyst, desconocida, None, None, False, False))
        casos.append((analyst, desconocida, None, None, True, False))
    else:
        omitidos += 2
    if senior:
        casos.append((senior, desconocida, None, None, False, True))
        casos.append((senior, desconocida, None, None, True, True))
    else:
        omitidos += 2
    if entre_pisos:
        # Un sueldo que basta para part time no basta para full time
        casos.append((base, "Acme", UP, entre_pisos, False, False))
        casos.append((base_pt or base, "Acme", UP, entre_pisos, True, True))
    else:
        omitidos += 2
    if paga_bien and analyst:
        # Un analyst sin sueldo pasa solo si la empresa esta en la lista.
        casos.append((analyst, paga_bien, None, None, False, True))
    else:
        omitidos += 1
    if no_califica and analyst:
        # Y la exclusion gana, incluso con la razon social completa alrededor del
        # nombre. Tener una cadena de retail entre las que pagan bien fue justo lo
        # que dejo pasar los cargos analyst que se querian cortar.
        casos.append((analyst, no_califica, None, None, False, False))
        casos.append((analyst, f"Grupo {no_califica.title()} S.A.",
                      None, None, False, False))
    else:
        omitidos += 2
    for titulo, empresa, mn, mx, pt, esperado in casos:
        obtenido, motivo = filtro_pago(titulo, empresa, mn, mx, pt)
        if obtenido != esperado:
            fallas.append(f"  {titulo!r} @ {empresa!r} [{mn},{mx}] pt={pt}: "
                          f"esperaba {esperado}, obtuvo {obtenido} ({motivo})")
    # --- 3. Deteccion de part time --------------------------------------------
    if base_pt:
        if not es_part_time(base_pt):
            fallas.append(f"  es_part_time no reconoce {base_pt!r}")
    else:
        omitidos += 1
    if es_part_time(senior or base):
        fallas.append("  es_part_time marca part time un titulo full time")
    # El filtro de part time NO puede alimentarse de la descripcion: los avisos full
    # time mencionan jornadas de pasada y eso saltaria el umbral. Se revisa el codigo.
    try:
        fuentes = [Path(__file__).read_text(encoding="utf-8")]
        for extra in ("fuente_linkedin.py", "fuente_empresas.py", "fuente_contratos.py"):
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

    # --- 4. Las CUATRO fuentes deben aplicar la regla, no solo GetOnBoard -------
    # (job sintetico, empresa fuera de la lista, sueldo bajo o ausente)
    e2e = [
        ("GetOnBoard sueldo bajo",
         lambda: score_job(_job_gob(base, mn=bajo - 300, mx=bajo))[0], False),
        ("GetOnBoard sobre umbral",
         lambda: score_job(_job_gob(base, mn=justo, mx=alto))[0], True),
        ("GetOnBoard part time por modality, bien pagado",
         lambda: score_job(_job_gob(base, mn=UP - 300, mx=pt_alto,
                                    modality=MODALITY_PART_TIME))[0], True),
        ("GetOnBoard part time por modality, mal pagado",
         lambda: score_job(_job_gob(base, mn=pt_bajo - 300, mx=pt_bajo,
                                    modality=MODALITY_PART_TIME))[0], False),
        ("GetOnBoard freelance mal pagado",
         lambda: score_job(_job_gob(base, mn=pt_bajo - 300, mx=pt_bajo,
                                    modality=MODALITY_FREELANCE))[0], False),
        ("GetOnBoard internship",
         lambda: score_job(_job_gob(base, mn=justo, mx=alto,
                                    modality=MODALITY_INTERNSHIP))[0], False),
    ]
    if T["pt"]:
        e2e.append(
            ("GetOnBoard descripcion que menciona part time no lo vuelve part time",
             lambda: score_job(_job_gob(base, mn=bajo - 800, mx=bajo - 300,
                                        desc=f"Horario flexible, {T['pt']} los viernes"))[0],
             False))
    else:
        omitidos += 1
    if analyst and lugar:
        e2e += [
            ("LinkedIn analyst empresa fuera de la lista",
             lambda: score_linkedin({"title": analyst, "company": desconocida,
                                     "location": lugar})[0], False),
            ("Empresas analyst fuera de la lista",
             lambda: score_empresa({"title": analyst, "company": desconocida,
                                    "location": lugar, "desc": ""})[0], False),
        ]
    else:
        omitidos += 2
    if senior and lugar:
        e2e += [
            ("LinkedIn senior sin sueldo",
             lambda: score_linkedin({"title": senior, "company": desconocida,
                                     "location": lugar})[0], True),
            ("Empresas senior sin sueldo",
             lambda: score_empresa({"title": senior, "company": desconocida,
                                    "location": lugar, "desc": ""})[0], True),
        ]
    else:
        omitidos += 2
    if paga_bien and analyst and lugar:
        e2e.append(("LinkedIn analyst empresa que paga bien",
                    lambda: score_linkedin({"title": analyst, "company": paga_bien,
                                            "location": lugar})[0], True))
        e2e.append(("Empresas analyst que paga bien",
                    lambda: score_empresa({"title": analyst, "company": paga_bien,
                                           "location": lugar, "desc": ""})[0], True))
    else:
        omitidos += 2
    if entre_pisos:
        e2e.append(("GetOnBoard freelance usa el umbral de part time",
                    lambda: score_job(_job_gob(base, mn=UP, mx=entre_pisos,
                                               modality=MODALITY_FREELANCE))[0], True))
    else:
        omitidos += 1

    # La ubicacion sale del perfil: lo que no es remoto ni local se descarta aunque
    # pase el pago. Los pares de arriba, con `lugar`, son el control positivo.
    if ciudad_sola and senior:
        e2e += [
            ("LinkedIn ciudad sin pais no basta",
             lambda: score_linkedin({"title": senior, "company": desconocida,
                                     "location": ciudad_sola.title()})[0], False),
            ("Empresas ciudad sin pais basta",
             lambda: score_empresa({"title": senior, "company": desconocida,
                                    "location": ciudad_sola.title(), "desc": ""})[0], True),
        ]
    else:
        omitidos += 2
    if fuera and UBICACION_PAIS_TERMS:
        e2e += [
            ("GetOnBoard presencial fuera de la ubicacion del perfil",
             lambda: score_job(_job_gob(base, mn=justo, mx=alto, remote=False,
                                        desc=f"Presencial en {fuera}"))[0], False),
            ("GetOnBoard presencial en la ubicacion del perfil",
             lambda: score_job(_job_gob(base, mn=justo, mx=alto, remote=False,
                                        desc=f"Presencial en {lugar}"))[0], True),
        ]
    else:
        omitidos += 2
    if fuera and senior:
        e2e += [
            ("LinkedIn fuera de la ubicacion del perfil",
             lambda: score_linkedin({"title": senior, "company": desconocida,
                                     "location": fuera})[0], False),
            ("Empresas fuera de la ubicacion del perfil",
             lambda: score_empresa({"title": senior, "company": desconocida,
                                    "location": fuera, "desc": ""})[0], False),
        ]
    else:
        omitidos += 2

    if CONTRATOS_ENABLED:
        # Tarifas por hora derivadas del piso de part time y de las horas del perfil:
        # una holgada sobre el piso y otra claramente bajo.
        h = HORAS_MES_PART_TIME
        hr_ok = (int(UP / h) + 1, int(UP / h * 1.8) + 1)
        hr_bajo = (int(UP / h * 0.5), int(UP / h * 0.7))

        def _ct(title, mn, mx, periodo="hourly", company="Acme", pt=False):
            return score_contrato({"title": title, "company": company, "desc": "",
                                   "salary_min": mn, "salary_max": mx,
                                   "salary_period": periodo, "part_time": pt})[0]
        e2e += [
            ("Contrato por hora sobre el piso de part time pasa",
             lambda: _ct(base, *hr_ok), True),
            ("Contrato por hora bajo el piso no llega",
             lambda: _ct(base, *hr_bajo), False),
            ("Part time mensual sobre el piso pasa",
             lambda: _ct(base, UP - 500, pt_alto, "monthly", pt=True), True),
        ]
        if T["sin_rol"]:
            e2e.append(("Contrato sin rol objetivo se descarta",
                        lambda: _ct(T["sin_rol"], *hr_ok), False))
        else:
            omitidos += 1
        if entre_pisos:
            e2e.append(("Contrato mensual es full time y no llega con sueldo de part time",
                        lambda: _ct(base, UP, entre_pisos, "monthly"), False))
        else:
            omitidos += 1
        if perfil.EMPRESAS_RUIDO:
            ruido = perfil.EMPRESAS_RUIDO[0]
            e2e.append(("Reclutador de entrenamiento de IA se descarta",
                        lambda: _ct(base, *hr_ok,
                                    company=f"{ruido.title()} Inc."), False))
        else:
            omitidos += 1

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
               f"pago + {len(e2e)} de las cuatro fuentes + integridad de las listas. "
               f"Titulos de prueba: {base!r}, {analyst!r}, {senior!r}.")
    if omitidos:
        resumen += (f" {omitidos} casos omitidos: el perfil trae vacia alguna lista "
                    "(empresas, nivel analyst, ubicacion, part time) o el piso de part "
                    "time no es mas bajo que el de full time.")
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
