import json
from typing import Any

PAID_SIGNATURES = (
    "paid model",
    "credits required",
    "add credits",
    "payment required",
    "insufficient credits",
    "billing_error",
)


def is_paid_model_failure(status: int, error: Any) -> bool:
    """Require both payment status and semantic evidence; never classify every 402."""
    if status != 402:
        return False
    text = json.dumps(error, ensure_ascii=False).lower() if not isinstance(error, str) else error.lower()
    if any(signature in text for signature in PAID_SIGNATURES):
        return True
    return "usage_limit_exceeded" in text and any(word in text for word in ("paid", "credit", "billing", "payment"))

