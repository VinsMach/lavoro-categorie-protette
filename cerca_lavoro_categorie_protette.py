#!/usr/bin/env python3
"""
=============================================================
  CERCA LAVORO - CATEGORIE PROTETTE (L.68/99) IN PUGLIA
  Articolo 1 (invalidità civile ≥46%) + Articolo 16 (Puglia)
=============================================================
Fonti monitorate:
  - InPA (portale nazionale PA)
  - Regione Puglia (concorsi e avvisi)
  - ANPAL / Centri per l'Impiego Puglia
  - Gazzetta Ufficiale (concorsi PA)
  - Comuni pugliesi (Bari, Taranto, Brindisi, Lecce, Foggia)
  - Aziende Sanitarie Puglia (ASL)
  - ARPAL Puglia (collocamento mirato)
  - Indeed / InfoJobs (privato)
=============================================================
"""

import requests
import feedparser
import json
import os
import re
import time
import smtplib
import logging
from datetime import datetime, timedelta
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlencode, quote_plus
from dataclasses import dataclass, asdict, field
from typing import Optional
import sqlite3

# ─────────────────────────────────────────────
#  CONFIGURAZIONE
#  In locale: modifica i valori qui sotto
#  Su GitHub Actions: usa i Secrets del repo
# ─────────────────────────────────────────────
CONFIG = {
    # ── Email (Gmail) ──────────────────────────
    # Locale: inserisci direttamente
    # GitHub Actions: lascia "" → legge da env
    "email_destinatario": os.getenv("EMAIL_DESTINATARIO", ""),
    "email_mittente":     os.getenv("EMAIL_MITTENTE", ""),
    "email_password":     os.getenv("EMAIL_PASSWORD", ""),
    "smtp_server": "smtp.gmail.com",
    "smtp_port": 587,

    # ── Telegram (opzionale) ───────────────────
    # Crea un bot su @BotFather e ottieni token+chat_id
    "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
    "telegram_chat_id":   os.getenv("TELEGRAM_CHAT_ID", ""),

    # ── File di output ─────────────────────────
    "db_path":      "lavoro_categorie_protette.db",
    "output_json":  "risultati_lavoro.json",
    "output_txt":   "risultati_lavoro.txt",

    # ── Comportamento ──────────────────────────
    "giorni_indietro": 30,
    "pausa_tra_richieste": 2,
    "timeout": 15,

    # ── Filtri geografici ──────────────────────
    "province_puglia": [
        "bari", "bat", "brindisi", "foggia",
        "lecce", "taranto", "puglia", "apulia"
    ],
}

# ─────────────────────────────────────────────
#  PAROLE CHIAVE DI RICERCA
# ─────────────────────────────────────────────
KEYWORDS_CATEGORIE_PROTETTE = [
    "categorie protette",
    "categoria protetta",
    "art. 1 legge 68",
    "articolo 1 l.68",
    "l. 68/99",
    "legge 68 1999",
    "collocamento mirato",
    "disabilità",
    "invalidità civile",
    "riserva disabili",
    "art. 16",              # legge regionale Puglia
    "articolo 16",
    "L.R. 17/2005",         # legge regionale puglia lavoro
    "inserimento lavorativo disabili",
    "ARPAL categorie protette",
]

KEYWORDS_LAVORO_PA = [
    "concorso puglia",
    "concorso bari",
    "concorso taranto",
    "concorso brindisi",
    "concorso lecce",
    "concorso foggia",
    "avviso pubblico puglia",
    "selezione pubblica puglia",
    "assunzione comune puglia",
    "ASL puglia concorso",
    "regione puglia assunzioni",
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
    descrizione: str = ""
    ente: str = ""
    scadenza: str = ""
    tipo: str = ""          # "PA" | "Privato" | "ARPAL"
    categoria_protetta: bool = False
    art_16_puglia: bool = False
    province: list = field(default_factory=list)
    trovato_il: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

    def __hash__(self):
        return hash(self.url)

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
#  HELPERS
# ─────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "it-IT,it;q=0.9",
}

def get_page(url: str, params: dict = None) -> Optional[BeautifulSoup]:
    """Scarica una pagina e restituisce BeautifulSoup o None."""
    try:
        r = requests.get(
            url, params=params, headers=HEADERS,
            timeout=CONFIG["timeout"]
        )
        r.raise_for_status()
        r.encoding = r.apparent_encoding or "utf-8"
        time.sleep(CONFIG["pausa_tra_richieste"])
        return BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        log.warning(f"Errore GET {url}: {e}")
        return None

