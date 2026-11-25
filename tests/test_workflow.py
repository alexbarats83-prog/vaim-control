import io
import os
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from flask_migrate import upgrade
from PIL import Image
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from werkzeug.security import generate_password_hash

from vaim import create_app
from vaim.models import ApprovalRun, Attachment, Audit, Counterparty, Invoice, Payment, Role, Route, User, db
from vaim.validation import today

PASSWORD = "test-password-for-vaim"
PASSWORD_HASH = generate_password_hash(PASSWORD)
PEOPLE = {
    "admin": ["admin"], "author": ["initiator"], "other": ["initiator"],
    "manager": ["manager"], "outside": ["manager"], "finance": ["finance", "observer"],
    "accountant": ["accountant"], "observer": ["observer"],
}


@pytest.fixture()
def database_url(tmp_path):
    postgres = os.getenv("VAIM_TEST_POSTGRES_URL")
    if not postgres:
        yield f"sqlite:///{tmp_path / 'vaim.db'}"
        return
    url = make_url(postgres)
    schema = "vaim_test_" + uuid4().hex
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    try:
        yield url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        engine.dispose()


@pytest.fixture()
def app(tmp_path, database_url):
    app = create_app({
        "TESTING": True, "VAIM_ENV": "testing", "SECRET_KEY": "test-secret-only",
        "SQLALCHEMY_DATABASE_URI": database_url,
        "UPLOAD_DIRECTORY": str(tmp_path / "documents"), "SUPABASE_URL": None,
    })
    with app.app_context():
        upgrade()
        for name, roles in PEOPLE.items():
            db.session.add(User(name=name, email=f"{name}@test.invalid", password_hash=PASSWORD_HASH,
                                department="Другой" if name == "outside" else "ИТ",
                                roles=[db.session.get(Role, role) for role in roles]))
        db.session.add(Counterparty(name="Контрагент", inn="7700123456", kpp="770001001"))
        db.session.add(Route(name="Общий", steps=[{"name": "Руководитель", "role": "manager"},
                                                  {"name": "Финансы", "role": "finance"}]))
        db.session.commit()
    yield app
    with app.app_context():
        db.engine.dispose()


def field(response, name):
    return re.search(fr'name="{name}" value="([^"]+)"', response.text)[1]


def log_in(app, name, password=PASSWORD):
    client = app.test_client()
    token = field(client.get("/login"), "csrf_token")
    response = client.post("/login", data=dict(email=f"{name}@test.invalid", password=password, csrf_token=token))
    return client, response


def actor(app, name):
    client, response = log_in(app, name)
    assert response.status_code == 302
    assert client.get("/").status_code == 200
    return client


def csrf(client):
    return field(client.get("/"), "csrf_token")


def make_invoice(client, **values):
    data = dict(number="V-001", invoice_date="2025-01-01", due_date="2025-01-31",
                entity="ООО ВАИМ", department="ИТ", counterparty_id=1, route_id=1, amount="1000,50",
                vat="100.00", currency="RUB", expense="Лицензии", purpose="Оплата лицензии", priority="normal")
    data.update(values)
    return client.post("/invoices/new", data=dict(csrf_token=csrf(client), **data))


def card(client, path):
    response = client.get(path)
    assert response.status_code == 200
    return response


def action(client, path, command, revision=None, **data):
    payload = dict(csrf_token=csrf(client), action=command, **data)
    payload["revision"] = revision if revision is not None else field(card(client, path), "revision")
    return client.post(path + "/action", data=payload)


def amount_paid(app, path):
    with app.app_context():
        item = db.session.get(Invoice, int(path.rsplit("/", 1)[1]))
        return item.status, item.paid


def ready_invoice(app):
    author = actor(app, "author")
    path = make_invoice(author).location
    assert action(author, path, "submit").status_code == 302
    assert action(actor(app, "manager"), path, "approve").status_code == 302
    assert action(actor(app, "finance"), path, "approve").status_code == 302
    assert amount_paid(app, path) == ("awaiting", 0)
    return path


def payment_data(client, path, **kwargs):
    source = card(client, path)
    data = dict(csrf_token=csrf(client), revision=field(source, "revision"), request_id=field(source, "request_id"),
                date=today().isoformat(), amount="250.15", order_number="ПП-42")
    data.update(kwargs)
    return data


