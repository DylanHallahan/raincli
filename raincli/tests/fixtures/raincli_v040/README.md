# raincli v0.4.0 queue loader (test fixture)

The parts of `raincli/raincli_agent` at tag `v0.4.0` (commit `a214082792ff64731cae1d6f12fc5cc900ae8b11`) that
`tests/agent/test_named_delivery.py::test_a_v04_connector_delivers_none_of_the_agent_messages`
loads: the connector's config, Herdr fake, queue and runner, with what they import
(`_winfiles` and `runtime/procinfo` are imported lazily on Windows and for hook sessions).
Each file is the tagged file byte for byte after its one-line provenance header; the test
checks that against `git show v0.4.0:<path>` whenever the tag is available.
