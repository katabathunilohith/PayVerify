from conftest import PASSWORD, signup


def login(client, email, password=PASSWORD, remember=False):
    data = {"email": email, "password": password}
    if remember:
        data["remember"] = "1"
    return client.post("/login", data=data)


def test_pages_require_login(app):
    client = app.test_client()
    for path in ["/", "/upload", "/bank/", "/settings/", "/settings/whatsapp", "/export.xlsx", "/payments/1"]:
        response = client.get(path)
        assert response.status_code == 302, path
        assert "/login" in response.headers["Location"]


def test_signup_logs_in_and_isolates_new_account(app):
    client = app.test_client()
    response = signup(client, "a@example.com")
    assert response.status_code == 302
    page = client.get("/").get_data(as_text=True)
    assert "Finish setting up" in page


def test_signup_validation(app):
    client = app.test_client()
    assert client.post("/signup", data={"name": "A", "email": "bad", "password": "short", "confirm": "x"}).status_code == 400
    signup(client, "dup@example.com")
    other = app.test_client()
    response = signup(other, "DUP@example.com")
    assert response.status_code == 400
    assert "already exists" in response.get_data(as_text=True)


def test_login_logout(app):
    client = app.test_client()
    signup(client, "owner@example.com")
    client.post("/logout")
    assert client.get("/").status_code == 302

    wrong = login(client, "owner@example.com", "nope-nope")
    assert wrong.status_code == 200
    assert "don&#39;t match" in wrong.get_data(as_text=True)

    ok = login(client, "Owner@Example.com ")
    assert ok.status_code == 302
    assert client.get("/").status_code == 200


def test_logout_requires_post(app):
    client = app.test_client()
    signup(client, "owner@example.com")
    assert client.get("/logout").status_code == 405


def test_logout_clears_remember_me(app):
    client = app.test_client()
    signup(client, "owner@example.com")
    client.post("/logout")
    login(client, "owner@example.com", remember=True)
    client.post("/logout")
    assert client.get("/").status_code == 302


def test_login_lockout_after_repeated_failures(app):
    client = app.test_client()
    signup(client, "owner@example.com")
    client.post("/logout")
    for _ in range(5):
        login(client, "owner@example.com", "wrong-password")
    blocked = login(client, "owner@example.com")  # right password, but locked out
    assert blocked.status_code == 429


def test_open_redirect_blocked(app):
    client = app.test_client()
    signup(client, "owner@example.com")
    client.post("/logout")
    for target in ["https://evil.example/x", "//evil.example", "/\\evil.example"]:
        client.post("/logout")
        response = client.post("/login", query_string={"next": target},
                               data={"email": "owner@example.com", "password": PASSWORD})
        assert response.headers["Location"] == "/", target


def test_signups_can_be_closed_or_need_invite_code(app):
    app.config["ALLOW_SIGNUPS"] = False
    client = app.test_client()
    assert "Sign-ups are closed" in client.get("/signup").get_data(as_text=True)

    app.config.update(ALLOW_SIGNUPS=True, SIGNUP_CODE="letmein")
    assert signup(client, "x@example.com").status_code == 400
    response = client.post("/signup", data={
        "name": "X", "email": "x@example.com", "password": PASSWORD, "confirm": PASSWORD, "signup_code": "letmein",
    })
    assert response.status_code == 302


def test_password_change_signs_out_other_devices(app):
    phone = app.test_client()
    signup(phone, "owner@example.com")
    laptop = app.test_client()
    assert login(laptop, "owner@example.com").status_code == 302

    response = phone.post("/settings/security", data={
        "action": "password", "current_password": PASSWORD,
        "new_password": "brand-new-pass", "confirm_password": "brand-new-pass",
    })
    assert response.status_code == 302
    assert phone.get("/").status_code == 200  # this device stays signed in
    assert laptop.get("/").status_code == 302  # the other one is signed out
    assert login(laptop, "owner@example.com", "brand-new-pass").status_code == 302


def test_sign_out_everywhere(app):
    phone = app.test_client()
    signup(phone, "owner@example.com")
    laptop = app.test_client()
    login(laptop, "owner@example.com", remember=True)
    phone.post("/settings/security", data={"action": "logout_all"})
    assert phone.get("/").status_code == 302
    assert laptop.get("/").status_code == 302


def test_account_pages_are_not_cached(app):
    client = app.test_client()
    signup(client, "owner@example.com")
    response = client.get("/")
    assert response.headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
