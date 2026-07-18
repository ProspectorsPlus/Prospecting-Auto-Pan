"""Prospector Engine -- the one shared runtime both editions run.

Phase 04 of the Prospector Integration campaign extracts the engine here
incrementally. At checkpoint C1 this package holds only the protocol
library (nothing imports it); the runtime itself moves in at C6.

The protocol is PPE1, defined normatively in the Studio repo:
docs/prospector-integration/reports/phase-02-protocol.md
"""

ENGINE_VERSION = "0.4.0"
