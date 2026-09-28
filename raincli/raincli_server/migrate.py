"""Programmatic migrations: ``python -m raincli_server.migrate [upgrade head|downgrade -1|current]``."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic import command
from alembic.config import Config

INI = Path(__file__).with_name("alembic.ini")


def alembic_config(url: str | None = None) -> Config:
    cfg = Config(str(INI))
    if url:
        cfg.attributes["url"] = url
    return cfg


def upgrade(url: str | None = None, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv) or ["upgrade", "head"]
    cfg = alembic_config()
    action = argv[0]
    if action == "upgrade":
        command.upgrade(cfg, argv[1] if len(argv) > 1 else "head")
    elif action == "downgrade":
        command.downgrade(cfg, argv[1] if len(argv) > 1 else "-1")
    elif action == "current":
        command.current(cfg)
    else:
        print("usage: python -m raincli_server.migrate [upgrade REV|downgrade REV|current]", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
