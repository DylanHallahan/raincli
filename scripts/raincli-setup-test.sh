#!/usr/bin/env bash
# Follows SETUP.md with an isolated HOME: clone -> --no-deps client install -> skill -> downloaded
# config -> connector (inbox mode) delivering to a FAKE herdr. Server side: throwaway database on the
# local TEST PostgreSQL + loopback uvicorn. Never touches real Herdr sessions or real credentials.
# `git clone` of this checkout stands in for `gh repo clone`; Herdr tab/agent creation (SETUP step 4)
# is represented by the fake herdr's two named agents.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRV="$ROOT/raincli/.venv/bin"
set -a; . "$HOME/.config/raincli-dev/test-pg.env"; set +a
: "${RAINCLI_TEST_DATABASE_URL:?}"
ADMIN_URL="$RAINCLI_TEST_DATABASE_URL"
T="$(mktemp -d)"; chmod 700 "$T"; SERVER_PID=""
cleanup() { [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null; psql "$ADMIN_URL" -qc "DROP DATABASE IF EXISTS raincli_setup_test" 2>/dev/null; rm -rf -- "${T:?}"; }
trap cleanup EXIT
PASS=0; FAIL=0
check() { if eval "$2"; then PASS=$((PASS+1)); echo "   PASS: $1"; else FAIL=$((FAIL+1)); echo "   FAIL: $1"; fi; }

echo "== server (test only)"
psql "$RAINCLI_TEST_DATABASE_URL" -qc "DROP DATABASE IF EXISTS raincli_setup_test" -c "CREATE DATABASE raincli_setup_test" 2>/dev/null
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
export RAINCLI_DATABASE_URL="${RAINCLI_TEST_DATABASE_URL%/*}/raincli_setup_test" RAINCLI_PUBLIC_URL="http://127.0.0.1:$PORT"
export RAINCLI_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')" RAINCLI_COOKIE_SECURE=0
(cd "$ROOT/raincli" && "$SRV/python" -m raincli_server.migrate upgrade head 2>/dev/null)
"$SRV/uvicorn" raincli_server.app:app_from_env --factory --host 127.0.0.1 --port "$PORT" --no-access-log >"$T/server.log" 2>&1 &
SERVER_PID=$!
until curl -fs "$RAINCLI_PUBLIC_URL/api/v1/health" >/dev/null 2>&1; do sleep 0.2; done
printf 'setup test password\n' | "$SRV/raincli-admin" create-user --email you@example.test --name You >/dev/null
printf 'setup test password\n' | "$SRV/raincli-admin" create-user --email mate@example.test --name Mate >/dev/null
"$SRV/raincli-admin" create-team --slug pilot --name Pilot --owner mate@example.test >/dev/null
"$SRV/raincli-admin" add-member --team pilot --email you@example.test >/dev/null
mkdir -p "$T/home/Downloads" "$T/mate"
# Stands in for the website's one-time "download config" (SETUP step 2); web download is covered by tests.
"$SRV/raincli-admin" register-agent --team pilot --owner you@example.test --handle you-inbox --out "$T/home/Downloads/raincli-you-inbox.json" >/dev/null
"$SRV/raincli-admin" register-agent --team pilot --owner mate@example.test --handle mate-agent --out "$T/mate/agent.json" >/dev/null
unset RAINCLI_DATABASE_URL RAINCLI_SECRET_KEY RAINCLI_PUBLIC_URL RAINCLI_COOKIE_SECURE RAINCLI_TEST_DATABASE_URL

# ---- teammate machine: isolated HOME, commands as in SETUP.md ----
export HOME="$T/home" PATH="$T/home/.local/bin:/usr/bin:/bin"
unset RAINCLI_CONFIG VIRTUAL_ENV PYTHONPATH
echo "== SETUP 1: clone + client install + skill"
git clone -q "$ROOT" ~/src/raincli-repo
( cd ~/src/raincli-repo/raincli && python3 -m venv .venv && .venv/bin/pip install --quiet --disable-pip-version-check --no-deps . \
  && mkdir -p ~/.local/bin && ln -sfn "$PWD/.venv/bin/raincli" ~/.local/bin/raincli )
check "raincli on PATH (client only, no server deps installed)" \
  'raincli --version >/dev/null && ! ~/src/raincli-repo/raincli/.venv/bin/python -c "import fastapi" 2>/dev/null'
d=~/.claude/skills/raincli
if [ -e "$d/SKILL.md" ]; then echo "exists: ask the user before replacing $d/SKILL.md"
else mkdir -p "$d" && raincli --skill > "$d/SKILL.md"; fi
check "skill installed from the package" 'cmp -s ~/.claude/skills/raincli/SKILL.md "$ROOT/raincli/raincli_agent/skill/SKILL.md"'

echo "== SETUP 3: store credential"
API_URL="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["api_url"])' ~/Downloads/raincli-you-inbox.json)"  # https://raincli.com in production
raincli config init --api-url "$API_URL" --token-file ~/Downloads/raincli-you-inbox.json >/dev/null
rm ~/Downloads/raincli-you-inbox.json
check "config written 0600 by config init" '[[ "$(stat -c %a ~/.config/raincli/agent.json)" == 600 ]]'
check "whoami" 'raincli whoami | grep -q you-inbox'
check "agents lists teammate" 'raincli agents | grep -q mate-agent'

echo "== SETUP 4: dedicated inbox workspace from the template"
test -e ~/herdr/inbox-agent && echo "exists: ask the user before changing it" \
  || { mkdir -p ~/herdr && cp -r ~/src/raincli-repo/docs/templates/inbox-agent ~/herdr/inbox-agent && rm -f ~/herdr/inbox-agent/SOURCE.md; }
check "inbox workspace has role, state and adapters" \
  'for f in INBOX.md STATE.md AGENTS.md CLAUDE.md; do [[ -s ~/herdr/inbox-agent/$f ]] || exit 1; done; [[ ! -e ~/herdr/inbox-agent/SOURCE.md ]]'

echo "== SETUP 5: connector (inbox mode) with fake herdr"
mkdir -p ~/raincli-shareable ~/fakeherdr
cat > ~/fakeherdr/herdr <<'FAKE'
#!/usr/bin/env python3
import json, os, sys
d = os.path.expanduser("~/fakeherdr"); a = sys.argv[1:]
panes = {"raincli-inbox": "w5:p1", "main-agent": "w5:p0"}
if a[:2] == ["agent", "get"] and a[2] in panes:
    print(json.dumps({"result": {"agent": {"name": a[2], "agent_status": "idle", "pane_id": panes[a[2]], "cwd": d}}}))
elif a[:2] == ["agent", "prompt"]:
    open(os.path.join(d, a[2] + ".prompts"), "a").write(json.dumps(a[3]) + "\n"); print("{}")
elif a[:2] == ["notification", "show"]:
    open(os.path.join(d, "notifications"), "a").write(json.dumps(a[2:]) + "\n"); print("{}")
else:
    print(json.dumps({"error": {"code": "agent_not_found"}}), file=sys.stderr); sys.exit(1)
FAKE
chmod 700 ~/fakeherdr/herdr
cat > ~/.config/raincli/connector.json <<JSON
{
  "agent_config": "~/.config/raincli/agent.json",
  "mode": "inbox",
  "herdr_agent": "raincli-inbox",
  "expect_pane_id": "w5:p1",
  "shareable_context": ["$HOME/raincli-shareable"],
  "escalation": {"herdr_agent": "main-agent", "expect_pane_id": "w5:p0", "notify": true},
  "herdr_bin": "$HOME/fakeherdr/herdr"
}
JSON
chmod 600 ~/.config/raincli/connector.json
RAINCLI_CONFIG="$T/mate/agent.json" raincli send --to you-inbox --body "Hello from a teammate" >/dev/null
raincli connector run --config ~/.config/raincli/connector.json --once >/dev/null 2>&1
check "teammate message delivered to the named inbox agent without approval" 'grep -q "Hello from a teammate" ~/fakeherdr/raincli-inbox.prompts'
check "nothing sent to the main agent" '[[ ! -e ~/fakeherdr/main-agent.prompts ]]'
check "connector status works" 'raincli connector status --config ~/.config/raincli/connector.json >/dev/null'

echo "== SETUP 6: try it"

raincli send --to mate-agent --body "Hello from setup." --id "$(python3 -c 'import uuid; print(uuid.uuid4())')" >/dev/null
check "send + conversations" 'raincli conversations | grep -q mate-agent'
check "no token outside the 0600 config" '! grep -rqs "rca_" ~/.claude ~/raincli-shareable ~/src/raincli-repo/raincli/raincli_agent/skill'
(( FAIL == 0 )) && echo "SETUP-TEST PASS ($PASS checks)" || { echo "SETUP-TEST FAIL ($FAIL of $((PASS+FAIL)))"; exit 1; }
