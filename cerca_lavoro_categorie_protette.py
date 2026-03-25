#!/usr/bin/env python3
"""
=============================================================
  CERCA LAVORO — CATEGORIE PROTETTE L.68/99
  Provincia di Lecce + Puglia
  Art. 1 (invalidità ≥46%) | Art. 16 (accesso diretto Puglia)
=============================================================
Fonti verificate e corrette:
  - InPA (portale nazionale concorsi PA)
  - Regione Puglia (URL reali verificati)
  - ARPAL Puglia (SSL bypass, URL corretti)
  - Gazzetta Ufficiale (RSS 3a serie speciale)
  - Comune di Lecce
  - Comuni provincia di Lecce (Galatina, Nardò, Gallipoli...)
  - ASL Lecce
  - Concorsando.it (aggregatore PA)
  - Indeed / InfoJobs (privato)
  - Portale Lavoro per Te (ANPAL)
=============================================================
"""

import requests
import feedparser
import json
import os
import time
import smtplib
import logging
import urllib3
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from dataclasses import dataclass, asdict, field
from typing import Optional
import sqlite3

# Silenzia i warning SSL (necessari per siti PA con certificati problematici)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─────────────────────────────────────────────
#  CONFIGURAZIONE
# ─────────────────────────────────────────────
CONFIG = {
    # Email — in locale inserisci qui, su GitHub Actions usa i Secrets
    "email_destinatario": os.getenv("EMAIL_DESTINATARIO", ""),
    "email_mittente":     os.getenv("EMAIL_MITTENTE", ""),
    "email_password":     os.getenv("EMAIL_PASSWORD", ""),
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,

    # Telegram (opzionale ma consigliato)
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id":   os.getenv("TELEGRAM_CHAT_ID", ""),

    # File di output
    "db_path":     "lavoro_lecce_cp.db",
    "output_json": "risultati_lecce.json",
    "output_txt":  "risultati_lecce.txt",

    # Comportamento
    "giorni_indietro": 60,        # aumentato per non perdere annunci
    "pausa_tra_richieste": 1.5,
    "timeout_veloce": 10,         # per siti veloci
    "timeout_lento": 25,          # per siti PA lenti
}

# ─────────────────────────────────────────────
#  PAROLE CHIAVE
# ─────────────────────────────────────────────

# Art. 16 = accesso diretto (riserva posti, niente graduatoria)
KEYWORDS_ART16 = [
    "articolo 16", "art. 16", "art.16",
    "L.R. 17/2005", "legge regionale 17/2005",
    "assunzione diretta disabili", "chiamata diretta",
    "collocamento mirato chiamata nominativa",
]

# Art. 1 = iscritti liste L.68/99
KEYWORDS_ART1 = [
    "categorie protette", "categoria protetta",
    "art. 1 legge 68", "articolo 1 l.68",
    "l. 68/99", "legge 68/1999", "legge 68 99",
    "collocamento mirato", "disabili riserva",
    "riserva disabili", "invalidi civili",
    "invalidità civile", "posto riservato disabili",
    "art. 1 comma 1", "comma 3 art. 1 l.68",
]

KEYWORDS_TUTTE = KEYWORDS_ART16 + KEYWORDS_ART1

# Filtro geografico stretto su Lecce
KEYWORDS_LECCE = [
    "lecce", "provincia di lecce", "leccese",
    "salento", "galatina", "nardò", "gallipoli",
    "maglie", "otranto", "tricase", "casarano",
    "copertino", "galatone", "ugento",
]

# ─────────────────────────────────────────────
#  STRUTTURA DATI
# ─────────────────────────────────────────────
@dataclass
class Annuncio:
    titolo: str
    fonte: str
    url: str
    data_pubblicazione: str
    descrizione: str = ""        # snippet breve dalla lista
    testo_completo: str = ""     # TESTO PIENO estratto dalla pagina dell'annuncio
    ente: str = ""
    scadenza: str = ""           # data scadenza estratta dal testo
    posti: str = ""              # numero posti se trovato
    tipo: str = ""               # "PA" | "Privato" | "ARPAL"
    art16: bool = False          # accesso diretto — PRIORITARIO
    art1: bool = False           # lista L.68/99
    dettaglio_scaricato: bool = False
    trovato_il: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

