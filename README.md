# Split — Shared Expense Calculator

A full-stack web app for two people to track shared bills, with income-proportional contribution splitting, persistent shared data, and secure authentication.

---

## Features

- **Authentication** — username + password with TOTP two-factor authentication (Google Authenticator / Authy)
- **Shared data** — SQLite database; both users always see the same people, bills, and figures
- **Persistent storage** — data survives container restarts via Docker volume
- **Auto-save** — income and bill changes are debounced and saved automatically
- **Bill frequencies** — monthly, quarterly, and yearly bills (all normalized to monthly for comparison)
- **Contribution toggle** — switch results between monthly and per-paycheck deposit amounts
- **Proportional splitting** — contributions weighted by each person's income, not 50/50

---

## Project Structure

```
shared-bills-app/
├── app/
│   ├── main.py               # FastAPI: auth, bills, people, calculate
│   └── static/
│       └── index.html        # Single-page frontend (HTML + CSS + JS)
├── requirements.txt
├── Dockerfile                # Python 3.12-slim → uvicorn
├── docker-stack.yml          # Docker Swarm deployment + secrets + volume
├── .github/
│   └── workflows/
│       └── deploy.yml        # Build → GHCR → SSH deploy
└── README.md
```

---

## Local Development

```bash
# Install deps
pip install -r requirements.txt

# Set credentials
export USER1_USERNAME=alice
export USER1_PASSWORD=changeme1
export USER2_USERNAME=bob
export USER2_PASSWORD=changeme2
export DB_PATH=./split.db
export SECRET_KEY=$(openssl rand -hex 32)

# Run
cd app
uvicorn main:app --reload --port 8080
# → http://localhost:8080
```

On first run the database is created and users are seeded. Navigate to the app,
log in, and you will be prompted to set up MFA with your authenticator app.

---

## Docker (local test)

```bash
docker build -t split .

docker run -p 3000:8080 \
  -e USER1_USERNAME=alice \
  -e USER1_PASSWORD=changeme1 \
  -e USER2_USERNAME=bob \
  -e USER2_PASSWORD=changeme2 \
  -e SECRET_KEY=$(openssl rand -hex 32) \
  -v split_data:/data \
  split

# → http://localhost:3000
```

---

## Docker Swarm Deployment

### 1. Initialize Swarm (one-time, if not already done)

```bash
docker swarm init
```

### 2. Create Docker Secrets

Secrets are never stored in the stack file or environment variables —
they are injected securely at runtime by Swarm:

```bash
# Random signing key for JWT tokens
printf "$(openssl rand -hex 32)" | docker secret create split_secret_key -

# User credentials
printf "alice"        | docker secret create split_user1_username -
printf "s3cr3tpass1"  | docker secret create split_user1_password -
printf "bob"          | docker secret create split_user2_username -
printf "s3cr3tpass2"  | docker secret create split_user2_password -
```

> Use `printf` (not `echo`) to avoid a trailing newline in the secret value.

### 3. Deploy

```bash
GITHUB_USER=your-gh-username \
  docker stack deploy -c docker-stack.yml split
```

### 4. Verify

```bash
docker service ls
docker service logs -f split_web
```

Access the app at `http://<swarm-node>:3000`

### Useful Commands

```bash
# Watch a rolling update in progress
docker service ps split_web

# Force re-deploy with the latest image
docker service update \
  --image ghcr.io/YOUR_USER/shared-bills-app:latest split_web

# Remove the stack (keeps the volume and data intact)
docker stack rm split

# Remove the data volume as well
docker volume rm split_split_data
```

---

## GitHub Actions CI/CD

On every push to `main` the workflow:
1. Builds the Docker image
2. Pushes it to **GitHub Container Registry** (`ghcr.io`)
3. SCPs `docker-stack.yml` to your Swarm manager
4. SSH's in and runs `docker stack deploy`

### Required GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|--------|-------|
| `SWARM_HOST` | IP or hostname of your Swarm manager |
| `SWARM_USER` | SSH username (e.g. `ubuntu`) |
| `SWARM_SSH_KEY` | Full private SSH key (`cat ~/.ssh/id_ed25519`) |
| `SWARM_SSH_PORT` | SSH port, usually `22` |

### Make the image public (optional)

After the first push: **ghcr.io → your package → Package settings →
Change visibility → Public**. This lets your Swarm pull without authentication.

---

## Authentication Flow

```
1. Login  (username + password)
         │
         ├─ First login → MFA Setup
         │     • Scan QR code with Google Authenticator or Authy
         │     • Enter 6-digit code to confirm
         │     • Session cookie issued
         │
         └─ Returning user → MFA Verify
               • Enter 6-digit code from app
               • Session cookie issued

Sessions : httpOnly cookie, 24 h TTL, SameSite=Strict
MFA      : TOTP RFC 6238 — compatible with any TOTP authenticator app
```

---

## Calculation Logic

```
Annual income  = paycheck × pay_frequency

Share %        = person_annual / (p1_annual + p2_annual)

Bill → monthly :
  monthly   →  amount  × 1
  quarterly →  amount  × (1/3)
  yearly    →  amount  × (1/12)

Monthly contribution   = Σ(bill_monthly) × share %
Per-paycheck deposit   = monthly_contribution × 12 / pay_frequency
```

---

## Environment Variables

| Variable | Default | Notes |
|----------|---------|-------|
| `DB_PATH` | `/data/split.db` | SQLite file path |
| `SECRET_KEY` | random | JWT signing key — **must be set persistently** |
| `TOKEN_HOURS` | `24` | Session duration in hours |
| `SECURE_COOKIE` | `false` | Set `true` behind an HTTPS reverse proxy |
| `USER1_USERNAME` | — | First user's username |
| `USER1_PASSWORD` | — | First user's password (hashed on first start) |
| `USER2_USERNAME` | — | Second user's username |
| `USER2_PASSWORD` | — | Second user's password |

Every variable also accepts a `_FILE` suffix pointing to a Docker Secret
(e.g. `SECRET_KEY_FILE=/run/secrets/split_secret_key`).

> **Important**: `SECRET_KEY` must be a stable value. If it changes,
> all active sessions are invalidated and everyone must log in again.

---

## SQLite & Replicas

SQLite uses file locking and cannot safely handle multiple concurrent
writers across containers. `replicas: 1` is intentional. One replica
is comfortably sufficient for two users.

To scale horizontally later, swap `sqlite3` for `asyncpg` / SQLAlchemy
with a PostgreSQL service in the stack file and increase `replicas`.
