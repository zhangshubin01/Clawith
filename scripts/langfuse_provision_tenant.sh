#!/usr/bin/env bash
# Langfuse tenant provision helper (P1 multi-tenant isolation).
#
# Creates a Langfuse project for one tenant and prints the JSON fragment to
# paste into backend env `LANGFUSE_TENANT_KEYS` (docker-compose.yml already
# passes it through). Requires org-level access to the Langfuse API — use the
# project owner's pk/sk of ANY existing project? No: project-level keys are
# rejected by org endpoints (403). Use an org API key or the admin UI.
#
# Two modes:
#   1. ORG_KEY provided: fully automatic via Public API
#      ORG_PK / ORG_SK = an Organization-level API key pair
#      (Langfuse UI: Organization Settings -> API Keys)
#   2. No ORG_KEY: prints manual UI steps (create project, generate API keys)
#
# Usage:
#   ORG_PK=pk-lf-... ORG_SK=sk-lf-... \
#     ./scripts/langfuse_provision_tenant.sh <tenant_id> [project_name] [base_url]
#
# Example output (mode 1):
#   "a4c4bc0b-f8f8-4783-8c63-99e46037428f": {"public_key": "pk-lf-...", "secret_key": "sk-lf-..."}

set -euo pipefail

TENANT_ID="${1:?usage: langfuse_provision_tenant.sh <tenant_id> [project_name] [base_url]}"
PROJECT_NAME="${2:-tenant-${TENANT_ID:0:8}}"
BASE_URL="${3:-http://localhost:3000}"
ORG_PK="${ORG_PK:-}"
ORG_SK="${ORG_SK:-}"

if [[ -z "${ORG_PK}" || -z "${ORG_SK}" ]]; then
  cat <<EOF
No ORG_PK/ORG_SK provided — manual mode.

Manual steps (Langfuse UI at ${BASE_URL}):
  1. Sign in as an admin/owner.
  2. Create a project named "${PROJECT_NAME}" (for tenant ${TENANT_ID}).
  3. Open project Settings -> API Keys, create a key pair.
  4. Add to backend env LANGFUSE_TENANT_KEYS:
     {"${TENANT_ID}": {"public_key": "<pk-lf-...>", "secret_key": "<sk-lf-...>"}}
  5. Recreate the backend container (docker compose up -d --no-deps backend).

To automate, pass an Organization-level API key pair (UI: Organization
Settings -> API Keys) as ORG_PK/ORG_SK.
EOF
  exit 1
fi

AUTH="${ORG_PK}:${ORG_SK}"

echo "== Creating project: ${PROJECT_NAME} ==" >&2
PROJECT_JSON="$(curl -sf -u "${AUTH}" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"${PROJECT_NAME}\"}" \
  "${BASE_URL}/api/public/projects")"
PROJECT_ID="$(echo "${PROJECT_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin)['id'])")"
echo "   project_id=${PROJECT_ID}" >&2

echo "== Creating API key ==" >&2
KEY_JSON="$(curl -sf -u "${AUTH}" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"clawith-${TENANT_ID:0:8}\"}" \
  "${BASE_URL}/api/public/projects/${PROJECT_ID}/apiKeys")"

python3 - "${TENANT_ID}" <<'PY'
import json, sys
tenant_id = sys.argv[1]
key = json.loads(sys.stdin.read())
public_key = key.get("public_key") or key.get("id")
secret_key = key.get("secret_key")
print(json.dumps({tenant_id: {"public_key": public_key, "secret_key": secret_key}}, indent=2))
PY
echo
echo "Paste the fragment above into LANGFUSE_TENANT_KEYS in backend env." >&2
