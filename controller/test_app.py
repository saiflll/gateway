from classifier import is_paid_model_failure


def test_paid_model_classifier_requires_402_and_signature():
    assert is_paid_model_failure(402, "Paid Model - Credits Required")
    assert is_paid_model_failure(402, {"error_type": "billing_error"})
    assert not is_paid_model_failure(402, "unrelated upstream failure")
    assert not is_paid_model_failure(429, "credits required")


def test_usage_limit_requires_payment_context():
    assert is_paid_model_failure(402, {"error_type": "usage_limit_exceeded", "message": "add credits"})
    assert not is_paid_model_failure(402, {"error_type": "usage_limit_exceeded"})
