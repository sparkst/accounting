"""Wealth performance analytics package (REQ-PERF-001..021).

Modules:
- ``classify``: maps a ``BrokerageTransaction`` × analytic scope to a
  ``CashFlowType``. The portfolio-scope result is what gets persisted in
  ``BrokerageTransaction.cash_flow_type`` by the backfill (REQ-PERF-003).
- ``performance``: principal/growth series + TWR + MWR (REQ-PERF-005..009).
"""
