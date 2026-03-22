"""
MiniCloud transformers: XSLT 1.0, xml2json, json2xml, Liquid op één service.
"""
import json
import logging
import os
from typing import Annotated

import xmltodict
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse, Response
from liquid import Template
from lxml import etree
from pydantic import BaseModel, Field

LOG = logging.getLogger("transformers")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))

app = FastAPI(title="MiniCloud Transformers", version="0.1.0")


class XmlBody(BaseModel):
    xml: str = Field(..., min_length=1, description="XML-document als string")


class JsonTextBody(BaseModel):
    json: str = Field(
        ...,
        min_length=1,
        description='JSON als string (json2xml: root moet een object zijn)',
    )


class LiquidBody(BaseModel):
    template: str = Field(..., min_length=1, description="Liquid-sjabloon")
    json: str = Field(
        ...,
        min_length=1,
        description="JSON-object als string voor template-context",
    )


class ApplyBody(BaseModel):
    xml: str = Field(..., min_length=1)
    xslt: str = Field(..., min_length=1, description="XSLT 1.0 stylesheet")


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


@app.post("/applyXSLT", response_class=PlainTextResponse)
def apply_xslt(
    body: ApplyBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> PlainTextResponse:
    rid = x_request_id or "-"
    try:
        out = _apply_xslt(body.xml, body.xslt)
    except ValueError as e:
        LOG.warning("xslt failed request_id=%s: %s", rid, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    LOG.info("xslt apply ok request_id=%s", rid)
    return PlainTextResponse(content=out, media_type="application/xml; charset=utf-8")


@app.post("/xml2json")
def xml_to_json(
    body: XmlBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> Response:
    rid = x_request_id or "-"
    try:
        parsed = xmltodict.parse(body.xml)
        out = json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception as e:
        LOG.warning("xml2json failed request_id=%s: %s", rid, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    LOG.info("xml2json ok request_id=%s", rid)
    return Response(
        content=out.encode("utf-8"),
        media_type="application/json; charset=utf-8",
    )


@app.post("/json2xml")
def json_to_xml(
    body: JsonTextBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> Response:
    rid = x_request_id or "-"
    try:
        data = json.loads(body.json)
        if not isinstance(data, dict):
            raise ValueError("Root JSON moet een object zijn (geen array of primitief op topniveau)")
        xml = xmltodict.unparse(data, pretty=True, full_document=True)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ongeldige JSON: {e}") from e
    except Exception as e:
        LOG.warning("json2xml failed request_id=%s: %s", rid, e)
        raise HTTPException(status_code=400, detail=str(e)) from e
    LOG.info("json2xml ok request_id=%s", rid)
    return Response(
        content=xml.encode("utf-8"),
        media_type="application/xml; charset=utf-8",
    )


@app.post("/applyLiquid")
def liquid_render(
    body: LiquidBody,
    x_request_id: Annotated[str | None, Header(alias="X-Request-ID")] = None,
) -> PlainTextResponse:
    rid = x_request_id or "-"
    try:
        ctx = json.loads(body.json)
        if not isinstance(ctx, dict):
            raise ValueError("Context-JSON moet een object zijn op topniveau")
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Ongeldige context-JSON: {e}") from e
    tpl = Template(body.template)
    try:
        out = tpl.render(**ctx)
    except Exception as e:
        LOG.warning("liquid failed request_id=%s: %s", rid, e)
        raise HTTPException(status_code=400, detail=f"Liquid: {e}") from e
    LOG.info("liquid ok request_id=%s", rid)
    return PlainTextResponse(content=out, media_type="text/plain; charset=utf-8")