def get_json(url: str, params: dict = None) -> Optional[dict]:
    """Scarica JSON da un endpoint API."""
    try:
        r = requests.get(
            url, params=params, headers=HEADERS,
            timeout=CONFIG["timeout"]
        )
        r.raise_for_status()
        time.sleep(CONFIG["pausa_tra_richieste"])
        return r.json()
    except Exception as e:
        log.warning(f"Errore JSON {url}: {e}")
        return None

def contiene_keyword(testo: str, keywords: list) -> bool:
    testo_lower = testo.lower()
    return any(kw.lower() in testo_lower for kw in keywords)

def e_puglia(testo: str) -> bool:
    return contiene_keyword(testo, CONFIG["province_puglia"])

def rileva_art16(testo: str) -> bool:
    return contiene_keyword(testo, ["art. 16", "articolo 16", "L.R. 17/2005", "legge regionale puglia"])

def rileva_cat_protetta(testo: str) -> bool:
    return contiene_keyword(testo, KEYWORDS_CATEGORIE_PROTETTE)

def data_recente(data_str: str, giorni: int = None) -> bool:
    """Controlla se una data è entro gli ultimi N giorni."""
    if not data_str:
        return True  # se non c'è data, includiamo
    giorni = giorni or CONFIG["giorni_indietro"]
    limite = datetime.now() - timedelta(days=giorni)
    for fmt in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"]:
        try:
            d = datetime.strptime(data_str[:10], fmt[:10])
            return d >= limite
        except ValueError:
            continue
    return True

# ─────────────────────────────────────────────────────────────
#  FONTE 1: InPA — Portale Nazionale Reclutamento PA
#  https://www.inpa.gov.it
# ─────────────────────────────────────────────────────────────
def scrape_inpa() -> list[Annuncio]:
    log.info("📋 InPA — Portale Nazionale PA...")
    annunci = []

    # API pubblica InPA (non ufficiale ma accessibile)
    api_url = "https://www.inpa.gov.it/wp-json/wp/v2/posts"
    
    for kw in ["categorie protette puglia", "concorso puglia disabili", "collocamento mirato puglia"]:
        params = {
            "search": kw,
            "per_page": 20,
            "_fields": "title,link,date,excerpt,content"
        }
        data = get_json(api_url, params)
        if not data:
            continue
        for item in data:
            titolo = BeautifulSoup(item.get("title", {}).get("rendered", ""), "html.parser").get_text()
            descrizione = BeautifulSoup(item.get("excerpt", {}).get("rendered", ""), "html.parser").get_text()
            url = item.get("link", "")
            data_pub = item.get("date", "")[:10]
            testo = titolo + " " + descrizione

            if not e_puglia(testo) and not rileva_cat_protetta(testo):
                continue

            annunci.append(Annuncio(
                titolo=titolo,
                fonte="InPA",
                url=url,
                data_pubblicazione=data_pub,
                descrizione=descrizione[:300],
                tipo="PA",
                categoria_protetta=rileva_cat_protetta(testo),
                art_16_puglia=rileva_art16(testo),
            ))

    # Scraping diretto pagina concorsi
    soup = get_page("https://www.inpa.gov.it/bandi/", {"area_geografica": "Puglia"})
    if soup:
        for card in soup.select("article, .bando-item, .job-listing"):
            titolo_el = card.select_one("h2, h3, .title")
            link_el = card.select_one("a[href]")
            if not titolo_el:
                continue
            titolo = titolo_el.get_text(strip=True)
            url = link_el["href"] if link_el else ""
            testo = card.get_text(" ", strip=True)
            if e_puglia(testo) or rileva_cat_protetta(testo):
                annunci.append(Annuncio(
                    titolo=titolo,
                    fonte="InPA",
                    url=url,
                    data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                    descrizione=testo[:300],
                    tipo="PA",
                    categoria_protetta=rileva_cat_protetta(testo),
                ))

    log.info(f"  → {len(annunci)} risultati InPA")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 2: Regione Puglia — Concorsi e Avvisi
