#!/usr/bin/env bash
# RainCLI local end-to-end demo: FastAPI + PostgreSQL, two agent CLIs, Markdown attachments,
# Herdr connector (against a FAKE herdr executable -- never real panes), revocation, and a
# PostgreSQL restart. Uses its own throwaway PostgreSQL container; never touches production.
#
#   scripts/raincli-demo.sh [DEMO_DIR]      (default: ./raincli-demo-run, recreated)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RC="$ROOT/raincli"
PY="$RC/.venv/bin/python"
[[ -x "$PY" ]] || { echo "run: cd raincli && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker is required for the demo database" >&2; exit 1; }

DEMO="${1:-$ROOT/raincli-demo-run}"
mkdir -p "$DEMO"; DEMO="$(cd "$DEMO" && pwd)"
if [[ -n "$(ls -A "$DEMO")" && ! -e "$DEMO/.raincli-demo" ]]; then
  echo "refusing to clean $DEMO: not a previous demo directory" >&2; exit 1
fi
find "$DEMO" -mindepth 1 -delete; touch "$DEMO/.raincli-demo"
umask 077
mkdir -p "$DEMO/creds" "$DEMO/fake-herdr" "$DEMO/work"

PG=raincli-demo-pg
PG_PORT=55434
SERVER_PID=""
PASS=0; FAIL=0; RESULTS=()
cleanup() {
  [[ -n "$SERVER_PID" ]] && kill "$SERVER_PID" 2>/dev/null && wait "$SERVER_PID" 2>/dev/null
  docker rm -f "$PG" >/dev/null 2>&1
}
trap cleanup EXIT
check() { if eval "$2"; then PASS=$((PASS+1)); RESULTS+=("PASS  $1"); echo "   PASS: $1"; else FAIL=$((FAIL+1)); RESULTS+=("FAIL  $1"); echo "   FAIL: $1"; fi; }
section() { echo; echo "== $*"; }
rc() { local who=$1; shift; RAINCLI_CONFIG="$DEMO/creds/$who.json" "$RC/.venv/bin/raincli" "$@"; }

section "Throwaway PostgreSQL 17 on 127.0.0.1:$PG_PORT"
PGPW="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(18))')"
docker rm -f "$PG" >/dev/null 2>&1
docker run -d --name "$PG" -e POSTGRES_USER=raincli -e POSTGRES_PASSWORD="$PGPW" -e POSTGRES_DB=raincli \
  -p "127.0.0.1:$PG_PORT:5432" postgres:17-alpine >/dev/null || exit 1
for _ in $(seq 60); do docker exec "$PG" pg_isready -U raincli -d raincli -q 2>/dev/null && break; sleep 1; done
sleep 1
PORT="$("$PY" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])')"
export RAINCLI_DATABASE_URL="postgresql+psycopg://raincli:$PGPW@127.0.0.1:$PG_PORT/raincli"
export RAINCLI_SECRET_KEY="$("$PY" -c 'import secrets; print(secrets.token_urlsafe(48))')"
export RAINCLI_PUBLIC_URL="http://127.0.0.1:$PORT" RAINCLI_COOKIE_SECURE=0 RAINCLI_MAX_PENDING=100
unset RAINCLI_CONFIG
cd "$RC" || exit 1
"$PY" -m raincli_server.migrate upgrade head 2>/dev/null
check "migrations applied" '"$PY" -m raincli_server.migrate current 2>/dev/null | grep -q head'

section "Start the service on loopback ($RAINCLI_PUBLIC_URL)"
"$RC/.venv/bin/uvicorn" raincli_server.app:app_from_env --factory --host 127.0.0.1 --port "$PORT" \
  --no-access-log > "$DEMO/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 100); do curl -fs "$RAINCLI_PUBLIC_URL/api/v1/health" >/dev/null 2>&1 && break; sleep 0.1; done
