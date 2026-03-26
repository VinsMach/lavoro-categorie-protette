#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CERCA LAVORO — CATEGORIE PROTETTE L.68/99
Provincia di Lecce / Salento
Art.16 (accesso diretto) + Art.1 (collocamento mirato)

Architettura basata sulla revisione ChatGPT con correzioni:
- SSL verify=False per siti PA con certificati problematici
- URL ARPAL verificati
- Aggregatori concorsipubblici.com + concorsi.it
- Filtro geo obbligatorio (evita concorsi di Torino)
- Score threshold abbassato + geo richiesto
- too_old gestisce annunci senza data
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
from urllib.parse import urljoin

import feedparser
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Silenzia warning SSL — necessario per siti PA con certificati problematici
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ============================================================
# CONFIG
# ============================================================

CONFIG = {
    "db_path":      "lavoro_lecce_cp.db",
    "output_json":  "risultati_lecce.json",
    "output_txt":   "risultati_lecce.txt",
    "output_html":  "risultati_lecce.html",

    "giorni_indietro": 120,
    "giorni_tolleranza_scadenza": 7,
    "timeout": 18,
    "timeout_fast": 10,
    "sleep": 1.0,
    "max_dettagli": 50,

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
    "riserva disabili", "posto riservato",
    "liste speciali", "categorie di cui all'art. 1",
]

ART16_HINTS = [
    "art. 16", "articolo 16", "chiamata nominativa",
    "chiamata diretta", "avviamento a selezione", "accesso diretto",
    "l.r. 17/2005", "lr 17/2005",
]

# Parole geografiche della provincia di Lecce — OBBLIGATORIE per filtrare
# concorsi da altre regioni (es. Torino, Milano)
GEO_WORDS = [
    "lecce", "salento", "provincia di lecce", "leccese",
    "puglia", "apulia",
    "casarano", "copertino", "gallipoli", "galatina", "nardò", "nardo",
    "maglie", "otranto", "tricase", "ugento", "surbo", "taviano",
    "racale", "galatone", "squinzano", "trepuzzi", "leverano",
    "campi salentina", "monteroni", "novoli", "lequile",
]

BANDO_WORDS = [
    "bando", "avviso pubblico", "concorso", "selezione pubblica",
    "reclutamento", "assunzione", "manifestazione di interesse",
]

# Scarta queste pagine — sono risultati/atti, non annunci aperti
NEGATIVE_WORDS = [
    "graduatoria", "graduatorie", "elenco idonei",
    "esito", "esiti", "ammessi", "non ammessi",
    "convocazione", "convocazioni",
    "verbale", "verbali",
    "rettifica bando", "nomina commissione",
    "tracce", "prova scritta", "prova orale",
    "presa d'atto", "utilizzo graduatoria",
    "assunti da altri enti", "interpello interno",
    "mobilità interna", "mobilita interna",
    "diario prova",
]

NAV_WORDS = [
    "home", "privacy", "cookie", "accessibilità",
    "mappa del sito", "newsletter",
    "facebook", "instagram", "youtube", "linkedin",
    "footer", "header", "vai al", "vai ai",
    "accedi all'area", "albo pretorio",
    "segnalazione disservizio", "prenotazione appuntamento",
    "dichiarazione di accessibilità",
]

ITALIAN_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4,
    "maggio": 5, "giugno": 6, "luglio": 7, "agosto": 8,
    "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}


# ============================================================
# ELENCO COMUNI (tutti i 96 comuni della provincia)
# ============================================================

COMUNI_LECCE = [
    "Alessano", "Alezio", "Alliste", "Andrano", "Aradeo", "Arnesano",
    "Bagnolo del Salento", "Botrugno", "Calimera", "Campi Salentina",
    "Cannole", "Caprarica di Lecce", "Carmiano", "Carpignano Salentino",
    "Casarano", "Castri di Lecce", "Castrignano de' Greci",
    "Castrignano del Capo", "Castro", "Cavallino", "Collepasso",
    "Copertino", "Corigliano d'Otranto", "Corsano", "Cursi",
    "Cutrofiano", "Diso", "Gagliano del Capo", "Galatina", "Galatone",
    "Gallipoli", "Giuggianello", "Giurdignano", "Guagnano",
    "Lequile", "Leverano", "Lizzanello", "Maglie", "Martano",
    "Martignano", "Matino", "Melendugno", "Melissano", "Melpignano",
    "Miggiano", "Minervino di Lecce", "Monteroni di Lecce",
    "Montesano Salentino", "Morciano di Leuca", "Muro Leccese", "Nardò",
    "Neviano", "Nociglia", "Novoli", "Ortelle", "Otranto", "Palmariggi",
    "Parabita", "Patù", "Poggiardo", "Porto Cesareo",
    "Presicce-Acquarica", "Racale", "Ruffano", "Salice Salentino",
    "Salve", "San Cassiano", "San Cesario di Lecce",
    "San Donato di Lecce", "San Pietro in Lama", "Sanarica",
    "Sannicola", "Santa Cesarea Terme", "Scorrano", "Seclì",
    "Sogliano Cavour", "Soleto", "Specchia", "Spongano", "Squinzano",
    "Sternatia", "Supersano", "Surano", "Surbo", "Taurisano",
    "Taviano", "Tiggiano", "Trepuzzi", "Tricase", "Tuglie", "Ugento",
    "Uggiano la Chiesa", "Veglie", "Vernole", "Zollino",
]

