import re
from hashlib import sha256
from hmac import digest
from ipaddress import ip_address
from secrets import token_urlsafe
from time import time

import click
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.security import check_password_hash, generate_password_hash

from .access import event, require
from .models import Counterparty, LoginSession, LoginWindow, Role, Route, User, db
from .validation import InputError

bp = Blueprint("auth", __name__)
DUMMY_HASH = generate_password_hash("dummy-login-check")


def email_address(value):
    value = value.strip().lower()
    if len(value) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value) or "\x00" in value:
        raise InputError("Укажите корректный рабочий email.")
    return value


def password_hash(value):
    if not 12 <= len(value) <= 128:
        raise InputError("Пароль должен содержать от 12 до 128 символов.")
    return generate_password_hash(value)


def reserve_login(email):
    """Reserve both quotas before performing a password hash, across all workers."""
    try:
        address = ip_address(request.remote_addr or "")
        if address.version == 6 and address.ipv4_mapped:
            address = address.ipv4_mapped
        source = str(address)
    except ValueError:
        source = "unknown"
    settings = current_app.config
    now = int(time())
    duration = settings["LOGIN_WINDOW_SECONDS"]
    expires = (now // duration + 1) * duration
    insert = postgres_insert if db.engine.dialect.name == "postgresql" else sqlite_insert

    def reserve(scope, value, limit):
        key = scope + ":" + digest(settings["SECRET_KEY"].encode(), value.encode(), "sha256").hex()
        stmt = insert(LoginWindow).values(key=key, count=1, expires_at=expires)
        stmt = stmt.on_conflict_do_update(
            index_elements=[LoginWindow.key],
            set_={"count": LoginWindow.count + 1},
            where=LoginWindow.count < limit,
        ).returning(LoginWindow.count)
        return db.session.scalar(stmt) is not None

    try:
        db.session.execute(delete(LoginWindow).where(LoginWindow.expires_at <= now))
        allowed = reserve("i", source, settings["LOGIN_IP_LIMIT"]) and reserve(
            "a", email, settings["LOGIN_ACCOUNT_LIMIT"]
        )
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        abort(503)
    return allowed, expires - now


def session_key(token):
    return sha256(token.encode()).hexdigest()


def start_session(user):
    token = token_urlsafe(32)
    now = int(time())
    lifetime = int(current_app.permanent_session_lifetime.total_seconds())
    db.session.execute(delete(LoginSession).where(LoginSession.expires_at <= now))
    db.session.add(LoginSession(key=session_key(token), user_id=user.id, expires_at=now + lifetime))
    event("login")
    db.session.commit()
    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    session["session_version"] = user.session_version
    session["login_token"] = token


@bp.before_app_request
def load_user():
    g.user = None
    if request.endpoint in {"health", "static"} or "user_id" not in session:
        return
    token = session.get("login_token")
    record = db.session.get(LoginSession, session_key(token)) if isinstance(token, str) else None
    if record and record.user_id == session["user_id"] and record.expires_at > int(time()):
        user = db.session.get(User, record.user_id)
        if user and user.active and session.get("session_version") == user.session_version:
            g.user = user
            return
    session.clear()


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("dashboard.home"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        valid_email = len(email) <= 254 and "\x00" not in email
        allowed, retry_after = reserve_login(email if valid_email else "")
        if not allowed:
            return (render_template("error.html", code=429, message="Слишком много попыток входа. Повторите позже."),
                    429, {"Retry-After": str(retry_after)})
        user = db.session.scalar(select(User).where(User.email == email)) if valid_email else None
        matched = len(password) <= 128 and check_password_hash(user.password_hash if user else DUMMY_HASH, password)
        if matched and user and user.active:
            g.user = user
            start_session(user)
            return redirect(url_for("dashboard.home"))
        flash("Неверный email или пароль.", "error")
        return render_template("login.html"), 401
    return render_template("login.html")


@bp.post("/logout")
@require()
def logout():
    db.session.execute(delete(LoginSession).where(LoginSession.key == session_key(session["login_token"])))
    event("logout")
    db.session.commit()
    session.clear()
    return redirect(url_for("auth.login"))


def register_commands(app):
    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", prompt=True)
    def create_admin(email, name):
        """Create an administrator using an interactively entered password."""
        name = name.strip()
        try:
            if not name or len(name) > 120 or "\x00" in name:
                raise InputError("Проверьте имя администратора.")
            admin_role = db.session.get(Role, "admin")
            if admin_role is None:
                raise InputError("Сначала выполните flask db upgrade.")
            email = email_address(email)
            password = click.prompt("Пароль", hide_input=True, confirmation_prompt=True)
            user = User(email=email, name=name, department="Администрация",
                        password_hash=password_hash(password), roles=[admin_role])
            db.session.add(user)
            db.session.flush()
            event("employee_created", employee=user.id, enabled=True, roles=["admin"])
            db.session.commit()
        except (InputError, IntegrityError) as exc:
            db.session.rollback()
            raise click.ClickException(str(exc) if isinstance(exc, InputError) else "Email уже используется.") from None
        click.echo("Администратор создан.")

    @app.cli.command("seed-demo")
    def seed_demo():
        """Populate an empty test database with sample employees and a route."""
        if current_app.config["VAIM_ENV"] != "testing" or db.engine.dialect.name != "sqlite":
            raise click.ClickException("Демо-данные разрешены только в VAIM_ENV=testing с SQLite.")
        if db.session.scalar(select(User.id).limit(1)) is not None:
            raise click.ClickException("Для демо-данных нужна отдельная пустая база.")
        demo_names = {
            "admin": "Елена Орлова", "initiator": "Антон Крылов", "manager": "Ирина Волкова",
            "finance": "Виктор Соколов", "director": "Мария Лебедева",
            "accountant": "Ольга Мельникова", "observer": "Павел Смирнов",
        }
        for key, name in demo_names.items():
            role = db.session.get(Role, key)
            if role is None:
                raise click.ClickException("Сначала выполните flask db upgrade.")
            db.session.add(User(email=f"{key}@demo.invalid", name=name,
                                department="Тестовый отдел", roles=[role], password_hash=password_hash("vaim-test-only")))
        db.session.add(Counterparty(name="ООО «Пример»", inn="7701234567", kpp="770101001"))
        db.session.add(Route(name="Стандартный маршрут", steps=[
            {"name": "Проверка подразделением", "role": "manager"},
            {"name": "Финансовый контроль", "role": "finance"},
            {"name": "Утверждение директором", "role": "director"},
        ]))
        db.session.commit()
        click.echo("Демо-данные созданы. Тестовый пароль: vaim-test-only")
