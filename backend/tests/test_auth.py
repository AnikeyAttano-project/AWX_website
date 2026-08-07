"""Тесты авторизации: регистрация, login, logout (отзыв токена №31), /me."""


def _register(client, email="user@vpn.local", password="strongpass123"):
    r = client.post("/api/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def test_register_returns_token_and_verified(client):
    """Email-верификация отключена (№32) — юзер сразу verified, токен рабочий."""
    data = _register(client)
    assert data["verified"] is True
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {data['token']}"})
    assert r.status_code == 200
    assert r.json()["email"] == "user@vpn.local"


def test_me_without_token_is_401(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 401


def test_logout_revokes_token(client):
    """После logout старый токен не работает; login выдаёт свежий (№31)."""
    data = _register(client)
    token = data["token"]
    auth = {"Authorization": f"Bearer {token}"}

    # Токен рабочий до logout.
    assert client.get("/api/auth/me", headers=auth).status_code == 200

    r = client.post("/api/auth/logout", headers=auth)
    assert r.status_code == 200

    # Тот же токен теперь отозван.
    r = client.get("/api/auth/me", headers=auth)
    assert r.status_code == 401

    # Повторный login даёт свежий токен (новый token_version).
    r = client.post("/api/auth/login", json={"email": "user@vpn.local", "password": "strongpass123"})
    assert r.status_code == 200
    new_token = r.json()["token"]
    assert new_token != token
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {new_token}"}).status_code == 200


def test_logout_requires_auth(client):
    r = client.post("/api/auth/logout")
    assert r.status_code == 401
