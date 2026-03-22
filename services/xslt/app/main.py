import logging
import os
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse
from lxml import etree
from pydantic import BaseModel, Field

LOG = logging.getLogger("xslt")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="MiniCloud XSLT", version="0.1.0")


class ApplyBody(BaseModel):
    xml: str = Field(..., min_length=1, description="XML document as string")
    xslt: str = Field(..., min_length=1, description="XSLT 1.0 stylesheet as string")


def _apply_xslt(xml_s: str, xslt_s: str) -> str:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    try:
        xml_doc = etree.fromstring(xml_s.encode("utf-8"), parser)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"Invalid XML: {e}") from e
    try:
        xslt_doc = etree.fromstring(xslt_s.encode("utf-8"), parser)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"Invalid XSLT (XML): {e}") from e
    try:
        transform = etree.XSLT(xslt_doc)
        result = transform(xml_doc)
    except etree.XSLTParseError as e:
        raise ValueError(f"XSLT parse error: {e}") from e
    except etree.XSLTApplyError as e:
        raise ValueError(f"XSLT apply error: {e}") from e
    return str(result)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
def readyz() -> dict:
    return {"status": "ready"}


@app.post("/apply", response_class=PlainTextResponse)
def apply_transform(
    body: ApplyBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> PlainTextResponse:
    rid = x_request_id or "-"
    try:
        out = _apply_xslt(body.xml, body.xslt)
    except ValueError as e:
        LOG.warning("transform failed request_id=%s: %s", rid, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    LOG.info("transform ok request_id=%s", rid)
    return PlainTextResponse(content=out, media_type="application/xml; charset=utf-8")
