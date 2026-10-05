"""RainCLI-app.exe, the tray app inside each version folder (protocol §15.5, §15.8 H4, §15.9 self-check)."""
import sys

from raincli_agent.app.tray import main

sys.exit(main())