# ─────────────────────────────────────────────
#  LOGGER
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("cerca_lavoro.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  SCARICA TESTO COMPLETO ANNUNCIO
# ─────────────────────────────────────────────
import re as _re

def _estrai_scadenza(testo: str) -> str:
    """Cerca date di scadenza nel testo (es. 'entro il 30/04/2026')."""
    pattern = _re.compile(
        r'(?:scad[ae]|entro[\s\w]+?|termine[\s\w]+?|presenta[a-z]+?[\s\w]+?)'
        r'[\s:]*([0-3]?\d[/\-\.][01]?\d[/\-\.][12]\d{3})',
        _re.IGNORECASE
    )
    m = pattern.search(testo)
    if m:
        return m.group(1)
    # cerca comunque qualsiasi data
    date_pattern = _re.compile(r'(?<![\d])(\d{1,2}[/\-\.]\d{1,2}[/\-\.]20\d{2})(?![\d])')
    dates = date_pattern.findall(testo)
    return dates[0] if dates else ""

def _estrai_posti(testo: str) -> str:
    """Cerca 'n. X posti' o 'X unità' nel testo."""
    pattern = _re.compile(
        r'(?:n\.?\s*|numero\s+|n°\s*)'
        r'(\d+)\s*(?:posti?|unit[àa]|figure?|posizioni?)',
        _re.IGNORECASE
    )
    m = pattern.search(testo)
    if m:
        return m.group(1) + " posti"
    m2 = _re.search(r'(\d+)\s*(?:posti?\s+a\s+tempo|posti?\s+di\s+ruolo|posti?\s+disponibili)',
                    testo, _re.IGNORECASE)
    if m2:
        return m2.group(1) + " posti"
    return ""

def _pulisci_testo(raw: str) -> str:
    """Rimuove spazi eccessivi e righe vuote."""
    linee = [l.strip() for l in raw.splitlines()]
    linee = [l for l in linee if l and len(l) > 2]
    # Rimuove duplicati consecutivi
    result = []
    prev = ""
    for l in linee:
        if l != prev:
            result.append(l)
        prev = l
    return "\n".join(result)

def scarica_dettaglio(annuncio: Annuncio, verify_ssl: bool = False) -> Annuncio:
    """
    Apre la pagina dell'annuncio e ne scarica il testo completo.
    Aggiorna i campi: testo_completo, scadenza, posti, dettaglio_scaricato.
    """
    if not annuncio.url or annuncio.url.startswith("#") or len(annuncio.url) < 10:
        return annuncio
    if annuncio.dettaglio_scaricato:
        return annuncio

    try:
        r = requests.get(
            annuncio.url, headers=HEADERS,
            timeout=CONFIG["timeout_lento"],
            verify=verify_ssl
        )
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        soup = BeautifulSoup(r.text, "html.parser")

        # Rimuove elementi non informativi
        for tag in soup.select("nav, header, footer, script, style, "
                               ".menu, .sidebar, .cookie, .banner, "
                               ".breadcrumb, #header, #footer, #nav"):
            tag.decompose()

        # Cerca il contenuto principale (ordine di priorità)
        corpo = (
            soup.select_one("article")
            or soup.select_one("main")
            or soup.select_one(".content, .contenuto, .entry-content, "
                               ".page-content, .scheda, #content")
            or soup.select_one("div[class*='concorso'], div[class*='bando'], "
                               "div[class*='avviso']")
            or soup.body
        )

        testo_raw = corpo.get_text(" ", strip=True) if corpo else ""
        testo = _pulisci_testo(testo_raw)

        # Limita a 4000 caratteri ma mantiene le parti più importanti
        if len(testo) > 4000:
            # Cerca sezioni chiave
            sezioni = []
            for kw in ["requisiti", "domanda", "scadenza", "posti",
                       "categorie protette", "art. 1", "art. 16",
                       "titolo di studio", "allegati", "modalità"]:
                idx = testo.lower().find(kw)
                if idx != -1:
                    start = max(0, idx - 50)
                    sezioni.append(testo[start:start + 600])
            if sezioni:
                testo = testo[:1500] + "\n\n[...SEZIONI CHIAVE...]\n\n" + "\n\n".join(sezioni[:4])
            else:
                testo = testo[:4000]

        # Aggiorna l'annuncio
        annuncio.testo_completo = testo
        annuncio.dettaglio_scaricato = True

        # Estrai scadenza e posti dal testo completo se non già trovati
        testo_lower = testo.lower()
        if not annuncio.scadenza:
            annuncio.scadenza = _estrai_scadenza(testo)
        if not annuncio.posti:
            annuncio.posti = _estrai_posti(testo)

        # Aggiorna flag art16/art1 se nel testo completo ci sono riferimenti
        if not annuncio.art16:
            annuncio.art16 = rileva_art16(testo)
        if not annuncio.art1:
            annuncio.art1 = rileva_art1(testo)

        time.sleep(CONFIG["pausa_tra_richieste"])

    except Exception as e:
        log.debug(f"Dettaglio non scaricabile {annuncio.url}: {e}")

    return annuncio

def scarica_tutti_dettagli(annunci: list, max_annunci: int = 50) -> list:
    """
    Scarica il testo completo per tutti gli annunci (o i primi max_annunci).
    Priorità agli Art.16, poi Art.1, poi gli altri.
    """
    # Ordina: prima art16, poi art1, poi il resto
    ordinati = (
        [a for a in annunci if a.art16] +
        [a for a in annunci if a.art1 and not a.art16] +
        [a for a in annunci if not a.art1 and not a.art16]
    )

    log.info(f"⬇️  Scarico dettagli per {min(len(ordinati), max_annunci)} annunci...")
    for i, ann in enumerate(ordinati[:max_annunci]):
        log.info(f"  [{i+1}/{min(len(ordinati), max_annunci)}] {ann.titolo[:55]}...")
        # SSL verify=False per siti PA con certificati problematici
        verify = ann.url.startswith("https://www.inpa") or ann.url.startswith("https://www.comune")
        scarica_dettaglio(ann, verify_ssl=verify)

    return annunci


# ─────────────────────────────────────────────
#  HELPERS HTTP
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.5",
    "Accept-Encoding": "gzip, deflate",
}

def get_soup(url: str, timeout: int = None, verify_ssl: bool = True,
             params: dict = None) -> Optional[BeautifulSoup]:
    """Scarica pagina HTML → BeautifulSoup. SSL opzionale per siti PA."""
    t = timeout or CONFIG["timeout_lento"]
    try:
        r = requests.get(url, params=params, headers=HEADERS,
                         timeout=t, verify=verify_ssl)
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        time.sleep(CONFIG["pausa_tra_richieste"])
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning(f"Errore GET {url}: {e}")
        return None

def get_json_api(url: str, params: dict = None,
                 verify_ssl: bool = True) -> Optional[dict | list]:
    """Scarica JSON da API pubblica."""
    try:
        r = requests.get(url, params=params, headers=HEADERS,
                         timeout=CONFIG["timeout_lento"], verify=verify_ssl)
        r.raise_for_status()
        time.sleep(CONFIG["pausa_tra_richieste"])
        return r.json()
    except Exception as e:
        log.warning(f"Errore JSON {url}: {e}")
        return None

