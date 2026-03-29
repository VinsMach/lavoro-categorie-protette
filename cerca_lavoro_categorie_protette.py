#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CERCA LAVORO — CATEGORIE PROTETTE L.68/99
Provincia di Lecce / Salento — v8

- Nessun DB
- Nessun file HTML
- Solo notifiche Telegram (un messaggio per annuncio)
- Gira su GitHub Actions ogni mattina

Fonti:
  ① InPA              portale.inpa.gov.it  API + fallback scraping
  ② Gazzetta Uff.     RSS 3a serie concorsi
  ③ ARPAL Puglia      collocamento mirato
  ④ ASL Lecce         sanita.puglia.it + csselezioni.it
  ⑤ Comune di Lecce   sottodominio trasparenza
  ⑥ Provincia LE      provincia.le.it
  ⑦ UniSalento        trasparenza.unisalento.it
  ⑧ concorsipubblici.com/lecce
  ⑨ concorsando.it/lecce + categorie-protette
  ⑩ ticonsiglio.com/puglia + categorie-protette
  ⑪ concorsi.it/ASL-Lecce
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
import urllib3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================
# CONFIG — tutto da variabili d'ambiente GitHub Secrets
# ============================================================

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

TIMEOUT      = 20
TIMEOUT_FAST = 12
SLEEP        = 1.2
MAX_DETTAGLI = 40          # quante pagine di dettaglio aprire
GIORNI_MAX   = 120         # scarta annunci pubblicati da oltre N giorni
GIORNI_SCAD  = 7           # tolleranza scadenza (annunci scaduti da max N giorni)


# ============================================================
# PAROLE CHIAVE
# ============================================================

POSITIVE_CP = [
    "categorie protette", "categoria protetta",
    "l.68/99", "l. 68/99", "legge 68/99", "legge 68/1999",
    "collocamento mirato", "art. 1", "articolo 1",
    "invalidità civile", "invalidita civile",
    "disabilità", "disabilita", "disabili",
    "riserva disabili", "posto riservato", "liste speciali",
    "art. 18", "articolo 18",
]

ART16_HINTS = [
    "art. 16", "articolo 16", "chiamata nominativa",
    "chiamata diretta", "avviamento a selezione", "accesso diretto",
    "l.r. 17/2005",
]

GEO_WORDS = [
    "lecce", "salento", "provincia di lecce", "leccese",
    "puglia", "apulia",
    "casarano", "copertino", "gallipoli", "galatina", "nardò", "nardo",
    "maglie", "otranto", "tricase", "ugento", "surbo", "taviano",
    "racale", "galatone", "squinzano", "trepuzzi", "leverano",
    "campi salentina", "monteroni", "novoli", "lequile",
    "asl lecce", "comune di lecce", "università del salento", "arpal puglia",
]

BANDO_WORDS = [
    "bando", "avviso pubblico", "concorso", "selezione pubblica",
    "reclutamento", "assunzione", "manifestazione di interesse",
]

NEGATIVE_WORDS = [
    "graduatoria finale", "elenco idonei", "esito finale",
    "ammessi alla prova", "non ammessi", "convocazione alla prova",
    "verbale della commissione", "rettifica bando",
    "nomina commissione", "tracce della prova",
    "diario delle prove", "utilizzo graduatoria",
    "interpello interno", "mobilità interna",
]

ITALIAN_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}


# ============================================================
# FONTI HTML
# ============================================================