# ─────────────────────────────────────────────────────────────
def scrape_regione_puglia() -> list[Annuncio]:
    log.info("🏛️  Regione Puglia — concorsi...")
    annunci = []
    
    urls = [
        "https://www.regione.puglia.it/web/lavoro/concorsi",
        "https://www.regione.puglia.it/web/lavoro/avvisi-pubblici",
        "https://www.regione.puglia.it/concorsi",
    ]

    for url in urls:
        soup = get_page(url)
        if not soup:
            continue
        for item in soup.select("article, li.concorso, .portlet-body tr, .entry"):
            testo = item.get_text(" ", strip=True)
            link_el = item.select_one("a[href]")
            titolo_el = item.select_one("h2, h3, h4, a, td")
            
            if not titolo_el:
                continue
            titolo = titolo_el.get_text(strip=True)[:200]
            href = link_el["href"] if link_el else url
            if href.startswith("/"):
                href = "https://www.regione.puglia.it" + href
            
            annunci.append(Annuncio(
                titolo=titolo,
                fonte="Regione Puglia",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300],
                ente="Regione Puglia",
                tipo="PA",
                categoria_protetta=rileva_cat_protetta(testo),
                art_16_puglia=rileva_art16(testo),
            ))

    # RSS feed regione puglia
    feed = feedparser.parse("https://www.regione.puglia.it/rss/lavoro")
    for entry in feed.entries:
        testo = entry.get("title", "") + " " + entry.get("summary", "")
        annunci.append(Annuncio(
            titolo=entry.get("title", "Senza titolo"),
            fonte="Regione Puglia RSS",
            url=entry.get("link", ""),
            data_pubblicazione=entry.get("published", "")[:10],
            descrizione=entry.get("summary", "")[:300],
            ente="Regione Puglia",
            tipo="PA",
            categoria_protetta=rileva_cat_protetta(testo),
            art_16_puglia=rileva_art16(testo),
        ))

    log.info(f"  → {len(annunci)} risultati Regione Puglia")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 3: ARPAL Puglia — Collocamento Mirato (L.68/99)
#  https://www.arpal.regione.puglia.it
# ─────────────────────────────────────────────────────────────
def scrape_arpal() -> list[Annuncio]:
    log.info("♿ ARPAL Puglia — collocamento mirato...")
    annunci = []

    urls_arpal = [
        "https://www.arpal.regione.puglia.it/avvisi-e-bandi/",
        "https://www.arpal.regione.puglia.it/collocamento-mirato/",
        "https://www.arpal.regione.puglia.it/opportunita-di-lavoro/",
        "https://www.arpal.regione.puglia.it/categorie-protette/",
    ]

    for url in urls_arpal:
        soup = get_page(url)
        if not soup:
            continue
        for item in soup.select("article, .entry, li, .bando"):
            testo = item.get_text(" ", strip=True)
            link_el = item.select_one("a[href]")
            if not link_el or len(testo) < 20:
                continue
            titolo = link_el.get_text(strip=True)[:200]
            href = link_el["href"]
            if href.startswith("/"):
                href = "https://www.arpal.regione.puglia.it" + href

            annunci.append(Annuncio(
                titolo=titolo,
                fonte="ARPAL Puglia",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300],
                ente="ARPAL Puglia",
                tipo="ARPAL",
                categoria_protetta=True,  # ARPAL è per definizione collocamento mirato
                art_16_puglia=rileva_art16(testo),
            ))

    log.info(f"  → {len(annunci)} risultati ARPAL")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 4: Gazzetta Ufficiale — Concorsi PA
#  Serie Concorsi ed Esami (3a Serie Speciale)
# ─────────────────────────────────────────────────────────────
def scrape_gazzetta_ufficiale() -> list[Annuncio]:
    log.info("📰 Gazzetta Ufficiale — concorsi PA...")
    annunci = []

    # RSS 3a serie speciale concorsi
    feed_url = "https://www.gazzettaufficiale.it/rss/esecutivi.xml"
    feed = feedparser.parse(feed_url)

    for entry in feed.entries:
        titolo = entry.get("title", "")
        descrizione = entry.get("summary", "")
        testo = titolo + " " + descrizione

        if not (e_puglia(testo) or rileva_cat_protetta(testo)):
            continue

        annunci.append(Annuncio(
            titolo=titolo,
            fonte="Gazzetta Ufficiale",
            url=entry.get("link", ""),
            data_pubblicazione=entry.get("published", "")[:10],
            descrizione=descrizione[:300],
            tipo="PA",
            categoria_protetta=rileva_cat_protetta(testo),
            art_16_puglia=rileva_art16(testo),
        ))

    # Ricerca diretta
    for kw in ["categorie protette puglia", "collocamento mirato puglia", "disabili concorso puglia"]:
        url = f"https://www.gazzettaufficiale.it/ricerca/json/gazzette_ricerca_risultato"
        params = {
            "q": kw,
            "tipoSerie": "concorsi",
            "p": 1,
        }
        data = get_json(url, params)
        if not data:
            continue
        for item in (data.get("gazzette") or []):
            titolo = item.get("titolo", "")
            testo = titolo + " " + item.get("testo", "")
            if e_puglia(testo) or rileva_cat_protetta(testo):
                annunci.append(Annuncio(
                    titolo=titolo,
                    fonte="Gazzetta Ufficiale",
                    url=f"https://www.gazzettaufficiale.it{item.get('url', '')}",
                    data_pubblicazione=item.get("dataPubblicazione", "")[:10],
                    descrizione=item.get("testo", "")[:300],
                    tipo="PA",
                    categoria_protetta=rileva_cat_protetta(testo),
                ))

    log.info(f"  → {len(annunci)} risultati GU")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 5: Comuni Pugliesi (siti istituzionali)