# Slug manuali per comuni con nomi complicati
COMUNE_SLUG_MAP: dict[str, str] = {
    "Nardò": "nardo",
    "Castrignano de' Greci": "castrignanodegreci",
    "Corigliano d'Otranto": "coriglianodotranto",
    "Patù": "patu",
    "Seclì": "secli",
    "Presicce-Acquarica": "presicceacquarica",
    "Santa Cesarea Terme": "santacesareaterme",
    "Uggiano la Chiesa": "uggianolachiesa",
    "San Cesario di Lecce": "sancesariodilecce",
    "San Donato di Lecce": "sandonatodilecce",
    "San Pietro in Lama": "sanpietroinlama",
    "Bagnolo del Salento": "bagnolodelsalento",
    "Campi Salentina": "campisalentina",
    "Caprarica di Lecce": "capraricadilecce",
    "Carpignano Salentino": "carpignanosalentino",
    "Castri di Lecce": "castridilecce",
    "Castrignano del Capo": "castrignanodelcapo",
    "Gagliano del Capo": "gaglianodelcapo",
    "Minervino di Lecce": "minervinodilecce",
    "Monteroni di Lecce": "monteronidilecce",
    "Montesano Salentino": "montesanosalentino",
    "Morciano di Leuca": "morcianodileuca",
    "Muro Leccese": "muroleccese",
    "Porto Cesareo": "portocesareo",
    "Salice Salentino": "salicesalentino",
    "San Cassiano": "sancassiano",
}

