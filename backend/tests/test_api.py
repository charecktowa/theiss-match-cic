from fastapi.testclient import TestClient

from app.main import app


def test_hello_returns_the_greeting() -> None:
    response = TestClient(app).get("/")

    assert response.status_code == 200
    assert response.json() == {"message": "Hello World"}