def png():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, format="PNG")
    return output.getvalue()


def test_login_csrf_logout_and_invalid_credentials(app):
    guest = app.test_client()
    assert guest.get("/").location == "/login"
    assert guest.post("/login", data={"email": "admin@test.invalid", "password": PASSWORD}).status_code == 400
    assert log_in(app, "admin", "wrong")[1].status_code == 401
    client = actor(app, "admin")
    assert client.get("/").headers["Cache-Control"] == "no-store"
    assert client.post("/logout", data={"csrf_token": csrf(client)}).location == "/login"
    assert client.get("/").location == "/login"


def test_employee_creation_roles_and_blocking_invalidates_sessions(app):
    admin = actor(app, "admin")
    for path in ("/employees", "/employees/new", "/roles", "/routes", "/routes/1", "/routes/new",
                 "/settings", "/audit", "/counterparties", "/reports", "/tasks", "/invoices/"):
        assert admin.get(path).status_code == 200
    values = dict(name="Новый сотрудник", email="new@test.invalid", department="ИТ",
                  password=PASSWORD, roles=["initiator", "observer"], active="on")
    assert admin.post("/employees/new", data=dict(csrf_token=csrf(admin), **values)).status_code == 302
    member = actor(app, "new")
    assert member.get("/invoices/new").status_code == 200
    assert member.get("/reports").status_code == 200
    assert member.get("/employees").status_code == 403
    with app.app_context():
        user = db.session.scalar(select(User).where(User.email == "new@test.invalid"))
        assert user.role_ids == {"initiator", "observer"}
        assert user.password_hash != PASSWORD
        user_id, revision = user.id, user.session_version
    values.pop("active")
    values["password"] = ""
    values["revision"] = revision
    assert admin.post(f"/employees/{user_id}", data=dict(csrf_token=csrf(admin), **values)).status_code == 302
    assert member.get("/").location == "/login"
    assert log_in(app, "new")[1].status_code == 401


def test_invoice_creation_is_exact_and_validated(app):
    client = actor(app, "author")
    assert make_invoice(client, amount="1.001").status_code == 200
    assert make_invoice(client, vat="1001").status_code == 200
    response = make_invoice(client)
    assert response.status_code == 302
    assert "V-001" in card(client, response.location).text
    assert "Просрочен" in card(client, response.location).text
    assert client.get(response.location + "/edit").status_code == 200
    with app.app_context():
        invoice = db.session.scalar(select(Invoice))
        assert (invoice.amount, invoice.vat, invoice.status) == (100050, 10000, "draft")
        assert isinstance(invoice.amount, int)


def test_file_validation_and_cross_invoice_access(app):
    author = actor(app, "author")
    other = actor(app, "other")
    path = make_invoice(author).location
    other_path = make_invoice(other, number="V-PERSONAL").location
    rev = field(card(author, path), "revision")
    data = dict(csrf_token=csrf(author), revision=rev)
    for content, filename in ((b"fake", "invoice.pdf"), (png(), "invoice.jpg"), (b"x", "unsafe.exe"),
                              (b"x" * (10 * 1024 * 1024 + 1), "invoice.png")):
        response = author.post(path + "/documents", data=dict(data, document=(io.BytesIO(content), filename)),
                               follow_redirects=True)
        assert response.status_code == 200
        assert "Размер документа" in response.text or "Содержимое документа" in response.text or "Допустимые форматы" in response.text
    assert author.post(path + "/documents", data=dict(data, document=(io.BytesIO(png()), "Счёт.png"))).status_code == 302
    with app.app_context():
        document = db.session.scalar(select(Attachment))
        assert document.name == "Счёт.png"
        doc_id = document.id
    download_url = f"{path}/documents/{doc_id}"
    response = author.get(download_url)
    assert response.data == png()
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert other.get(path).status_code == 404
    assert other.get(download_url).status_code == 404
    assert other.get(f"{other_path}/documents/{doc_id}").status_code == 404
    assert other.post(path + "/action", data={"csrf_token": csrf(other), "revision": rev, "action": "comment"}).status_code == 404
    assert "Счёт.png" not in other.get("/").text