check "health endpoint ok with no private data" \
  '[[ "$(curl -fs "$RAINCLI_PUBLIC_URL/api/v1/health")" == "{\"ok\":true,\"db\":\"ok\"}" ]]'

section "Operator enrolment (invite-only; passwords via stdin, tokens only in 0600 files)"
printf 'demo password 123\n' | "$RC/.venv/bin/raincli-admin" create-user --email alice@demo.test --name Alice >/dev/null
printf 'demo password 456\n' | "$RC/.venv/bin/raincli-admin" create-user --email bob@demo.test --name Bob >/dev/null
"$RC/.venv/bin/raincli-admin" create-team --slug demo --name "Demo team" --owner alice@demo.test >/dev/null
"$RC/.venv/bin/raincli-admin" add-member --team demo --email bob@demo.test >/dev/null 2>&1 \
  || "$RC/.venv/bin/raincli-admin" add-member --help | head -3
"$RC/.venv/bin/raincli-admin" register-agent --team demo --owner alice@demo.test --handle alice-agent --out "$DEMO/creds/alice.json" >/dev/null
"$RC/.venv/bin/raincli-admin" register-agent --team demo --owner bob@demo.test --handle bob-inbox --out "$DEMO/creds/bob.json" >/dev/null
check "credential files are 0600" '[[ "$(stat -c %a "$DEMO/creds/alice.json")" == 600 ]]'
check "alice whoami" 'rc alice whoami | grep -q alice-agent'

section "Alice sends a report with Markdown attachments (and retries the same id)"
printf '# Q3 report\r\n\r\n- shipped ✅\n- trailing   \n' > "$DEMO/work/Q3 report.md"
printf 'Please review the attached report.\n' > "$DEMO/work/body.md"
MID="$("$PY" -c 'import uuid; print(uuid.uuid4())')"
rc alice send --to bob-inbox --body-file "$DEMO/work/body.md" --attach "$DEMO/work/Q3 report.md" --id "$MID" >/dev/null
rc alice send --to bob-inbox --body-file "$DEMO/work/body.md" --attach "$DEMO/work/Q3 report.md" --id "$MID" >/dev/null
check "retry did not duplicate" '[[ "$(rc bob inbox --json | grep -c "\"id\"")" == 1 ]]'
rc bob inbox

section "Bob's connector (fake Herdr target 'bob-session'; direct mode, approval required)"
cat > "$DEMO/fake-herdr/herdr" <<'FAKE'
#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_HERDR_DIR"]
a = sys.argv[1:]
def log(kind, **kw):
    with open(os.path.join(d, kind + ".jsonl"), "a") as f: f.write(json.dumps(kw) + "\n")
PANES = {"bob-session": "w9:p1", "main-session": "w9:p2"}
if a[:2] == ["agent", "get"]:
    if a[2] not in PANES:
        print(json.dumps({"error": {"code": "agent_not_found", "message": "no such agent"}}), file=sys.stderr); sys.exit(1)
    sf = os.path.join(d, "status" if a[2] == "bob-session" else "status-" + a[2])
    status = open(sf).read().strip() if os.path.exists(sf) else "idle"
    print(json.dumps({"result": {"agent": {"name": a[2], "agent_status": status, "pane_id": PANES[a[2]], "cwd": d}}}))
elif a[:2] == ["agent", "prompt"]:
    log("prompts" if a[2] == "bob-session" else "prompts-" + a[2], name=a[2], text=a[3])
    print(json.dumps({"result": {"ok": True}}))
elif a[:2] == ["notification", "show"]:
    log("notifications", args=a[2:]); print(json.dumps({"result": {"shown": True}}))
else:
    sys.exit(2)
FAKE
chmod 700 "$DEMO/fake-herdr/herdr"
export FAKE_HERDR_DIR="$DEMO/fake-herdr"
cat > "$DEMO/creds/connector.json" <<EOF
{"agent_config": "$DEMO/creds/bob.json", "herdr_agent": "bob-session", "expect_pane_id": "w9:p1",
 "state_dir": "$DEMO/connector-state", "herdr_bin": "$DEMO/fake-herdr/herdr", "poll_wait": 1}
