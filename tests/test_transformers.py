"""Transformers-service: health + applyXSLT."""
from __future__ import annotations

from starlette.testclient import TestClient


def test_healthz_readyz(transformers_app):
    c = TestClient(transformers_app)
    assert c.get("/healthz").json() == {"status": "ok"}
    assert c.get("/readyz").json() == {"status": "ready"}


def test_apply_xslt_minimal(transformers_app):
    c = TestClient(transformers_app)
    xslt = """<?xml version="1.0" encoding="UTF-8"?>
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
  <xsl:output method="xml" encoding="UTF-8"/>
  <xsl:template match="/"><out><xsl:value-of select="name(*)"/></out></xsl:template>
</xsl:stylesheet>"""
    r = c.post(
        "/applyXSLT",
        json={"xml": "<root/>", "xslt": xslt},
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert "<out>" in r.text
    assert "root" in r.text