def test_order_permissions_and_route_snapshot(app):
    author = actor(app, "author")
    manager = actor(app, "manager")
    finance = actor(app, "finance")
    outside = actor(app, "outside")
    admin = actor(app, "admin")
    path = make_invoice(author).location
    assert action(author, path, "submit").status_code == 302
    rev = field(card(manager, path), "revision")
    assert action(finance, path, "approve", revision=rev).status_code == 403
    assert action(outside, path, "approve", revision=rev).status_code == 404
    assert admin.post("/routes/1", data=dict(csrf_token=csrf(admin), revision=1, name="Новый порядок",
                      active="on", step_name=["Бухгалтерия"], step_target=["role:accountant"])).status_code == 302
    assert action(manager, path, "approve", revision=rev).status_code == 302
    assert action(manager, path, "approve", revision=rev).status_code == 409
    assert action(finance, path, "approve").status_code == 302
    assert amount_paid(app, path) == ("awaiting", 0)
    with app.app_context():
        run = db.session.scalar(select(ApprovalRun))
        assert run.route_name == "Общий"
        assert run.route_revision == 1
        assert [step["role"] for step in run.steps] == ["manager", "finance"]
        assert db.session.get(Route, 1).revision == 2


def test_return_edit_resubmit_and_reject_require_reason(app):
    author = actor(app, "author")
    manager = actor(app, "manager")
    path = make_invoice(author).location
    assert action(author, path, "submit").status_code == 302
    assert action(manager, path, "return").status_code == 302
    assert amount_paid(app, path) == ("review", 0)
    assert action(manager, path, "return", comment="Исправьте назначение платежа").status_code == 302
    assert amount_paid(app, path) == ("returned", 0)
    assert "Исправьте назначение платежа" in card(author, path).text
    assert action(author, path, "submit").status_code == 302
    assert action(manager, path, "reject").status_code == 302
    assert amount_paid(app, path) == ("review", 0)
    assert action(manager, path, "reject", comment="Не согласован бюджет").status_code == 302
    assert amount_paid(app, path) == ("rejected", 0)
    with app.app_context():
        assert [run.number for run in db.session.scalars(select(ApprovalRun).order_by(ApprovalRun.number))] == [1, 2]
        actions = {event.action for event in db.session.scalars(select(Audit).where(Audit.invoice_id.is_not(None)))}
        assert {"created", "submitted", "return", "reject"} <= actions


def test_partial_full_invalid_and_repeated_payment(app):
    path = ready_invoice(app)
    accountant = actor(app, "accountant")
    author = actor(app, "author")
    endpoint = path + "/payments"
    initial = payment_data(accountant, path)
    assert author.post(endpoint, data=dict(initial, csrf_token=csrf(author))).status_code == 403
    for value in ("-10", "1000.51", "0", "66.666"):
        assert accountant.post(endpoint, data=dict(initial, amount=value)).status_code == 302
        assert amount_paid(app, path) == ("awaiting", 0)
    assert accountant.post(endpoint, data=dict(initial, document=(io.BytesIO(png()), "Поручение.png"))).status_code == 302
    assert amount_paid(app, path) == ("partial", 25015)
    assert accountant.post(endpoint, data=initial).status_code == 409
    duplicate = payment_data(accountant, path, order_number=" пп-42 ")
    assert accountant.post(endpoint, data=duplicate).status_code == 409
    reused_token = dict(duplicate, order_number="ПП-43", request_id=initial["request_id"])
    assert accountant.post(endpoint, data=reused_token).status_code == 409
    full = payment_data(accountant, path, order_number="ПП-43", amount="750.35")
    assert accountant.post(endpoint, data=full).status_code == 302
    assert amount_paid(app, path) == ("paid", 100050)
    assert "Оплачен" in card(author, path).text
    with app.app_context():
        assert len(db.session.scalars(select(Payment)).all()) == 2
        assert db.session.scalar(select(Attachment)).payment_id is not None


