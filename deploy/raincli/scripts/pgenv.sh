# Source me: exports libpq variables (PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE) parsed from a
# RainCLI database URL, so passwords never appear in process arguments.   . pgenv.sh "$URL"
# The URL reaches python3 through the environment (readable only by this user), never argv.
_raincli_pgenv="$(RAINCLI_PGENV_URL="$1" python3 - <<'PY'
import os, shlex, sys
from urllib.parse import unquote, urlsplit
u = urlsplit(os.environ["RAINCLI_PGENV_URL"].replace("postgresql+psycopg://", "postgresql://", 1))
if u.scheme != "postgresql" or not u.path.strip("/"):
    sys.exit("database URL must be postgresql://USER:PASSWORD@HOST:PORT/DBNAME")
env = {"PGHOST": u.hostname or "", "PGPORT": str(u.port or 5432), "PGUSER": unquote(u.username or ""),
       "PGPASSWORD": unquote(u.password or ""), "PGDATABASE": unquote(u.path.lstrip("/"))}
print(" ".join(f"export {k}={shlex.quote(v)}" for k, v in env.items()))
PY
)" || return 1
eval "$_raincli_pgenv"; unset _raincli_pgenv
