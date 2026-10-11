"""Unit tests for RFC 7807 Problem Detail error handling."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from xiosync.api.middleware.problem_detail import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    class Payload(BaseModel):
        name: str
        age: int

    @app.post("/test/validation")
    def post_val(body: Payload) -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/test/http-error")
    def get_http_error() -> None:
        raise HTTPException(status_code=404, detail="Resource not found")

    @app.get("/test/unhandled")
    def get_unhandled() -> None:
        raise RuntimeError("Something blew up unexpectedly")

    return app


def test_http_exception_returns_rfc7807(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/test/http-error")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    data = resp.json()
    assert data["type"] == "https://xiosync.dev/problems/http_404"
    assert data["title"] == "Resource not found"
    assert data["status"] == 404
    assert "/test/http-error" in data["instance"]


def test_validation_error_returns_rfc7807(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/test/validation", json={"name": "Alice", "age": "not-an-int"})
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    data = resp.json()
    assert data["type"] == "https://xiosync.dev/problems/validation_error"
    assert data["title"] == "Validation Error"
    assert data["status"] == 422
    assert "errors" in data
    assert len(data["errors"]) > 0


def test_unhandled_exception_returns_rfc7807(app: FastAPI) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/test/unhandled")
    assert resp.status_code == 500
    assert resp.headers["content-type"] == "application/problem+json"
    data = resp.json()
    assert data["type"] == "https://xiosync.dev/problems/internal_error"
    assert data["title"] == "Internal Server Error"
    assert data["status"] == 500
    assert data["detail"] == "An unexpected error occurred. Please try again later."
    assert "/test/unhandled" in data["instance"]