def test_competing_payments_only_one_updates_invoice(app):
    path = ready_invoice(app)
    clients = [actor(app, "accountant") for _ in range(2)]
    submissions = [payment_data(client, path, order_number=f"ПП-{index}", request_id=str(uuid4()), amount="600")
                   for index, client in enumerate(clients)]
    barrier = Barrier(2)

    def post(client, data):
        barrier.wait(timeout=5)
        return client.post(path + "/payments", data=data).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks = [pool.submit(post, client, data) for client, data in zip(clients, submissions)]
        assert sorted(task.result(timeout=10) for task in tasks) == [302, 409]
    assert amount_paid(app, path) == ("partial", 60000)
    with app.app_context():
        assert len(db.session.scalars(select(Payment)).all()) == 1


def test_observer_cannot_comment_and_dashboard_respects_access(app):
    author = actor(app, "author")
    other = actor(app, "other")
    observer = actor(app, "observer")
    path = make_invoice(author, number="confidential-number").location
    assert "confidential-number" in card(observer, path).text
    with app.app_context():
        revision = db.session.get(Invoice, 1).revision
    assert action(observer, path, "comment", revision=revision, comment="No").status_code == 403
    assert "confidential-number" not in other.get("/invoices/").text
    assert "confidential-number" in author.get("/invoices/").text
    assert other.get("/audit").status_code == 403
    assert observer.get("/health").json == {"status": "ok"}


def test_named_step_missing_employee_and_owner_department(app):
    admin = actor(app, "admin")
    author = actor(app, "author")
    finance = actor(app, "finance")
    with app.app_context():
        owner = db.session.scalar(select(User).where(User.email == "author@test.invalid"))
        observer = db.session.scalar(select(User).where(User.email == "observer@test.invalid"))
        owner_id, observer_id = owner.id, observer.id
    values = dict(csrf_token=csrf(admin), revision=1, name="Маршрут сотрудника", active="on", step_name=["Проверка"])
    assert admin.post("/routes/1", data=dict(values, step_target=[f"user:{observer_id}"])).status_code == 200
    assert admin.post("/routes/1", data=dict(values, step_target=[f"user:{owner_id}"])).status_code == 302
    path = make_invoice(author).location
    assert action(author, path, "submit").status_code == 302
    assert amount_paid(app, path) == ("draft", 0)
    allowed = dict(values, revision=2, step_self_approval=["1"])
    assert admin.post("/routes/1", data=dict(allowed, step_target=[f"user:{owner_id}"])).status_code == 302
    path = make_invoice(author, department="Несуществующий отдел").location
    assert action(author, path, "submit").status_code == 302
    assert action(finance, path, "approve", revision=field(card(author, path), "revision")).status_code == 403
    assert action(author, path, "approve").status_code == 302
    assert amount_paid(app, path) == ("awaiting", 0)
    with app.app_context():
        assert db.session.get(Invoice, 1).department == "ИТ"


def test_health_checks_schema_and_demo_is_test_only(tmp_path):
    app = create_app({
        "TESTING": True, "VAIM_ENV": "development", "SECRET_KEY": "test-secret",
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'empty.db'}",
    })
    assert app.test_client().get("/health").status_code == 503
    runner = app.test_cli_runner()
    assert runner.invoke(args=["db", "upgrade"]).exit_code == 0
    assert app.test_client().get("/health").json == {"status": "ok"}
    assert runner.invoke(args=["seed-demo"]).exit_code != 0
    args = ["create-admin", "--email", "new-admin@test.invalid", "--name", "Новый администратор"]
    assert runner.invoke(args=args, input=f"{PASSWORD}\n{PASSWORD}\n").exit_code == 0
    assert log_in(app, "new-admin")[1].status_code == 302
    app.config["VAIM_ENV"] = "testing"
    assert runner.invoke(args=["seed-demo"]).exit_code != 0


def test_dashboard_paid_period_and_currency_totals(app):
    path = ready_invoice(app)
    author = actor(app, "author")
    accountant = actor(app, "accountant")
    assert make_invoice(author, number="EURO", currency="EUR").status_code == 302
    data = payment_data(accountant, path, amount="1000.50")
    assert accountant.post(path + "/payments", data=data).status_code == 302
    day = today().isoformat()
    home = author.get(f"/?start={day}&end={day}").text
    assert re.search(r"Оплачены за период</span><strong>1</strong>", home)
    report = accountant.get(f"/reports?start={day}&end={day}").text
    assert "1\u202f000,50" in report
    assert "EUR" in report and "RUB" in report
    assert "Полностью оплачено счетов за период: 1" in report
    assert "Полностью оплачено счетов за период: 0" in accountant.get(
        "/reports?start=2000-01-01&end=2000-01-31"
    ).text


