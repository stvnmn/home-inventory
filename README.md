# Home Inventory

A self-hosted household inventory tracker. Log items in and out (by barcode scan or manually), track expiration dates, and get Pushover alerts when it's time to restock or something is about to expire. It runs in Docker and stores its data in a local SQLite database.

## Features

- Scan UPC/EAN barcodes with the phone camera (name suggested from Open Food Facts), or add items manually
- Per-unit tracking: each bottle/box is logged in and marked used up, so the app learns how long one lasts
- Run-out prediction from your own usage history, with a restock reminder before you run out
- Expiration tracking with warnings for items expiring soon
- Pushover notifications, configured from the Settings page (with a test button)
- Search, filters, alerts view, history with undo, edit/delete
- Local accounts (shared inventory) with a login page
- Responsive, mobile-friendly layout

## Requirements

- Docker Engine and Docker Compose v2 (or Python 3.9+ to run locally)

## Quick start (Docker)

```
git clone https://github.com/stvnmn/home-inventory.git
cd home-inventory
docker compose up -d --build
```

Open [http://localhost:3081](http://localhost:3081) and create your account on the first visit.

The Compose setup stores the database at `/DATA/AppData/home_inventory/data/inventory.db` on the host. To use a different location, change the host path in the `volumes` section of `docker-compose.yml`. Back up this directory regularly.

## Run locally (no Docker)

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python run.py                  # http://127.0.0.1:8000
.venv/bin/python run.py --host 0.0.0.0   # reachable from other devices on your LAN
```

An optional `.env` file is loaded automatically, and data goes in `./data/inventory.db`.

## Configuration

Pushover keys and alert timing are set on the app's **Settings** page and saved in the database. Environment variables only provide the initial defaults; anything saved in Settings takes precedence.

| Variable                 | Default              | Description                                                 |
| ------------------------ | -------------------- | ----------------------------------------------------------- |
| `DB_PATH`                | `/data/inventory.db` | SQLite database path inside the container                   |
| `TZ`                     | `Etc/UTC`            | Timezone used for dates (added/used up/expiry calculations) |
| `RESTOCK_LEAD_DAYS`      | `14`                 | Days before predicted run-out to send a restock reminder    |
| `EXPIRY_WARN_DAYS`       | `30`                 | Days before expiration to warn                              |
| `PUSHOVER_APP_TOKEN`     | empty                | Default Pushover app token (or set it in Settings)          |
| `PUSHOVER_USER_KEY`      | empty                | Default Pushover user key (or set it in Settings)           |
| `CHECK_INTERVAL_SECONDS` | `3600`               | How often the background alert check runs                   |

## Camera scanning needs HTTPS

Browsers only allow camera access on HTTPS (or `localhost`). Put the app behind a local Nginx Proxy Manager host: forward to the Docker host's IP on port `3081` (scheme `http`), attach your local SSL certificate, and open the `https://` name on your phone. Typing a barcode works without HTTPS.

## How it works

- **Log in:** scan or type a barcode, add a quantity and optional expiry date.
- **Log used up:** marks one unit finished, soonest-expiring first.
- **Prediction:** the average days each unit lasted (from when it was added or when the previous one ran out, whichever is later) times the units in stock. The restock alert fires `RESTOCK_LEAD_DAYS` before the predicted run-out, or when stock hits 0 after past usage. A prediction needs at least one finished unit.
- **Alerts:** checked hourly, each alert is sent once. Use **Settings → Run alert check now** to trigger a check manually.

## ZimaOS / CasaOS

### Publish the image to GitHub Container Registry

The workflow in `.github/workflows/publish-image.yml` automatically builds and publishes multi-architecture images (`linux/amd64` and `linux/arm64`) to `ghcr.io/stvnmn/home-inventory` when code is pushed to `main` or a version tag such as `v1.0.0` is pushed. It also supports manual runs from the repository's **Actions** tab. The `main` branch image is tagged `latest`, which is what the ZimaOS Compose file uses. The workflow uses GitHub's built-in token; no registry credentials need to be added to repository secrets.

After the first successful workflow run, open the container package from the repository's **Packages** section and set its visibility to **Public** in **Package settings** so ZimaOS can pull without registry credentials. This is separate from making the source repository public.

### Install

In ZimaOS, open **App Store → Custom Install → Compose**, paste the contents of `docker-compose_zimaos.yml`, and install. The app is then available on port `3081`. The `pull_policy: always` setting makes Docker check GHCR for the current image when deploying.

Keep the database mount path, `/DATA/AppData/home_inventory/data:/data`, unchanged when updating if you want to keep the same database. Back up the database before replacing an existing deployment. Stop or uninstall a previous deployment first, but do not delete its app data; duplicate deployments can conflict because they use the same container name and port.

For code updates, push to `main`, wait for the **Publish container image** workflow to succeed, then redeploy/update the ZimaOS app so it pulls the new `latest` image. To use this template for a fork, change the image in both the workflow and the Compose file to the fork's GHCR package path.

## Security and privacy

The app stores inventory, account password hashes, and your Pushover token in its SQLite database on the host; the repository and container image contain only the application. Do not commit `.env` files or database backups.

Accounts are local to the app and all accounts share the same inventory and settings. **Signup is open to anyone who can reach the page**, and the Pushover token is stored unencrypted in the database. Keep the app on a trusted network and do not expose it directly to the public internet. For remote access, use a VPN or an access-controlled HTTPS reverse proxy.

## Data backup

The database is stored in `/DATA/AppData/home_inventory/data/inventory.db` by default. To make a consistent backup, stop the container, copy the file to a safe location outside the repository, then start the container again. The default container name is `home_inventory`:

```
docker stop home_inventory
cp /DATA/AppData/home_inventory/data/inventory.db "$HOME/inventory-backup.db"
docker start home_inventory
```

Do not commit the backup file. If you changed the host data path in Compose, use that path instead.

## Tech stack

Python 3.12 · FastAPI · SQLite · Uvicorn · vanilla JS · Docker
