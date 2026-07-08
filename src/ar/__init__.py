"""AR chaser — draft-for-approval reminder ladder (REQ-ARC-001..003).

Deterministic 14/30/45-day reminder ladder over unpaid SENT/OVERDUE invoices.
Every rung produces a *draft* that only sends after an explicit human approval
(Telegram callback or CLI). No LLM: draft bodies are template-rendered.
"""