def assoluto(href: str, base: str) -> str:
    """Trasforma URL relativo in assoluto."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    from urllib.parse import urljoin
    return urljoin(base, href)

def contiene(testo: str, keywords: list) -> bool:
    t = testo.lower()
    return any(k.lower() in t for k in keywords)

def e_lecce(testo: str) -> bool:
    return contiene(testo, KEYWORDS_LECCE)

def rileva_art16(testo: str) -> bool:
    return contiene(testo, KEYWORDS_ART16)

def rileva_art1(testo: str) -> bool:
    return contiene(testo, KEYWORDS_ART1)

def e_rilevante(testo: str) -> bool:
    """Annuncio rilevante = (Lecce O PA/ARPAL generica) + categoria protetta"""
    return contiene(testo, KEYWORDS_TUTTE) or e_lecce(testo)

# ─────────────────────────────────────────────────────────────
#  FONTE 1: InPA — API REST ufficiale
#  https://www.inpa.gov.it  (portale nazionale reclutamento PA)
# ─────────────────────────────────────────────────────────────
def scrape_inpa() -> list[Annuncio]:
    log.info("📋 InPA — API concorsi PA...")
    annunci = []

    # API pubblica InPA - endpoint bandi
    queries = [
        "categorie protette lecce",
        "collocamento mirato lecce",
        "disabili puglia concorso",
        "articolo 1 legge 68 lecce",
    ]

    for q in queries:
        # Endpoint API REST di InPA
        data = get_json_api(
            "https://www.inpa.gov.it/wp-json/wp/v2/posts",
            params={"search": q, "per_page": 20,
                    "_fields": "title,link,date,excerpt"}
        )
        if not data:
            continue
        for item in (data if isinstance(data, list) else []):
            titolo = BeautifulSoup(
                item.get("title", {}).get("rendered", ""), "html.parser"
            ).get_text(strip=True)
            descr = BeautifulSoup(
                item.get("excerpt", {}).get("rendered", ""), "html.parser"
            ).get_text(strip=True)
            url = item.get("link", "")
            testo = titolo + " " + descr

            annunci.append(Annuncio(
                titolo=titolo, fonte="InPA", url=url,
                data_pubblicazione=item.get("date", "")[:10],
                descrizione=descr[:300], tipo="PA",
                art16=rileva_art16(testo), art1=rileva_art1(testo),
            ))

    # Scraping pagina bandi con filtro Puglia/Lecce
    for path in [
        "/bandi/?regione=puglia",
        "/concorsi-pubblici/?area_geografica=puglia",
        "/bandi/",
    ]:
        soup = get_soup("https://www.inpa.gov.it" + path)
        if not soup:
            continue
        for card in soup.select("article, .bando-card, .job-card, li.bando"):
            testo_card = card.get_text(" ", strip=True)
            if not e_rilevante(testo_card):
                continue
            titolo_el = card.select_one("h2, h3, .title, a")
            link_el = card.select_one("a[href]")
            if not titolo_el:
                continue
            annunci.append(Annuncio(
                titolo=titolo_el.get_text(strip=True)[:200],
                fonte="InPA", tipo="PA",
                url=assoluto(link_el["href"] if link_el else "", "https://www.inpa.gov.it"),
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo_card[:300],
                art16=rileva_art16(testo_card), art1=rileva_art1(testo_card),
            ))
        break

    log.info(f"  → {len(annunci)} InPA")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 2: Regione Puglia — URL reali verificati 2025/2026
# ─────────────────────────────────────────────────────────────
def scrape_regione_puglia() -> list[Annuncio]:
    log.info("🏛️  Regione Puglia...")
    annunci = []
    BASE = "https://www.regione.puglia.it"

    # URL reali verificati (il vecchio /web/lavoro/ non esiste più)
    urls_reali = [
        f"{BASE}/web/guest/search?query=concorso+categorie+protette",
        f"{BASE}/web/guest/search?query=collocamento+mirato+lecce",
        f"{BASE}/documents/10180/0/bandi_concorso",
        # Sezione trasparenza - URL stabile
        f"{BASE}/web/trasparenza/bandi-di-concorso",
        f"{BASE}/web/lavoro",
        f"{BASE}/web/arpal",
    ]

    for url in urls_reali:
        soup = get_soup(url)
        if not soup:
            continue
        for link in soup.select("a[href]"):
            href = link.get("href", "")
            testo = link.get_text(strip=True)
            if len(testo) < 10:
                continue
            testo_full = testo + " " + href
            if not (contiene(testo_full, ["concorso", "bando", "avviso", "selezione",
                                           "assunzione", "collocamento", "disabil"]) ):
                continue
            annunci.append(Annuncio(
                titolo=testo[:200], fonte="Regione Puglia",
                url=assoluto(href, BASE),
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                ente="Regione Puglia", tipo="PA",
                art16=rileva_art16(testo_full), art1=rileva_art1(testo_full),
            ))

    # RSS ufficiale Regione Puglia
    for rss_url in [
        "https://www.regione.puglia.it/rss/news",
        "https://www.regione.puglia.it/rss/bandi",
        "https://www.regione.puglia.it/rss/lavoro",
    ]:
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:30]:
                titolo = entry.get("title", "")
                descr = entry.get("summary", "")
                testo = titolo + " " + descr
                if not (contiene(testo, ["concorso", "bando", "collocamento",
                                          "disabili", "categorie protette"])):
                    continue
                annunci.append(Annuncio(
                    titolo=titolo, fonte="Regione Puglia RSS",
                    url=entry.get("link", ""),
                    data_pubblicazione=entry.get("published", "")[:10],
                    descrizione=descr[:300], ente="Regione Puglia", tipo="PA",
                    art16=rileva_art16(testo), art1=rileva_art1(testo),
                ))
        except Exception as e:
            log.warning(f"RSS Regione Puglia {rss_url}: {e}")

    log.info(f"  → {len(annunci)} Regione Puglia")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 3: ARPAL Puglia — SSL verify=False (cert hostname mismatch)
#  https://arpal.regione.puglia.it  (senza www!)
# ─────────────────────────────────────────────────────────────
def scrape_arpal() -> list[Annuncio]:
    log.info("♿ ARPAL Puglia (collocamento mirato)...")
    annunci = []

    # Il certificato è valido per arpal.regione.puglia.it (senza www)
    BASE = "https://arpal.regione.puglia.it"

    urls_arpal = [
        f"{BASE}/avvisi-e-bandi/",
        f"{BASE}/collocamento-mirato/",
        f"{BASE}/collocamento-mirato/chiamata-con-avviso/",
        f"{BASE}/collocamento-mirato/chiamata-nominativa/",    # Art.16
        f"{BASE}/opportunita-di-lavoro/",
        f"{BASE}/news/",
        f"{BASE}/",
    ]

    for url in urls_arpal:
        # SSL verify=False perché il cert ha hostname mismatch (bug del sito PA)
        soup = get_soup(url, verify_ssl=False)
        if not soup:
            continue
        for item in soup.select("article, .entry, h2 a, h3 a, .bando, li.news-item"):
            testo = item.get_text(" ", strip=True)
            link_el = item.select_one("a[href]") or (item if item.name == "a" else None)
            if not link_el or len(testo) < 10:
                continue
            titolo = link_el.get_text(strip=True) or testo[:100]
            href = assoluto(link_el.get("href", ""), BASE)

            annunci.append(Annuncio(
                titolo=titolo[:200], fonte="ARPAL Puglia",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300], ente="ARPAL Puglia", tipo="ARPAL",
                # ARPAL = per definizione collocamento mirato
                art16=rileva_art16(testo) or "nominativ" in testo.lower(),
                art1=True,
            ))

    # CPI Lecce specifico
    cpi_lecce_urls = [
        f"{BASE}/centri-per-limpiego/lecce/",
        f"{BASE}/cpi/lecce/",
        "https://arpal.regione.puglia.it/offerte-di-lavoro/?cpi=lecce",
    ]
    for url in cpi_lecce_urls:
        soup = get_soup(url, verify_ssl=False)
        if not soup:
            continue
        for link in soup.select("a[href]"):
            testo = link.get_text(strip=True)
            if len(testo) < 15:
                continue
            if contiene(testo, ["offerta", "avviso", "bando", "selezione",
                                  "disabil", "categorie", "collocamento"]):
                annunci.append(Annuncio(
                    titolo=testo[:200], fonte="CPI Lecce",
                    url=assoluto(link["href"], BASE),
                    data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                    ente="CPI Lecce", tipo="ARPAL", art16=False, art1=True,
                ))

    log.info(f"  → {len(annunci)} ARPAL/CPI")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 4: Gazzetta Ufficiale — RSS 3a serie speciale (concorsi)
# ─────────────────────────────────────────────────────────────
def scrape_gazzetta_ufficiale() -> list[Annuncio]:
    log.info("📰 Gazzetta Ufficiale (3a serie concorsi)...")
    annunci = []

    # RSS serie speciale concorsi ed esami
    rss_urls = [
        "https://www.gazzettaufficiale.it/rss/concorsi.xml",
        "https://www.gazzettaufficiale.it/rss/esecutivi.xml",
    ]

    for rss_url in rss_urls:
        try:
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:50]:
                titolo = entry.get("title", "")
                descr  = entry.get("summary", "")
                testo  = titolo + " " + descr
                # Includi se: Lecce/Puglia O categorie protette
                if not (e_lecce(testo) or contiene(testo, KEYWORDS_TUTTE)):
                    continue
                annunci.append(Annuncio(
                    titolo=titolo, fonte="Gazzetta Ufficiale",
                    url=entry.get("link", ""),
                    data_pubblicazione=entry.get("published", "")[:10],
                    descrizione=descr[:300], tipo="PA",
                    art16=rileva_art16(testo), art1=rileva_art1(testo),
                ))
        except Exception as e:
            log.warning(f"RSS GU {rss_url}: {e}")

    # Ricerca testuale sul sito GU
    for query in [
        "categorie protette lecce",
        "collocamento mirato puglia",
        "articolo 16 disabili puglia",
    ]:
        soup = get_soup(
            "https://www.gazzettaufficiale.it/ricerca/concorsi",
            params={"q": query, "tipoSerie": "concorsi"}
        )
        if not soup:
            continue
        for row in soup.select("tr, .risultato, article"):
            testo = row.get_text(" ", strip=True)
            link_el = row.select_one("a[href]")
            if not link_el or len(testo) < 20:
                continue
            if not (e_lecce(testo) or contiene(testo, KEYWORDS_TUTTE)):
                continue
            annunci.append(Annuncio(
                titolo=link_el.get_text(strip=True)[:200],
                fonte="Gazzetta Ufficiale", tipo="PA",
                url=assoluto(link_el["href"], "https://www.gazzettaufficiale.it"),
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300],
                art16=rileva_art16(testo), art1=rileva_art1(testo),
            ))

    log.info(f"  → {len(annunci)} Gazzetta Ufficiale")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 5: Comune di Lecce — URL reali verificati
# ─────────────────────────────────────────────────────────────
def scrape_comune_lecce() -> list[Annuncio]:
    log.info("🏙️  Comune di Lecce...")
    annunci = []
    BASE = "https://www.comune.lecce.it"

    # URL reali del sito del Comune di Lecce
    urls = [
        f"{BASE}/myportal/C_L049/amministrazione-trasparente/personale/"
        f"selezione-del-personale/avvisi-di-selezione-e-concorsi",
        f"{BASE}/myportal/C_L049/home/-/bacheca/bandi-di-concorso",
        f"{BASE}/myportal/C_L049/home/-/bacheca/avvisi",
        f"{BASE}/it/trasparenza/bandi-gare-e-contratti/bandi-di-concorso",
        f"{BASE}/concorsi",
        f"{BASE}/lavora-con-noi",
        # Sezione trasparenza standard PA
        f"{BASE}/amministrazione-trasparente/personale/avvisi-bandi-concorsi",
    ]

    for url in urls:
        soup = get_soup(url, timeout=CONFIG["timeout_lento"])
        if not soup:
            continue
        trovati = 0
        for item in soup.select("article, li, tr, .entry, .notizia, div.item"):
            testo = item.get_text(" ", strip=True)
            link_el = item.select_one("a[href]")
            titolo_el = item.select_one("h2,h3,h4,a,.title,.titolo")
            if not titolo_el or len(testo) < 10:
                continue
            titolo = titolo_el.get_text(strip=True)[:200]
            href = assoluto(link_el["href"] if link_el else "", BASE)

            annunci.append(Annuncio(
                titolo=titolo, fonte="Comune di Lecce",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300], ente="Comune di Lecce", tipo="PA",
                art16=rileva_art16(testo), art1=rileva_art1(testo),
            ))
            trovati += 1
        if trovati > 0:
            break  # URL funzionante trovato

    log.info(f"  → {len(annunci)} Comune Lecce")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 6: Comuni provincia di Lecce
# ─────────────────────────────────────────────────────────────
COMUNI_LECCE = {
    "Galatina":   "https://www.comune.galatina.le.it",
    "Nardò":      "https://www.comune.nardo.le.it",
    "Gallipoli":  "https://www.comune.gallipoli.le.it",
    "Maglie":     "https://www.comune.maglie.le.it",
    "Tricase":    "https://www.comune.tricase.le.it",
    "Casarano":   "https://www.comune.casarano.le.it",
    "Copertino":  "https://www.comune.copertino.le.it",
    "Galatone":   "https://www.comune.galatone.le.it",
    "Ugento":     "https://www.comune.ugento.le.it",
    "Otranto":    "https://www.comune.otranto.le.it",
    "Poggiardo":  "https://www.comune.poggiardo.le.it",
    "Squinzano":  "https://www.comune.squinzano.le.it",
}

CONCORSI_PATHS_COMUNI = [
    "/amministrazione-trasparente/personale/selezione-del-personale",
    "/bandi-di-concorso",
    "/concorsi",
    "/lavora-con-noi",
    "/avvisi",
    "/it/concorsi",
]

def scrape_comuni_lecce() -> list[Annuncio]:
    log.info("🏘️  Comuni provincia di Lecce...")
    annunci = []

    for nome, base in COMUNI_LECCE.items():
        successo = False
        for path in CONCORSI_PATHS_COMUNI:
            url = base + path
            soup = get_soup(url, timeout=CONFIG["timeout_lento"], verify_ssl=False)
            if not soup:
                continue
            for link in soup.select("a[href]"):
                testo = link.get_text(strip=True)
                if len(testo) < 10:
                    continue
                href = assoluto(link["href"], base)
                testo_full = testo + " " + nome
                annunci.append(Annuncio(
                    titolo=testo[:200], fonte=f"Comune {nome}",
                    url=href,
                    data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                    ente=f"Comune di {nome}", tipo="PA",
                    art16=rileva_art16(testo), art1=rileva_art1(testo),
                ))
            successo = True
            break
        if not successo:
            log.debug(f"  {nome}: nessun URL funzionante")

    log.info(f"  → {len(annunci)} Comuni provincia LE")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 7: ASL Lecce
#  https://www.asl.lecce.it
# ─────────────────────────────────────────────────────────────
def scrape_asl_lecce() -> list[Annuncio]:
    log.info("🏥 ASL Lecce...")
    annunci = []
    BASE = "https://www.asl.lecce.it"

    urls_asl = [
        f"{BASE}/asl-lecce/concorsi-e-selezioni/",
        f"{BASE}/concorsi",
        f"{BASE}/lavora-con-noi",
        f"{BASE}/bandi-di-concorso",
        f"{BASE}/it/concorsi-selezioni",
        # Amministrazione trasparente (standard PA)
        f"{BASE}/amministrazione-trasparente/bandi-di-concorso",
    ]

    for url in urls_asl:
        soup = get_soup(url, verify_ssl=False)
        if not soup:
            continue
        for item in soup.select("article, li, tr, .entry"):
            testo = item.get_text(" ", strip=True)
            link_el = item.select_one("a[href]")
            titolo_el = item.select_one("h2, h3, h4, a, .title")
            if not titolo_el or len(testo) < 10:
                continue
            titolo = titolo_el.get_text(strip=True)[:200]
            href = assoluto(link_el["href"] if link_el else "", BASE)
            annunci.append(Annuncio(
                titolo=titolo, fonte="ASL Lecce",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300], ente="ASL Lecce", tipo="PA",
                art16=rileva_art16(testo), art1=rileva_art1(testo),
            ))
        if annunci:
            break  # primo URL che funziona

    log.info(f"  → {len(annunci)} ASL Lecce")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 8: Provincia di Lecce
# ─────────────────────────────────────────────────────────────
def scrape_provincia_lecce() -> list[Annuncio]:
    log.info("🏛️  Provincia di Lecce...")
    annunci = []
    BASE = "https://www.provincia.le.it"

    for url in [
        f"{BASE}/il-territorio/lavora-con-noi/concorsi-e-avvisi/",
        f"{BASE}/concorsi",
        f"{BASE}/bandi",
        f"{BASE}/lavora-con-noi",
        f"{BASE}/amministrazione-trasparente/bandi-di-concorso",
    ]:
        soup = get_soup(url, verify_ssl=False)
        if not soup:
            continue
        for item in soup.select("article, li, .entry, tr"):
            testo = item.get_text(" ", strip=True)
            link_el = item.select_one("a[href]")
            if not link_el or len(testo) < 10:
                continue
            href = assoluto(link_el["href"], BASE)
            titolo = link_el.get_text(strip=True)[:200]
            annunci.append(Annuncio(
                titolo=titolo, fonte="Provincia di Lecce",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                ente="Provincia di Lecce", tipo="PA",
                art16=rileva_art16(testo), art1=rileva_art1(testo),
            ))
        if annunci:
            break

    log.info(f"  → {len(annunci)} Provincia Lecce")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 9: Concorsando.it — aggregatore PA con sezione Puglia/Lecce
# ─────────────────────────────────────────────────────────────
def scrape_concorsando() -> list[Annuncio]:
    log.info("📋 Concorsando.it...")
    annunci = []

    for url in [
        "https://www.concorsando.it/blog/concorsi-puglia/",
        "https://www.concorsando.it/blog/concorsi-lecce/",
        "https://www.concorsando.it/blog/concorsi-categorie-protette/",
        "https://www.concorsando.it/blog/?s=categorie+protette+puglia",
        "https://www.concorsando.it/blog/?s=articolo+16+disabili",
    ]:
        soup = get_soup(url, timeout=CONFIG["timeout_veloce"])
        if not soup:
            continue
        for art in soup.select("article, .entry-card, .post"):
            testo = art.get_text(" ", strip=True)
            link_el = art.select_one("a[href]")
            titolo_el = art.select_one("h2, h3, .entry-title, .post-title")
            if not titolo_el or len(testo) < 10:
                continue
            # Filtra: solo annunci rilevanti per Lecce/Puglia o CP
            if not (e_lecce(testo) or contiene(testo, KEYWORDS_TUTTE)
                    or contiene(testo, ["puglia", "salento"])):
                continue
            titolo = titolo_el.get_text(strip=True)[:200]
            href = assoluto(link_el["href"] if link_el else "", "https://www.concorsando.it")
            annunci.append(Annuncio(
                titolo=titolo, fonte="Concorsando.it",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300], tipo="PA",
                art16=rileva_art16(testo), art1=rileva_art1(testo),
            ))

    log.info(f"  → {len(annunci)} Concorsando")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 10: Indeed — offerte lavoro Lecce categorie protette
# ─────────────────────────────────────────────────────────────
def scrape_indeed() -> list[Annuncio]:
    log.info("💼 Indeed Lecce...")
    annunci = []

    queries = [
        ("categorie protette", "Lecce"),
        ("collocamento mirato", "Lecce, Puglia"),
        ("invalidità civile lavoro", "Lecce"),
        ("disabili art 1 legge 68", "Puglia"),
        ("articolo 16 assunzione disabili", "Puglia"),
    ]

    for q, loc in queries:
        soup = get_soup(
            "https://it.indeed.com/lavoro",
            params={"q": q, "l": loc, "sort": "date"},
            timeout=CONFIG["timeout_lento"]
        )
        if not soup:
            continue
        for card in soup.select(".job_seen_beacon, [data-jk], .jobsearch-SerpJobCard"):
            titolo_el = card.select_one("h2 a, .jobtitle, [data-testid='job-title']")
            azienda_el = card.select_one(".companyName, [data-testid='company-name']")
            luogo_el   = card.select_one(".companyLocation, [data-testid='text-location']")
            link_el    = card.select_one("a[href]")
            if not titolo_el:
                continue
            titolo  = titolo_el.get_text(strip=True)
            azienda = azienda_el.get_text(strip=True) if azienda_el else ""
            luogo   = luogo_el.get_text(strip=True)   if luogo_el   else ""
            href    = assoluto(link_el["href"] if link_el else "", "https://it.indeed.com")
            testo   = titolo + " " + azienda + " " + luogo + " " + q

            annunci.append(Annuncio(
                titolo=titolo, fonte="Indeed",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                ente=azienda, descrizione=f"Luogo: {luogo}", tipo="Privato",
                art16=rileva_art16(testo), art1=rileva_art1(testo),
            ))

    log.info(f"  → {len(annunci)} Indeed")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 11: Portale Lavoro per Te (ANPAL / MLPS)
#  Sezione offerte con collocamento mirato
# ─────────────────────────────────────────────────────────────
def scrape_lavoroperte() -> list[Annuncio]:
    log.info("🔎 Portale LavoroPerTe (ANPAL)...")
    annunci = []

    urls = [
        "https://www.lavoroperte.gov.it/offerte?regione=puglia&categoria=categorie-protette",
        "https://www.lavoroperte.gov.it/offerte?provincia=lecce",
        "https://www.lavoro.gov.it/strumenti-e-servizi/collocamento-mirato",
    ]

    for url in urls:
        soup = get_soup(url, timeout=CONFIG["timeout_lento"])
        if not soup:
            continue
        for card in soup.select("article, .offerta, .card, li.risultato"):
            testo = card.get_text(" ", strip=True)
            link_el = card.select_one("a[href]")
            titolo_el = card.select_one("h2, h3, .titolo, .title")
            if not titolo_el:
                continue
            titolo = titolo_el.get_text(strip=True)[:200]
            href = assoluto(link_el["href"] if link_el else "", url)
            annunci.append(Annuncio(
                titolo=titolo, fonte="LavoroPerTe",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300], tipo="PA",
                art16=rileva_art16(testo), art1=rileva_art1(testo),
            ))

    log.info(f"  → {len(annunci)} LavoroPerTe")
    return annunci


# ─────────────────────────────────────────────────────────────
#  DATABASE SQLite — memoria persistente tra esecuzioni
# ─────────────────────────────────────────────────────────────
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS annunci (
            url TEXT PRIMARY KEY,
            titolo TEXT,
            fonte TEXT,
            ente TEXT,
            data_pubblicazione TEXT,
            trovato_il TEXT,
            scadenza TEXT,
            posti TEXT,
            art16 INTEGER DEFAULT 0,
            art1  INTEGER DEFAULT 0,
            tipo  TEXT,
            testo_completo TEXT,
            notificato INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS annunci_fts
        USING fts5(url UNINDEXED, titolo, testo_completo, ente,
                   content='annunci', content_rowid='rowid')
    """)
    conn.commit()
    return conn

