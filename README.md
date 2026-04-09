# Split — Shared Expense Calculator

A full-stack web app for two people to track shared bills, with income-proportional
contribution splitting, persistent shared data, and secure authentication.

---

## Features

- **Authentication** — username + password with TOTP two-factor authentication (Google Authenticator / Authy)
- **Shared data** — SQLite database; both users always see the same people, bills, and figures
- **Persistent storage** — data survives container restarts via a named Docker volume
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
├── docker-stack.yml          # Stack definition for Portainer
├── .github/
│   └── workflows/
│       └── deploy.yml        # Builds image and pushes to GHCR on every push to main
└── README.md
```

---

## Development Workflow

### 1. Edit code in VS Code

Make your changes locally. The integrated terminal (`Ctrl+\``) is useful for
running the app locally before pushing (see Local Development below).

### 2. Push with GitHub Desktop

- Open GitHub Desktop
- Your changed files will appear automatically under **Changes**
- Write a short summary in the description box (e.g. "Add electric bill")
- Click **Commit to main**, then **Push origin**

### 3. GitHub Actions builds the image

Every push to `main` triggers the workflow in `.github/workflows/deploy.yml`.
It builds the Docker image and pushes it to **GitHub Container Registry (GHCR)**
at `ghcr.io/YOUR_USERNAME/shared-bills-app:latest`.

Watch it run under the **Actions** tab on your GitHub repo. A green checkmark
means the image is ready to pull.

### 4. Portainer deploys the update

Once the image is pushed, go to Portainer and either:
- **Swarm stack** — update the stack and Portainer will pull the new image
- **Standalone stack** — re-pull the image and recreate the container

See the Portainer Deployment section below for first-time setup.

---

## Local Development

```bash
# Install dependencies
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

On first run the database is created and both users are seeded. Log in and
you will be prompted to set up MFA with your authenticator app.

---

## Portainer Deployment

### Make the GHCR image accessible

After your first push, the package on GHCR is private by default. You have two options:

**Option A — Make it public (simplest for a home lab)**
Go to `github.com → YOUR_USERNAME → Packages → shared-bills-app →
Package settings → Change visibility → Public`.
Portainer can then pull it with no credentials.

**Option B — Keep it private and add a registry credential in Portainer**
In Portainer go to **Registries → Add registry → GitHub Container Registry**
and enter your GitHub username and a Personal Access Token (PAT) with
`read:packages` scope. Create a PAT at `github.com → Settings →
Developer settings → Personal access tokens`.

---

### Docker Swarm stack (via Portainer)

In Portainer, go to your Swarm environment → **Stacks → Add stack →
Repository** (or paste the contents of `docker-stack.yml` directly).

Before deploying, create the five Docker secrets Portainer's Swarm
environment needs. In Portainer go to **Secrets → Add secret**:

| Secret name | Value |
|-------------|-------|
| `split_secret_key` | Output of `openssl rand -hex 32` |
| `split_user1_username` | First person's username |
| `split_user1_password` | First person's password |
| `split_user2_username` | Second person's username |
| `split_user2_password` | Second person's password |

Then deploy the stack. The SQLite data is stored in the `split_data` volume
which Portainer will create automatically.

> **Replicas**: Keep `replicas: 1` in `docker-stack.yml`. SQLite cannot handle
> concurrent writes from multiple containers. One replica is plenty for two users.

---

### Standalone container (via Portainer)

If you are running this on a standalone Docker host rather than Swarm,
go to **Containers → Add container** and fill in:

- **Image**: `ghcr.io/YOUR_USERNAME/shared-bills-app:latest`
- **Port mapping**: host `3000` → container `8080`
- **Volumes**: create a volume named `split_data` mapped to `/data`
- **Env variables**:

| Variable | Value |
|----------|-------|
| `USER1_USERNAME` | First person's username |
| `USER1_PASSWORD` | First person's password |
| `USER2_USERNAME` | Second person's username |
| `USER2_PASSWORD` | Second person's password |
| `SECRET_KEY` | Output of `openssl rand -hex 32` |
| `DB_PATH` | `/data/split.db` |

Click **Deploy the container**. Access the app at `http://<host-ip>:3000`.

---

### Updating to a new version

Once the GitHub Actions build completes:

**Swarm (Portainer)** — go to your stack → **Editor** → click **Update the stack**.
Portainer pulls the new `latest` image and does a rolling restart.

**Standalone (Portainer)** — go to the container → **Recreate** → check
**Re-pull image** → confirm. The volume keeps your data safe across the recreate.

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
  monthly   →  amount × 1
  quarterly →  amount × (1/3)
  yearly    →  amount × (1/12)

Monthly contribution  = Σ(bill_monthly) × share %
Per-paycheck deposit  = monthly_contribution × 12 / pay_frequency
```

---

## Environment Variables

| Variable | Default | Notes |
|----------|---------|-------|
| `DB_PATH` | `/data/split.db` | SQLite file path |
| `SECRET_KEY` | random | JWT signing key — **must be set to a stable value** |
| `TOKEN_HOURS` | `24` | Session duration in hours |
| `SECURE_COOKIE` | `false` | Set `true` when behind an HTTPS reverse proxy |
| `USER1_USERNAME` | — | First user's username |
| `USER1_PASSWORD` | — | First user's password (hashed on first start) |
| `USER2_USERNAME` | — | Second user's username |
| `USER2_PASSWORD` | — | Second user's password |

Every variable also accepts a `_FILE` suffix pointing to a Docker Secret file
(e.g. `SECRET_KEY_FILE=/run/secrets/split_secret_key`), which is how the
Swarm stack passes them in.

> **Important**: `SECRET_KEY` must be a stable value. If it changes,
> all active sessions are invalidated and everyone must log in again.