FONTI_HTML: list[dict] = [
    {
        "nome": "Comune di Lecce",
        "fonte": "Comune di Lecce",
        "ente": "Comune di Lecce",
        "tipo": "PA",
        "ssl": True,
        "urls": [
            "https://amministrazionetrasparente.comune.lecce.it/amministrazione-trasparente/bandi-di-concorso",
            "https://www.comune.lecce.it/novita",
        ],
    },
    {
        "nome": "Provincia di Lecce",
        "fonte": "Provincia di Lecce",
        "ente": "Provincia di Lecce",
        "tipo": "PA",
        "ssl": False,
        "urls": [
            "https://www.provincia.le.it/categoria/selezioni-uniche/",
            "https://www.provincia.le.it/elenchi-di-idonei/",
        ],
    },
    {
        "nome": "ASL Lecce",
        "fonte": "ASL Lecce",
        "ente": "ASL Lecce",
        "tipo": "PA",
        "ssl": False,
        "urls": [
            "https://www.sanita.puglia.it/web/asl-lecce/bandi-di-concorso",
            "https://www.sanita.puglia.it/aol/listRepertorio?aziendaParam=asllecce",
            "https://www.csselezioni.it/asl-lecce/",
        ],
    },
    {
        "nome": "Università del Salento",
        "fonte": "UniSalento",
        "ente": "Università del Salento",
        "tipo": "PA",
        "ssl": True,
        "urls": [
            "https://trasparenza.unisalento.it/page/75/concorsi-attivi.html",
            "https://trasparenza.unisalento.it/page/5/bandi-di-concorso.html",
        ],
    },
    {
        "nome": "ARPAL Puglia",
        "fonte": "ARPAL Puglia",
        "ente": "ARPAL — Collocamento Mirato Lecce",
        "tipo": "ARPAL",
        "ssl": False,
        "urls": [
            "https://arpal.regione.puglia.it/servizi/persone/collocamento-mirato",
            "https://arpal.regione.puglia.it/notizie",
            "https://arpal.regione.puglia.it/",
        ],
    },
    {
        "nome": "concorsipubblici.com",
        "fonte": "concorsipubblici.com",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            "https://www.concorsipubblici.com/concorsi/regione/loc/lecce",
        ],
    },
    {
        "nome": "Concorsando Lecce",
        "fonte": "Concorsando.it",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            "https://www.concorsando.it/blog/concorsi-lecce/",
            "https://www.concorsando.it/blog/?s=categorie+protette+puglia",
            "https://www.concorsando.it/blog/concorsi-pubblici-per-categorie-protette/",
        ],
    },
    {
        "nome": "TiConsiglio Puglia",
        "fonte": "ticonsiglio.com",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            "https://www.ticonsiglio.com/concorsi-pubblici/concorsi-puglia/",
            "https://www.ticonsiglio.com/concorsi-pubblici/categorie-protette/",
        ],
    },
    {
        "nome": "concorsi.it — Lecce / L.68",
        "fonte": "concorsi.it",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            # ASL Lecce
            "https://www.concorsi.it/ente/35707-azienda-sanitaria-locale-di-lecce.html",
            # Ricerca "1999" = anno legge 68/99 — trova tutti i bandi CP
            "https://www.concorsi.it/risultati?ric=1999",
            # Puglia + categorie protette
            "https://www.concorsi.it/concorsi/regione/puglia/?q=categorie+protette",
        ],
    },
    {
        "nome": "MinInterno — GU Concorsi",
        "fonte": "mininterno.net",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            # Sezione GU tag 13 = bandi per categorie protette/disabili
            "https://www.mininterno.net/gu-tag-13",
        ],
    },
]


# ============================================================
# MODEL
# ============================================================

@dataclass
class Annuncio:
    titolo: str
    fonte: str
    url: str
    ente: str = ""
    tipo: str = ""
    data_pub: str = ""
    scadenza: str = ""
    posti: str = ""
    descrizione: str = ""
    testo: str = ""
    art1: bool = False
    art16: bool = False
    score: int = 0


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("cp_lecce")


# ============================================================
# HTTP
# ============================================================

def _build(verify: bool) -> requests.Session:
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=1, connect=1, read=1, backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",), raise_on_status=False,
    )))
    s.headers.update(HEADERS)
    s.verify = verify
    return s

S    = _build(True)
S_NO = _build(False)

def _s(ssl: bool) -> requests.Session:
    return S if ssl else S_NO

