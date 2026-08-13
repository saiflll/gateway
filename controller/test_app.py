import asyncio

import httpx

import app as controller
from classifier import is_paid_model_failure


def test_paid_model_classifier_requires_402_and_signature():
    assert is_paid_model_failure(402, "Paid Model - Credits Required")
    assert is_paid_model_failure(402, {"error_type": "billing_error"})
    assert not is_paid_model_failure(402, "unrelated upstream failure")
    assert not is_paid_model_failure(429, "credits required")


def test_usage_limit_requires_payment_context():
    assert is_paid_model_failure(402, {"error_type": "usage_limit_exceeded", "message": "add credits"})
    assert not is_paid_model_failure(402, {"error_type": "usage_limit_exceeded"})


def test_management_request_logs_in_and_retries(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/auth/login":
            return httpx.Response(200, headers={"set-cookie": "auth_token=test; Path=/"})
        if len(requests) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(controller, "ROUTER_ADMIN_PASSWORD", "dashboard-secret")
    transport = httpx.MockTransport(handler)

    async def call():
        async with httpx.AsyncClient(transport=transport) as client:
            return await controller.router_management_request(
                client, "POST", "/api/models/disabled", json={"ids": ["model"]}
            )

    response = asyncio.run(call())
    assert response.status_code == 200
    assert [request.url.path for request in requests] == [
        "/api/models/disabled",
        "/api/auth/login",
        "/api/models/disabled",
    ]
    assert requests[-1].headers["cookie"] == "auth_token=test"
