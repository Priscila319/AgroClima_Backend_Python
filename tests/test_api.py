from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_health_reports_java_connection_without_exposing_secrets():
    settings = Settings(
        java_api_url="http://java.test",
        internal_api_key="this-is-a-long-internal-key-for-tests-123",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["javaApiConfigured"] is True
    assert body["internalKeyConfigured"] is True
    assert "thingspeakConfigured" not in body
    assert "this-is-a-long-internal-key-for-tests-123" not in response.text


def test_swagger_ui_assets_are_served_from_the_local_api_without_cdn():
    with TestClient(create_app(Settings())) as client:
        docs = client.get("/docs")
        javascript = client.get("/static/swagger-ui/swagger-ui-bundle.js")
        stylesheet = client.get("/static/swagger-ui/swagger-ui.css")
        favicon = client.get("/static/swagger-ui/favicon-32x32.png")

    assert docs.status_code == 200
    assert "/static/swagger-ui/swagger-ui-bundle.js" in docs.text
    assert "/static/swagger-ui/swagger-ui.css" in docs.text
    assert "cdn.jsdelivr.net" not in docs.text
    assert "fastapi.tiangolo.com" not in docs.text
    assert javascript.status_code == 200 and "javascript" in javascript.headers["content-type"]
    assert stylesheet.status_code == 200 and "text/css" in stylesheet.headers["content-type"]
    assert favicon.status_code == 200


def test_dashboard_requires_bearer_auth_and_uses_consistent_error_shape():
    with TestClient(create_app(Settings())) as client:
        response = client.get("/api/v1/dashboard/summary")

    assert response.status_code == 401
    payload = response.json()
    assert payload["status"] == 401
    assert payload["error"] == "Unauthorized"
    assert payload["message"] == "Autenticação Bearer obrigatória."
    assert payload["path"] == "/api/v1/dashboard/summary"


def test_cors_defaults_allow_local_development_origins_without_wildcard():
    with TestClient(create_app(Settings())) as client:
        response = client.options(
            "/api/v1/dashboard/summary",
            headers={
                "Origin": "http://127.0.0.1:5500",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5500"
    assert response.headers["access-control-allow-origin"] != "*"


def test_cors_uses_explicit_configured_origin_not_wildcard():
    settings = Settings(cors_allowed_origins="https://frontend.example.test")
    with TestClient(create_app(settings)) as client:
        response = client.options(
            "/api/v1/dashboard/summary",
            headers={
                "Origin": "https://frontend.example.test",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Authorization",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://frontend.example.test"
    assert response.headers["access-control-allow-origin"] != "*"
