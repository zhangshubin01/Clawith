# v1.8.2 Release Notes

## What's New

### Security
- **Fix account takeover via username collision** (#300): Prevents an attacker from creating an account with a username matching an existing SSO user's email, which could lead to unauthorized account access.
- **Fix duplicate user creation on repeated SSO logins**: Feishu and DingTalk SSO now correctly reuse existing accounts instead of creating duplicate users.

### AgentBay — Cloud Computer & Browser Automation
- **New: `agentbay_file_transfer` tool**: Transfer files between any two environments — agent workspace, browser sandbox, cloud desktop (computer), or code sandbox — in any direction.
- **Fix: Computer Take Control (TC) white screen**: TC now connects to the correct environment session (computer vs. browser) based on `env_type`. Previously, an existing browser session could hijack the computer TC connection.
- **Fix: OS-aware desktop paths**: The `agentbay_file_transfer` tool description now automatically reflects the correct paths for the agent's configured OS type:
  - Windows: `C:\Users\Administrator\Desktop\`
  - Linux: `/home/wuying/Desktop/`
- **Fix: Desktop file refresh**: After uploading to the Linux desktop directory, GNOME is notified to refresh icon display.
- Multiple Take Control stability fixes: CDP polling replaced with sleep, multi-tab cleanup, 40s navigate timeout, unhashable type errors.

### Feishu (Lark) — CardKit Streaming Cards
- Feishu bot responses now stream as animated typing-effect cards using the CardKit API (#287).
- Fixed SSE stream hang issues and websocket proxy bypass for system proxy conflicts.

### WeCom (Enterprise WeChat) Integration
- WeCom features are currently hidden behind a prerequisites notice (pending enterprise approval setup).
- Backend: Full org sync, domain verification endpoint, dual-credential architecture for API access.
- nginx: Added `WW_verify_*.txt` routing for WeCom domain ownership verification.

### DingTalk & Organization Sync
- Fixed DingTalk org sync permissions guide (`Contact.User.Read` scope).
- Fixed `open_id` vs `employee_id` user type handling in Feishu org sync.

### Other Bug Fixes
- **Fix: SSE stream protection** — `finish_reason` break guard added for OpenAI and Gemini streams to prevent runaway streams.
- **Fix: Duplicate tool `send_feishu_message`** — Removed duplicate DB entry; added dedup guard in tool loading to prevent `Tool names must be unique` LLM errors.
- **Fix: JWT token not consumed** on reset-password and verify-email routes.
- **Fix: NULL username/email** for SSO-created users in `list_users`.
- **Fix: Company name slug generation** — Added `anyascii` + `pypinyin` for universal CJK/Latin transliteration.
- **Fix: `publish_page` URL** — Correctly generates `try.clawith.ai` links on source deployments.
- **Fix: Agent template directory** — Dynamic default for source deployments.
- Various i18n fixes (TakeControlPanel, WeCom, DingTalk guide).

---

## Upgrade Guide

> **No database migrations required.** No new environment variables.

### Docker Deployment (Recommended)

```bash
git pull origin main
docker compose down && docker compose up -d --build
```

> **Important**: If your server does not have Node.js/npm, the frontend must be built locally and uploaded, or installed via nvm (see note below).

### Source Deployment

```bash
git pull origin main

# Install new Python dependency
pip install anyascii>=0.3.2

# Rebuild frontend
cd frontend && npm install && npm run build
cd ..

# Restart services
```

### nginx Update Required

A new routing rule for WeCom domain verification has been added to `nginx.conf`. If you manage nginx separately (not via Docker), add this block inside your `server {}` before the WebSocket proxy section:

```nginx
location ~ ^/WW_verify_[A-Za-z0-9]+\.txt$ {
    proxy_pass http://backend:8000/api/wecom-verify$request_uri;
    proxy_set_header Host $http_host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### Kubernetes (Helm)

```bash
helm upgrade clawith helm/clawith/ -f values.yaml
```

No migration job needed.

---

## Upgrade Notes — Lessons Learned (from our production upgrade)

The following issues were encountered during the v1.8.1 → v1.8.2 production upgrade and may affect other deployers:

### 1. Server without npm: dist.zip may be stale in git

**Problem**: Our production server did not have Node.js/npm installed. The `frontend/dist.zip` tracked in git can fall behind when frontend changes are made and committed without a corresponding build (e.g., when the build was done on a separate dev server).

**Symptoms**: After `git pull`, the dist.zip in the repo may not include the latest frontend changes, causing new features to be invisible in the UI even though the backend is updated.

**Solutions**:
- **Option A (Recommended)**: Install Node.js on the deployment server via nvm (no root required):
  ```bash
  curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
  source ~/.nvm/nvm.sh
  nvm install 20
  ```
  Then build on the server: `cd frontend && npm install && npm run build`
- **Option B**: Build locally and upload via SCP:
  ```bash
  # On local machine:
  cd frontend && npm run build && cd dist && zip -r ../dist.zip .
  scp frontend/dist.zip user@server:/path/to/Clawith/frontend/dist.zip
  ```

> **Note**: In China-based server environments, downloading from raw.githubusercontent.com may be very slow. If so, use a proxy or mirror.

### 2. `anyascii` is a new required Python dependency

**Problem**: Starting from this release, `anyascii>=0.3.2` is required. If upgrading without rebuilding the Docker image (e.g., using `docker cp` to update only the app directory), this dependency must be installed separately inside the container:

```bash
docker exec clawith-backend-1 pip install anyascii>=0.3.2
```

For standard `docker compose up --build` upgrades, this is handled automatically.