def test_login_quotas_shared_by_workers_and_ignore_untrusted_forwarding(app, monkeypatch):
    app.config.update(LOGIN_IP_LIMIT=4, LOGIN_ACCOUNT_LIMIT=2)
    monkeypatch.setattr("vaim.auth.time", lambda: 1800)
    for _ in range(2):
        assert log_in(app, "author", "wrong")[1].status_code == 401
    limited = log_in(app, "author")[1]
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "900"
    assert log_in(app, "unknown", "wrong")[1].status_code == 401

    client = app.test_client()
    response = client.post("/login", headers={"X-Forwarded-For": "192.0.2.10"}, data={
        "csrf_token": field(client.get("/login"), "csrf_token"),
        "email": "outside@test.invalid", "password": PASSWORD,
    })
    assert response.status_code == 429
    monkeypatch.setattr("vaim.auth.time", lambda: 2700)
    assert log_in(app, "author")[1].status_code == 302


def test_logout_revokes_copied_cookie_without_revoking_other_sessions(app):
    client = actor(app, "author")
    separate = actor(app, "author")
    replay = app.test_client()
    cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"])
    replay.set_cookie(cookie.key, cookie.value)
    assert replay.get("/").status_code == 200
    assert client.post("/logout", data={"csrf_token": csrf(client)}).status_code == 302
    assert replay.get("/").location == "/login"
    assert separate.get("/").status_code == 200


def test_author_with_approval_role_still_needs_explicit_permission(app):
    with app.app_context():
        author = db.session.scalar(select(User).where(User.email == "author@test.invalid"))
        author.roles.append(db.session.get(Role, "manager"))
        db.session.commit()
    author = actor(app, "author")
    path = make_invoice(author).location
    assert action(author, path, "submit").status_code == 302
    assert action(author, path, "approve").status_code == 403
    assert action(actor(app, "manager"), path, "approve").status_code == 302
    assert action(actor(app, "finance"), path, "approve").status_code == 302
    assert author.post(path + "/edit", data={"csrf_token": csrf(author), "amount": "0.01"}).status_code == 403
    assert amount_paid(app, path) == ("awaiting", 0)


def test_simultaneous_approval_only_records_one_step(app):
    author = actor(app, "author")
    path = make_invoice(author).location
    assert action(author, path, "submit").status_code == 302
    clients = [actor(app, "manager") for _ in range(2)]
    common_revision = field(card(clients[0], path), "revision")
    payloads = [dict(csrf_token=csrf(client), revision=common_revision, action="approve") for client in clients]
    barrier = Barrier(2)

    def approve(client, data):
        barrier.wait(timeout=5)
        return client.post(path + "/action", data=data).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(approve, client, data) for client, data in zip(clients, payloads)]
        assert sorted(future.result(timeout=10) for future in futures) == [302, 409]
    with app.app_context():
        assert db.session.get(Invoice, 1).current_step == 1
        assert len(db.session.scalars(select(Audit).where(Audit.action == "approve")).all()) == 1


def test_untrusted_text_is_escaped_and_search_is_literal(app):
    author = actor(app, "author")
    other = actor(app, "other")
    disallowed_text = '<img src=x onerror="ignored">'
    path = make_invoice(author, number="VISIBLE", purpose=disallowed_text).location
    assert make_invoice(other, number="OTHER").status_code == 302
    response = card(author, path)
    assert disallowed_text not in response.text and "&lt;img" in response.text
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert "VISIBLE" not in author.get("/invoices/", query_string={"q": "%"}).text
    assert "№ OTHER" not in author.get("/invoices/", query_string={"q": "OTHER"}).text
    assert author.post(path + "/action", data={"action": "submit", "revision": 1}).status_code == 400
    assert amount_paid(app, path) == ("draft", 0)
