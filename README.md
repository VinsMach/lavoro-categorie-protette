# Cerca Lavoro — Categorie Protette Puglia 🔍

Script automatico che cerca ogni giorno opportunità lavorative per **categorie protette (L.68/99)** in Puglia, con focus su Pubblica Amministrazione.

## Cosa monitora

| Fonte | Tipo |
|-------|------|
| InPA — portale nazionale PA | PA |
| Regione Puglia (concorsi + RSS) | PA |
| ARPAL Puglia (collocamento mirato) | ARPAL |
| Centri per l'Impiego (tutte le province) | ARPAL |
| Gazzetta Ufficiale 3a serie | PA |
| Comuni: Bari, Taranto, Brindisi, Lecce, Foggia, Andria | PA |
| ASL Puglia (tutte e 6) | PA |
| Concorsando.it / EasyGov | PA |
| Indeed / InfoJobs | Privato |

## Setup su GitHub (gratuito)

### 1. Crea il repository

```bash
git init lavoro-categorie-protette
cd lavoro-categorie-protette
# Copia tutti i file qui
git add .
git commit -m "Setup iniziale"
git remote add origin https://github.com/TUO_USERNAME/lavoro-categorie-protette.git
git push -u origin main
```

### 2. Aggiungi i Secrets

Vai su **Settings → Secrets and variables → Actions → New repository secret** e aggiungi:

| Secret | Valore | Obbligatorio |
|--------|--------|-------------|
| `EMAIL_DESTINATARIO` | la tua email | ✅ |
| `EMAIL_MITTENTE` | email mittente Gmail | ✅ |
| `EMAIL_PASSWORD` | password app Gmail* | ✅ |
| `TELEGRAM_BOT_TOKEN` | token da @BotFather | ⬜ opzionale |
| `TELEGRAM_CHAT_ID` | il tuo chat ID | ⬜ opzionale |

*Per la password app Gmail: account Google → Sicurezza → Verifica in 2 passaggi → Password per le app

### 3. Abilita le Actions

Vai su **Actions** nel repository → clicca **"I understand my workflows, go ahead and enable them"**.

Lo script girerà automaticamente **ogni giorno alle 08:00** (ora italiana).

### 4. Esecuzione manuale

Actions → "Cerca Lavoro - Categorie Protette Puglia" → **Run workflow**

## Come ricevere i risultati

### Via email (Gmail)
Riceverai un'email HTML con tutti i nuovi annunci, suddivisi per priorità.

### Via Telegram (consigliato ✅)
1. Apri Telegram → cerca `@BotFather` → `/newbot`
2. Salva il token nel secret `TELEGRAM_BOT_TOKEN`
3. Cerca `@userinfobot` → invia `/start` → salva il chat ID in `TELEGRAM_CHAT_ID`

### Via GitHub Artifacts
Dopo ogni esecuzione, vai su **Actions → ultimo run → Artifacts** e scarica il file `.zip` con report.txt e risultati.json.

## Riferimenti legali

- **Legge 68/1999** — norme per il diritto al lavoro dei disabili
- **Art. 1** — lavoratori con invalidità civile ≥ 46%, cecità, sordità, invalidi del lavoro ≥ 33%
- **Art. 16** — L.R. Puglia 17/2005 — norme regionali aggiuntive sul collocamento mirato
- **ARPAL Puglia** — [arpal.regione.puglia.it](https://www.arpal.regione.puglia.it)

## Esecuzione locale

```bash
pip install -r requirements.txt
python cerca_lavoro_categorie_protette.py
```

Oppure con variabili d'ambiente:
```bash
EMAIL_DESTINATARIO=tua@email.it \
EMAIL_MITTENTE=bot@gmail.com \
EMAIL_PASSWORD=xxxx \
python cerca_lavoro_categorie_protette.py
```
