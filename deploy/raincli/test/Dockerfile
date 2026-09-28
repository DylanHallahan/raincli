FROM postgres:17-bookworm
COPY --from=python:3.11-slim-bookworm /usr/local /usr/local
RUN ldconfig && python3 --version && python3 -m venv --help >/dev/null
