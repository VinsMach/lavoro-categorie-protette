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


# ============================================================
# PAROLE CHIAVE
# ============================================================

# Segnali generici di categorie protette (Art.1 + Art.18 insieme)
POSITIVE_CP = [
    "categorie protette", "categoria protetta",
    "l.68/99", "l. 68/99", "legge 68/99", "legge 68/1999",
    "collocamento mirato",
    "invalidità civile", "invalidita civile",
    "disabilità", "disabilita", "disabili",
    "riserva disabili", "posto riservato", "liste speciali",
    # Art.1 esplicito — NON include art.18
    "art. 1", "art.1", "articolo 1",
    "comma 1", "comma 2", "comma 3",       # commi tipici art.1 L.68
]

# Segnali che indicano SOLO Art.18 (orfani, vedove, vittime terrorismo)
# → NON accessibili con invalidità civile
ART18_ONLY = [
    "art. 18", "art.18", "articolo 18",
    "orfani", "orfano", "orfane",
    "vedove di guerra", "vedova di guerra",
    "vittime del terrorismo", "vittime di terrorismo",
    "vittime del dovere",
    "profughi", "rifugiati politici",
    "legge 407/1998", "l. 407/1998", "l.407/98",  # altra norma CP art.18
]

ART16_HINTS = [
    "art. 16", "art.16", "articolo 16",
    "chiamata nominativa", "chiamata diretta",
    "avviamento a selezione", "accesso diretto",
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
            # ASL Lecce (verificato)
            "https://www.concorsi.it/ente/35707-azienda-sanitaria-locale-di-lecce.html",
            # Ricerca "1999" = anno legge 68/99 (verificato)
            "https://www.concorsi.it/risultati?ric=1999",
            # FIX: rimosso /concorsi/regione/puglia/?q= → 404
            # Usa la ricerca testuale che funziona
            "https://www.concorsi.it/risultati?ric=categorie+protette+puglia",
            "https://www.concorsi.it/risultati?ric=art+1+legge+68+puglia",
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


def art1_match(text: str) -> bool:
    """
    Cerca tutte le varianti di "Art.1 L.68/99" nel testo:
      art. 1 / art.1 / art 1 / art1 / articolo 1 / article 1
    Tutte con o senza spazi, punteggiatura opzionale.
    """
    low = n(text).lower()
    # Regex: "art" + spazi/punti opzionali + "1" non seguito da altra cifra
    # Cattura: art1, art.1, art. 1, art 1, art  1
    if re.search(r'art[\.\s]*1(?!\d)', low):
        return True
    # Forma estesa
    if "articolo 1" in low or "articolo1" in low:
        return True
    return False


def art18_only(text: str) -> bool:
    """
    True se il testo menziona Art.18 MA NON Art.1.
    Questi bandi sono riservati a categorie (orfani, vedove, ecc.)
    che non includono l'invalidità civile Art.1 L.68/99.
    """
    low = n(text).lower()
    ha_art18 = has(low, ART18_ONLY)
    ha_art1  = art1_match(low)
    return ha_art18 and not ha_art1


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
    """Estrae data di scadenza con pattern multipli e robusti."""
    tl = t.lower()
    MESI = {
        'gennaio': 1, 'febbraio': 2, 'marzo': 3, 'aprile': 4,
        'maggio': 5, 'giugno': 6, 'luglio': 7, 'agosto': 8,
        'settembre': 9, 'ottobre': 10, 'novembre': 11, 'dicembre': 12,
    }
    MESI_PAT = '|'.join(MESI.keys())

    def parse_dmy(s):
        s = s.replace(' ', '')
        for fmt in ('%d/%m/%Y', '%d-%m-%Y', '%d.%m.%Y'):
            try:
                return datetime.strptime(s, fmt).strftime('%Y-%m-%d')
            except ValueError:
                pass
        return ''

    # 1. keyword + dd/mm/yyyy (con qualsiasi separatore / - .)
    m = re.search(
        r'(?:scadenza|scad\.?|termine(?:\s+\w+)*|entro(?:\s+il)?'
        r'|chiusura\s+candidature|data(?:\s+di)?\s+(?:chiusura|scadenza))'
        r'[\s:]+([0-3]?\d[\s]*[/\-\.][ ]*[01]?\d[\s]*[/\-\.][ ]*20\d{2})',
        tl
    )
    if m:
        d = parse_dmy(m.group(1))
        if d: return d

    # 2. keyword + mese italiano
    m = re.search(
        r'(?:scadenza|scad\.?|termine(?:\s+\w+)*|entro(?:\s+il)?|chiusura)'
        r'[\s:]+([0-3]?\d\s+(?:' + MESI_PAT + r')\s+20\d{2})',
        tl
    )
    if m:
        parts = m.group(1).split()
        try:
            return datetime(int(parts[2]), MESI[parts[1]], int(parts[0])).strftime('%Y-%m-%d')
        except (ValueError, KeyError, IndexError):
            pass

    # 3. "fino al dd/mm/yyyy"
    m = re.search(
        r'fino\s+al\s+([0-3]?\d[\s]*[/\-\.][ ]*[01]?\d[\s]*[/\-\.][ ]*20\d{2})',
        tl
    )
    if m:
        d = parse_dmy(m.group(1))
        if d: return d

    # 4. data ISO yyyy-mm-dd — cerca keyword nelle 60 chars precedenti
    for m in re.finditer(r'(20\d{2}-[01]\d-[0-3]\d)', tl):
        before = tl[max(0, m.start() - 60):m.start()]
        if re.search(r'scadenza|termine|entro|chiusura|scad\.?|fino\s+al', before):
            return m.group(1)

    return ""

def bando_chiuso_nel_testo(t: str) -> bool:
    """Rileva bandi chiusi/conclusi da frasi tipiche nel testo."""
    segnali = [
        "termini scaduti", "termine scaduto",
        "candidature chiuse", "candidature concluse",
        "procedura conclusa", "selezione conclusa",
        "bando scaduto", "avviso scaduto",
        "graduatoria definitiva approvata",
        "assunzione effettuata", "posto coperto",
        "procedura archiviata", "procedura chiusa",
        "asta chiusa", "asta conclusa",
        "avviamento effettuato", "avviamento concluso",
        "non ci sono aste aperte",
        "nessuna asta aperta", "nessun avviso attivo",
        "al momento non sono presenti",
        "non sono presenti avvisi",
    ]
    return has(t, segnali)

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
    """True se la data è passata. Nessuna tolleranza — scaduto = scaduto."""
    dt = to_dt(s)
    if not dt:
        return False
    # Confronta solo la data (non l'ora) per evitare falsi positivi
    # dovuti a bandi che scadono alle 23:59 del giorno indicato
    return dt.date() < datetime.now().date()

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
    # Art.1: usa la regex robusta (cattura art1/art.1/art 1/articolo 1)
    if not a.art1:     a.art1     = art1_match(t)
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
# ③ Aste Art.16 — portali regionali di tutta Italia
#
# L'Art.16 L.56/87 è l'accesso DIRETTO alla PA (senza concorso,
# solo licenza media richiesta). Ogni regione gestisce le proprie
# aste tramite il CPI / agenzia regionale per il lavoro.
#
# Priorità assoluta: Puglia (ARPAL + SINTESI Lecce)
# Poi tutte le altre regioni per completezza nazionale.
# ============================================================

ASTE_ART16_FONTI = [
    # ── PUGLIA — URL ESATTO fornito ────────────────────────────
    {
        "nome": "SINTESI Puglia — Art.16 Lecce",
        "url":  "https://sintesi.regione.puglia.it/web/sintesi-lecce/articolo-16",
        "ssl":  False,
    },
    # Le altre province pugliesi (comodo averle per confronto)
    {
        "nome": "SINTESI Puglia — Art.16 Bari",
        "url":  "https://sintesi.regione.puglia.it/web/sintesi-bari/articolo-16",
        "ssl":  False,
    },
    {
        "nome": "SINTESI Puglia — Art.16 Brindisi",
        "url":  "https://sintesi.regione.puglia.it/web/sintesi-brindisi/articolo-16",
        "ssl":  False,
    },
    {
        "nome": "SINTESI Puglia — Art.16 Taranto",
        "url":  "https://sintesi.regione.puglia.it/web/sintesi-taranto/articolo-16",
        "ssl":  False,
    },
    {
        "nome": "SINTESI Puglia — Art.16 Foggia",
        "url":  "https://sintesi.regione.puglia.it/web/sintesi-foggia/articolo-16",
        "ssl":  False,
    },
    # ── ABRUZZO ────────────────────────────────────────────────
    {
        "nome": "Abruzzo — SELFI selezioni",
        "url":  "https://selfi.regione.abruzzo.it/menu_items/selezioni",
        "ssl":  True,
    },
    # ── BASILICATA ─────────────────────────────────────────────
    {
        "nome": "Basilicata — Agenzia Regionale Lab",
        "url":  "https://www.agenziaregionalelab.it/category/avvisi-e-bandi-c-p-i/",
        "ssl":  True,
    },
    # ── EMILIA-ROMAGNA — mappa nazionale ───────────────────────
    {
        "nome": "Emilia-Romagna — Mappa aste Art.16",
        "url":  "https://www.agenzialavoro.emr.it/mappe-aste-art-16",
        "ssl":  True,
    },
    # ── FRIULI-VENEZIA GIULIA ───────────────────────────────────
    {
        "nome": "Friuli VG — Bandi avvisi CPI",
        "url":  "https://www.regione.fvg.it/rafvg/cms/RAFVG/MODULI/bandi_avvisi/?p=cpi",
        "ssl":  True,
    },
    # ── LAZIO ──────────────────────────────────────────────────
    {
        "nome": "Lazio — Avviamento Art.16 L.56/87",
        "url":  "https://www.regione.lazio.it/cittadini/lavoro/offerte-di-lavoro-bandi-avvisi/Avviamento-ex-art-16-L56-1987",
        "ssl":  True,
    },
    # ── LIGURIA — 4 province ────────────────────────────────────
    {
        "nome": "Liguria — Chiamate Art.16 Genova",
        "url":  "https://www.regione.liguria.it/homepage-lavoro/come-fare-per/chiamate-pubbliche/chiamate-exart16-ge.html",
        "ssl":  True,
    },
    {
        "nome": "Liguria — Chiamate Art.16 Imperia",
        "url":  "https://www.regione.liguria.it/homepage-lavoro/come-fare-per/chiamate-pubbliche/chiamate-exart16-im.html",
        "ssl":  True,
    },
    {
        "nome": "Liguria — Chiamate Art.16 La Spezia",
        "url":  "https://www.regione.liguria.it/homepage-lavoro/come-fare-per/chiamate-pubbliche/chiamate-exart16-sp.html",
        "ssl":  True,
    },
    {
        "nome": "Liguria — Chiamate Art.16 Savona",
        "url":  "https://www.regione.liguria.it/homepage-lavoro/come-fare-per/chiamate-pubbliche/chiamate-exart16-sa.html",
        "ssl":  True,
    },
    # ── MARCHE ─────────────────────────────────────────────────
    {
        "nome": "Marche — JANET Art.16 avvisi pubblici",
        "url":  "https://janet.regione.marche.it/Art16_AvvisiPubblici/Articolo16/Ricerca",
        "ssl":  True,
    },
    # ── PIEMONTE ───────────────────────────────────────────────
    {
        "nome": "Piemonte — Agenzia Piemonte Lavoro chiamate",
        "url":  "https://agenziapiemontelavoro.it/scheda-informativa/offerte-di-lavoro/chiamata-pubblica/chiamata-pubblica-chiamate-integrate/",
        "ssl":  True,
    },
    # ── SARDEGNA ───────────────────────────────────────────────
    {
        "nome": "Sardegna — Agenzia Reg. Lavoro Art.16",
        "url":  "https://agenziaregionaleperillavoro.regione.sardegna.it/index.php?xsl=2362&s=44&v=9&c=93504&nodesc=2&c1=4920&tipodoc=2&tipoconc=2",
        "ssl":  False,
    },
    # ── TOSCANA ────────────────────────────────────────────────
    {
        "nome": "Toscana — ARTI avvisi pubblici altri enti",
        "url":  "https://arti.toscana.it/avvisi-pubblici-degli-altri-enti",
        "ssl":  True,
    },
    # ── TRENTINO-ALTO ADIGE ─────────────────────────────────────
    {
        "nome": "Trentino — Concorsi e selezioni",
        "url":  "https://www.regione.taa.it/Documenti/Concorsi-e-selezioni",
        "ssl":  True,
    },
    # ── UMBRIA — 6 territori ────────────────────────────────────
    {
        "nome": "Umbria — Avvisi Art.16 Perugia",
        "url":  "https://www.arpalumbria.it/territorio-perugia-avvisi-attivi-selezione-lavoro-nella-pubblica-amministrazione",
        "ssl":  True,
    },
    {
        "nome": "Umbria — Avvisi Art.16 Foligno",
        "url":  "https://www.arpalumbria.it/territorio-foligno-avvisi-attivi-selezione-lavoro-nella-pubblica-amministrazione",
        "ssl":  True,
    },
    {
        "nome": "Umbria — Avvisi Art.16 Città di Castello",
        "url":  "https://www.arpalumbria.it/territorio-citta-di-castello-avvisi-attivi-selezione-lavoro-nella-pubblica-amministrazione",
        "ssl":  True,
    },
    {
        "nome": "Umbria — Avvisi Art.16 Terni",
        "url":  "https://www.arpalumbria.it/territorio-terni-avvisi-attivi-selezione-lavoro-nella-pubblica-amministrazione",
        "ssl":  True,
    },
    {
        "nome": "Umbria — Avvisi Art.16 Orvieto",
        "url":  "https://www.arpalumbria.it/territorio-orvieto-avvisi-attivi-selezione-lavoro-nella-pubblica-amministrazione",
        "ssl":  True,
    },
    {
        "nome": "Umbria — Avvisi Art.16 regionale",
        "url":  "https://www.arpalumbria.it/territorio-regionale-avvisi-attivi-selezione-lavoro-nella-pubblica-amministrazione",
        "ssl":  True,
    },
    # ── VALLE D'AOSTA ───────────────────────────────────────────
    {
        "nome": "Valle d'Aosta — Chiamate pubbliche attive",
        "url":  "https://lavoro.regione.vda.it/cittadini/lavoro/chiamate-pubbliche/chiamate-pubbliche-attive",
        "ssl":  True,
    },
    # ── VENETO ─────────────────────────────────────────────────
    {
        "nome": "Veneto — Assunzioni PA ex Art.16 L.56/87",
        "url":  "https://concorsi.regione.veneto.it/home/assunzioni-nella-pubblica-amministrazione-ex-art-16-l-56-87",
        "ssl":  True,
    },
    # ── CALABRIA (confermato funzionante) ──────────────────────
    {
        "nome": "Calabria — Avviamenti Art.16",
        "url":  "https://lavoro.regione.calabria.it/offerte-di-lavoro/aste-articolo-16/",
        "ssl":  False,
    },
]

def scrape_aste_art16() -> list[Annuncio]:
    """
    Scarica le aste Art.16 da tutti i portali regionali italiani.
    L'Art.16 L.56/87 = accesso diretto alla PA, solo licenza media,
    riservato agli iscritti alle liste di collocamento.
    NON confondere con Art.16 L.68/99 (collocamento mirato disabili).
    Qui intendiamo Art.16 L.56/87 nella sua accezione di
    chiamata numerica dagli iscritti al collocamento.
    """
    log.info("⭐ Aste Art.16 L.56/87 — portali regionali...")
    items = []
    seen_href: set[str] = set()

    for fonte in ASTE_ART16_FONTI:
        nome = fonte["nome"]
        url  = fonte["url"]
        ssl  = fonte["ssl"]

        pg = soup(url, ssl=ssl, timeout=TIMEOUT_FAST)
        if not pg:
            continue

        # Cerca tutti i link/item nella pagina
        trovati_qui = 0
        for sel in ["article", "li", "tr", ".entry", ".avviso", ".bando",
                    "div.item", "a[href]"]:
            for el in pg.select(sel):
                lnk = el if el.name == "a" else el.select_one("a[href]")
                if not lnk:
                    continue
                href   = aurl(lnk.get("href", ""), url)
                titolo = n(lnk.get_text(" ", strip=True))
                ctx    = n(el.get_text(" ", strip=True))
                full   = f"{titolo} {ctx} {href}".lower()

                if href in seen_href or len(titolo) < 8:
                    continue
                # Salta link di navigazione chiari
                if any(nav in titolo.lower() for nav in [
                    "home", "privacy", "contatti", "newsletter",
                    "mappa", "geolocalizzazione", "indietro", "vai al",
                    "come accedere", "compilare il modulo",
                ]):
                    continue
                # Salta se è chiaramente una graduatoria/esito già concluso
                if neg(full):
                    continue

                seen_href.add(href)

                # Ogni link su questi portali È per definizione Art.16
                # Aggiusta il tipo e i flag
                a = Annuncio(
                    titolo=titolo[:240],
                    fonte=nome,
                    url=href,
                    ente=nome,
                    tipo="ART16",        # tipo dedicato per aste art.16
                    data_pub=first_date(ctx),
                    descrizione=ctx[:400],
                    art16=True,          # per definizione
                    art1=False,
                )
                finalize(a)
                items.append(a)
                trovati_qui += 1

        if trovati_qui:
            log.info(f"   ✓ {nome}: {trovati_qui} candidati")

    log.info(f"   → {len(items)} aste Art.16 totali")
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
    finalize(a)
    # Tenta di estrarre/aggiornare la scadenza dal testo completo
    if not a.scadenza:
        a.scadenza = scad_from(a.testo)
    if a.scadenza:
        log.debug(f"   Scadenza estratta: {a.scadenza} — {a.titolo[:50]}")
    return a

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
    "medico specialista", "odontoiatra", "farmacista", "veterinario",
    "ingegnere", "architetto", "avvocato",
    "dottorato", "master universitario",
    "funzionario direttivo",   # area funzionari richiede laurea
]

