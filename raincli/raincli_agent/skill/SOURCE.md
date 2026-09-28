# Packaged skill

`raincli --skill` prints `SKILL.md` (Herdr-style `--skill` discovery). `agents/openai.yaml` is optional
metadata for agents that read it. Every `raincli` example in `SKILL.md` is checked against the real
CLI parser by `tests/agent/test_skill_examples.py`.
