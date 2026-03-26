from __future__ import annotations

import pytest

from tests.conftest import load_workflow_runner_standalone


class _DummyResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload


class _DummyClient:
    def __init__(self):
        self.calls = []

    async def post(self, url: str, json: dict, headers: dict):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return _DummyResponse(
            200,
            {
                "status": "published",
                "exchange": json["exchange"],
                "routing_key": json.get("routing_key", ""),
            },
        )


@pytest.mark.asyncio
async def test_rabbitmq_publish_step_with_property_refs():
    wr = load_workflow_runner_standalone()
    doc = wr.WorkflowDoc.model_validate(
        {
            "name": "rabbit-publish",
            "steps": [
                {
                    "id": "set_domain",
                    "type": "context_set",
                    "context_key": "domain",
                    "value": "Sales",
                },
                {
                    "id": "pub",
                    "type": "rabbitmq_publish",
                    "rabbitmq": {
                        "url": "amqp://guest:guest@rabbitmq:5672/",
                        "exchange": "minicloud.events",
                        "exchange_type": "topic",
                        "message_from": "initial",
                        "property_refs": {
                            "Domain": "context:domain",
                        },
                        "properties": {
                            "Service": "Orders",
                            "Action": "Created",
                            "Version": "1",
                        },
                    },
                },
            ],
        },
    )

    client = _DummyClient()
    final, outputs, trace, _ctx = await wr.run_workflow(
        doc,
        "<order/>",
        transformers_base_url="http://unused",
        egress_http_url="http://unused",
        egress_ftp_url="http://unused",
        egress_ssh_url="http://unused",
        egress_sftp_url="http://unused",
        egress_rabbitmq_url="http://egress-rabbitmq:8080",
        request_id="t-rabbit",
        httpx_client=client,
    )

    assert "published" in final
    assert outputs["pub"] == final
    assert trace[-1]["type"] == "rabbitmq_publish"
    assert trace[-1]["ok"] is True
    call = client.calls[-1]
    assert call["url"].endswith("/publish")
    assert call["json"]["properties"]["Domain"] == "Sales"
    assert call["json"]["message"] == "<order/>"
