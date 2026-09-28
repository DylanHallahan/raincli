"""Environment-only configuration (protocol §7). Secrets are never logged."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str = field(repr=False)
    public_url: str = "http://127.0.0.1:8000"
    root_path: str = ""
    cookie_secure: bool = True
    max_pending: int = 1000
    rate_limit_per_min: int = 120

    def __repr__(self) -> str:  # never show secrets or DB passwords
        return f"Settings(public_url={self.public_url!r}, root_path={self.root_path!r})"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    url = env.get("RAINCLI_DATABASE_URL", "")
    if not url.startswith("postgresql"):
        raise ConfigError("RAINCLI_DATABASE_URL must be a postgresql:// or postgresql+psycopg:// URL")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    secret = env.get("RAINCLI_SECRET_KEY", "")
    if len(secret.encode()) < 32:
        raise ConfigError("RAINCLI_SECRET_KEY must be set to at least 32 bytes")
    root = env.get("RAINCLI_ROOT_PATH", "").rstrip("/")
    if root and not root.startswith("/"):
        raise ConfigError("RAINCLI_ROOT_PATH must start with '/'")
    return Settings(
        database_url=url,
        secret_key=secret,
        public_url=env.get("RAINCLI_PUBLIC_URL", "http://127.0.0.1:8000").rstrip("/"),
        root_path=root,
        cookie_secure=env.get("RAINCLI_COOKIE_SECURE", "1") not in ("0", "false", "no"),
        max_pending=int(env.get("RAINCLI_MAX_PENDING", "1000")),
        rate_limit_per_min=int(env.get("RAINCLI_RATE_LIMIT_PER_MIN", "120")),
    )