# Enti fissi con URL verificati
ENTI_SALENTO: list[dict] = [
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
            "https://www.provincia.le.it/concorsi-e-selezioni/",
            "https://www.provincia.le.it/elenchi-di-idonei/",
        ],
    },
    {
        "nome": "ASL Lecce",
        "fonte": "ASL Lecce",
        "ente": "ASL Lecce",
        "tipo": "PA",
        "ssl": False,
        # URL VERIFICATI dalla ricerca — ASL usa il portale regionale sanità
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
        "nome": "ARPAL Puglia — Ambito Lecce",
        "fonte": "ARPAL Puglia",
        "ente": "ARPAL Puglia — Collocamento Mirato Lecce",
        "tipo": "ARPAL",
        "ssl": False,
        # URL VERIFICATI dalla ricerca ufficiale ARPAL
        "urls": [
            "https://arpal.regione.puglia.it/servizi/persone/collocamento-mirato",
            "https://arpal.regione.puglia.it/notizie",
            "https://arpal.regione.puglia.it/",
        ],
    },
    # Aggregatori con URL stabili e verificati — coprono tutti gli enti di Lecce
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
        "nome": "concorsi.it — ASL Lecce",
        "fonte": "concorsi.it",
        "ente": "",
        "tipo": "AGGREGATORE",
        "ssl": True,
        "urls": [
            "https://www.concorsi.it/ente/35707-azienda-sanitaria-locale-di-lecce.html",
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
    rilevante: bool = False
    dettaglio_scaricato: bool = False
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
# HTTP — sessione con retry e SSL opzionale
# ============================================================

def build_session(verify_ssl: bool = True) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=2, connect=2, read=2,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(HEADERS)
    session.verify = verify_ssl
    return session

# Due sessioni: una normale, una con SSL disabilitato per siti PA problematici
SESSION      = build_session(verify_ssl=True)
SESSION_NOSSL = build_session(verify_ssl=False)


def _session(ssl: bool) -> requests.Session:
    return SESSION if ssl else SESSION_NOSSL


def sleep_polite(t: float = None):
    time.sleep(t or CONFIG["sleep"])


def get(url: str, timeout: int = None, ssl: bool = True) -> Optional[requests.Response]:
    try:
        r = _session(ssl).get(url, timeout=timeout or CONFIG["timeout"])
        r.raise_for_status()
        if not r.encoding:
            r.encoding = r.apparent_encoding or "utf-8"
        sleep_polite()
        return r
    except Exception as e:
        log.warning(f"GET {url} → {e}")
        return None


def get_soup(url: str, timeout: int = None, ssl: bool = True) -> Optional[BeautifulSoup]:
    r = get(url, timeout=timeout, ssl=ssl)
    return BeautifulSoup(r.text, "html.parser") if r else None


def assoluto(href: str, base: str) -> str:
    return urljoin(base, href) if href else ""


# ============================================================
# HELPERS TESTO
# ============================================================

def norm(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def slugify_comune(nome: str) -> str:
    if nome in COMUNE_SLUG_MAP:
        return COMUNE_SLUG_MAP[nome]
    s = nome.lower().strip()
    for old, new in {"à":"a","è":"e","é":"e","ì":"i","ò":"o","ù":"u","'":"","'":"","-":"","'":"","'":""," ":""}.items():
        s = s.replace(old, new)
    return re.sub(r"[^a-z0-9]", "", s)


def contains_any(text: str, words: list[str]) -> bool:
    low = norm(text).lower()
    return any(w.lower() in low for w in words)


def geo_match(text: str)  -> bool: return contains_any(text, GEO_WORDS)
def cp_match(text: str)   -> bool: return contains_any(text, POSITIVE_CP)
def art16_match(text: str)-> bool: return contains_any(text, ART16_HINTS)
def bando_match(text: str)-> bool: return contains_any(text, BANDO_WORDS)
def neg_match(text: str)  -> bool: return contains_any(text, NEGATIVE_WORDS)
def nav_match(text: str)  -> bool: return contains_any(text, NAV_WORDS)


def extract_dates(text: str) -> list[str]:
    out = []
    for m in re.finditer(r"\b([0-3]?\d)[/\-.]([01]?\d)[/\-.](20\d{2})\b", text):
        dd, mm, yyyy = m.groups()
        try:
            out.append(datetime(int(yyyy), int(mm), int(dd)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    for m in re.finditer(
        r"\b([0-3]?\d)\s+(gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s+(20\d{2})\b",
        text.lower()
    ):
        dd, mese, yyyy = m.groups()
        try:
            out.append(datetime(int(yyyy), ITALIAN_MONTHS[mese], int(dd)).strftime("%Y-%m-%d"))
        except ValueError:
            pass
    return list(dict.fromkeys(out))


def extract_first_date(text: str) -> str:
    d = extract_dates(text)
    return d[0] if d else ""


def extract_scadenza(text: str) -> str:
    patterns = [
        r"(?:scadenza|termine(?:\s+di)?\s+presentazione|entro(?:\s+il)?)[:\s]+([0-3]?\d[/\-.][01]?\d[/\-.]20\d{2})",
        r"(?:scadenza|termine(?:\s+di)?\s+presentazione|entro(?:\s+il)?)[:\s]+([0-3]?\d\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s+20\d{2})",
        r"(?:data chiusura candidature)[:\s]+([0-3]?\d\s+(?:gennaio|febbraio|marzo|aprile|maggio|giugno|luglio|agosto|settembre|ottobre|novembre|dicembre)\s+20\d{2})",
    ]
    for p in patterns:
        m = re.search(p, text.lower(), flags=re.IGNORECASE)
        if m:
            return extract_first_date(m.group(1))
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


def parse_date_iso(s: str) -> Optional[datetime]:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            pass
    return None


def expired(scadenza: str) -> bool:
    dt = parse_date_iso(scadenza)
    return bool(dt and dt < datetime.now() - timedelta(days=CONFIG["giorni_tolleranza_scadenza"]))


def too_old(data_pub: str) -> bool:
    """
    FIX v5: se data_pubblicazione è vuota NON scartare —
    molti annunci PA non hanno data ed è meglio tenerli che perderli.
    """
    if not data_pub:
        return False
    dt = parse_date_iso(data_pub)
    return bool(dt and dt < datetime.now() - timedelta(days=CONFIG["giorni_indietro"]))


def clean_page_text(soup: BeautifulSoup) -> str:
    for tag in soup.select("script,style,nav,footer,header,noscript,iframe,.cookie,.menu"):
        tag.decompose()
    main = (
        soup.select_one("main") or soup.select_one("article")
        or soup.select_one(".content") or soup.select_one(".entry-content")
        or soup.select_one(".page-content") or soup.body
    )
    if not main:
        return ""
    raw = main.get_text("\n", strip=True)
    lines, prev = [], None
    for line in [norm(x) for x in raw.splitlines()]:
        if len(line) >= 3 and line != prev:
            lines.append(line)
        prev = line
    return "\n".join(lines)[:10_000]


def looks_like_attachment(url: str) -> bool:
    return any(url.lower().endswith(e) for e in [".pdf",".doc",".docx",".zip",".odt"])


# ============================================================
# SCORING
# ============================================================

def score_annuncio(a: Annuncio) -> int:
    text = " ".join([a.titolo, a.descrizione, a.testo_completo, a.ente, a.fonte, a.url])
    low = text.lower()

    if nav_match(low):   return -100
    if neg_match(low):   return -90

    score = 0
    if cp_match(low):    score += 12
    if art16_match(low): score += 8
    if bando_match(low): score += 4
    if geo_match(low):   score += 4   # FIX v5: +4 invece di +3 (peso geografico aumentato)
    if "lecce" in low:   score += 3   # FIX v5: bonus extra per Lecce specifica
    if a.tipo == "ARPAL": score += 2
    if "aperto" in low:  score += 3
    if "chiuso" in low:  score -= 4

    return score


def finalize(a: Annuncio) -> Annuncio:
    text = " ".join([a.titolo, a.descrizione, a.testo_completo, a.url, a.ente, a.fonte])
    if not a.data_pubblicazione:
        a.data_pubblicazione = extract_first_date(text)
    if not a.scadenza:
        a.scadenza = extract_scadenza(text)
    if not a.posti:
        a.posti = extract_posti(text)
    if not a.art1:
        a.art1 = cp_match(text)
    if not a.art16:
        a.art16 = art16_match(text)
    low = text.lower()
    if "stato: aperto" in low:
        a.stato = "Aperto"
    elif "stato: chiuso" in low:
        a.stato = "Chiuso"
    a.score = score_annuncio(a)
    a.rilevante = a.score >= 12   # FIX v5: soglia 12 (era 10)
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
            rilevante INTEGER,
            testo_completo TEXT,
            trovato_il TEXT
        )
    """)
    conn.commit()
    return conn


def seen_url(conn: sqlite3.Connection, url: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM annunci WHERE url=?", (url,)).fetchone())


def save_annunci(conn: sqlite3.Connection, items: list[Annuncio]) -> None:
    for a in items:
        conn.execute("""
            INSERT OR REPLACE INTO annunci (
                url, chiave, titolo, fonte, ente, data_pubblicazione,
                scadenza, posti, tipo, art1, art16, score, stato,
                rilevante, testo_completo, trovato_il
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            a.url, chiave_annuncio(a), a.titolo, a.fonte, a.ente,
            a.data_pubblicazione, a.scadenza, a.posti, a.tipo,
            int(a.art1), int(a.art16), a.score, a.stato,
            int(a.rilevante), a.testo_completo, a.trovato_il,
        ))
    conn.commit()


# ============================================================
# DEDUP
# ============================================================

def normalizza_titolo(t: str) -> str:
    t = norm(t).lower()
    t = re.sub(r"\b(avviso|bando|selezione|concorso|manifestazione di interesse|"
               r"rettifica|allegato|graduatoria|verbale|convocazione|esito)\b", " ", t)
    t = re.sub(r"\b\d+\b", " ", t)
    return re.sub(r"\s+", " ", t).strip()[:160]


def chiave_annuncio(a: Annuncio) -> str:
    return f"{norm(a.ente or a.fonte).lower()}::{normalizza_titolo(a.titolo)}"


def dedup_by_url(items: list[Annuncio]) -> list[Annuncio]:
    seen, out = set(), []
    for a in items:
        if a.url and a.url not in seen:
            seen.add(a.url)
            out.append(a)
    return out


def dedup_by_key(items: list[Annuncio]) -> list[Annuncio]:
    best: dict[str, Annuncio] = {}
    for a in items:
        key = chiave_annuncio(a)
        cur = best.get(key)
        if cur is None:
            best[key] = a
            continue
        rank_a = (a.score, int(a.rilevante), int(bool(a.testo_completo)), int(bool(a.scadenza)))
        rank_c = (cur.score, int(cur.rilevante), int(bool(cur.testo_completo)), int(bool(cur.scadenza)))
        if rank_a > rank_c:
            best[key] = a
    return list(best.values())


# ============================================================
# SCRAPER GENERICO ENTI
# ============================================================

def is_candidate(entity: dict, text: str) -> bool:
    """
    FIX v5: filtro geo SEMPRE obbligatorio, tranne per aggregatori
    (che già filtrano per provincia nella loro URL).
    """
    low = text.lower()
    if len(low) < 20 or nav_match(low) or neg_match(low):
        return False

    tipo = entity.get("tipo", "PA")

    # Gli aggregatori indicizzano già per Lecce — basta che sia un bando
    if tipo == "AGGREGATORE":
        return bando_match(low) and (cp_match(low) or art16_match(low) or "lecce" in low)

    if tipo == "ARPAL":
        return cp_match(low) or art16_match(low) or "collocamento mirato" in low

    # Per tutti gli enti PA: bando + (CP o Art16) + geo obbligatorio
    if not bando_match(low):
        return False
    if not (cp_match(low) or art16_match(low)):
        return False
    if not geo_match(low):   # FIX v5: geo richiesto
        return False
    return True


def extract_items_from_page(entity: dict, soup: BeautifulSoup, page_url: str) -> list[Annuncio]:
    out, seen_local = [], set()
    for sel in ["article","tr","li",".card",".entry",".item",".news-item","a[href]"]:
        for el in soup.select(sel):
            link = el if el.name == "a" else el.select_one("a[href]")
            if not link:
                continue
            href = assoluto(link.get("href",""), page_url)
            titolo = norm(link.get_text(" ", strip=True))
            ctx = norm(el.get_text(" ", strip=True))
            whole = f"{titolo} {ctx} {href}"

            if href in seen_local or len(titolo) < 8:
                continue
            seen_local.add(href)

            if not is_candidate(entity, whole):
                continue

            a = Annuncio(
                titolo=titolo[:240], fonte=entity["fonte"],
                url=href, data_pubblicazione=extract_first_date(ctx),
                descrizione=ctx[:600], ente=entity["ente"],
                tipo=entity["tipo"],
            )
            finalize(a)
            out.append(a)
    return out


def scrape_entity(entity: dict) -> list[Annuncio]:
    log.info(f"🏛️  {entity['nome']}...")
    ssl = entity.get("ssl", False)   # FIX v5: ogni ente ha il suo flag SSL
    results = []
    for url in entity["urls"]:
        soup = get_soup(url, timeout=CONFIG["timeout_fast"], ssl=ssl)
        if not soup:
            continue
        try:
            found = extract_items_from_page(entity, soup, url)
            results.extend(found)
        except Exception as e:
            log.warning(f"Parse failed {entity['nome']} {url} → {e}")
    log.info(f"   → {len(results)} candidati")
    return results


# ============================================================
# BUILDER COMUNI
# ============================================================

def build_comune_entity(nome: str) -> dict:
    slug = slugify_comune(nome)
    base_at  = f"https://amministrazionetrasparente.comune.{slug}.le.it"
    base_www = f"https://www.comune.{slug}.le.it"
    return {
        "nome": f"Comune di {nome}",
        "fonte": f"Comune di {nome}",
        "ente": f"Comune di {nome}",
        "tipo": "PA",
        "ssl": False,  # verify=False perché molti comuni hanno cert problematici
        "urls": [
            f"{base_at}/amministrazione-trasparente/bandi-di-concorso",
            f"{base_at}/bandi-di-concorso",
            f"{base_www}/amministrazione-trasparente/bandi-di-concorso",
            f"{base_www}/concorsi",
            f"{base_www}/bandi-concorso",
        ],
    }


def all_entities() -> list[dict]:
    out = list(ENTI_SALENTO)
    for comune in COMUNI_LECCE:
        out.append(build_comune_entity(comune))
    return out


# ============================================================
# GAZZETTA UFFICIALE
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
            titolo  = norm(entry.get("title",""))
            summary = norm(BeautifulSoup(entry.get("summary",""), "html.parser").get_text(" ",strip=True))
            link    = entry.get("link","")
            pub     = ""
            if entry.get("published_parsed"):
                pub = datetime(*entry.published_parsed[:6]).strftime("%Y-%m-%d")
            whole = f"{titolo} {summary} {link}"
            if neg_match(whole): continue
            if not (cp_match(whole) or art16_match(whole)): continue
            if not geo_match(whole): continue
            a = Annuncio(titolo=titolo, fonte="Gazzetta Ufficiale",
                         url=link, data_pubblicazione=pub,
                         descrizione=summary[:500], ente="PA", tipo="PA")
            finalize(a)
            if a.score >= 12:
                items.append(a)
    log.info(f"   → {len(items)} candidati")
    return items


# ============================================================
# INPA
# ============================================================

def scrape_inpa() -> list[Annuncio]:
    log.info("📋 InPA API...")
    items = []
    queries = [
        "categorie protette puglia",
        "categorie protette lecce",
        "collocamento mirato puglia",
        "articolo 16 puglia",
        "legge 68/99 lecce",
    ]
    for q in queries:
        try:
            r = SESSION.get(
                "https://www.inpa.gov.it/wp-json/wp/v2/posts",
                params={"search": q, "per_page": 15,
                        "_fields": "title,link,date,excerpt"},
                timeout=CONFIG["timeout"],
            )
            r.raise_for_status()
            sleep_polite()
            data = r.json()
        except Exception as e:
            log.warning(f"InPA [{q}] → {e}")
            continue
        for item in data:
            titolo  = norm(BeautifulSoup(item.get("title",{}).get("rendered",""), "html.parser").get_text())
            excerpt = norm(BeautifulSoup(item.get("excerpt",{}).get("rendered",""), "html.parser").get_text())
            link    = item.get("link","")
            pub     = (item.get("date","") or "")[:10]
            whole   = f"{titolo} {excerpt} {link}"
            if neg_match(whole): continue
            if not (cp_match(whole) or art16_match(whole)): continue
            if not geo_match(whole): continue
            a = Annuncio(titolo=titolo, fonte="InPA", url=link,
                         data_pubblicazione=pub, descrizione=excerpt[:500],
                         ente="PA", tipo="PA")
            finalize(a)
            if a.score >= 12:
                items.append(a)
    log.info(f"   → {len(items)} candidati")
    return items


# ============================================================
# DETTAGLIO PAGINA
# ============================================================

def download_detail(a: Annuncio, ssl: bool = False) -> Annuncio:
    if a.dettaglio_scaricato or not a.url or len(a.url) < 12:
        return a
    r = get(a.url, timeout=CONFIG["timeout"], ssl=ssl)
    if not r:
        return a
    content_type = (r.headers.get("Content-Type") or "").lower()
    if "pdf" in content_type or looks_like_attachment(a.url):
        a.dettaglio_scaricato = True
        return finalize(a)
    soup = BeautifulSoup(r.text, "html.parser")
    a.testo_completo = clean_page_text(soup)
    a.dettaglio_scaricato = True
    return finalize(a)


def download_best_details(items: list[Annuncio], limit: int) -> None:
    ordered = sorted(items, key=lambda x: (x.art16, x.art1, x.score), reverse=True)
    log.info(f"⬇️  Scarico dettagli per i migliori {min(limit, len(ordered))} annunci...")
    for i, a in enumerate(ordered[:limit], 1):
        log.info(f"   [{i}/{min(limit, len(ordered))}] {a.titolo[:80]}")
        # SSL = False per la maggior parte dei siti PA
        ssl = any(d in a.url for d in ["inpa.gov.it", "gazzettaufficiale.it",
                                        "concorsando.it", "concorsipubblici.com",
                                        "concorsi.it", "unisalento.it"])
        download_detail(a, ssl=ssl)


# ============================================================
# FILTRO FINALE
# ============================================================

def final_filter(items: list[Annuncio]) -> list[Annuncio]:
    out = []
    for a in items:
        finalize(a)
        text = " ".join([a.titolo, a.descrizione, a.testo_completo, a.url]).lower()

        if nav_match(text) or neg_match(text):
            continue
        if expired(a.scadenza):
            continue
        if too_old(a.data_pubblicazione):   # FIX v5: safe su date vuote
            continue

        # FIX v5: geo sempre obbligatorio (anche dopo aver scaricato il dettaglio)
        if a.tipo not in ("ARPAL", "AGGREGATORE") and not geo_match(text):
            continue

        if a.score < 12:   # FIX v5: soglia 12
            continue

        if a.tipo == "ARPAL":
            if cp_match(text) or art16_match(text) or "collocamento mirato" in text:
                out.append(a)
            continue

        if cp_match(text) or art16_match(text):
            out.append(a)

    out.sort(key=lambda x: (
        x.art16, x.art1,
        x.stato == "Aperto",
        parse_date_iso(x.data_pubblicazione) or datetime.min,
        x.score,
    ), reverse=True)
    return out


def filter_new(conn: sqlite3.Connection, items: list[Annuncio]) -> list[Annuncio]:
    return [a for a in items if not seen_url(conn, a.url)]


# ============================================================
# REPORT
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
    if a.descrizione:
        lines += ["", "── SNIPPET ──", a.descrizione[:400]]
    if a.testo_completo:
        lines += ["", "── TESTO COMPLETO ──"]
        lines += [line for line in a.testo_completo[:3000].splitlines() if line.strip()]
    return "\n".join(lines)


def _he(s: str) -> str:
    return html.escape(s or "")


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
    if art16:  parts += ["⭐ ART.16 / CHIAMATA DIRETTA", ""] + [txt_card(a) for a in art16]
    if art1:   parts += ["", "✅ ART.1 / L.68/99", ""] + [txt_card(a) for a in art1]
    if others: parts += ["", "📌 ALTRI RISULTATI", ""] + [txt_card(a) for a in others]

    with open(CONFIG["output_txt"], "w", encoding="utf-8") as f:
        f.write("\n".join(parts))

    with open(CONFIG["output_json"], "w", encoding="utf-8") as f:
        json.dump({
            "aggiornato": now,
            "totale_nuovi": len(items),
            "art16": len(art16), "art1": len(art1),
            "annunci": [asdict(a) for a in items],
        }, f, ensure_ascii=False, indent=2)

    # HTML
    def card_html(a: Annuncio) -> str:
        badges = ""
        if a.art16: badges += '<span class="badge b16">⭐ ART.16 DIRETTO</span> '
        if a.art1:  badges += '<span class="badge b1">✅ ART.1 / L.68</span> '
        if a.stato: badges += f'<span class="badge bst">{_he(a.stato)}</span>'
        testo = (_he(a.testo_completo[:3000]) if a.testo_completo
                 else _he(a.descrizione[:500])).replace("\n","<br>")
        # Evidenzia parole chiave nel testo
        for kw in ["categorie protette","art. 16","art. 1","collocamento mirato",
                   "scadenza","posti","disabil","invalidità","legge 68"]:
            testo = re.sub(f"({re.escape(kw)})", r'<mark>\1</mark>', testo, flags=re.IGNORECASE)
        return f"""
        <div class="card">
          <h3><a href="{_he(a.url)}" target="_blank" rel="noopener">{_he(a.titolo)}</a></h3>
          <div class="meta">
            <span>📌 {_he(a.ente or a.fonte)}</span>
            <span>🏷️ {_he(a.tipo)}</span>
            <span>📅 {_he(a.data_pubblicazione or '-')}</span>
            {'<span class="scad">⚠️ Scadenza: <strong>' + _he(a.scadenza) + '</strong></span>' if a.scadenza else ""}
            {'<span>👥 ' + _he(a.posti) + '</span>' if a.posti else ""}
            <span>Score: {a.score}</span>
          </div>
          <div class="badges">{badges}</div>
          <details>
            <summary>Testo completo ↓</summary>
            <div class="testo">{testo}</div>
          </details>
        </div>"""

    def sezione(emoji, titolo, lista):
        if not lista: return ""
        return f"<h2>{emoji} {titolo} <small>({len(lista)})</small></h2>" + "".join(card_html(a) for a in lista)

    html_doc = f"""<!doctype html>
<html lang="it">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Categorie Protette — Lecce — {now}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#f0f4fb;color:#1a1a1a;line-height:1.6}}
header{{background:#1346a0;color:#fff;padding:20px 28px}}
header h1{{font-size:20px;font-weight:600}}
header p{{font-size:13px;opacity:.8;margin-top:4px}}
.stats{{display:flex;gap:12px;flex-wrap:wrap;padding:16px 28px;background:#dce8ff}}
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
</head>
<body>
<header>
  <h1>🔔 Categorie Protette L.68/99 — Lecce / Salento</h1>
  <p>Aggiornato: {now} · Art.16 (accesso diretto) + Art.1 (collocamento mirato) · 96 comuni + enti</p>
</header>
<div class="stats">
  <div class="stat"><strong>{len(items)}</strong><span>Nuovi annunci</span></div>
  <div class="stat"><strong style="color:#e65100">{len(art16)}</strong><span>Art.16 Diretti</span></div>
  <div class="stat"><strong style="color:#2e7d32">{len(art1)}</strong><span>Art.1 L.68/99</span></div>
  <div class="stat"><strong style="color:#555">{len(others)}</strong><span>Altri concorsi</span></div>
  <div class="stat"><strong style="color:#888">{total_db}</strong><span>Tot. in archivio</span></div>
</div>
<main>
{sezione("⭐", "Art.16 — Accesso Diretto (PRIORITARI)", art16)}
{sezione("✅", "Art.1 L.68/99 — Collocamento Mirato", art1)}
{sezione("📌", "Altri concorsi PA — Lecce / Salento", others)}
</main>
</body>
</html>"""

    with open(CONFIG["output_html"], "w", encoding="utf-8") as f:
        f.write(html_doc)

    log.info(f"Report → {CONFIG['output_txt']} | {CONFIG['output_json']} | {CONFIG['output_html']}")


# ============================================================
# NOTIFICHE
# ============================================================

def send_email(items: list[Annuncio]) -> None:
    if not items or not all([CONFIG["email_destinatario"],
                              CONFIG["email_mittente"],
                              CONFIG["email_password"]]):
        return

    def row(a: Annuncio) -> str:
        c = "#e65100" if a.art16 else "#2e7d32" if a.art1 else "#1346a0"
        tags = " | ".join(filter(None, [
            "⭐ ART.16" if a.art16 else "",
            "✅ ART.1" if a.art1 else "",
            a.stato or "",
        ]))
        snippet = _he(a.testo_completo[:400] if a.testo_completo else a.descrizione[:400])
        return f"""
        <div style="border-left:4px solid {c};background:#f8faff;padding:10px 14px;margin:10px 0;border-radius:0 8px 8px 0">
          <div style="font-weight:700;margin-bottom:4px">{_he(a.titolo)}</div>
          <div style="font-size:12px;color:#555">{_he(a.fonte)} · {_he(a.ente or '-')}</div>
          <div style="font-size:12px;color:#555">
            📅 {_he(a.data_pubblicazione or '-')} · ⚠️ Scadenza: <strong>{_he(a.scadenza or '???')}</strong> · 👥 {_he(a.posti or '-')}
          </div>
          <div style="font-size:12px;margin:4px 0">{tags}</div>
          {'<div style="font-size:12px;color:#333;margin:6px 0">' + snippet + '</div>' if snippet else ''}
          <a href="{_he(a.url)}" style="font-size:12px;color:#1346a0">{_he(a.url[:80])}</a>
        </div>"""

    art16 = [a for a in items if a.art16]
    art1  = [a for a in items if a.art1 and not a.art16]
    altri = [a for a in items if not a.art1 and not a.art16]

    body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:760px;margin:auto">
      <h2 style="color:#1346a0">🔔 Categorie Protette — Lecce / Salento</h2>
      <p>Nuovi: <b>{len(items)}</b> · Art.16: <b>{len(art16)}</b> · Art.1: <b>{len(art1)}</b></p>
      {"<h3>⭐ Art.16 — Accesso Diretto</h3>" + "".join(row(a) for a in art16) if art16 else ""}
      {"<h3>✅ Art.1 L.68/99</h3>" + "".join(row(a) for a in art1) if art1 else ""}
      {"<h3>📌 Altri concorsi</h3>" + "".join(row(a) for a in altri[:15]) if altri else ""}
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
        log.info("📧 Email inviata")
    except Exception as e:
        log.error(f"Email failed → {e}")


def send_telegram(items: list[Annuncio]) -> None:
    token   = CONFIG["telegram_bot_token"]
    chat_id = CONFIG["telegram_chat_id"]
    if not token or not chat_id or not items:
        return

    art16 = [a for a in items if a.art16]
    lines = [
        "🔔 *Categorie Protette — Lecce / Salento*",
        f"Nuovi: *{len(items)}* · Art.16: *{len(art16)}*", "",
    ]
    for a in items[:12]:
        tags = " | ".join(filter(None,[
            "⭐ ART.16" if a.art16 else "",
            "✅ ART.1"  if a.art1  else "",
            a.stato or "",
        ]))
        title = a.titolo[:70].replace("[","(").replace("]",")")
        lines += [f"• *{title}*", f"  `{a.fonte}` {tags}", f"  {a.url}"]

    try:
        r = SESSION.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(lines),
                  "parse_mode": "Markdown", "disable_web_page_preview": True},
            timeout=12,
        )
        r.raise_for_status()
        log.info("📱 Telegram inviato")
    except Exception as e:
        log.error(f"Telegram failed → {e}")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    print("\n" + "═" * 80)
    print("CERCA LAVORO — CATEGORIE PROTETTE L.68/99 — LECCE / SALENTO")
    print("96 comuni + enti pubblici + aggregatori · Art.16 + Art.1")
    print("═" * 80 + "\n")

    conn = init_db(CONFIG["db_path"])
    raw: list[Annuncio] = []

    for fn in [scrape_gazzetta, scrape_inpa]:
        try:
            raw.extend(fn())
        except Exception as e:
            log.error(f"{fn.__name__} → {e}")

    enti = all_entities()
    log.info(f"Enti da scandire: {len(enti)}")
    for entity in enti:
        try:
            raw.extend(scrape_entity(entity))
        except Exception as e:
            log.error(f"Entity {entity['nome']} → {e}")

    log.info(f"Totale grezzo: {len(raw)}")

    stage1 = dedup_by_url(raw)
    log.info(f"Dopo dedup URL: {len(stage1)}")

    stage1.sort(key=lambda x: x.score, reverse=True)
    download_best_details(stage1, CONFIG["max_dettagli"])

    stage2 = [finalize(a) for a in stage1]
    stage3 = dedup_by_key(stage2)
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

    print(f"\n✅ Completato — nuovi risultati pertinenti: {len(new_items)}")
    print(f"   ⭐ Art.16: {sum(1 for a in new_items if a.art16)}")
    print(f"   ✅ Art.1:  {sum(1 for a in new_items if a.art1 and not a.art16)}")
    print(f"   📄 TXT:   {CONFIG['output_txt']}")
    print(f"   🌐 HTML:  {CONFIG['output_html']}")
    print(f"   🗂️  DB:    {CONFIG['db_path']}")
    conn.close()


if __name__ == "__main__":
    main()