def get(url: str, ssl: bool = True, timeout: int = None,
        params: dict = None) -> Optional[requests.Response]:
    try:
        r = _s(ssl).get(url, timeout=timeout or TIMEOUT, params=params)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        time.sleep(SLEEP)
        return r
    except Exception as e:
        log.warning(f"GET {url} → {e}")
        return None

def soup(url: str, ssl: bool = True, timeout: int = None) -> Optional[BeautifulSoup]:
    r = get(url, ssl=ssl, timeout=timeout)
    return BeautifulSoup(r.text, "html.parser") if r else None

def jget(url: str, params: dict = None, ssl: bool = True) -> Optional[dict | list]:
    try:
        r = _s(ssl).get(url, params=params, timeout=TIMEOUT,
                        headers={**HEADERS, "Accept": "application/json"})
        r.raise_for_status()
        time.sleep(SLEEP)
        return r.json()
    except Exception as e:
        log.warning(f"JGET {url} → {e}")
        return None

def aurl(href: str, base: str) -> str:
    return urljoin(base, href) if href else ""


# ============================================================
# HELPERS TESTO
# ============================================================

def n(t: str) -> str:
    if not t: return ""
    return re.sub(r"\s+", " ", html.unescape(t).replace("\xa0", " ")).strip()

def has(text: str, words: list[str]) -> bool:
    low = n(text).lower()
    return any(w.lower() in low for w in words)

def geo(t):  return has(t, GEO_WORDS)
def cp(t):   return has(t, POSITIVE_CP)
def a16(t):  return has(t, ART16_HINTS)
def bando(t):return has(t, BANDO_WORDS)
def neg(t):  return has(t, NEGATIVE_WORDS)


def dates(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"\b([0-3]?\d)[/\-.]([01]?\d)[/\-.](20\d{2})\b", text):
        try:
            out.append(datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).strftime("%Y-%m-%d"))
        except ValueError: pass
    for m in re.finditer(
        r"\b([0-3]?\d)\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|"
        r"agosto|settembre|ottobre|novembre|dicembre)\s+(20\d{2})\b", text.lower()):
        try:
            out.append(datetime(int(m.group(3)), ITALIAN_MONTHS[m.group(2)], int(m.group(1))).strftime("%Y-%m-%d"))
        except ValueError: pass
    return list(dict.fromkeys(out))

def first_date(t: str) -> str:
    d = dates(t); return d[0] if d else ""

def scad_from(t: str) -> str:
    for p in [
        r"(?:scadenza|termine(?:\s+di)?\s+presentazione|entro(?:\s+il)?)[:\s]+([0-3]?\d[/\-.][01]?\d[/\-.]20\d{2})",
        r"(?:scadenza|entro(?:\s+il)?)[:\s]+([0-3]?\d\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s+20\d{2})",
    ]:
        m = re.search(p, t.lower())
        if m: return first_date(m.group(1))
    return ""

def posti_from(t: str) -> str:
    for p in [r"\b(?:n\.?|nr\.?|numero)\s*(\d+)\s*(?:posti?|unità|unita)\b",
              r"\b(\d+)\s*(?:posti?|unità|unita)\b"]:
        m = re.search(p, t.lower())
        if m: return f"{m.group(1)} posti"
    return ""

def to_dt(s: str) -> Optional[datetime]:
    for f in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try: return datetime.strptime(s, f)
        except (ValueError, TypeError): pass
    return None

def expired(s: str) -> bool:
    dt = to_dt(s)
    return bool(dt and dt < datetime.now() - timedelta(days=GIORNI_SCAD))

def too_old(s: str) -> bool:
    if not s: return False
    dt = to_dt(s)
    return bool(dt and dt < datetime.now() - timedelta(days=GIORNI_MAX))

def page_text(pg: BeautifulSoup) -> str:
    for t in pg.select("script,style,nav,footer,header,noscript,iframe,.cookie,.menu"):
        t.decompose()
    main = (pg.select_one("main") or pg.select_one("article")
            or pg.select_one(".content") or pg.select_one(".entry-content") or pg.body)
    if not main: return ""
    out, prev = [], None
    for line in [n(x) for x in main.get_text("\n", strip=True).splitlines()]:
        if len(line) >= 3 and line != prev:
            out.append(line)
        prev = line
    return "\n".join(out)[:8000]


