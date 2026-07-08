"""Vision statement ingestion — SHADOW MODE (REQ-VIS-001..004).

A read-only shadow pipeline that runs an LLM vision extractor alongside the
legacy per-institution adapters, produces a field-level diff, and tracks a
per-institution promotion ledger — WITHOUT ever writing to the register,
brokerage, or history tables. Gemini is the default provider; OpenAI is the
fallback (env ``VISION_PROVIDER``). Every extraction logs an ``LLMUsageLog``
row and the harness enforces a hard per-run cost ceiling.
"""
