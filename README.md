# SnipAlert (Docker)

Discord bot + biweekly reminder + earlier-slot watcher for booking haircuts at
Barbershop Alan (SalonHub), with Google Calendar sync. One container, runs on
your Proxmox/Docker host.

## What it does
- **Bot** (real-time via Discord Gateway/websocket): reads your messages — `check <datum>`, `boek <tijd>`,
  `status`, `help` — books via SalonHub, writes the event to Google Calendar,
  and cancels your previous appointment on a swap. All replies in Dutch.
- **Earlier-slot watcher** (every 15 min): if a slot *earlier than your booked
  time* opens on your booked day, it pings you.
- **Biweekly reminder** (Wed + Fri, `REMINDER_HOUR`): nudges you with Fri /
  Sat-afternoon / Sun availability once you're ~10+ days past your last cut.

## One-time setup

### 1. Discord
Reuse your existing **SnipAlert** bot token + channel ID (Message Content Intent ON).
Put them in `.env`.

### 2. Google Calendar (service account — headless, no login on the server)
1. Google Cloud Console → create a project → **Enable the Google Calendar API**.
2. **APIs & Services → Credentials → Create credentials → Service account.**
3. On the service account → **Keys → Add key → JSON** → download it.
4. Save that file as **`secrets/gcal-sa.json`** in this folder.
5. Copy the service account's email (looks like `name@project.iam.gserviceaccount.com`).
6. In **Google Calendar** (web) → your calendar → **Settings → Share with specific
   people → Add** the service-account email → permission **"Make changes to events."**
7. Set `GCAL_CALENDAR_ID` in `.env` to your calendar's ID (your Gmail address for
   your primary calendar).

> Events land on your Google Calendar — and show up in Apple Calendar too if your
> Google account is added there.

### 3. Configure
```bash
cp .env.example .env
# edit .env: Discord token/channel, customer details, GCAL_CALENDAR_ID
```

### 4. Seed state (continuity with the Mac version)
`data/state.json` is included with your current appointment so nothing is lost.
(If starting fresh, just delete it — the bot recreates it.)

### 5. Run
```bash
docker compose up -d --build
docker compose logs -f          # watch it boot; type `help` in Discord to verify
```

## Switching off the Mac version
Once the container works, **stop the macOS launchd jobs** so they don't double up
(double alerts / double bookings):
```bash
launchctl unload ~/Library/LaunchAgents/com.knipalert.gateway.plist
launchctl unload ~/Library/LaunchAgents/com.knipalert.reminder.plist
launchctl unload ~/Library/LaunchAgents/com.knipalert.dayreminder.plist
launchctl unload ~/Library/LaunchAgents/com.knipalert.watch.plist
```

## Deploy from GitHub (commit → live)

The repo includes a GitHub Action (`.github/workflows/docker-publish.yml`) that
builds the image and pushes it to **GHCR** (`ghcr.io/jdfs404/knipalert`) on every push to
`main`. The Proxmox host runs the prebuilt image and Watchtower auto-pulls updates —
so committing to `main` makes it live within ~2 minutes.

> ⚠️ **Never commit secrets.** `.env`, `secrets/` and `data/` are git-ignored.
> Only code goes to GitHub; your token, customer details, Google key and state
> stay on the server. (`.env.example` ships with placeholders only.)

### One-time
1. **Create a repo** (public recommended — no secrets in it) and push:
   ```bash
   cd ~/knipalert
   git init && git add . && git commit -m "SnipAlert"
   git branch -M main
   git remote add origin git@github.com:JDFS404/knipalert.git
   git push -u origin main
   ```
   The Action runs and publishes `ghcr.io/jdfs404/knipalert:latest`.

2. **On the Proxmox host**, put only the runtime bits in a folder:
   `.env` (real values), `secrets/gcal-sa.json`, `data/state.json`.

3. **Private image?** Authenticate Docker once so it (and Watchtower) can pull:
   ```bash
   echo "$GH_PAT" | docker login ghcr.io -u JDFS404 --password-stdin
   ```
   (`GH_PAT` = a GitHub token with `read:packages`. Or make the GHCR *package*
   public — the image holds no secrets — and skip login.)

4. **Start it:**
   ```bash
   docker compose -f docker-compose.prod.yml pull
   docker compose -f docker-compose.prod.yml up -d
   ```

### From then on
`git push` to `main` → Action rebuilds → Watchtower pulls & restarts. Done.
(Or update manually: `docker compose -f docker-compose.prod.yml pull && up -d`.)

## Notes
- Secrets live in `.env` + `secrets/` (git-ignored). Nothing is committed.
- State persists in the `./data` volume across restarts/rebuilds.
- Change reminder days in `knipalert/run.py` (`REMINDER_DAYS`), hour via `REMINDER_HOUR`.