# ============================================================
# SCORING & FINALIZE
# ============================================================

def score(a: Annuncio) -> int:
    t = " ".join([a.titolo, a.descrizione, a.testo, a.ente, a.fonte, a.url]).lower()
    if neg(t): return -90
    s = 0
    if cp(t):    s += 12
    if a16(t):   s += 8
    if bando(t): s += 4
    if geo(t):   s += 4
    if "lecce" in t: s += 3
    if a.tipo == "ARPAL": s += 2
    if "aperto" in t:  s += 3
    if "chiuso" in t:  s -= 4
    return s

def finalize(a: Annuncio) -> Annuncio:
    t = " ".join([a.titolo, a.descrizione, a.testo, a.url, a.ente])
    if not a.data_pub: a.data_pub = first_date(t)
    if not a.scadenza: a.scadenza = scad_from(t)
    if not a.posti:    a.posti    = posti_from(t)
    if not a.art1:     a.art1     = cp(t)
    if not a.art16:    a.art16    = a16(t)
    a.score = score(a)
    return a


# ============================================================
# DEDUP
# ============================================================

def dedup(items: list[Annuncio]) -> list[Annuncio]:
    seen: set[str] = set()
    out = []
    for a in items:
        if a.url and a.url not in seen:
            seen.add(a.url)
            out.append(a)
    return out


# ============================================================
# ① InPA — pagine pubbliche /bandi-e-avvisi/ con filtri URL
#   regioneId=13 = Puglia  |  status=OPEN = aperti
#   L'API portale.inpa.gov.it richiede autenticazione (401)
# ============================================================

# URL pubblici InPA con filtri — regioneId 13 = Puglia
INPA_URLS = [
    # Puglia + "categorie protette"
    "https://www.inpa.gov.it/bandi-e-avvisi/?text=categorie+protette&regioneId=13&status=OPEN&page_num=0",
    # Puglia + "68/99" (riferimento legge)
    "https://www.inpa.gov.it/bandi-e-avvisi/?text=68%2F99&regioneId=13&status=OPEN&page_num=0",
    # Puglia + "art. 1" (art.1 L.68)
    "https://www.inpa.gov.it/bandi-e-avvisi/?text=art.+1&regioneId=13&status=OPEN&page_num=0",
    # Puglia + "collocamento mirato"
    "https://www.inpa.gov.it/bandi-e-avvisi/?text=collocamento+mirato&regioneId=13&status=OPEN&page_num=0",
    # Puglia + "1999" (anno legge)
    "https://www.inpa.gov.it/bandi-e-avvisi/?text=1999&regioneId=13&status=OPEN&page_num=0",
    # Tutti i bandi aperti in Puglia (per non perdere nulla)
    "https://www.inpa.gov.it/bandi-e-avvisi/?regioneId=13&status=OPEN&page_num=0",
]

def _inpa_parse_page(pg: BeautifulSoup, base: str) -> list[Annuncio]:
    """Estrae annunci da una pagina InPA."""
    out = []
    # InPA è una SPA React — il contenuto può essere in diversi contenitori
    for sel in [
        "article", ".bando-card", ".job-card", ".opportunity-card",
        ".card", "li.bando", "[class*='bando']", "[class*='concorso']",
        # fallback: qualsiasi link con testo lungo
        "a[href]",
    ]:
        for el in pg.select(sel):
            lnk = el if el.name == "a" else el.select_one("a[href]")
            tit = el.select_one("h2, h3, h4, .title, .titolo, strong") or lnk
            if not tit or not lnk:
                continue
            titolo = n(tit.get_text())
            ctx    = n(el.get_text(" "))
            href   = aurl(lnk.get("href", ""), base)
            if len(titolo) < 10 or not href:
                continue
            if neg(ctx):
                continue
            a = Annuncio(
                titolo=titolo[:240], fonte="InPA", url=href,
                ente="PA", tipo="PA",
                data_pub=first_date(ctx), descrizione=ctx[:400],
            )
            finalize(a)
            out.append(a)
    return out


