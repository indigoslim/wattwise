# Wattwise — Docker Guide

This document covers everything Docker-related: architecture decisions, volume management, networking, reverse proxy setup, and advanced configuration.

For general installation see [README.md](README.md).  
For development and code architecture see [DEVELOPMENT.md](DEVELOPMENT.md).

---

## Contents

- [Container architecture](#container-architecture)
- [Volumes](#volumes)
- [Networking](#networking)
- [Environment variables](#environment-variables)
- [Compose reference](#compose-reference)
- [Logs](#logs)
- [Health check](#health-check)
- [Using a NAS or network share for backups](#using-a-nas-or-network-share-for-backups)
- [Reverse proxy](#reverse-proxy)
- [Running on a Raspberry Pi](#running-on-a-raspberry-pi)
- [Updating the container](#updating-the-container)
- [Data management](#data-management)

---

## Container architecture

```
Host
│
├─ compose.yml
├─ .env
│
├─ [named volume] wattwise_data     → /app/data      (SQLite database)
├─ [named volume] wattwise_backups  → /app/backups   (automated backups)
│
└─ Container: energy
   ├─ python:3.12-slim base
   ├─ FastAPI + Uvicorn (port 9521 internal)
   ├─ Backup scheduler (daemon thread)
   └─ Static files served from /app/static/
```

Single container, no external database, no message queue, no external dependencies at runtime. Chart.js is the only CDN resource — loaded in the browser from `cdnjs.cloudflare.com`.

---

## Volumes

The project uses two **named volumes** by default. Named volumes are managed by Docker and persist independently of the container lifecycle — `docker compose down` does not delete them.

| Volume | Mount point | Contents |
|---|---|---|
| `wattwise_data` | `/app/data` | `wattwise.db` — the SQLite database |
| `wattwise_backups` | `/app/backups` | Automated backup `.db` files |

### Inspecting volumes

```bash
# List volumes
docker volume ls | grep energy

# Inspect a volume (shows mount path on host)
docker volume inspect wattwise_data

# Find where Docker stores volumes on disk (Linux default)
# /var/lib/docker/volumes/wattwise_data/_data/
```

### Switching to bind mounts

If you want the database or backups at a specific host path (e.g. pointing at an existing database, or directing backups to a NAS mount), replace the named volumes in `compose.yml`:

```yaml
volumes:
  - /your/host/path/data:/app/data
  - /your/host/path/backups:/app/backups
```

Remove the `volumes:` block at the bottom of the file when using bind mounts:

```yaml
# Remove or comment out:
# volumes:
#   wattwise_data:
#   wattwise_backups:
```

### Migrating from bind mounts to named volumes

```bash
# 1. Copy your existing database into the new named volume
docker run --rm \
  -v /your/old/path/data:/source \
  -v wattwise_data:/dest \
  alpine cp /source/wattwise.db /dest/wattwise.db

# 2. Update compose.yml to use named volumes
# 3. Restart
docker compose up -d
```

---

## Networking

The container runs on an isolated bridge network `wattwise_net`. The dashboard is published on the host at `PORT` (default 9521).

```yaml
networks:
  wattwise_net:
    driver: bridge
```

### Changing the port

Edit `.env`:

```
PORT=8080
```

Then restart:

```bash
docker compose up -d
```

The internal container port is always 9521. Only the host-side port changes.

### Accessing from other devices on your network

Use your host machine's LAN IP address:

```
http://192.168.1.x:9521
```

Find your host IP:
```bash
ip addr show          # Linux
ipconfig              # Windows
ifconfig en0          # Mac
```

---

## Environment variables

All variables are set in `.env`. Copy `.env.example` to start.

| Variable | Default | Required | Description |
|---|---|---|---|
| `PORT` | `9521` | No | Host port |
| `TZ` | `UTC` | **Yes** | IANA timezone — must match your CSV export timezone |
| `DB_PATH` | `/app/data/wattwise.db` | No | Database path inside container |
| `BACKUP_DIR` | `/app/backups` | No | Backup directory inside container |
| `DEBUG` | `false` | No | Verbose Python logging |
| `PYTHONUNBUFFERED` | `1` | No | Ensures logs appear immediately |
| `CONS_START` | _(unset)_ | No | Override consumption valid-from date (YYYY-MM-DD) |
| `PROD_START` | _(unset)_ | No | Override production valid-from date (YYYY-MM-DD) |
| `NET_START` | _(unset)_ | No | Override chart display floor date (YYYY-MM-DD) |

`TZ` is the most important variable to set correctly. It controls how timestamps are interpreted across the container, scheduler, and log output.

---

## Compose reference

Full annotated `compose.yml`:

```yaml
services:
  energy:
    build: .                          # Build from local Dockerfile
    image: wattwise:latest              # Tag the built image
    container_name: wattwise            # Fixed name for easy CLI use
    restart: unless-stopped           # Auto-restart on failure or reboot
    ports:
      - "${PORT:-9521}:9521"          # HOST:CONTAINER — host port from .env
    volumes:
      - wattwise_data:/app/data         # Database
      - wattwise_backups:/app/backups   # Automated backups
    env_file:
      - .env                          # All env vars from .env file
    logging:
      driver: "json-file"
      options:
        max-size: "10m"               # Rotate logs at 10MB
        max-file: "3"                 # Keep 3 rotated files
    networks:
      - wattwise_net

volumes:
  wattwise_data:                        # Persistent database volume
  wattwise_backups:                     # Persistent backup volume

networks:
  wattwise_net:
    driver: bridge                    # Isolated bridge network
```

---

## Logs

```bash
# Follow live logs
docker compose logs -f

# Last 100 lines
docker compose logs --tail=100

# Logs with timestamps
docker compose logs -f -t

# Filter for errors only
docker compose logs | grep -i error
```

Log files are stored by Docker's json-file driver and rotated automatically (10MB × 3 files).

---

## Health check

```bash
# Quick health check
curl http://localhost:9521/api/health

# Expected response
{"status": "ok"}
```

---

## Using a NAS or network share for backups

The backup scheduler writes to `BACKUP_DIR` inside the container. To direct backups to a NAS:

### NFS mount (Linux)

```bash
# 1. Mount NFS share on host
sudo mount -t nfs 192.168.1.x:/volume1/backups /mnt/nas/wattwise-backups

# 2. Add to /etc/fstab for persistence across reboots
192.168.1.x:/volume1/backups  /mnt/nas/wattwise-backups  nfs  defaults  0  0

# 3. Use bind mount in compose.yml
volumes:
  - /mnt/nas/wattwise-backups:/app/backups
```

### SMB/CIFS mount (Linux)

```bash
sudo mount -t cifs //192.168.1.x/backups /mnt/nas/wattwise-backups \
  -o username=user,password=pass,uid=1000,gid=1000
```

**Important:** The scheduler skips backup silently if `BACKUP_DIR` is unavailable (e.g. NAS offline). It logs a warning and continues — the app will not crash. Backups resume automatically when the mount becomes available again.

---

## Reverse proxy

Wattwise has no built-in authentication. If you want to access it outside your LAN, put it behind a reverse proxy with authentication.

### Nginx with basic auth

```nginx
server {
    listen 443 ssl;
    server_name energy.yourdomain.com;

    ssl_certificate     /etc/ssl/certs/your.crt;
    ssl_certificate_key /etc/ssl/private/your.key;

    auth_basic           "Wattwise";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass         http://localhost:9521;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

Create a password file:
```bash
htpasswd -c /etc/nginx/.htpasswd yourusername
```

### Traefik (Docker labels)

```yaml
# Add to the energy service in compose.yml
labels:
  - "traefik.enable=true"
  - "traefik.http.routers.energy.rule=Host(`energy.yourdomain.com`)"
  - "traefik.http.routers.energy.entrypoints=websecure"
  - "traefik.http.routers.energy.tls.certresolver=letsencrypt"
  - "traefik.http.middlewares.energy-auth.basicauth.users=user:$$hashed$$password"
  - "traefik.http.routers.energy.middlewares=energy-auth"
```

---

## Running on a Raspberry Pi

The standard Docker install works on Pi 4 and Pi 5 (64-bit OS recommended).

```bash
# Install Docker on Raspberry Pi OS
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in, then:
docker compose up -d --build
```

Performance notes:
- Pi 4 (4GB): comfortable, chart rendering is fast
- Pi 3: usable, may be slow on first load with large datasets
- SQLite performs well at the data volumes this app handles

---

## Updating the container

Data is safe — it lives in named volumes, not in the container.

```bash
# 1. Stop the container
docker compose down

# 2. Replace the project files with the new version
#    Keep your existing .env — do not overwrite it

# 3. Rebuild and start
docker compose up -d --build --no-cache

# 4. Confirm it started cleanly
docker compose logs -f
```

### Removing old images after upgrade

```bash
# Remove dangling images to free space
docker image prune -f
```

---

## Data management

### Backing up the database manually

```bash
# Download via API
curl http://localhost:9521/api/backup -o energy_backup_$(date +%Y%m%d).db

# Or copy directly from the volume
docker cp wattwise:/app/data/wattwise.db ./energy_backup_$(date +%Y%m%d).db
```

### Restoring a database

Via the UI: **Settings → Backup & Restore → Restore from Backup**

Via CLI:
```bash
docker compose down
docker cp your_backup.db energy:/app/data/wattwise.db
docker compose up -d
```

### Completely resetting the database

```bash
docker compose down
docker volume rm wattwise_data
docker compose up -d
# The app will create a fresh database and run the onboarding wizard
```

### Exporting the volume for migration

```bash
# Export volume contents to a tar archive
docker run --rm \
  -v wattwise_data:/data \
  -v $(pwd):/backup \
  alpine tar czf /backup/wattwise_data_backup.tar.gz -C /data .

# Restore on another machine
docker run --rm \
  -v wattwise_data:/data \
  -v $(pwd):/backup \
  alpine tar xzf /backup/wattwise_data_backup.tar.gz -C /data
```
