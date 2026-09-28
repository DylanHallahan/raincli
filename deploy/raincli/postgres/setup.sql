-- Run as the postgres superuser on the host (safe to re-run):
--   sudo -u postgres psql -v ON_ERROR_STOP=1 -f setup.sql
--   sudo -u postgres psql -c '\password raincli'        # set the password interactively (not in argv/logs)
-- PostgreSQL must listen on localhost only (postgresql.conf: listen_addresses = 'localhost').
SELECT 'CREATE ROLE raincli LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'raincli') \gexec
SELECT 'CREATE DATABASE raincli OWNER raincli ENCODING ''UTF8'' TEMPLATE template0'
  WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'raincli') \gexec
REVOKE ALL ON DATABASE raincli FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE raincli TO raincli;
\connect raincli
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO raincli;