def scrape_inpa() -> list[Annuncio]:
    log.info("📋 InPA (pagine pubbliche /bandi-e-avvisi/)...")
    items = []
    seen_href: set[str] = set()
    BASE = "https://www.inpa.gov.it"

    for url in INPA_URLS:
        pg = soup(url, ssl=True, timeout=TIMEOUT)
        if not pg:
            continue
        candidati = _inpa_parse_page(pg, BASE)
        for a in candidati:
            if a.url in seen_href:
                continue
            seen_href.add(a.url)
            # Score minimo 8 — meno restrittivo perché siamo già filtrati per Puglia
            if a.score >= 8:
                items.append(a)

    log.info(f"   → {len(items)} InPA")
    return items


# ============================================================
# ② Gazzetta Ufficiale
# ============================================================

def scrape_gu() -> list[Annuncio]:
    log.info("📰 Gazzetta Ufficiale RSS...")
    items = []
    for rss in ["https://www.gazzettaufficiale.it/rss/concorsi.xml",
                "https://www.gazzettaufficiale.it/rss/esecutivi.xml"]:
        try:
            feed = feedparser.parse(rss)
        except Exception as e:
            log.warning(f"RSS {rss} → {e}"); continue
        for e in feed.entries[:100]:
            titolo = n(e.get("title", ""))
            summ   = n(BeautifulSoup(e.get("summary", ""), "html.parser").get_text())
            link   = e.get("link", "")
            pub    = datetime(*e.published_parsed[:6]).strftime("%Y-%m-%d") if e.get("published_parsed") else ""
            whole  = f"{titolo} {summ}"
            if neg(whole): continue
            if not (cp(whole) or a16(whole)): continue
            if not geo(whole): continue
            a = Annuncio(titolo=titolo, fonte="Gazzetta Ufficiale", url=link,
                         ente="PA", tipo="PA", data_pub=pub, descrizione=summ[:400])
            finalize(a)
            if a.score >= 12: items.append(a)
    log.info(f"   → {len(items)} GU")
    return items


# ============================================================
# ③–⑪ Scraper generico HTML
# ============================================================

def _candidato(tipo: str, text: str) -> bool:
    low = text.lower()
    if len(low) < 20 or neg(low): return False
    if tipo == "ARPAL":       return cp(low) or a16(low) or "collocamento mirato" in low
    if tipo == "AGGREGATORE": return bando(low) and (cp(low) or a16(low) or "lecce" in low or "puglia" in low)
    return bando(low) and (cp(low) or a16(low)) and geo(low)

def scrape_fonte(fonte: dict) -> list[Annuncio]:
    log.info(f"🏛️  {fonte['nome']}...")
    ssl, tipo, out = fonte.get("ssl", False), fonte["tipo"], []
    seen_local: set[str] = set()
    for url in fonte["urls"]:
        pg = soup(url, ssl=ssl, timeout=TIMEOUT_FAST)
        if not pg: continue
        for sel in ["article", "tr", "li", ".card", ".entry", ".item", "a[href]"]:
            for el in pg.select(sel):
                lnk = el if el.name == "a" else el.select_one("a[href]")
                if not lnk: continue
                href   = aurl(lnk.get("href", ""), url)
                titolo = n(lnk.get_text(" ", strip=True))
                ctx    = n(el.get_text(" ", strip=True))
                if href in seen_local or len(titolo) < 8: continue
                seen_local.add(href)
                if not _candidato(tipo, f"{titolo} {ctx} {href}"): continue
                a = Annuncio(titolo=titolo[:240], fonte=fonte["fonte"], url=href,
                             ente=fonte["ente"], tipo=tipo,
                             data_pub=first_date(ctx), descrizione=ctx[:500])
                finalize(a)
                out.append(a)
    log.info(f"   → {len(out)} candidati")
    return out