# ─────────────────────────────────────────────────────────────
COMUNI_PUGLIA = {
    "Bari":     "https://www.comune.bari.it",
    "Taranto":  "https://www.comune.taranto.it",
    "Brindisi": "https://www.comune.brindisi.it",
    "Lecce":    "https://www.comune.lecce.it",
    "Foggia":   "https://www.comune.foggia.it",
    "Andria":   "https://www.comune.andria.bt.it",
    "Barletta": "https://www.comune.barletta.bt.it",
    "Altamura": "https://www.comune.altamura.ba.it",
}

CONCORSI_PATHS = [
    "/concorsi", "/avvisi/concorsi", "/lavora-con-noi",
    "/amministrazione-trasparente/bandi-di-concorso",
    "/bandi-di-concorso", "/personale/concorsi",
    "/selezioni-pubbliche",
]

def scrape_comuni() -> list[Annuncio]:
    log.info("🏙️  Comuni pugliesi — concorsi...")
    annunci = []

    for comune, base_url in COMUNI_PUGLIA.items():
        for path in CONCORSI_PATHS:
            url = base_url + path
            soup = get_page(url)
            if not soup:
                continue
            for link in soup.select("a[href]"):
                testo = link.get_text(strip=True)
                if len(testo) < 10:
                    continue
                href = link["href"]
                if href.startswith("/"):
                    href = base_url + href
                full_testo = testo + " " + comune
                
                annunci.append(Annuncio(
                    titolo=testo[:200],
                    fonte=f"Comune di {comune}",
                    url=href,
                    data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                    ente=f"Comune di {comune}",
                    tipo="PA",
                    categoria_protetta=rileva_cat_protetta(testo),
                    art_16_puglia=rileva_art16(testo),
                ))
            break  # primo path che funziona, passiamo al comune successivo

    log.info(f"  → {len(annunci)} risultati Comuni")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 6: ASL Puglia (Aziende Sanitarie)
# ─────────────────────────────────────────────────────────────
ASL_PUGLIA = {
    "ASL Bari":     "https://www.asl.bari.it",
    "ASL BT":       "https://www.asl.barletta-andria-trani.it",
    "ASL Brindisi": "https://www.asl.brindisi.it",
    "ASL FG":       "https://www.auslfoggia.it",
    "ASL Lecce":    "https://www.asl.lecce.it",
    "ASL Taranto":  "https://www.aslta.it",
}

def scrape_asl_puglia() -> list[Annuncio]:
    log.info("🏥 ASL Puglia — concorsi...")
    annunci = []

    for nome, base_url in ASL_PUGLIA.items():
        for path in ["/concorsi", "/avvisi/concorsi", "/bandi-di-concorso", "/lavora-con-noi"]:
            soup = get_page(base_url + path)
            if not soup:
                continue
            for link in soup.select("a[href]"):
                testo = link.get_text(strip=True)
                if len(testo) < 10:
                    continue
                href = link["href"]
                if href.startswith("/"):
                    href = base_url + href
                annunci.append(Annuncio(
                    titolo=testo[:200],
                    fonte=nome,
                    url=href,
                    data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                    ente=nome,
                    tipo="PA",
                    categoria_protetta=rileva_cat_protetta(testo),
                ))
            break

    log.info(f"  → {len(annunci)} risultati ASL")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 7: Indeed Italia