# Profili amministrativi accessibili con diploma — CERCATI ATTIVAMENTE
PROFILI_AMMINISTRATIVI = [
    # Profili espliciti
    "istruttore amministrativo", "istruttore amm",
    "collaboratore amministrativo", "collaboratore amm",
    "assistente amministrativo", "assistente amm",
    "funzionario amministrativo",  # alcuni con diploma
    "addetto amministrativo", "addetto amm",
    "impiegato di concetto", "impiegato amm",
    "operatore amministrativo",
    # Tribunali e giustizia
    "operatore giudiziario", "operatrice giudiziaria",
    "assistente giudiziario", "assistente di cancelleria",
    "cancelliere", "ufficiale giudiziario",
    "addetto ufficio per il processo",
    "ministero della giustizia",
    "corte di appello", "tribunale",
    # Categorie contrattuali compatibili
    "area degli istruttori", "area istruttori",
    "area degli assistenti", "area assistenti",
    "area degli operatori", "area operatori",
    "categoria b", "categoria c",
    "area b", "area c",
    # Altre figure amministrative
    "segretario", "archivista", "protocollo",
    "ufficio personale", "ragioniere", "contabile",
    "sportello", "front office", "back office",
    "usciere", "messo comunale", "commesso",
    "centralinista",
]