# ============================================================
# DETTAGLIO — apre la pagina e scarica il testo
# ============================================================

def fetch_detail(a: Annuncio) -> Annuncio:
    if not a.url or len(a.url) < 12: return a
    if any(a.url.lower().endswith(e) for e in [".pdf", ".doc", ".docx", ".zip"]): return a
    use_ssl = any(d in a.url for d in [
        "inpa.gov.it", "gazzettaufficiale.it", "concorsando.it",
        "concorsipubblici.com", "concorsi.it", "ticonsiglio.com", "unisalento.it",
    ])
    r = get(a.url, ssl=use_ssl)
    if not r: return a
    if "pdf" in (r.headers.get("Content-Type") or "").lower(): return a
    a.testo = page_text(BeautifulSoup(r.text, "html.parser"))
    return finalize(a)

def fetch_best(items: list[Annuncio]) -> None:
    ordered = sorted(items, key=lambda x: (x.art16, x.art1, x.score), reverse=True)
    log.info(f"⬇️  Dettagli per i migliori {min(MAX_DETTAGLI, len(ordered))} annunci...")
    for i, a in enumerate(ordered[:MAX_DETTAGLI], 1):
        log.info(f"   [{i}/{min(MAX_DETTAGLI, len(ordered))}] {a.titolo[:75]}")
        fetch_detail(a)


# ============================================================
# FILTRO FINALE
# ============================================================

# Parole che indicano laurea obbligatoria — candidata ha solo diploma
RICHIEDE_LAUREA = [
    "laurea magistrale", "laurea specialistica", "laurea triennale",
    "laureati", "laurea in ", "in possesso di laurea",
    "dirigente medico", "dirigenti medici",
    "medico", "odontoiatra", "farmacista", "veterinario",
    "ingegnere", "architetto", "avvocato",
    "dottorato", "master universitario",
]

# Parole che confermano accessibilità con diploma
OK_DIPLOMA = [
    "diploma", "scuola secondaria superiore", "maturità",
    "licenza media", "scuola dell'obbligo",
    "istruttore", "collaboratore", "assistente",
    "operatore", "addetto", "esecutore",
    "categoria b", "categoria c", "area b", "area c",
    "area degli assistenti", "area degli operatori",
]


def richiede_solo_laurea(t: str) -> bool:
    """True se il testo indica chiaramente che serve la laurea."""
    low = t.lower()
    # se menziona diploma, è probabilmente accessibile
    if has(low, OK_DIPLOMA):
        return False
    # se menziona laurea senza diploma, escludi
    return has(low, RICHIEDE_LAUREA)


def filtra(items: list[Annuncio]) -> list[Annuncio]:
    out = []
    scartati_laurea = 0
    for a in items:
        finalize(a)
        t = " ".join([a.titolo, a.descrizione, a.testo, a.url]).lower()
        if neg(t): continue
        if expired(a.scadenza): continue
        if too_old(a.data_pub): continue
        if a.tipo not in ("ARPAL", "AGGREGATORE") and not geo(t): continue
        if a.score < 12: continue
        if not (cp(t) or a16(t)): continue
        # Filtra bandi che richiedono esplicitamente la laurea
        if richiede_solo_laurea(t):
            scartati_laurea += 1
            log.debug(f"Escluso (laurea): {a.titolo[:60]}")
            continue
        out.append(a)
    if scartati_laurea:
        log.info(f"   Scartati per requisito laurea: {scartati_laurea}")
    out.sort(key=lambda x: (x.art16, x.art1, to_dt(x.data_pub) or datetime.min, x.score), reverse=True)
    return out


# ============================================================
# TELEGRAM — un messaggio per annuncio + riepilogo iniziale
# ============================================================

