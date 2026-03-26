#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CERCA LAVORO — CATEGORIE PROTETTE L.68/99
Provincia di Lecce / Salento — v7

Fonti (tutte verificate, nessun comune diretto):
  ① InPA            portale.inpa.gov.it  — API REST ufficiale con filtri
  ② Gazzetta Uff.   RSS 3a serie concorsi
  ③ ARPAL Puglia    arpal.regione.puglia.it  (collocamento mirato)
  ④ ASL Lecce       sanita.puglia.it + csselezioni.it
  ⑤ Comune Lecce    sottodominio trasparenza verificato
  ⑥ Provincia LE    provincia.le.it
  ⑦ UniSalento      trasparenza.unisalento.it
  ⑧ concorsipubblici.com/lecce    (23 risultati nel test)
  ⑨ concorsando.it/lecce          (16 risultati nel test)
  ⑩ concorsando.it/categorie-protette  (nazionale, filtro geo)
  ⑪ ticonsiglio.com/concorsi-puglia     (nuovo — copre CP + Puglia)
  ⑫ concorsi.it/ASL-Lecce
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import smtplib
import sqlite3
import time
import urllib3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional
from urllib.parse import urljoin, urlencode

import feedparser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================
# CONFIG
# ============================================================

CONFIG = {
    "db_path":     "lavoro_lecce_cp.db",
    "output_json": "risultati_lecce.json",
    "output_txt":  "risultati_lecce.txt",
    "output_html": "risultati_lecce.html",

    "giorni_indietro": 120,
    "giorni_tolleranza_scadenza": 7,
    "timeout": 20,
    "timeout_fast": 12,
    "sleep": 1.2,
    "max_dettagli": 40,

    "email_destinatario": os.getenv("EMAIL_DESTINATARIO", ""),
    "email_mittente":     os.getenv("EMAIL_MITTENTE", ""),
    "email_password":     os.getenv("EMAIL_PASSWORD", ""),
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,

    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id":   os.getenv("TELEGRAM_CHAT_ID", ""),
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
# PAROLE CHIAVE
# ============================================================

POSITIVE_CP = [
    "categorie protette", "categoria protetta",
    "l.68/99", "l. 68/99", "legge 68/99", "legge 68/1999",
    "collocamento mirato", "art. 1", "articolo 1",
    "invalidità civile", "invalidita civile",
    "disabilità", "disabilita", "disabili",
    "riserva disabili", "posto riservato", "liste speciali",
    "art. 18", "articolo 18",          # altra norma L.68 spesso citata
]

ART16_HINTS = [
    "art. 16", "articolo 16", "chiamata nominativa",
    "chiamata diretta", "avviamento a selezione", "accesso diretto",
    "l.r. 17/2005", "lr 17/2005",
]

GEO_WORDS = [
    "lecce", "salento", "provincia di lecce", "leccese",
    "puglia", "apulia",
    "casarano", "copertino", "gallipoli", "galatina", "nardò", "nardo",
    "maglie", "otranto", "tricase", "ugento", "surbo", "taviano",
    "racale", "galatone", "squinzano", "trepuzzi", "leverano",
    "campi salentina", "monteroni", "novoli", "lequile",
    "asl lecce", "comune di lecce", "università del salento", "unisalento",
    "arpal puglia",
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

NAV_WORDS = [
    "home page", "privacy policy", "cookie policy",
    "mappa del sito", "footer", "header", "login", "logout",
]

ITALIAN_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}


# ============================================================
# FONTI SCRAPING CLASSICO (HTML)
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
        "fonte": "Università del Salento",
        "ente": "Università del Salento",
        "tipo": "PA",
        "ssl": True,
        "urls": [
            "https://trasparenza.unisalento.it/page/75/concorsi-attivi.html",
            "https://trasparenza.unisalento.it/page/5/bandi-di-concorso.html",
        ],
    },
    {
        "nome": "ARPAL Puglia — Collocamento Mirato",
        "fonte": "ARPAL Puglia",
        "ente": "ARPAL Puglia — Collocamento Mirato Lecce",
        "tipo": "ARPAL",
        "ssl": False,
        "urls": [
            "https://arpal.regione.puglia.it/servizi/persone/collocamento-mirato",
            "https://arpal.regione.puglia.it/notizie",
            "https://arpal.regione.puglia.it/",
        ],
    },
    # ── Aggregatori ─────────────────────────────────────────
    {
        "nome": "concorsipubblici.com — Lecce",
        "fonte": "concorsipubblici.com",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            "https://www.concorsipubblici.com/concorsi/regione/loc/lecce",
        ],
    },
    {
        "nome": "Concorsando — Lecce",
        "fonte": "Concorsando.it",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            "https://www.concorsando.it/blog/concorsi-lecce/",
            "https://www.concorsando.it/blog/?s=categorie+protette+puglia",
        ],
    },
    {
        "nome": "Concorsando — Categorie Protette",
        "fonte": "Concorsando.it",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            "https://www.concorsando.it/blog/concorsi-pubblici-per-categorie-protette/",
        ],
    },
    {
        "nome": "TiConsiglio — Concorsi Puglia",
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
        "nome": "concorsi.it — ASL Lecce",
        "fonte": "concorsi.it",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            "https://www.concorsi.it/ente/35707-azienda-sanitaria-locale-di-lecce.html",
            "https://www.concorsi.it/concorsi/regione/puglia/?q=categorie+protette",
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
    data_pubblicazione: str = ""
    descrizione: str = ""
    testo_completo: str = ""
    ente: str = ""
    scadenza: str = ""
    posti: str = ""
    tipo: str = ""
    art1: bool = False
    art16: bool = False
    score: int = 0
    stato: str = ""
    trovato_il: str = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("cerca_lavoro.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("cerca_lavoro")


# ============================================================
# HTTP
# ============================================================

def _build_session(verify_ssl: bool) -> requests.Session:
    s = requests.Session()
    retry = Retry(
        total=1, connect=1, read=1,
        backoff_factor=0.3,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    s.headers.update(HEADERS)
    s.verify = verify_ssl
    return s


SESSION       = _build_session(verify_ssl=True)
SESSION_NOSSL = _build_session(verify_ssl=False)


def _sess(ssl: bool) -> requests.Session:
    return SESSION if ssl else SESSION_NOSSL


def _sleep():
    time.sleep(CONFIG["sleep"])


def get(url: str, timeout: int = None, ssl: bool = True,
        params: dict = None) -> Optional[requests.Response]:
    try:
        r = _sess(ssl).get(url, timeout=timeout or CONFIG["timeout"], params=params)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        _sleep()
        return r
    except Exception as e:
        log.warning(f"GET {url} → {e}")
        return None


def get_soup(url: str, timeout: int = None, ssl: bool = True) -> Optional[BeautifulSoup]:
    r = get(url, timeout=timeout, ssl=ssl)
    return BeautifulSoup(r.text, "html.parser") if r else None


def get_json(url: str, params: dict = None, ssl: bool = True) -> Optional[dict | list]:
    try:
        r = _sess(ssl).get(
            url, params=params,
            timeout=CONFIG["timeout"],
            headers={**HEADERS, "Accept": "application/json"},
        )
        r.raise_for_status()
        _sleep()
        return r.json()
    except Exception as e:
        log.warning(f"GET JSON {url} → {e}")
        return None


def abs_url(href: str, base: str) -> str:
    return urljoin(base, href) if href else ""


# ============================================================
# HELPERS TESTO
# ============================================================

def norm(t: str) -> str:
    if not t:
        return ""
    t = html.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()


def has(text: str, words: list[str]) -> bool:
    low = norm(text).lower()
    return any(w.lower() in low for w in words)


def geo_ok(t: str)  -> bool: return has(t, GEO_WORDS)
def cp_ok(t: str)   -> bool: return has(t, POSITIVE_CP)
def a16_ok(t: str)  -> bool: return has(t, ART16_HINTS)
def bando_ok(t: str)-> bool: return has(t, BANDO_WORDS)
def neg_ok(t: str)  -> bool: return has(t, NEGATIVE_WORDS)
def nav_ok(t: str)  -> bool: return has(t, NAV_WORDS)


def extract_dates(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"\b([0-3]?\d)[/\-.]([01]?\d)[/\-.](20\d{2})\b", text):
        dd, mm, yy = m.groups()
        try:
            out.append(datetime(int(yy), int(mm), int(dd)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    for m in re.finditer(
        r"\b([0-3]?\d)\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|"
        r"agosto|settembre|ottobre|novembre|dicembre)\s+(20\d{2})\b",
        text.lower()
    ):
        dd, mese, yy = m.groups()
        try:
            out.append(datetime(int(yy), ITALIAN_MONTHS[mese], int(dd)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return list(dict.fromkeys(out))


def first_date(text: str) -> str:
    d = extract_dates(text)
    return d[0] if d else ""


def extract_scadenza(text: str) -> str:
    pats = [
        r"(?:scadenza|termine(?:\s+di)?\s+presentazione|entro(?:\s+il)?)"
        r"[:\s]+([0-3]?\d[/\-.][01]?\d[/\-.]20\d{2})",
        r"(?:scadenza|termine(?:\s+di)?\s+presentazione|entro(?:\s+il)?)"
        r"[:\s]+([0-3]?\d\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|"
        r"luglio|agosto|settembre|ottobre|novembre|dicembre)\s+20\d{2})",
    ]
    for p in pats:
        m = re.search(p, text.lower())
        if m:
            return first_date(m.group(1))
    return ""


def extract_posti(text: str) -> str:
    for p in [
        r"\b(?:n\.?|nr\.?|numero)\s*(\d+)\s*(?:posti?|unità|unita)\b",
        r"\b(\d+)\s*(?:posti?|unità|unita)\b",
    ]:
        m = re.search(p, text.lower())
        if m:
            return f"{m.group(1)} posti"
    return ""


def to_date(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            pass
    return None


def is_expired(scad: str) -> bool:
    dt = to_date(scad)
    return bool(dt and dt < datetime.now() - timedelta(days=CONFIG["giorni_tolleranza_scadenza"]))


def is_too_old(pub: str) -> bool:
    if not pub:
        return False
    dt = to_date(pub)
    return bool(dt and dt < datetime.now() - timedelta(days=CONFIG["giorni_indietro"]))


def clean_text(soup: BeautifulSoup) -> str:
    for t in soup.select("script,style,nav,footer,header,noscript,iframe,.cookie,.menu"):
        t.decompose()
    main = (soup.select_one("main") or soup.select_one("article")
            or soup.select_one(".content") or soup.select_one(".entry-content")
            or soup.body)
    if not main:
        return ""
    lines, prev = [], None
    for line in [norm(x) for x in main.get_text("\n", strip=True).splitlines()]:
        if len(line) >= 3 and line != prev:
            lines.append(line)
        prev = line
    return "\n".join(lines)[:10_000]


def is_attachment(url: str) -> bool:
    return any(url.lower().endswith(e) for e in [".pdf", ".doc", ".docx", ".zip", ".odt"])


# ============================================================
# SCORING
# ============================================================

def calc_score(a: Annuncio) -> int:
    t = " ".join([a.titolo, a.descrizione, a.testo_completo, a.ente, a.fonte, a.url])
    low = t.lower()
    if nav_ok(low): return -100
    if neg_ok(low): return -90
    s = 0
    if cp_ok(low):    s += 12
    if a16_ok(low):   s += 8
    if bando_ok(low): s += 4
    if geo_ok(low):   s += 4
    if "lecce" in low: s += 3
    if a.tipo == "ARPAL": s += 2
    if "aperto" in low:  s += 3
    if "chiuso" in low:  s -= 4
    return s


def finalize(a: Annuncio) -> Annuncio:
    t = " ".join([a.titolo, a.descrizione, a.testo_completo, a.url, a.ente, a.fonte])
    if not a.data_pubblicazione: a.data_pubblicazione = first_date(t)
    if not a.scadenza:           a.scadenza = extract_scadenza(t)
    if not a.posti:              a.posti    = extract_posti(t)
    if not a.art1:               a.art1     = cp_ok(t)
    if not a.art16:              a.art16    = a16_ok(t)
    low = t.lower()
    if "stato: aperto" in low:   a.stato = "Aperto"
    elif "stato: chiuso" in low: a.stato = "Chiuso"
    a.score = calc_score(a)
    return a


# ============================================================
# DB
# ============================================================

def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS annunci (
            url TEXT PRIMARY KEY,
            chiave TEXT,
            titolo TEXT,
            fonte TEXT,
            ente TEXT,
            data_pubblicazione TEXT,
            scadenza TEXT,
            posti TEXT,
            tipo TEXT,
            art1 INTEGER,
            art16 INTEGER,
            score INTEGER,
            stato TEXT,
            testo_completo TEXT,
            trovato_il TEXT
        )
    """)
    conn.commit()
    return conn


def seen(conn: sqlite3.Connection, url: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM annunci WHERE url=?", (url,)).fetchone())


def save_annunci(conn: sqlite3.Connection, items: list[Annuncio]) -> None:
    for a in items:
        conn.execute("""
            INSERT OR REPLACE INTO annunci
            (url,chiave,titolo,fonte,ente,data_pubblicazione,scadenza,posti,
             tipo,art1,art16,score,stato,testo_completo,trovato_il)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            a.url, _chiave(a), a.titolo, a.fonte, a.ente,
            a.data_pubblicazione, a.scadenza, a.posti, a.tipo,
            int(a.art1), int(a.art16), a.score, a.stato,
            a.testo_completo, a.trovato_il,
        ))
    conn.commit()


def _chiave(a: Annuncio) -> str:
    t = norm(a.titolo).lower()
    t = re.sub(r"\b(avviso|bando|selezione|concorso|graduatoria|verbale|convocazione|esito)\b", " ", t)
    t = re.sub(r"\b\d+\b", " ", t)
    return f"{norm(a.ente or a.fonte).lower()}::{re.sub(r' +', ' ', t).strip()[:150]}"


# ============================================================
# DEDUP
# ============================================================

def dedup_url(items: list[Annuncio]) -> list[Annuncio]:
    seen_set, out = set(), []
    for a in items:
        if a.url and a.url not in seen_set:
            seen_set.add(a.url)
            out.append(a)
    return out


def dedup_key(items: list[Annuncio]) -> list[Annuncio]:
    best: dict[str, Annuncio] = {}
    for a in items:
        k = _chiave(a)
        cur = best.get(k)
        if cur is None:
            best[k] = a
        else:
            if (a.score, int(bool(a.testo_completo)), int(bool(a.scadenza))) > \
               (cur.score, int(bool(cur.testo_completo)), int(bool(cur.scadenza))):
                best[k] = a
    return list(best.values())


# ============================================================
# ① InPA — API REST ufficiale
#   portale.inpa.gov.it  (backend diverso da www.inpa.gov.it)
#   Endpoint scoperto dall'app mobile e dai link PDF nei bandi
# ============================================================

def scrape_inpa() -> list[Annuncio]:
    log.info("📋 InPA — API portale.inpa.gov.it...")
    items = []

    # L'API usa paginazione e filtri per regione/keywords
    # regione Puglia = codice "PUG" o "puglia" (variante)
    # stato = "APERTO" per escludere già chiusi
    base = "https://portale.inpa.gov.it/api"

    endpoint_queries = [
        # ricerca per keyword + regione Puglia
        (f"{base}/concorsi/search", {
            "keyword": "categorie protette",
            "regione": "Puglia",
            "stato": "APERTO",
            "page": 0, "size": 50,
        }),
        (f"{base}/concorsi/search", {
            "keyword": "collocamento mirato",
            "regione": "Puglia",
            "stato": "APERTO",
            "page": 0, "size": 50,
        }),
        (f"{base}/concorsi/search", {
            "keyword": "art. 1 legge 68",
            "regione": "Puglia",
            "stato": "APERTO",
            "page": 0, "size": 50,
        }),
        (f"{base}/concorsi/search", {
            "keyword": "lecce categorie protette",
            "stato": "APERTO",
            "page": 0, "size": 50,
        }),
    ]

    for url, params in endpoint_queries:
        data = get_json(url, params=params, ssl=True)
        if not data:
            continue
        # L'API può rispondere con lista diretta o con {content: [...]}
        entries = data if isinstance(data, list) else data.get("content", data.get("items", []))
        if not isinstance(entries, list):
            continue
        for item in entries:
            titolo = norm(item.get("titolo") or item.get("title") or item.get("denominazione") or "")
            descr  = norm(item.get("descrizione") or item.get("description") or item.get("testo") or "")
            concorso_id = item.get("id") or item.get("concorsoId") or item.get("uuid") or ""
            link = (
                item.get("url") or item.get("link") or
                (f"https://www.inpa.gov.it/bandi-e-avvisi/dettaglio-bando-avviso/?concorso_id={concorso_id}"
                 if concorso_id else "")
            )
            pub    = (item.get("dataPubblicazione") or item.get("dataInserimento") or "")[:10]
            scad   = (item.get("dataScadenza") or item.get("scadenza") or "")[:10]
            ente   = norm(item.get("amministrazione") or item.get("ente") or item.get("denominazioneEnte") or "")
            posti  = str(item.get("numeroPosti") or item.get("posti") or "")

            if not titolo or not link:
                continue

            whole = f"{titolo} {descr} {ente} {link}"
            a = Annuncio(
                titolo=titolo[:240], fonte="InPA",
                url=link, data_pubblicazione=pub,
                descrizione=descr[:500], ente=ente, tipo="PA",
                scadenza=scad,
                posti=f"{posti} posti" if posti.isdigit() else posti,
            )
            finalize(a)
            if a.score >= 10:
                items.append(a)

    # Fallback: scraping della pagina /bandi-e-avvisi/ se l'API non risponde
    if not items:
        log.info("   InPA API non risponde — provo scraping pagina bandi...")
        for url_fb in [
            "https://www.inpa.gov.it/bandi-e-avvisi/?regione=puglia&stato=aperto",
            "https://www.inpa.gov.it/bandi-e-avvisi/?q=categorie+protette&stato=aperto",
        ]:
            soup = get_soup(url_fb, ssl=True)
            if not soup:
                continue
            for card in soup.select("article, .bando-card, .job-card, li.bando, .card"):
                tit_el = card.select_one("h2, h3, .title, a")
                lnk_el = card.select_one("a[href]")
                if not tit_el:
                    continue
                titolo = norm(tit_el.get_text())
                ctx    = norm(card.get_text(" "))
                link   = abs_url(lnk_el["href"] if lnk_el else "", "https://www.inpa.gov.it")
                whole  = f"{titolo} {ctx}"
                if neg_ok(whole) or nav_ok(whole):
                    continue
                if not (cp_ok(whole) or a16_ok(whole)):
                    continue
                a = Annuncio(
                    titolo=titolo[:240], fonte="InPA",
                    url=link, descrizione=ctx[:500],
                    ente="PA", tipo="PA",
                    data_pubblicazione=first_date(ctx),
                )
                finalize(a)
                if a.score >= 10:
                    items.append(a)

    log.info(f"   → {len(items)} candidati InPA")
    return items


# ============================================================
# ② Gazzetta Ufficiale RSS
# ============================================================

def scrape_gazzetta() -> list[Annuncio]:
    log.info("📰 Gazzetta Ufficiale RSS...")
    items = []
    for rss in [
        "https://www.gazzettaufficiale.it/rss/concorsi.xml",
        "https://www.gazzettaufficiale.it/rss/esecutivi.xml",
    ]:
        try:
            feed = feedparser.parse(rss)
        except Exception as e:
            log.warning(f"RSS {rss} → {e}")
            continue
        for entry in feed.entries[:100]:
            titolo  = norm(entry.get("title", ""))
            summary = norm(BeautifulSoup(entry.get("summary", ""), "html.parser").get_text())
            link    = entry.get("link", "")
            pub     = ""
            if entry.get("published_parsed"):
                pub = datetime(*entry.published_parsed[:6]).strftime("%Y-%m-%d")
            whole = f"{titolo} {summary} {link}"
            if neg_ok(whole):
                continue
            if not (cp_ok(whole) or a16_ok(whole)):
                continue
            if not geo_ok(whole):
                continue
            a = Annuncio(titolo=titolo, fonte="Gazzetta Ufficiale",
                         url=link, data_pubblicazione=pub,
                         descrizione=summary[:500], ente="PA", tipo="PA")
            finalize(a)
            if a.score >= 12:
                items.append(a)
    log.info(f"   → {len(items)} candidati GU")
    return items


# ============================================================
# ③–⑫ Scraper generico per tutte le fonti HTML
# ============================================================

def _is_candidate(tipo: str, text: str) -> bool:
    low = text.lower()
    if len(low) < 20 or nav_ok(low) or neg_ok(low):
        return False
    if tipo == "ARPAL":
        return cp_ok(low) or a16_ok(low) or "collocamento mirato" in low
    if tipo == "AGGREGATORE":
        # aggregatori già filtrati per provincia — basta che sia un bando
        return bando_ok(low) and (cp_ok(low) or a16_ok(low) or "lecce" in low or "puglia" in low)
    # PA: bando + (CP o Art16) + geo
    return bando_ok(low) and (cp_ok(low) or a16_ok(low)) and geo_ok(low)


def scrape_fonte_html(fonte: dict) -> list[Annuncio]:
    log.info(f"🏛️  {fonte['nome']}...")
    ssl  = fonte.get("ssl", False)
    tipo = fonte["tipo"]
    out  = []

    for url in fonte["urls"]:
        soup = get_soup(url, timeout=CONFIG["timeout_fast"], ssl=ssl)
        if not soup:
            continue
        seen_local: set[str] = set()
        for sel in ["article", "tr", "li", ".card", ".entry", ".item", "a[href]"]:
            for el in soup.select(sel):
                lnk = el if el.name == "a" else el.select_one("a[href]")
                if not lnk:
                    continue
                href   = abs_url(lnk.get("href", ""), url)
                titolo = norm(lnk.get_text(" ", strip=True))
                ctx    = norm(el.get_text(" ", strip=True))
                whole  = f"{titolo} {ctx} {href}"
                if href in seen_local or len(titolo) < 8:
                    continue
                seen_local.add(href)
                if not _is_candidate(tipo, whole):
                    continue
                a = Annuncio(
                    titolo=titolo[:240], fonte=fonte["fonte"],
                    url=href, data_pubblicazione=first_date(ctx),
                    descrizione=ctx[:600], ente=fonte["ente"], tipo=tipo,
                )
                finalize(a)
                out.append(a)

    log.info(f"   → {len(out)} candidati")
    return out


# ============================================================
# DETTAGLIO PAGINA
# ============================================================

def download_detail(a: Annuncio) -> Annuncio:
    if not a.url or len(a.url) < 12:
        return a
    # Salta allegati PDF
    if is_attachment(a.url):
        return a
    # SSL: True per siti noti con cert ok, False per PA
    ssl = any(d in a.url for d in [
        "inpa.gov.it", "gazzettaufficiale.it",
        "concorsando.it", "concorsipubblici.com",
        "concorsi.it", "ticonsiglio.com", "unisalento.it",
    ])
    r = get(a.url, timeout=CONFIG["timeout"], ssl=ssl)
    if not r:
        return a
    ct = (r.headers.get("Content-Type") or "").lower()
    if "pdf" in ct:
        return a
    soup = BeautifulSoup(r.text, "html.parser")
    a.testo_completo = clean_text(soup)
    return finalize(a)


def download_best(items: list[Annuncio], limit: int) -> None:
    ordered = sorted(items, key=lambda x: (x.art16, x.art1, x.score), reverse=True)
    log.info(f"⬇️  Scarico dettagli per i migliori {min(limit, len(ordered))} annunci...")
    for i, a in enumerate(ordered[:limit], 1):
        log.info(f"   [{i}/{min(limit, len(ordered))}] {a.titolo[:80]}")
        download_detail(a)


# ============================================================
# FILTRO FINALE
# ============================================================

def final_filter(items: list[Annuncio]) -> list[Annuncio]:
    out = []
    for a in items:
        finalize(a)
        t = " ".join([a.titolo, a.descrizione, a.testo_completo, a.url]).lower()
        if nav_ok(t) or neg_ok(t):
            continue
        if is_expired(a.scadenza):
            continue
        if is_too_old(a.data_pubblicazione):
            continue
        # geo obbligatorio per fonti PA (non per aggregatori che già filtrano)
        if a.tipo not in ("ARPAL", "AGGREGATORE") and not geo_ok(t):
            continue
        if a.score < 12:
            continue
        if cp_ok(t) or a16_ok(t):
            out.append(a)
    out.sort(key=lambda x: (
        x.art16, x.art1,
        x.stato == "Aperto",
        to_date(x.data_pubblicazione) or datetime.min,
        x.score,
    ), reverse=True)
    return out


def filter_new(conn: sqlite3.Connection, items: list[Annuncio]) -> list[Annuncio]:
    return [a for a in items if not seen(conn, a.url)]


# ============================================================
# REPORT TXT + JSON + HTML
# ============================================================

def txt_card(a: Annuncio) -> str:
    lines = [
        "=" * 80,
        f"TITOLO:    {a.titolo}",
        f"FONTE:     {a.fonte}  |  ENTE: {a.ente or '-'}",
        f"TIPO:      {a.tipo}  |  SCORE: {a.score}",
        f"DATA:      {a.data_pubblicazione or '-'}  |  SCADENZA: {a.scadenza or '???'}",
        f"POSTI:     {a.posti or '-'}  |  STATO: {a.stato or '-'}",
        f"ART.1:     {'✅ SÌ' if a.art1 else 'NO'}  |  ART.16: {'⭐ SÌ' if a.art16 else 'NO'}",
        f"URL:       {a.url}",
    ]
    if a.testo_completo:
        lines += ["", "── TESTO ──"]
        lines += [l for l in a.testo_completo[:3000].splitlines() if l.strip()]
    elif a.descrizione:
        lines += ["", f"SNIPPET: {a.descrizione[:400]}"]
    return "\n".join(lines)


_he = html.escape


def generate_reports(items: list[Annuncio], total_db: int) -> None:
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    art16  = [a for a in items if a.art16]
    art1   = [a for a in items if a.art1 and not a.art16]
    others = [a for a in items if not a.art1 and not a.art16]

    # TXT
    parts = [
        "═" * 80,
        "CERCA LAVORO — CATEGORIE PROTETTE L.68/99 — LECCE / SALENTO",
        f"Aggiornato: {now}",
        "═" * 80, "",
        f"Nuovi: {len(items)}  |  Art.16: {len(art16)}  |  Art.1: {len(art1)}  |  DB: {total_db}", "",
    ]
    if art16:  parts += ["⭐ ART.16 / CHIAMATA DIRETTA", ""]  + [txt_card(a) for a in art16]
    if art1:   parts += ["", "✅ ART.1 / L.68/99", ""]        + [txt_card(a) for a in art1]
    if others: parts += ["", "📌 ALTRI RISULTATI", ""]         + [txt_card(a) for a in others]

    with open(CONFIG["output_txt"], "w", encoding="utf-8") as f:
        f.write("\n".join(parts))

    with open(CONFIG["output_json"], "w", encoding="utf-8") as f:
        json.dump({
            "aggiornato": now,
            "totale_nuovi": len(items),
            "art16": len(art16), "art1": len(art1),
            "annunci": [asdict(a) for a in items],
        }, f, ensure_ascii=False, indent=2)

    def card_html(a: Annuncio) -> str:
        b = ""
        if a.art16: b += '<span class="badge b16">⭐ ART.16 DIRETTO</span> '
        if a.art1:  b += '<span class="badge b1">✅ ART.1 / L.68</span> '
        if a.stato: b += f'<span class="badge bst">{_he(a.stato)}</span>'
        raw = (a.testo_completo[:3000] if a.testo_completo else a.descrizione[:600])
        for kw in ["categorie protette","art. 16","art. 1","collocamento mirato",
                   "scadenza","disabil","invalidità","legge 68","art. 18"]:
            raw = re.sub(f"({re.escape(kw)})", r"<mark>\1</mark>",
                         raw, flags=re.IGNORECASE)
        testo = _he(raw).replace("\n", "<br>")
        return f"""
        <div class="card">
          <h3><a href="{_he(a.url)}" target="_blank" rel="noopener">{_he(a.titolo)}</a></h3>
          <div class="meta">
            <span>📌 {_he(a.ente or a.fonte)}</span>
            <span>🏷️ {_he(a.tipo)}</span>
            <span>📅 {_he(a.data_pubblicazione or '-')}</span>
            {'<span class="scad">⚠️ Scadenza: <strong>' + _he(a.scadenza) + '</strong></span>' if a.scadenza else ""}
            {'<span>👥 ' + _he(a.posti) + '</span>' if a.posti else ""}
          </div>
          <div class="badges">{b}</div>
          <details><summary>Testo completo ↓</summary>
            <div class="testo">{testo}</div>
          </details>
        </div>"""

    def sez(emoji, titolo, lista):
        if not lista: return ""
        return (f"<h2>{emoji} {_he(titolo)}"
                f" <small>({len(lista)})</small></h2>"
                + "".join(card_html(a) for a in lista))

    html_doc = f"""<!doctype html>
<html lang="it"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cat. Protette Lecce — {now}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#f0f4fb;color:#1a1a1a;line-height:1.6}}
header{{background:#1346a0;color:#fff;padding:20px 28px}}
header h1{{font-size:20px;font-weight:600}}
header p{{font-size:13px;opacity:.8;margin-top:4px}}
.stats{{display:flex;gap:12px;flex-wrap:wrap;padding:14px 28px;background:#dce8ff}}
.stat{{background:#fff;border-radius:10px;padding:10px 18px;text-align:center}}
.stat strong{{display:block;font-size:24px;color:#1346a0}}
.stat span{{font-size:12px;color:#555}}
main{{max-width:980px;margin:0 auto;padding:20px 16px}}
h2{{margin:28px 0 12px;font-size:17px;padding:8px 14px;background:#fff;border-radius:8px;border-left:4px solid #1346a0}}
h2 small{{font-weight:400;color:#666;font-size:13px}}
.card{{background:#fff;border-radius:12px;padding:16px 20px;margin-bottom:14px;box-shadow:0 1px 5px rgba(0,0,0,.07)}}
.card h3{{font-size:15px;margin-bottom:6px}}
.card h3 a{{color:#1346a0;text-decoration:none}}
.card h3 a:hover{{text-decoration:underline}}
.meta{{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:#666;margin-bottom:8px}}
.scad{{color:#c0392b;font-weight:500}}
.badges{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}}
.badge{{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:700}}
.b16{{background:#fff3e0;color:#e65100;border:1px solid #ffb74d}}
.b1{{background:#e8f5e9;color:#2e7d32;border:1px solid #81c784}}
.bst{{background:#e8eaf6;color:#283593;border:1px solid #9fa8da}}
details summary{{cursor:pointer;padding:8px 0;font-size:13px;color:#1346a0}}
.testo{{font-size:13px;line-height:1.6;color:#333;padding:10px 0;max-height:450px;overflow-y:auto}}
mark{{background:#fff9c4;padding:1px 2px;border-radius:2px;font-weight:600}}
</style>
</head><body>
<header>
  <h1>🔔 Categorie Protette L.68/99 — Lecce / Salento</h1>
  <p>Aggiornato: {now} · Art.16 (accesso diretto) + Art.1 · Fonti: InPA + GU + ARPAL + ASL + aggregatori</p>
</header>
<div class="stats">
  <div class="stat"><strong>{len(items)}</strong><span>Nuovi annunci</span></div>
  <div class="stat"><strong style="color:#e65100">{len(art16)}</strong><span>Art.16 Diretti</span></div>
  <div class="stat"><strong style="color:#2e7d32">{len(art1)}</strong><span>Art.1 L.68/99</span></div>
  <div class="stat"><strong style="color:#555">{len(others)}</strong><span>Altri concorsi</span></div>
  <div class="stat"><strong style="color:#888">{total_db}</strong><span>Tot. archivio</span></div>
</div>
<main>
{sez("⭐", "Art.16 — Accesso Diretto (PRIORITARI)", art16)}
{sez("✅", "Art.1 L.68/99 — Collocamento Mirato", art1)}
{sez("📌", "Altri concorsi PA — Lecce / Salento", others)}
</main></body></html>"""

    with open(CONFIG["output_html"], "w", encoding="utf-8") as f:
        f.write(html_doc)

    log.info(f"Report → {CONFIG['output_txt']} | {CONFIG['output_json']} | {CONFIG['output_html']}")


# ============================================================
# NOTIFICHE
# ============================================================

def _tg_send(token: str, chat_id: str, text: str) -> bool:
    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4096],
                  "disable_web_page_preview": True},
            timeout=12,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram send → {e}")
        return False


def send_telegram(items: list[Annuncio]) -> None:
    token   = CONFIG["telegram_bot_token"]
    chat_id = CONFIG["telegram_chat_id"]
    if not token or not chat_id or not items:
        return

    art16 = [a for a in items if a.art16]
    art1  = [a for a in items if a.art1 and not a.art16]

    # Messaggio 1: riepilogo
    _tg_send(token, chat_id, (
        f"Categorie Protette — Lecce / Salento\n"
        f"Nuovi annunci: {len(items)}\n"
        f"Art.16 diretti: {len(art16)}\n"
        f"Art.1 L.68/99:  {len(art1)}\n"
        f"Aggiornato: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    ))

    # Messaggi singoli per ogni annuncio (max 15)
    prioritari = art16 + art1 + [a for a in items if not a.art1 and not a.art16]
    for a in prioritari[:15]:
        tags = " | ".join(filter(None, [
            "ART.16 ACCESSO DIRETTO" if a.art16 else "",
            "ART.1 L.68/99"          if a.art1  else "",
            a.stato or "",
        ]))
        card = (
            f"{'[ART.16]' if a.art16 else '[ART.1]' if a.art1 else '[PA]'} "
            f"{a.titolo[:100]}\n"
            f"Ente: {a.ente or a.fonte}\n"
            + (f"Scadenza: {a.scadenza}\n" if a.scadenza else "")
            + (f"Posti: {a.posti}\n"       if a.posti    else "")
            + (f"{tags}\n"                  if tags       else "")
            + f"{a.url}"
        )
        _tg_send(token, chat_id, card)
        time.sleep(0.3)

    log.info(f"Telegram: inviati {min(len(prioritari), 15) + 1} messaggi")


def send_email(items: list[Annuncio]) -> None:
    if not items or not all([CONFIG["email_destinatario"],
                              CONFIG["email_mittente"],
                              CONFIG["email_password"]]):
        return

    art16 = [a for a in items if a.art16]
    art1  = [a for a in items if a.art1 and not a.art16]
    altri = [a for a in items if not a.art1 and not a.art16]

    def row(a: Annuncio) -> str:
        c = "#e65100" if a.art16 else "#2e7d32" if a.art1 else "#1346a0"
        tags = " | ".join(filter(None, [
            "ART.16 DIRETTO" if a.art16 else "",
            "ART.1 L.68/99"  if a.art1  else "",
            a.stato or "",
        ]))
        snip = _he(a.testo_completo[:500] if a.testo_completo else a.descrizione[:500])
        return f"""
        <div style="border-left:4px solid {c};background:#f8faff;padding:10px 14px;margin:10px 0;border-radius:0 8px 8px 0">
          <div style="font-weight:700;margin-bottom:4px">{_he(a.titolo)}</div>
          <div style="font-size:12px;color:#555">{_he(a.fonte)} · {_he(a.ente or '-')}</div>
          <div style="font-size:12px;color:#555">
            Scadenza: <strong>{_he(a.scadenza or '???')}</strong> · Posti: {_he(a.posti or '-')}
          </div>
          <div style="font-size:12px;margin:4px 0">{tags}</div>
          {'<div style="font-size:12px;color:#333;margin:6px 0">' + snip + '</div>' if snip else ''}
          <a href="{_he(a.url)}" style="font-size:12px;color:#1346a0">{_he(a.url[:90])}</a>
        </div>"""

    body = f"""<html><body style="font-family:Arial,sans-serif;max-width:760px;margin:auto">
      <h2 style="color:#1346a0">Categorie Protette — Lecce / Salento</h2>
      <p>Nuovi: <b>{len(items)}</b> · Art.16: <b>{len(art16)}</b> · Art.1: <b>{len(art1)}</b></p>
      {"<h3>Art.16 — Accesso Diretto</h3>" + "".join(row(a) for a in art16) if art16 else ""}
      {"<h3>Art.1 L.68/99</h3>" + "".join(row(a) for a in art1) if art1 else ""}
      {"<h3>Altri concorsi</h3>" + "".join(row(a) for a in altri[:15]) if altri else ""}
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (f"[Lecce CP] {len(items)} nuovi"
                      + (f" — {len(art16)} ART.16!" if art16 else ""))
    msg["From"] = CONFIG["email_mittente"]
    msg["To"]   = CONFIG["email_destinatario"]
    msg.attach(MIMEText(body, "html", "utf-8"))

    try:
        with smtplib.SMTP(CONFIG["smtp_server"], CONFIG["smtp_port"]) as s:
            s.starttls()
            s.login(CONFIG["email_mittente"], CONFIG["email_password"])
            s.sendmail(CONFIG["email_mittente"], CONFIG["email_destinatario"], msg.as_string())
        log.info("Email inviata")
    except Exception as e:
        log.error(f"Email → {e}")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    print("\n" + "═" * 80)
    print("CERCA LAVORO — CATEGORIE PROTETTE L.68/99 — LECCE / SALENTO")
    print("InPA + GU + ARPAL + ASL + Comune Lecce + aggregatori")
    print("═" * 80 + "\n")

    conn = init_db(CONFIG["db_path"])
    raw: list[Annuncio] = []

    # ① InPA via API
    try:
        raw.extend(scrape_inpa())
    except Exception as e:
        log.error(f"InPA → {e}")

    # ② Gazzetta Ufficiale
    try:
        raw.extend(scrape_gazzetta())
    except Exception as e:
        log.error(f"GU → {e}")

    # ③–⑫ Tutte le fonti HTML
    for fonte in FONTI_HTML:
        try:
            raw.extend(scrape_fonte_html(fonte))
        except Exception as e:
            log.error(f"{fonte['nome']} → {e}")

    log.info(f"Totale grezzo: {len(raw)}")

    stage1 = dedup_url(raw)
    log.info(f"Dopo dedup URL: {len(stage1)}")

    stage1.sort(key=lambda x: x.score, reverse=True)
    download_best(stage1, CONFIG["max_dettagli"])

    stage2 = [finalize(a) for a in stage1]
    stage3 = dedup_key(stage2)
    log.info(f"Dopo dedup chiave: {len(stage3)}")

    final_items = final_filter(stage3)
    log.info(f"Finali pertinenti: {len(final_items)}")

    new_items = filter_new(conn, final_items)
    log.info(f"Nuovi non in DB: {len(new_items)}")

    save_annunci(conn, new_items)
    total_db = conn.execute("SELECT COUNT(*) FROM annunci").fetchone()[0]

    generate_reports(new_items, total_db)

    if new_items:
        send_email(new_items)
        send_telegram(new_items)

    print(f"\n✅ Completato — nuovi risultati: {len(new_items)}")
    print(f"   ⭐ Art.16: {sum(1 for a in new_items if a.art16)}")
    print(f"   ✅ Art.1:  {sum(1 for a in new_items if a.art1 and not a.art16)}")
    print(f"   🌐 HTML:  {CONFIG['output_html']}")
    print(f"   📄 TXT:   {CONFIG['output_txt']}")
    print(f"   🗂️  DB:    {CONFIG['db_path']}")
    conn.close()


if __name__ == "__main__":
    main()