# ─────────────────────────────────────────────────────────────
def scrape_indeed() -> list[Annuncio]:
    log.info("💼 Indeed — offerte private...")
    annunci = []

    queries = [
        ("categorie protette puglia", "Puglia"),
        ("disabili art 1 legge 68 puglia", "Puglia"),
        ("collocamento mirato puglia", "Puglia"),
        ("invalidità civile lavoro puglia", "Puglia"),
    ]

    for query, location in queries:
        url = "https://it.indeed.com/lavoro"
        params = {"q": query, "l": location, "sort": "date"}
        soup = get_page(url, params)
        if not soup:
            continue

        for card in soup.select(".job_seen_beacon, .jobsearch-SerpJobCard, [data-jk]"):
            titolo_el = card.select_one("h2 a, .jobtitle, [data-testid='job-title']")
            azienda_el = card.select_one(".companyName, .company, [data-testid='company-name']")
            luogo_el = card.select_one(".companyLocation, [data-testid='text-location']")
            link_el = card.select_one("a[href]")

            if not titolo_el:
                continue

            titolo = titolo_el.get_text(strip=True)
            azienda = azienda_el.get_text(strip=True) if azienda_el else ""
            luogo = luogo_el.get_text(strip=True) if luogo_el else ""
            href = link_el["href"] if link_el else ""
            if href.startswith("/"):
                href = "https://it.indeed.com" + href
            testo = titolo + " " + azienda + " " + luogo

            annunci.append(Annuncio(
                titolo=titolo,
                fonte="Indeed",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                ente=azienda,
                descrizione=f"Luogo: {luogo}",
                tipo="Privato",
                categoria_protetta=rileva_cat_protetta(testo),
            ))

    log.info(f"  → {len(annunci)} risultati Indeed")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 8: InfoJobs Italia
# ─────────────────────────────────────────────────────────────
def scrape_infojobs() -> list[Annuncio]:
    log.info("💼 InfoJobs — offerte private...")
    annunci = []

    for query in ["categorie protette puglia", "collocamento mirato puglia", "disabili art.1 puglia"]:
        url = "https://www.infojobs.it/lavoro/"
        params = {"keyword": query, "normalizedJobCategoryCode": "qualsiasi", "pageNumber": 1}
        soup = get_page(url, params)
        if not soup:
            continue
        for card in soup.select("li.ij-OfferCard, article.ij-OfferCard"):
            titolo_el = card.select_one("h2, .ij-OfferCard-description-title")
            link_el = card.select_one("a[href]")
            azienda_el = card.select_one(".ij-OfferCard-description-company, .company")

            if not titolo_el:
                continue
            titolo = titolo_el.get_text(strip=True)
            href = link_el["href"] if link_el else ""
            azienda = azienda_el.get_text(strip=True) if azienda_el else ""

            annunci.append(Annuncio(
                titolo=titolo,
                fonte="InfoJobs",
                url=href if href.startswith("http") else f"https://www.infojobs.it{href}",
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                ente=azienda,
                tipo="Privato",
                categoria_protetta=rileva_cat_protetta(titolo),
            ))

    log.info(f"  → {len(annunci)} risultati InfoJobs")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 9: Concorsando.it e EasyGov (aggregatori concorsi PA)
# ─────────────────────────────────────────────────────────────
def scrape_aggregatori_pa() -> list[Annuncio]:
    log.info("📋 Aggregatori PA (Concorsando, EasyGov)...")
    annunci = []

    # Concorsando
    url = "https://www.concorsando.it/blog/concorsi-puglia/"
    soup = get_page(url)
    if soup:
        for art in soup.select("article, .entry-card"):
            titolo_el = art.select_one("h2, h3, .entry-title")
            link_el = art.select_one("a[href]")
            if not titolo_el:
                continue
            titolo = titolo_el.get_text(strip=True)
            href = link_el["href"] if link_el else ""
            testo = art.get_text(" ", strip=True)
            annunci.append(Annuncio(
                titolo=titolo,
                fonte="Concorsando.it",
                url=href,
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300],
                tipo="PA",
                categoria_protetta=rileva_cat_protetta(testo),
            ))

    # EasyGov
    url2 = "https://www.easygov.it/bandi/?regione=puglia&tipo=concorso"
    soup2 = get_page(url2)
    if soup2:
        for item in soup2.select(".bando-item, article, li"):
            titolo_el = item.select_one("h2, h3, a")
            link_el = item.select_one("a[href]")
            if not titolo_el:
                continue
            titolo = titolo_el.get_text(strip=True)
            href = link_el["href"] if link_el else ""
            testo = item.get_text(" ", strip=True)
            annunci.append(Annuncio(
                titolo=titolo,
                fonte="EasyGov",
                url=href if href.startswith("http") else f"https://www.easygov.it{href}",
                data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                descrizione=testo[:300],
                tipo="PA",
                categoria_protetta=rileva_cat_protetta(testo),
            ))

    log.info(f"  → {len(annunci)} risultati aggregatori PA")
    return annunci


