import logging
import os
import importlib
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

LOG = logging.getLogger("egress.rabbitmq")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

DEFAULT_RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672/")
DEFAULT_EXCHANGE = os.environ.get("RABBITMQ_EXCHANGE", "minicloud.events")
DEFAULT_EXCHANGE_TYPE = os.environ.get("RABBITMQ_EXCHANGE_TYPE", "topic")
DEFAULT_ROUTING_KEY = os.environ.get("RABBITMQ_ROUTING_KEY", "")

app = FastAPI(title="MiniCloud egress RabbitMQ", version="0.1.0")


class PublishBody(BaseModel):
    message: str = Field(..., min_length=1)
    url: str = Field(default=DEFAULT_RABBITMQ_URL, min_length=1)
    exchange: str = Field(default=DEFAULT_EXCHANGE, min_length=1)
    exchange_type: Literal["topic", "direct", "fanout", "headers"] = Field(
        default=DEFAULT_EXCHANGE_TYPE,
    )
    routing_key: str = Field(default=DEFAULT_ROUTING_KEY)
    properties: dict[str, str] = Field(
        default_factory=dict,
        description="Message properties used by subscribers, e.g. Domain/Service/Action/Version",
    )
    headers: dict[str, str] = Field(default_factory=dict)
    message_id: str | None = None
    correlation_id: str | None = None
    content_type: str = "text/plain"
    persistent: bool = True


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    return {"status": "ready"}


@app.post("/publish")
async def publish(body: PublishBody) -> dict[str, Any]:
    aio_pika = importlib.import_module("aio_pika")
    DeliveryMode = aio_pika.DeliveryMode
    ExchangeType = aio_pika.ExchangeType
    Message = aio_pika.Message
    connect_robust = aio_pika.connect_robust

    headers: dict[str, str] = dict(body.headers)
    headers.update(body.properties)

    routing_key = body.routing_key
    if not routing_key:
        d = body.properties.get("Domain", "")
        s = body.properties.get("Service", "")
        a = body.properties.get("Action", "")
        v = body.properties.get("Version", "")
        if d and s and a and v:
            routing_key = f"{d}.{s}.{a}.{v}".lower()

    try:
        connection = await connect_robust(body.url)
        async with connection:
            channel = await connection.channel(publisher_confirms=True)
            exchange = await channel.declare_exchange(
                body.exchange,
                ExchangeType(body.exchange_type),
                durable=True,
            )
            msg = Message(
                body.message.encode("utf-8"),
                delivery_mode=DeliveryMode.PERSISTENT if body.persistent else DeliveryMode.NOT_PERSISTENT,
                content_type=body.content_type,
                headers=headers,
                message_id=body.message_id,
                correlation_id=body.correlation_id,
                timestamp=datetime.now(tz=timezone.utc),
            )
            await exchange.publish(msg, routing_key=routing_key)
    except Exception as e:
        LOG.error("rabbitmq publish failed: %s", e)
        raise HTTPException(status_code=502, detail=f"RabbitMQ publish failed: {e}") from e

    return {
        "status": "published",
        "exchange": body.exchange,
        "exchange_type": body.exchange_type,
        "routing_key": routing_key,
        "properties": body.properties,
        "message_id": body.message_id,
        "correlation_id": body.correlation_id,
    }