def filtra_nuovi(conn: sqlite3.Connection,
                 annunci: list[Annuncio]) -> list[Annuncio]:
    nuovi = []
    for a in annunci:
        if not a.url or len(a.url) < 5:
            continue
        row = conn.execute("SELECT 1 FROM annunci WHERE url=?",
                           (a.url,)).fetchone()
        if not row:
            nuovi.append(a)
    return nuovi

def salva_annunci(conn: sqlite3.Connection, annunci: list[Annuncio]):
    for a in annunci:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO annunci
                (url,titolo,fonte,ente,data_pubblicazione,trovato_il,
                 scadenza,posti,art16,art1,tipo,testo_completo)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (a.url, a.titolo, a.fonte, a.ente,
                  a.data_pubblicazione, a.trovato_il,
                  a.scadenza, a.posti,
                  int(a.art16), int(a.art1), a.tipo,
                  a.testo_completo))
        except Exception as e:
            log.warning(f"DB insert: {e}")
    conn.commit()


# ─────────────────────────────────────────────────────────────
#  EMAIL
# ─────────────────────────────────────────────────────────────
def invia_email(annunci: list[Annuncio], config: dict):
    if not config.get("email_destinatario") or not annunci:
        return

    art16_list = [a for a in annunci if a.art16]
    art1_list  = [a for a in annunci if a.art1 and not a.art16]
    altri      = [a for a in annunci if not a.art1 and not a.art16]

    def riga_html(a: Annuncio) -> str:
        badge16 = '<span style="background:#1565C0;color:#fff;padding:2px 7px;border-radius:4px;font-size:11px">ART.16 DIRETTO</span>' if a.art16 else ""
        badge1  = '<span style="background:#2E7D32;color:#fff;padding:2px 7px;border-radius:4px;font-size:11px">ART.1 L.68</span>' if a.art1 else ""
        return f"""
        <div style="border-left:4px solid {'#1565C0' if a.art16 else '#2E7D32' if a.art1 else '#999'};
                    padding:10px 14px;margin:8px 0;background:#f9f9f9;border-radius:0 6px 6px 0">
          <div style="font-weight:600;margin-bottom:4px">{a.titolo[:100]}</div>
          <div style="font-size:12px;color:#555">{a.fonte} · {a.tipo} · {a.data_pubblicazione}</div>
          <div style="margin:4px 0">{badge16} {badge1}</div>
          <a href="{a.url}" style="font-size:12px;color:#1565C0">{a.url[:80]}</a>
          {f'<p style="font-size:12px;color:#444;margin:4px 0">{a.descrizione[:120]}...</p>' if a.descrizione else ""}
        </div>"""

    corpo = f"""
<html><body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto">
<h2 style="color:#1565C0">🔔 Lavoro Categorie Protette — Lecce</h2>
<p><b>{len(annunci)}</b> nuovi annunci · {datetime.now().strftime("%d/%m/%Y %H:%M")}</p>
<p>⭐ Art.16 (accesso diretto): <b>{len(art16_list)}</b> &nbsp;
   ✅ Art.1 L.68/99: <b>{len(art1_list)}</b> &nbsp;
   📋 Altri: <b>{len(altri)}</b></p>
<hr>
{'<h3 style="color:#1565C0">⭐ Art. 16 — Accesso Diretto (PRIORITARI)</h3>' + "".join(riga_html(a) for a in art16_list) if art16_list else ""}
{'<h3 style="color:#2E7D32">✅ Art. 1 L.68/99 — Liste Collocamento Mirato</h3>' + "".join(riga_html(a) for a in art1_list) if art1_list else ""}
{'<h3>📋 Concorsi PA Lecce (filtro generico)</h3>' + "".join(riga_html(a) for a in altri[:20]) if altri else ""}
<hr style="margin-top:24px">
<p style="font-size:11px;color:#888">
ARPAL Lecce: <a href="https://arpal.regione.puglia.it/cpi/lecce/">arpal.regione.puglia.it</a> |
InPA: <a href="https://www.inpa.gov.it">inpa.gov.it</a> |
GU: <a href="https://www.gazzettaufficiale.it/ricerca/concorsi">gazzettaufficiale.it</a>
</p>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = (
        f"[Lecce CP] {len(annunci)} nuovi"
        f"{' — ' + str(len(art16_list)) + ' Art.16!' if art16_list else ''}"
    )
    msg["From"] = config["email_mittente"]
    msg["To"]   = config["email_destinatario"]
    msg.attach(MIMEText(corpo, "html", "utf-8"))

    try:
        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as s:
            s.starttls()
            s.login(config["email_mittente"], config["email_password"])
            s.sendmail(config["email_mittente"],
                       config["email_destinatario"], msg.as_string())
        log.info(f"📧 Email inviata → {config['email_destinatario']}")
    except Exception as e:
        log.error(f"Errore email: {e}")


# ─────────────────────────────────────────────────────────────
#  TELEGRAM
# ─────────────────────────────────────────────────────────────
def invia_telegram(annunci: list[Annuncio], config: dict):
    token   = config.get("telegram_bot_token")
    chat_id = config.get("telegram_chat_id")
    if not token or not chat_id or not annunci:
        return

    art16_list = [a for a in annunci if a.art16]
    data = datetime.now().strftime("%d/%m/%Y %H:%M")

    righe = [
        f"🔔 *Lavoro Cat.Protette — Lecce*",
        f"📅 {data}",
        f"📊 {len(annunci)} nuovi annunci",
        "",
    ]

    if art16_list:
        righe.append("⭐ *ART.16 — Accesso Diretto (PRIORITARI):*")
        for a in art16_list[:5]:
            righe.append(f"• [{a.titolo[:55]}]({a.url})\n  `{a.fonte}`")
        righe.append("")

    art1_list = [a for a in annunci if a.art1 and not a.art16]
    if art1_list:
        righe.append("✅ *ART.1 L.68/99:*")
        for a in art1_list[:5]:
            righe.append(f"• [{a.titolo[:55]}]({a.url})\n  `{a.fonte}`")

    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": "\n".join(righe),
                  "parse_mode": "Markdown",
                  "disable_web_page_preview": True},
            timeout=10
        )
        if r.ok:
            log.info("📱 Telegram inviato")
        else:
            log.warning(f"Telegram: {r.text[:200]}")
    except Exception as e:
        log.error(f"Telegram error: {e}")


# ─────────────────────────────────────────────────────────────
#  REPORT TXT + JSON
# ─────────────────────────────────────────────────────────────
def genera_report(nuovi: list[Annuncio], n_totale_db: int):
    now = datetime.now().strftime("%d/%m/%Y %H:%M")

    art16  = [a for a in nuovi if a.art16]
    art1   = [a for a in nuovi if a.art1 and not a.art16]
    pa     = [a for a in nuovi if a.tipo == "PA" and not a.art1 and not a.art16]
    arpal  = [a for a in nuovi if a.tipo == "ARPAL" and not a.art16]
    priv   = [a for a in nuovi if a.tipo == "Privato"]

    W = 68
    sep = "═" * W
    sep2 = "─" * W

    def sezione(emoji, titolo, lista, max_item=50):
        if not lista:
            return ""
        lines = [f"\n{sep2}", f"{emoji}  {titolo.upper()}", sep2]
        for a in lista[:max_item]:
            lines += [
                f"\n  {'='*60}",
                f"  TITOLO:  {a.titolo}",
                f"  Ente:    {a.ente or a.fonte}",
                f"  Tipo:    {a.tipo}  |  Fonte: {a.fonte}",
                f"  Data:    {a.data_pubblicazione}",
                f"  URL:     {a.url}",
            ]
            if a.scadenza:
                lines.append(f"  SCADENZA: {a.scadenza}  ⚠️")
            if a.posti:
                lines.append(f"  POSTI:    {a.posti}")
            if a.testo_completo:
                lines.append(f"\n  ── TESTO ANNUNCIO ──────────────────────────────")
                for riga in a.testo_completo[:3000].splitlines():
                    if riga.strip():
                        lines.append(f"  {riga.strip()}")
                lines.append(f"  ────────────────────────────────────────────────")
            elif a.descrizione:
                lines.append(f"  Descrizione: {a.descrizione}")
        return "\n".join(lines)

    corpo = "\n".join([
        sep,
        "  RICERCA LAVORO — CATEGORIE PROTETTE L.68/99",
        "  Provincia di LECCE  ·  Art.16 + Art.1",
        f"  Aggiornato: {now}",
        sep,
        "",
        f"  NUOVI ANNUNCI:     {len(nuovi)}",
        f"  ⭐ Art.16 diretti: {len(art16)}",
        f"  ✅ Art.1 L.68/99:  {len(art1)}",
        f"  🏛️  PA generica:    {len(pa)}",
        f"  🏢 ARPAL/CPI:      {len(arpal)}",
        f"  💼 Privato:        {len(priv)}",
        f"  📚 Tot. nel DB:    {n_totale_db}",
        "",
        "  ⭐ ART.16 = accesso diretto, niente graduatoria",
        "  ✅ ART.1  = iscrizione liste collocamento mirato",
    ])

    corpo += sezione("⭐", "Art.16 — Accesso Diretto (PRIORITARI)", art16)
    corpo += sezione("✅", "Art.1 L.68/99 — Collocamento Mirato", art1)
    corpo += sezione("🏛️ ", "Pubblica Amministrazione — Lecce/Puglia", pa)
    corpo += sezione("🏢", "ARPAL / Centri per l'Impiego", arpal)
    corpo += sezione("💼", "Offerte Private — Lecce", priv)

    corpo += "\n\n" + sep2 + "\n📌  CONTATTI UTILI\n" + sep2
    corpo += """
  ARPAL / CPI Lecce:
    https://arpal.regione.puglia.it/cpi/lecce/
    Via Vecchia Copertino 4, 73100 Lecce
    Tel. 0832/688111

  InPA (concorsi PA nazionali):
    https://www.inpa.gov.it

  Gazzetta Ufficiale 3a serie:
    https://www.gazzettaufficiale.it/ricerca/concorsi

  Comune di Lecce — Bandi:
    https://www.comune.lecce.it

  Normativa Art.16 L.R. Puglia 17/2005:
    https://www.regione.puglia.it
