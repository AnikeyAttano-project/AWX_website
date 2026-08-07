"""
Часть 6: кастомизация витрины из админки.

Брендинг (логотип base64 + hero/footer/meta) и контент тарифов
(description/features/badge) доезжают до /api/tariffs, валидация лого работает.
"""
import base64
import json

ADMIN_HEADERS = {"X-Admin-Key": "test-admin-key-0123456789abcdefghijklmnopqrstuvwxyz"}

PNG_LOGO = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nfakepng").decode()


def _save_branding(client, body):
    return client.post("/admin/settings/branding", json=body, headers=ADMIN_HEADERS)


def _save_tariff(client, tariff):
    return client.post(
        "/admin/settings/tariffs",
        json={"tariffs": {"quantum_month": tariff}},
        headers=ADMIN_HEADERS,
    )


def test_branding_saves_logo_and_texts(client):
    body = {
        "site_name": "MyVPN",
        "accent_color": "#123456",
        "support_contact": "hi@my.com",
        "logo_data_url": PNG_LOGO,
        "hero_title": "Заголовок",
        "hero_subtitle": "Подзаголовок",
        "site_description": "meta",
        "footer_text": "© MyVPN",
    }
    r = _save_branding(client, body)
    assert r.status_code == 200, r.text
    saved = r.json()["branding"]
    assert saved["logo_data_url"] == PNG_LOGO
    assert saved["hero_title"] == "Заголовок"
    assert saved["footer_text"] == "© MyVPN"

    d = client.get("/admin/settings", headers=ADMIN_HEADERS).json()
    assert d["branding"]["logo_data_url"] == PNG_LOGO


def test_branding_rejects_bad_logo(client):
    r = _save_branding(client, {
        "site_name": "X", "accent_color": "#123456",
        "logo_data_url": "data:text/html;base64,AAAA",
    })
    assert r.status_code == 400


def test_branding_rejects_huge_logo(client):
    big = "data:image/png;base64," + "A" * 350_000  # >300KB, < pydantic max 400K
    r = _save_branding(client, {
        "site_name": "X", "accent_color": "#123456", "logo_data_url": big,
    })
    assert r.status_code == 400


def test_tariff_content_fields_reach_storefront(client):
    tariff = {
        "days": 31, "price": 300, "title": "Месяц", "devices": 5, "discount": 0,
        "inbounds": [], "description": "Описание тарифа",
        "features": ["Безлимит", "Все страны"], "badge": "Хит",
    }
    assert _save_tariff(client, tariff).status_code == 200

    t = client.get("/api/tariffs").json()["tariffs"][0]
    assert t["description"] == "Описание тарифа"
    assert t["features"] == ["Безлимит", "Все страны"]
    assert t["badge"] == "Хит"


def test_tariff_without_new_fields_is_graceful(client):
    # Старые сохранённые тарифы без новых полей не ломают API
    tariff = {"days": 31, "price": 300, "title": "Q2", "devices": 5, "discount": 0, "inbounds": []}
    assert _save_tariff(client, tariff).status_code == 200

    t = client.get("/api/tariffs").json()["tariffs"][0]
    assert t["description"] == ""
    assert t["features"] == []
    assert t["badge"] == ""


def test_tariff_content_validation(client):
    tariff = {
        "days": 31, "price": 300, "title": "М", "devices": 5, "discount": 0,
        "inbounds": [], "description": "x" * 250,  # > 200
    }
    r = _save_tariff(client, tariff)
    assert r.status_code == 400

    tariff["description"] = "ok"
    tariff["features"] = ["f"] * 11  # > 10
    r = _save_tariff(client, tariff)
    assert r.status_code == 400