# Parole che confermano accessibilità con diploma (più ampio)
OK_DIPLOMA = [
    "diploma", "scuola secondaria superiore", "maturità",
    "licenza media", "scuola dell'obbligo",
] + PROFILI_AMMINISTRATIVI


def profilo_ok(t: str) -> bool:
    """True se il bando riguarda un profilo amministrativo cercato."""
    return has(t, PROFILI_AMMINISTRATIVI)


def richiede_solo_laurea(t: str) -> bool:
    """True se il testo indica chiaramente che serve la laurea."""
    low = t.lower()
    if has(low, OK_DIPLOMA):
        return False
    return has(low, RICHIEDE_LAUREA)


def filtra(items: list[Annuncio]) -> list[Annuncio]:
    out = []
    sc_laurea = sc_art18 = sc_score = 0

    for a in items:
        finalize(a)
        t = " ".join([a.titolo, a.descrizione, a.testo, a.url]).lower()

        if neg(t):                                                  continue

        # ── Verifica scadenza ─────────────────────────────────
        # Se il testo completo è disponibile, prova a estrarre la
        # scadenza da lì (più accurato dello snippet iniziale)
        if a.testo and not a.scadenza:
            a.scadenza = scad_from(a.testo)
        # Ri-verifica anche dalla descrizione se ancora non trovata
        if not a.scadenza and a.descrizione:
            a.scadenza = scad_from(a.descrizione)

        # Scarta se la scadenza è passata
        if expired(a.scadenza):
            log.info(f"Scaduto ({a.scadenza}): {a.titolo[:55]}")
            continue

        # Scarta se il testo dice esplicitamente che è chiuso/concluso
        if bando_chiuso_nel_testo(t):
            log.info(f"Bando chiuso (testo): {a.titolo[:55]}")
            continue

        if too_old(a.data_pub):                                     continue
        if a.tipo not in ("ARPAL", "AGGREGATORE", "ART16") and not geo(t):  continue
        # Per le aste Art.16 L.56/87 abbassa la soglia score
        # (sono già filtrate per definizione sul portale)
        soglia = 0 if a.tipo == "ART16" else 12
        if a.score < soglia:
            sc_score += 1;                                          continue

        # Per le aste Art.16: tieni tutte MA filtra per profilo
        # se il titolo menziona esplicitamente un profilo NON amministrativo
        # (es. "manovratore", "operaio", "autista") scarta
        if a.tipo == "ART16":
            profili_tecnici = [
                "operaio", "manovratore", "autista", "conducente",
                "giardiniere", "netturbino", "spazzino", "ecologico",
                "idraulico", "elettricista", "falegname", "muratore",
                "cuoco", "cameriere", "custode", "guardia",
                "bidello", "collaboratore scolastico",
                "operatore ecologico",
            ]
            tit_low = a.titolo.lower()
            # Se il titolo menziona un profilo tecnico/manuale NON cercato
            # e non menziona nulla di amministrativo → salta
            if has(tit_low, profili_tecnici) and not profilo_ok(tit_low):
                log.debug(f"Skip profilo tecnico: {a.titolo[:60]}")
                continue

        # ── Filtro CP + Art.1 ─────────────────────────────────
        # Le aste ART16 passano direttamente, gli altri devono avere CP/Art16
        if a.tipo != "ART16" and not (cp(t) or a16(t)):            continue

        # ESCLUDI se il bando è riservato solo all'Art.18
        # (orfani, vedove, vittime terrorismo — categoria diversa)
        if art18_only(t):
            sc_art18 += 1
            log.info(f"Escluso Art.18 only: {a.titolo[:65]}")
            continue

        # Se il testo è lungo abbastanza (dettaglio scaricato) e non trova
        # alcuna variante di Art.1, è probabilmente solo Art.18 → escludi
        if len(t) > 300 and cp(t) and not art1_match(t) and not a16(t):
            sc_art18 += 1
            log.info(f"Escluso (no Art.1): {a.titolo[:65]}")
            continue

        # ── Filtro titolo di studio ───────────────────────────
        if richiede_solo_laurea(t):
            sc_laurea += 1
            log.info(f"Escluso (laurea):   {a.titolo[:65]}")
            continue

        # Aggiorna flag art1 con la regex robusta
        if not a.art1:
            a.art1 = art1_match(t)

        out.append(a)

    log.info(f"   Scartati — score basso: {sc_score} | "
             f"solo Art.18: {sc_art18} | laurea: {sc_laurea}")

    out.sort(key=lambda x: (
        x.art16, x.art1,
        to_dt(x.data_pub) or datetime.min,
        x.score,
    ), reverse=True)
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
    aste16 = [a for a in items if a.tipo == "ART16"]
    tg(
        f"Categorie Protette + Aste Art.16\n"
        f"{datetime.now().strftime('%d/%m/%Y %H:%M')}\n\n"
        f"Annunci trovati: {len(items)}\n"
        f"  Aste Art.16 L.56/87:   {len(aste16)}\n"
        f"  Art.16 accesso diretto:{len(art16)}\n"
        f"  Art.1 L.68/99:         {len(art1)}\n"
        f"  Altri concorsi PA:     {len(altri)}"
    )
    time.sleep(0.5)

    # ── Messaggi singoli per ogni annuncio ──────────────────
    for a in items:
        tipo_label = (
            "[ART.16 ASTA DIRETTA]"    if a.tipo == "ART16" else
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

        # Segnala profilo e diploma nel messaggio
        testo_ann = " ".join([a.titolo, a.descrizione, a.testo]).lower()
        p_ok  = profilo_ok(testo_ann)
        d_ok  = has(testo_ann, OK_DIPLOMA)
        note  = ""
        if p_ok:  note += " [profilo AMM]"
        elif d_ok: note += " [diploma OK]"

        msg = (
            f"{tipo_label}{note}\n"
            f"{a.titolo[:120]}\n\n"
            f"Ente: {a.ente or a.fonte}\n"
            + (f"Scadenza: {a.scadenza}\n" if a.scadenza else "Scadenza: da verificare\n")
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

    # Aste Art.16 — portali regionali Italia
    try:
        raw.extend(scrape_aste_art16())
    except Exception as e:
        log.error(f"Aste Art.16 → {e}")

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