def tg(text: str) -> None:
    """Invia un singolo messaggio Telegram (testo puro, max 4096 char)."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        r = S.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID,
                  "text": text[:4096],
                  "disable_web_page_preview": True},
            timeout=12,
        )
        r.raise_for_status()
    except Exception as e:
        log.error(f"Telegram → {e}")


def invia_telegram(items: list[Annuncio]) -> None:
    if not items:
        tg("Categorie Protette Lecce\nNessun nuovo annuncio oggi.")
        return

    art16 = [a for a in items if a.art16]
    art1  = [a for a in items if a.art1 and not a.art16]
    altri = [a for a in items if not a.art1 and not a.art16]

    # ── Messaggio 1: riepilogo ──────────────────────────────
    tg(
        f"Categorie Protette — Lecce / Salento\n"
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"Annunci trovati: {len(items)}\n"
        f"  Art.16 accesso diretto: {len(art16)}\n"
        f"  Art.1 L.68/99:         {len(art1)}\n"
        f"  Altri concorsi PA:     {len(altri)}"
    )
    time.sleep(0.5)

    # ── Messaggi singoli per ogni annuncio ──────────────────
    for a in items:
        tipo_label = (
            "[ART.16 ACCESSO DIRETTO]" if a.art16 else
            "[ART.1 L.68/99]"          if a.art1  else
            "[PA]"
        )

        # Estratto del testo: prime 3 righe rilevanti
        estratto = ""
        if a.testo:
            righe = [r for r in a.testo.splitlines() if len(r.strip()) > 20][:3]
            estratto = "\n".join(righe)
        elif a.descrizione:
            estratto = a.descrizione[:300]

        # Segnala se il bando sembra accessibile con diploma
        diploma_ok = has(
            " ".join([a.titolo, a.descrizione, a.testo]).lower(),
            OK_DIPLOMA
        )
        diploma_nota = " [diploma OK]" if diploma_ok else ""

        msg = (
            f"{tipo_label}{diploma_nota}\n"
            f"{a.titolo[:120]}\n\n"
            f"Ente: {a.ente or a.fonte}\n"
            + (f"Scadenza: {a.scadenza}\n" if a.scadenza else "")
            + (f"Posti:    {a.posti}\n"    if a.posti    else "")
            + (f"\n{estratto}\n"            if estratto   else "")
            + f"\n{a.url}"
        )
        tg(msg)
        time.sleep(0.4)   # anti-rate-limit Telegram

    log.info(f"Telegram: inviati {len(items) + 1} messaggi")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    print("\n" + "=" * 70)
    print("CERCA LAVORO — CATEGORIE PROTETTE — LECCE / SALENTO")
    print("=" * 70 + "\n")

    raw: list[Annuncio] = []

    # InPA
    try:
        raw.extend(scrape_inpa())
    except Exception as e:
        log.error(f"InPA → {e}")

    # Gazzetta Ufficiale
    try:
        raw.extend(scrape_gu())
    except Exception as e:
        log.error(f"GU → {e}")

    # Tutte le fonti HTML
    for fonte in FONTI_HTML:
        try:
            raw.extend(scrape_fonte(fonte))
        except Exception as e:
            log.error(f"{fonte['nome']} → {e}")

    log.info(f"Grezzo: {len(raw)}")

    # Dedup per URL
    stage1 = dedup(raw)
    log.info(f"Dopo dedup: {len(stage1)}")

    # Scarica dettagli dei migliori
    stage1.sort(key=lambda x: x.score, reverse=True)
    fetch_best(stage1)

    # Finalize + filtro finale
    final = filtra([finalize(a) for a in stage1])
    log.info(f"Pertinenti: {len(final)}")

    # Stampa a console (visibile nel log GitHub Actions)
    print(f"\nRisultati: {len(final)}")
    for a in final:
        label = "ART.16" if a.art16 else "ART.1" if a.art1 else "PA"
        print(f"  [{label}] {a.titolo[:70]}")
        if a.scadenza: print(f"         Scadenza: {a.scadenza}")
        print(f"         {a.url}")

    # Telegram
    invia_telegram(final)

    print(f"\nFatto — {len(final)} annunci inviati su Telegram")


if __name__ == "__main__":
    main()
