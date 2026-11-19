"""Connectors to applicant-tracking systems' public job-board APIs.

One module per vendor. Each walks the boards listed for it in
``company_boards.yaml`` once per run and filters locally; the shared machinery
lives in ``_common.py``, whose leading underscore keeps the registry from
treating it as a connector. Nothing is imported here on purpose: the registry
discovers the modules itself (see ``app.sources.registry.load_sources``).
"""
