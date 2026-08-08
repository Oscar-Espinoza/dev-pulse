"""User registration endpoint."""


async def test_create_user_returns_api_key(client) -> None:
    response = await client.post("/users", json={"email": "new@example.com"})
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "new@example.com"
    assert len(body["api_key"]) > 20
    assert "id" in body and "created_at" in body


async def test_duplicate_email_conflicts(client) -> None:
    first = await client.post("/users", json={"email": "dup@example.com"})
    assert first.status_code == 201
    second = await client.post("/users", json={"email": "dup@example.com"})
    assert second.status_code == 409


async def test_invalid_email_rejected(client) -> None:
    response = await client.post("/users", json={"email": "not-an-email"})
    assert response.status_code == 422
