"""RainCLI-app.exe, the tray app inside each version folder (protocol §15.5, §15.8 H4)."""
import sys

from raincli_agent.app import main

sys.exit(main())