EOF
chmod 600 "$DEMO/creds/connector.json"
echo busy > "$DEMO/fake-herdr/status"
rc bob connector run --config "$DEMO/creds/connector.json" --once >/dev/null 2>&1
check "attachment stored locally before ack" \
  'cmp -s "$DEMO/work/Q3 report.md" "$(find "$DEMO/connector-state" -name "Q3 report.md" | head -1)"'
check "message acked (received) on the server" '[[ "$(rc alice show "$MID" --json | "$PY" -c "import json,sys; print(bool(json.load(sys.stdin)[\"message\"][\"acked_at\"]))")" == True ]]'
check "untrusted sender held for approval (nothing submitted)" '[[ ! -s "$DEMO/fake-herdr/prompts.jsonl" ]]'
rc bob connector approve --config "$DEMO/creds/connector.json" "$MID" >/dev/null
rc bob connector run --config "$DEMO/creds/connector.json" --once >/dev/null 2>&1
check "busy session: still held" '[[ ! -s "$DEMO/fake-herdr/prompts.jsonl" ]]'
echo idle > "$DEMO/fake-herdr/status"
rc bob connector run --config "$DEMO/creds/connector.json" --once >/dev/null 2>&1
check "idle session: submitted exactly once" '[[ "$(wc -l < "$DEMO/fake-herdr/prompts.jsonl")" == 1 ]]'
check "prompt references attachment path, not its content" \
  'grep -q "Q3 report.md" "$DEMO/fake-herdr/prompts.jsonl" && ! grep -q "shipped" "$DEMO/fake-herdr/prompts.jsonl"'
rc bob connector run --config "$DEMO/creds/connector.json" --once >/dev/null 2>&1
check "rerun does not resubmit" '[[ "$(wc -l < "$DEMO/fake-herdr/prompts.jsonl")" == 1 ]]'
check "sender sees submitted" 'rc alice show "$MID" --json | grep -q "\"submitted\""'
rc bob connector status --config "$DEMO/creds/connector.json"

section "Bob fetches the attachment safely and replies"
rc bob fetch "$MID" --dir "$DEMO/work/bob-inbox" >/dev/null
check "fetched bytes identical" 'cmp -s "$DEMO/work/Q3 report.md" "$DEMO/work/bob-inbox/Q3 report.md"'
echo "local edit" > "$DEMO/work/bob-inbox/Q3 report.md"
rc bob fetch "$MID" --dir "$DEMO/work/bob-inbox" >/dev/null 2>&1; FETCH_RC=$?
check "fetch never overwrites a different local file (exit 3)" '[[ $FETCH_RC == 3 && "$(cat "$DEMO/work/bob-inbox/Q3 report.md")" == "local edit" ]]'
rc bob reply "$MID" --body "Reviewed; two comments inline." >/dev/null
check "alice sees the reply and state replied" \
  'rc alice inbox --json | grep -q "\"in_reply_to\": \"$MID\"" && rc alice show "$MID" --json | grep -q "\"replied\""'

section "Inbox mode: enrolled-team delivery without approval, escalation to the main session"
mkdir -p "$DEMO/work/shareable"; printf '# Release notes (team-shareable)\n' > "$DEMO/work/shareable/release.md"
cat > "$DEMO/creds/inbox.json" <<JSON
{"agent_config": "$DEMO/creds/bob.json", "mode": "inbox", "herdr_agent": "bob-session", "expect_pane_id": "w9:p1",
 "shareable_context": ["$DEMO/work/shareable"], "state_dir": "$DEMO/connector-state",
 "escalation": {"herdr_agent": "main-session", "expect_pane_id": "w9:p2", "notify": true},
 "herdr_bin": "$DEMO/fake-herdr/herdr", "poll_wait": 1}
