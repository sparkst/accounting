"""Controlled outbound-contact constants.

REQ-ID: REQ-FIX-API-004  The legacy ``.com`` sibling domain is not one we
control; every outbound sender/contact address must use the controlled
``sparkry.ai`` domain via this single constant so it can never drift
file-by-file again.
"""

from __future__ import annotations

SPARKRY_CONTACT_EMAIL = "travis@sparkry.ai"
INVOICE_FROM_ADDRESS = f"Sparkry LLC <{SPARKRY_CONTACT_EMAIL}>"