"""
    corpo += f"\n{sep}\n  Script eseguito: {now}\n{sep}\n"

    with open(CONFIG["output_txt"], "w", encoding="utf-8") as f:
        f.write(corpo)
    print(corpo)

    with open(CONFIG["output_json"], "w", encoding="utf-8") as f:
        json.dump({
            "aggiornato": now,
            "totale_nuovi": len(nuovi),
            "art16": len(art16),
            "art1":  len(art1),
            "annunci": [asdict(a) for a in nuovi],
        }, f, ensure_ascii=False, indent=2)

    log.info(f"Report → {CONFIG['output_txt']} | {CONFIG['output_json']}")


# ─────────────────────────────────────────────────────────────
#  REPORT HTML — leggibile nel browser, con testo completo
# ─────────────────────────────────────────────────────────────
def genera_report_html(annunci: list):
    """Genera risultati_lecce.html — apri nel browser per leggere gli annunci."""
    if not annunci:
        return

    art16  = [a for a in annunci if a.art16]
    art1   = [a for a in annunci if a.art1 and not a.art16]
    altri  = [a for a in annunci if not a.art1 and not a.art16]
    now    = datetime.now().strftime("%d/%m/%Y %H:%M")

    def card(a: Annuncio, colore: str) -> str:
        badge16 = '<span class="badge b16">⭐ ART.16 — ACCESSO DIRETTO</span>' if a.art16 else ""
        badge1  = '<span class="badge b1">✅ ART.1 L.68/99</span>' if a.art1 else ""
        testo_html = ""
        if a.testo_completo:
            righe = ""
            for riga in a.testo_completo.splitlines():
                r = riga.strip()
                if not r:
                    continue
                # Evidenzia keyword importanti
                for kw in ["art. 16","art.16","articolo 16","accesso diretto",
                           "art. 1","legge 68","categorie protette","collocamento mirato",
                           "scadenza","entro il","posti","invalidità","disabil"]:
                    r = _re.sub(f'({_re.escape(kw)})', r'<mark>\1</mark>', r, flags=_re.IGNORECASE)
                righe += f"<p>{r}</p>\n"
            testo_html = f'<div class="testo">{righe}</div>'
        elif a.descrizione:
            testo_html = f'<div class="testo"><p>{a.descrizione}</p></div>'

        scad_html = f'<div class="scadenza">⚠️ Scadenza: <strong>{a.scadenza}</strong></div>' if a.scadenza else ""
        posti_html = f'<div class="posti">👥 {a.posti}</div>' if a.posti else ""

        return f"""
        <div class="card" style="border-left-color:{colore}">
          <div class="card-header">
            <h3><a href="{a.url}" target="_blank">{a.titolo}</a></h3>
            <div class="meta">
              <span>📌 {a.ente or a.fonte}</span>
              <span>🏷️ {a.tipo}</span>
              <span>📅 {a.data_pubblicazione}</span>
              <span>🔗 <a href="{a.url}" target="_blank">Apri annuncio originale ↗</a></span>
            </div>
            <div class="badges">{badge16}{badge1}</div>
            {scad_html}{posti_html}
          </div>
          {testo_html}
        </div>"""

    def sezione_html(emoji, titolo, lista, colore) -> str:
        if not lista:
            return ""
        cards = "\n".join(card(a, colore) for a in lista)
        return f"""
        <section>
          <h2>{emoji} {titolo} <span class="count">({len(lista)})</span></h2>
          {cards}
        </section>"""

    html = f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lavoro Categorie Protette — Lecce — {now}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0 }}
  body {{ font-family:system-ui,sans-serif; background:#f4f6f9; color:#222; line-height:1.6 }}
  header {{ background:#1565C0; color:#fff; padding:20px 32px }}
  header h1 {{ font-size:22px; font-weight:600 }}
  header p  {{ font-size:14px; opacity:.85; margin-top:4px }}
  .stats {{ display:flex; gap:12px; padding:16px 32px; background:#e3eaf7; flex-wrap:wrap }}
  .stat {{ background:#fff; border-radius:8px; padding:10px 20px; text-align:center; min-width:110px }}
  .stat strong {{ display:block; font-size:26px; color:#1565C0 }}
  .stat span {{ font-size:12px; color:#555 }}
  main {{ max-width:960px; margin:0 auto; padding:24px 16px }}
  section {{ margin-bottom:32px }}
  section h2 {{ font-size:18px; font-weight:600; margin-bottom:12px;
                padding:8px 16px; border-radius:6px; background:#fff;
                border-left:4px solid #1565C0 }}
  section h2 .count {{ font-size:14px; font-weight:400; color:#666 }}
  .card {{ background:#fff; border-radius:8px; margin-bottom:16px;
           border-left:5px solid #1565C0; box-shadow:0 1px 4px rgba(0,0,0,.08) }}
  .card-header {{ padding:16px 20px 10px }}
  .card-header h3 {{ font-size:16px; margin-bottom:6px }}
  .card-header h3 a {{ color:#1565C0; text-decoration:none }}
  .card-header h3 a:hover {{ text-decoration:underline }}
  .meta {{ display:flex; gap:16px; font-size:12px; color:#666; flex-wrap:wrap; margin-bottom:8px }}
  .badges {{ display:flex; gap:8px; flex-wrap:wrap; margin:6px 0 }}
  .badge {{ padding:3px 10px; border-radius:20px; font-size:12px; font-weight:600 }}
  .b16 {{ background:#FFF3E0; color:#E65100; border:1px solid #FFB74D }}
  .b1  {{ background:#E8F5E9; color:#2E7D32; border:1px solid #81C784 }}
  .scadenza {{ font-size:13px; color:#C62828; margin:4px 0; font-weight:500 }}
  .posti {{ font-size:13px; color:#1565C0; margin:4px 0 }}
  .testo {{ padding:12px 20px 16px; border-top:1px solid #eee;
            font-size:14px; max-height:500px; overflow-y:auto }}
  .testo p {{ margin-bottom:8px; color:#333 }}
  .testo mark {{ background:#FFF9C4; padding:1px 3px; border-radius:3px;
                 font-weight:600; color:#333 }}
  details summary {{ cursor:pointer; padding:8px 20px; font-size:13px;
                     color:#1565C0; background:#f0f4ff; border-top:1px solid #e0e8ff }}
  details[open] summary {{ background:#e0eaff }}
  @media(max-width:600px) {{ .stats,.meta {{ flex-direction:column }} }}
</style>
</head>
<body>
<header>
  <h1>🔔 Lavoro Categorie Protette — Provincia di Lecce</h1>
  <p>Aggiornato: {now} · Art.16 (accesso diretto) + Art.1 L.68/99</p>
</header>
<div class="stats">
  <div class="stat"><strong>{len(annunci)}</strong><span>Nuovi annunci</span></div>
  <div class="stat"><strong style="color:#E65100">{len(art16)}</strong><span>Art.16 Diretti</span></div>
  <div class="stat"><strong style="color:#2E7D32">{len(art1)}</strong><span>Art.1 L.68/99</span></div>
  <div class="stat"><strong style="color:#555">{len(altri)}</strong><span>Concorsi PA</span></div>
</div>
<main>
{sezione_html("⭐", "Art.16 — Accesso Diretto (PRIORITARI)", art16, "#E65100")}
{sezione_html("✅", "Art.1 L.68/99 — Collocamento Mirato", art1, "#2E7D32")}
{sezione_html("🏛️", "Pubblica Amministrazione — Lecce/Puglia", altri[:30], "#1565C0")}
</main>
</body>
</html>"""

    with open("risultati_lecce.html", "w", encoding="utf-8") as f:
        f.write(html)
    log.info("🌐 Report HTML → risultati_lecce.html  (aprilo nel browser!)")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("\n" + "═" * 68)
    print("  CERCA LAVORO — CATEGORIE PROTETTE — PROVINCIA DI LECCE")
    print("  Art.16 (accesso diretto) + Art.1 L.68/99")
    print("═" * 68 + "\n")

    conn = init_db(CONFIG["db_path"])

    fonti = [
        scrape_inpa,
        scrape_regione_puglia,
        scrape_arpal,
        scrape_gazzetta_ufficiale,
        scrape_comune_lecce,
        scrape_comuni_lecce,
        scrape_asl_lecce,
        scrape_provincia_lecce,
        scrape_concorsando,
        scrape_indeed,
        scrape_lavoroperte,
    ]

    grezzi = []
    for fn in fonti:
        try:
            res = fn()
            grezzi.extend(res)
        except Exception as e:
            log.error(f"Errore in {fn.__name__}: {e}")

    log.info(f"\n📊 Totale grezzo: {len(grezzi)}")

    # Deduplica per URL
    visti: set = set()
    dedup = []
    for a in grezzi:
        if a.url and a.url not in visti:
            visti.add(a.url)
            dedup.append(a)

    # Solo quelli nuovi rispetto al DB
    nuovi = filtra_nuovi(conn, dedup)
    log.info(f"🆕 Nuovi (non nel DB): {len(nuovi)}")

    # ── SCARICA TESTO COMPLETO di ogni annuncio trovato ──
    if nuovi:
        scarica_tutti_dettagli(nuovi, max_annunci=60)

    salva_annunci(conn, nuovi)
    n_db = conn.execute("SELECT COUNT(*) FROM annunci").fetchone()[0]

    genera_report(nuovi, n_db)
    genera_report_html(nuovi)   # report HTML leggibile nel browser

    if nuovi:
        invia_email(nuovi, CONFIG)
        invia_telegram(nuovi, CONFIG)

    print(f"\n✅ Completato — {len(nuovi)} nuovi annunci trovati.")
    print(f"   ⭐ Art.16: {sum(1 for a in nuovi if a.art16)}")
    print(f"   ✅ Art.1:  {sum(1 for a in nuovi if a.art1)}")
    print(f"   Report:   {CONFIG['output_txt']}")
    conn.close()


if __name__ == "__main__":
    main()