JSON
chmod 600 "$DEMO/creds/inbox.json"
MID3="$("$PY" -c 'import uuid; print(uuid.uuid4())')"
rc alice send --to bob-inbox --body "When is the Q4 deploy window?" --id "$MID3" >/dev/null
rc bob connector run --config "$DEMO/creds/inbox.json" --once >/dev/null 2>&1
check "teammate message auto-delivered to inbox agent (no approval)" \
  '[[ "$(grep -c "$MID3" "$DEMO/fake-herdr/prompts.jsonl")" == 1 ]]'
check "inbox prompt names shareable context and escalate command" \
  'grep "$MID3" "$DEMO/fake-herdr/prompts.jsonl" | grep -q "shareable" && grep "$MID3" "$DEMO/fake-herdr/prompts.jsonl" | grep -q "connector escalate"'
echo working > "$DEMO/fake-herdr/status-main-session"
ESC_BODY="Q: Q4 deploy window. Checked release.md in shareable context; no date recorded. Needs your decision."
rc bob connector escalate --config "$DEMO/creds/inbox.json" "$MID3" --body "$ESC_BODY" >/dev/null
rc bob connector escalate --config "$DEMO/creds/inbox.json" "$MID3" --body "$ESC_BODY" >/dev/null
rc bob connector run --config "$DEMO/creds/inbox.json" --once >/dev/null 2>&1
check "escalation raises one visible notification while main is busy" '[[ "$(wc -l < "$DEMO/fake-herdr/notifications.jsonl")" == 1 ]]'
check "busy main session: escalation pending, not submitted" '[[ ! -s "$DEMO/fake-herdr/prompts-main-session.jsonl" ]]'
echo idle > "$DEMO/fake-herdr/status-main-session"
rc bob connector run --config "$DEMO/creds/inbox.json" --once >/dev/null 2>&1
rc bob connector run --config "$DEMO/creds/inbox.json" --once >/dev/null 2>&1
check "escalation submitted to main exactly once (retried escalate deduplicated)" \
  '[[ "$(wc -l < "$DEMO/fake-herdr/prompts-main-session.jsonl")" == 1 ]]'
check "status says submitted, not confirmed seen" \
  'rc bob connector status --config "$DEMO/creds/inbox.json" | grep -qi "not confirmed seen"'

section "PostgreSQL restart: data persists, clients recover"
docker restart -t 5 "$PG" >/dev/null
MID2="$("$PY" -c 'import uuid; print(uuid.uuid4())')"
rc alice send --to bob-inbox --body "after restart" --id "$MID2" >/dev/null
check "send after restart succeeds (client retries)" 'rc bob show "$MID2" --json | grep -q "after restart"'
check "earlier message still present" 'rc bob show "$MID" --json | grep -q "$MID"'

section "Rotation and revocation"
"$RC/.venv/bin/raincli-admin" rotate-agent --team demo --handle alice-agent --out "$DEMO/creds/alice-new.json" >/dev/null
rc alice whoami >/dev/null 2>&1; OLD_RC=$?
check "rotated-out token rejected" '[[ $OLD_RC != 0 ]]'
check "new token works" 'RAINCLI_CONFIG="$DEMO/creds/alice-new.json" "$RC/.venv/bin/raincli" whoami | grep -q alice-agent'
"$RC/.venv/bin/raincli-admin" revoke-agent --team demo --handle alice-agent >/dev/null
RAINCLI_CONFIG="$DEMO/creds/alice-new.json" "$RC/.venv/bin/raincli" whoami >/dev/null 2>&1; REV_RC=$?
check "revoked agent rejected" '[[ $REV_RC != 0 ]]'
check "no token in server log" '! grep -q "rca_" "$DEMO/server.log"'

section "Summary"
printf '   %s\n' "${RESULTS[@]}"
if (( FAIL == 0 )); then echo "PASS: all $PASS checks passed"; else echo "FAIL: $FAIL of $((PASS+FAIL)) checks failed"; exit 1; fi