# ─────────────────────────────────────────────────────────────
#  FONTE 10: Centri per l'Impiego Puglia (via ARPAL)
#  I CPI gestiscono le liste L.68 a livello provinciale
# ─────────────────────────────────────────────────────────────
def scrape_cpi_puglia() -> list[Annuncio]:
    log.info("🏢 Centri per l'Impiego Puglia...")
    annunci = []

    cpi_urls = [
        ("CPI Bari",     "https://www.arpal.regione.puglia.it/centri-per-limpiego/bari/"),
        ("CPI Brindisi", "https://www.arpal.regione.puglia.it/centri-per-limpiego/brindisi/"),
        ("CPI Taranto",  "https://www.arpal.regione.puglia.it/centri-per-limpiego/taranto/"),
        ("CPI Lecce",    "https://www.arpal.regione.puglia.it/centri-per-limpiego/lecce/"),
        ("CPI Foggia",   "https://www.arpal.regione.puglia.it/centri-per-limpiego/foggia/"),
        ("CPI BAT",      "https://www.arpal.regione.puglia.it/centri-per-limpiego/bat/"),
    ]

    for nome, url in cpi_urls:
        soup = get_page(url)
        if not soup:
            continue
        for link in soup.select("a[href]"):
            testo = link.get_text(strip=True)
            if len(testo) < 15:
                continue
            href = link["href"]
            if href.startswith("/"):
                href = "https://www.arpal.regione.puglia.it" + href
            if any(kw in testo.lower() for kw in ["categorie", "disabili", "collocamento", "avviso", "bando", "offerta"]):
                annunci.append(Annuncio(
                    titolo=testo[:200],
                    fonte=nome,
                    url=href,
                    data_pubblicazione=datetime.now().strftime("%Y-%m-%d"),
                    ente=nome,
                    tipo="ARPAL",
                    categoria_protetta=rileva_cat_protetta(testo),
                ))

    log.info(f"  → {len(annunci)} risultati CPI")
    return annunci


