# 🔔 NudgeBot

Il reminder Telegram che non ti molla.

## Setup Locale

1. **Crea il bot su Telegram**
   - Vai su [@BotFather](https://t.me/BotFather)
   - Crea un nuovo bot con `/newbot`
   - Copia il token

2. **Configura le variabili d'ambiente**
   ```bash
   cp .env.example .env
   # Modifica .env con il tuo BOT_TOKEN e DATABASE_URL
   ```

3. **Installa le dipendenze**
   ```bash
   pip install -r requirements.txt
   ```

4. **Avvia PostgreSQL** (locale o Docker)
   ```bash
   docker run -d --name nudgebot-db \
     -e POSTGRES_DB=nudgebot \
     -e POSTGRES_USER=nudgebot \
     -e POSTGRES_PASSWORD=nudgebot \
     -p 5432:5432 postgres:16
   ```

5. **Avvia il bot** (in modalità polling per sviluppo)
   ```bash
   export BOT_TOKEN="il_tuo_token"
   export DATABASE_URL="postgresql://nudgebot:nudgebot@localhost:5432/nudgebot"
   python bot.py
   ```

## Deploy su Railway

1. **Crea un repo GitHub** e pusha il codice
   ```bash
   git init
   git add .
   git commit -m "NudgeBot v1.0"
   git remote add origin https://github.com/tuo-user/nudgebot.git
   git push -u origin main
   ```

2. **Su Railway:**
   - Crea un nuovo progetto → Deploy from GitHub
   - Aggiungi il plugin **PostgreSQL** (click su + New → Database → PostgreSQL)
   - Railway genera automaticamente `DATABASE_URL`

3. **Variabili d'ambiente su Railway:**
   - `BOT_TOKEN` → il token di BotFather
   - `WEBHOOK_URL` → il dominio Railway (es. `https://nudgebot-xxx.up.railway.app`)
   - `DATABASE_URL` → già impostata dal plugin PostgreSQL

4. **Deploy!** Railway fa build e deploy automatico ad ogni push.

## Struttura del Progetto

```
nudgebot/
├── bot.py                  # Entry point principale
├── config.py               # Configurazione e variabili d'ambiente
├── database.py             # Modelli SQLAlchemy + setup DB
├── handlers/
│   ├── start.py            # Onboarding (/start, categorie, farmaci)
│   ├── commands.py         # Comandi (/oggi, /lista, /farmaci, ecc.)
│   ├── callbacks.py        # Bottoni inline (fatto, snooze, cancella)
│   └── reminders.py        # Creazione reminder da testo libero
├── services/
│   ├── parser.py           # Parser linguaggio naturale italiano
│   ├── messages.py         # Template messaggi del bot
│   └── scheduler.py        # APScheduler per invio reminder e nudge
├── requirements.txt
├── Procfile                # Per Railway
├── railway.toml            # Config Railway
└── .env.example            # Template variabili d'ambiente
```

## Comandi Disponibili

| Comando | Funzione |
|---------|----------|
| `/start` | Avvia il bot e onboarding |
| `/oggi` | Reminder di oggi |
| `/domani` | Reminder di domani |
| `/settimana` | Prossimi 7 giorni |
| `/lista` | Tutti i reminder attivi |
| `/farmaci` | Farmaci configurati |
| `/scadenze` | Scadenze future |
| `/fatto` | Completa l'ultimo reminder |
| `/cancella` | Cancella un reminder |
| `/silenzio 2h` | Muto temporaneo |
| `/timezone` | Cambia fuso orario |
| `/impostazioni` | Le tue preferenze |
| `/export` | Esporta i dati in JSON |
| `/help` | Guida rapida |

## Come Funziona

1. Scrivi al bot in linguaggio naturale: *"ricordami di comprare il latte domani alle 10"*
2. Il bot analizza il testo e ti chiede conferma
3. All'orario impostato ricevi il reminder
4. Se non rispondi, il bot insiste con fino a 3 nudge progressivi
5. Confermi con un tap o rispondendo "ok" / "fatto"

## Licenza

MIT