# ─────────────────────────────────────────────────────────────
#  DATABASE SQLITE — evita duplicati tra esecuzioni
# ─────────────────────────────────────────────────────────────
def init_db(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS annunci (
            url TEXT PRIMARY KEY,
            titolo TEXT,
            fonte TEXT,
            data_pubblicazione TEXT,
            trovato_il TEXT,
            categoria_protetta INTEGER,
            art_16_puglia INTEGER,
            tipo TEXT,
            notificato INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    return conn

def filtra_nuovi(conn: sqlite3.Connection, annunci: list[Annuncio]) -> list[Annuncio]:
    """Restituisce solo annunci non ancora visti."""
    nuovi = []
    for a in annunci:
        if not a.url:
            continue
        row = conn.execute("SELECT 1 FROM annunci WHERE url=?", (a.url,)).fetchone()
        if not row:
            nuovi.append(a)
    return nuovi

def salva_annunci(conn: sqlite3.Connection, annunci: list[Annuncio]):
    for a in annunci:
        try:
            conn.execute("""
                INSERT OR IGNORE INTO annunci
                (url, titolo, fonte, data_pubblicazione, trovato_il,
                 categoria_protetta, art_16_puglia, tipo)
                VALUES (?,?,?,?,?,?,?,?)
            """, (
                a.url, a.titolo, a.fonte, a.data_pubblicazione,
                a.trovato_il, int(a.categoria_protetta),
                int(a.art_16_puglia), a.tipo
            ))
        except Exception as e:
            log.warning(f"DB insert error: {e}")
    conn.commit()


# ─────────────────────────────────────────────────────────────
#  EMAIL NOTIFICA
# ─────────────────────────────────────────────────────────────
def invia_email(annunci: list[Annuncio], config: dict):
    if not config.get("email_destinatario"):
        return
    if not annunci:
        return

    prioritari = [a for a in annunci if a.categoria_protetta or a.art_16_puglia]
    altri = [a for a in annunci if not a.categoria_protetta and not a.art_16_puglia]

    corpo = f"""
<html><body style="font-family: Arial, sans-serif;">
<h2>🔔 Nuove opportunità lavorative — Categorie Protette Puglia</h2>
<p>Trovati <strong>{len(annunci)}</strong> nuovi annunci ({len(prioritari)} prioritari)</p>
<hr>

<h3>⭐ Annunci con Categorie Protette / Art.16</h3>
{''.join(f'''
<div style="border-left: 4px solid #2196F3; padding: 10px; margin: 10px 0; background: #f5f5f5;">
  <strong>{a.titolo}</strong><br>
  Fonte: {a.fonte} | Tipo: {a.tipo}<br>
  {"🏅 Categoria Protetta " if a.categoria_protetta else ""}
  {"📍 Art.16 Puglia" if a.art_16_puglia else ""}<br>
  <a href="{a.url}">{a.url}</a>
</div>''' for a in prioritari)}

<h3>📋 Altri annunci PA in Puglia</h3>
{''.join(f'<p>• <a href="{a.url}">{a.titolo}</a> ({a.fonte})</p>' for a in altri[:20])}

<hr>
<small>Script categorie protette puglia — {datetime.now().strftime("%d/%m/%Y %H:%M")}</small>
</body></html>
"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[Lavoro CP Puglia] {len(annunci)} nuovi annunci ({len(prioritari)} prioritari)"
    msg["From"] = config["email_mittente"]
    msg["To"] = config["email_destinatario"]
    msg.attach(MIMEText(corpo, "html", "utf-8"))

    try:
        with smtplib.SMTP(config["smtp_server"], config["smtp_port"]) as server:
            server.starttls()
            server.login(config["email_mittente"], config["email_password"])
            server.sendmail(config["email_mittente"], config["email_destinatario"], msg.as_string())
        log.info(f"📧 Email inviata a {config['email_destinatario']}")
    except Exception as e:
        log.error(f"Errore invio email: {e}")


# ─────────────────────────────────────────────────────────────
#  REPORT TXT / JSON
# ─────────────────────────────────────────────────────────────
def genera_report(annunci: list[Annuncio], tutti: list[Annuncio]):
    now = datetime.now().strftime("%d/%m/%Y %H:%M")
    
    prioritari = [a for a in annunci if a.categoria_protetta or a.art_16_puglia]
    pa_annunci = [a for a in annunci if a.tipo == "PA"]
    privati = [a for a in annunci if a.tipo == "Privato"]
    arpal = [a for a in annunci if a.tipo == "ARPAL"]

    # ── TXT
    linee = [
        "=" * 65,
        "  RICERCA LAVORO — CATEGORIE PROTETTE (L.68/99) — PUGLIA",
        f"  Aggiornato: {now}",
        "=" * 65,
        "",
        f"TOTALE NUOVI ANNUNCI: {len(annunci)}",
        f"  ⭐ Con keyword categorie protette/art.16: {len(prioritari)}",
        f"  🏛️  Pubblica Amministrazione: {len(pa_annunci)}",
        f"  🏢 ARPAL/CPI: {len(arpal)}",
        f"  💼 Privato: {len(privati)}",
        f"  📚 Totale nel database: {len(tutti)}",
        "",
    ]

    if prioritari:
        linee += [
            "━" * 65,
            "⭐ ANNUNCI PRIORITARI (Categorie Protette / Art.16 Puglia)",
            "━" * 65,
        ]
        for a in prioritari:
            flags = []
            if a.categoria_protetta: flags.append("CAT.PROTETTA")
            if a.art_16_puglia:      flags.append("ART.16 PUGLIA")
            linee += [
                f"\n[{a.fonte}] {'|'.join(flags)}",
                f"  Titolo:  {a.titolo}",
                f"  Tipo:    {a.tipo}",
                f"  Data:    {a.data_pubblicazione}",
                f"  URL:     {a.url}",
            ]
            if a.descrizione:
                linee.append(f"  Descr.:  {a.descrizione[:150]}...")

    if pa_annunci:
        linee += ["", "━" * 65, "🏛️  PUBBLICA AMMINISTRAZIONE", "━" * 65]
        for a in pa_annunci:
            linee.append(f"\n• [{a.fonte}] {a.titolo}")
            linee.append(f"  {a.url}")

    if arpal:
        linee += ["", "━" * 65, "🏢 ARPAL / CENTRI PER L'IMPIEGO", "━" * 65]
        for a in arpal:
            linee.append(f"\n• [{a.fonte}] {a.titolo}")
            linee.append(f"  {a.url}")

    if privati:
        linee += ["", "━" * 65, "💼 OFFERTE PRIVATE", "━" * 65]
        for a in privati:
            linee.append(f"\n• [{a.fonte}] {a.titolo}")
            linee.append(f"  {a.url}")

    linee += [
        "",
        "━" * 65,
        "📌 RISORSE UTILI",
        "━" * 65,
        "• ARPAL Puglia (collocamento mirato):",
        "  https://www.arpal.regione.puglia.it/collocamento-mirato/",
        "• InPA (concorsi PA):",
        "  https://www.inpa.gov.it",
        "• Gazzetta Ufficiale concorsi:",
        "  https://www.gazzettaufficiale.it/ricerca/concorsi",
        "• Regione Puglia lavoro:",
        "  https://www.regione.puglia.it/web/lavoro",
        "• Guida ANPAL collocamento mirato L.68/99:",
        "  https://www.anpal.gov.it/collocamento-mirato",
        "",
        f"Script eseguito: {now}",
    ]

    txt = "\n".join(linee)
    with open(CONFIG["output_txt"], "w", encoding="utf-8") as f:
        f.write(txt)
    print(txt)

    # ── JSON
    with open(CONFIG["output_json"], "w", encoding="utf-8") as f:
        json.dump({
            "aggiornato": now,
            "totale_nuovi": len(annunci),
            "prioritari": len(prioritari),
            "annunci": [asdict(a) for a in annunci],
        }, f, ensure_ascii=False, indent=2)

    log.info(f"Report salvato: {CONFIG['output_txt']} | {CONFIG['output_json']}")


# ─────────────────────────────────────────────────────────────
#  TELEGRAM NOTIFICA
# ─────────────────────────────────────────────────────────────
def invia_telegram(annunci: list[Annuncio], config: dict):
    """Invia un messaggio Telegram con i nuovi annunci prioritari."""
    token = config.get("telegram_bot_token")
    chat_id = config.get("telegram_chat_id")
    if not token or not chat_id or not annunci:
        return

    prioritari = [a for a in annunci if a.categoria_protetta or a.art_16_puglia]
    totale = len(annunci)
    data = datetime.now().strftime("%d/%m/%Y %H:%M")

    righe = [
        f"🔔 *Lavoro Categorie Protette Puglia*",
        f"📅 {data}",
        f"📊 {totale} nuovi annunci ({len(prioritari)} prioritari)\n",
    ]

    if prioritari:
        righe.append("⭐ *Annunci prioritari:*")
        for a in prioritari[:8]:  # max 8 per Telegram
            flags = []
            if a.categoria_protetta: flags.append("CAT.PROT.")
            if a.art_16_puglia:      flags.append("ART.16")
            tag = " | ".join(flags)
            righe.append(f"• [{a.titolo[:60]}]({a.url})\n  `{a.fonte}` {tag}")
    else:
        righe.append("📋 Nessun annuncio con keywords prioritarie.")
        for a in annunci[:5]:
            righe.append(f"• [{a.titolo[:60]}]({a.url})")

    testo = "\n".join(righe)

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        r = requests.post(url, json={
            "chat_id": chat_id,
            "text": testo,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }, timeout=10)
        if r.ok:
            log.info("📱 Telegram inviato con successo")
        else:
            log.warning(f"Telegram error: {r.text}")
    except Exception as e:
        log.error(f"Errore Telegram: {e}")


# ─────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────
def main():
    print("\n" + "=" * 65)
    print("  AVVIO RICERCA LAVORO CATEGORIE PROTETTE — PUGLIA")
    print("=" * 65 + "\n")

    # Init DB
    conn = init_db(CONFIG["db_path"])

    # Esegui tutte le fonti
    fonti = [
        scrape_inpa,
        scrape_regione_puglia,
        scrape_arpal,
        scrape_gazzetta_ufficiale,
        scrape_comuni,
        scrape_asl_puglia,
        scrape_cpi_puglia,
        scrape_aggregatori_pa,
        scrape_indeed,
        scrape_infojobs,
    ]

    tutti_annunci_grezzi = []
    for fn in fonti:
        try:
            risultati = fn()
            tutti_annunci_grezzi.extend(risultati)
        except Exception as e:
            log.error(f"Errore in {fn.__name__}: {e}")

    log.info(f"\n📊 Totale grezzo: {len(tutti_annunci_grezzi)} annunci")

    # Deduplicazione per URL
    visti = set()
    annunci_dedup = []
    for a in tutti_annunci_grezzi:
        if a.url and a.url not in visti:
            visti.add(a.url)
            annunci_dedup.append(a)

    # Filtra solo nuovi (non già nel DB)
    nuovi = filtra_nuovi(conn, annunci_dedup)
    log.info(f"🆕 Nuovi (non nel DB): {len(nuovi)}")

    # Salva nel DB
    salva_annunci(conn, nuovi)

    # Recupera tutti dal DB per statistiche
    tutti_db = conn.execute("SELECT url FROM annunci").fetchall()

    # Genera report
    genera_report(nuovi, tutti_db)

    # Invia email se configurata
    if nuovi and CONFIG.get("email_destinatario"):
        invia_email(nuovi, CONFIG)

    # Invia notifica Telegram se configurata
    if nuovi and CONFIG.get("telegram_bot_token"):
        invia_telegram(nuovi, CONFIG)

    print(f"\n✅ Fatto! Trovati {len(nuovi)} nuovi annunci.")
    print(f"   Report: {CONFIG['output_txt']}")
    print(f"   JSON:   {CONFIG['output_json']}")
    print(f"   DB:     {CONFIG['db_path']}")
    conn.close()


if __name__ == "__main__":
    main()
