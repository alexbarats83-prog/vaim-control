import os
from datetime import timedelta, timezone
from pathlib import Path
from secrets import token_hex
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from flask import Flask, g, jsonify, render_template, request
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import event
from sqlalchemy.engine import make_url
from werkzeug.middleware.proxy_fix import ProxyFix

from .access import ACTIONS, PERMISSIONS, ROLES, STATUSES, can, responsible
from .models import LoginSession, LoginWindow, Role, db
from .validation import PRIORITIES, format_money, today

migrate = Migrate()
csrf = CSRFProtect()


def create_app(config=None):
    app = Flask(__name__, instance_relative_config=True)
    url = os.getenv("DATABASE_URL", "sqlite:///vaim.sqlite3")
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            url = url.replace(prefix, "postgresql+psycopg://", 1)
    app.config.from_mapping(
        VAIM_ENV=os.getenv("VAIM_ENV", "development"),
        SECRET_KEY=os.getenv("SECRET_KEY"), SQLALCHEMY_DATABASE_URI=url,
        SUPABASE_URL=os.getenv("SUPABASE_URL"),
        SUPABASE_SERVICE_KEY=os.getenv("SUPABASE_SERVICE_KEY"),
        SUPABASE_ANON_KEY=os.getenv("SUPABASE_ANON_KEY"),
        SUPABASE_BUCKET=os.getenv("SUPABASE_BUCKET", "vaim-private"),
        TRUST_PROXY_HOPS=int(os.getenv("TRUST_PROXY_HOPS", "0")),
        LOGIN_WINDOW_SECONDS=900, LOGIN_ACCOUNT_LIMIT=8, LOGIN_IP_LIMIT=30,
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8), SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=11 * 1024 * 1024, MAX_FORM_MEMORY_SIZE=256 * 1024,
        MAX_FORM_PARTS=100, UPLOAD_DIRECTORY=str(Path(app.instance_path) / "documents"),
    )
    app.config.update(config or {})
    if app.config["VAIM_ENV"] not in {"development", "testing", "production"}:
        raise RuntimeError("Invalid VAIM_ENV")
    if app.config["SUPABASE_URL"]:
        storage_url = urlsplit(app.config["SUPABASE_URL"])
        if storage_url.scheme != "https" or not storage_url.hostname or storage_url.username or storage_url.password or storage_url.query or storage_url.fragment or storage_url.path not in {"", "/"}:
            raise RuntimeError("SUPABASE_URL must be an HTTPS project origin")
    if app.config["VAIM_ENV"] == "production":
        if len(app.config["SECRET_KEY"] or "") < 32:
            raise RuntimeError("Production requires SECRET_KEY with at least 32 characters")
        if not app.config["SQLALCHEMY_DATABASE_URI"].startswith("postgresql+psycopg://"):
            raise RuntimeError("Production requires PostgreSQL via psycopg")
        if not (app.config["SUPABASE_URL"] or "").startswith("https://") or not app.config["SUPABASE_SERVICE_KEY"]:
            raise RuntimeError("Production requires HTTPS Supabase Storage and a service key")
        app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
            "pool_pre_ping": True, "hide_parameters": True, "connect_args": {"connect_timeout": 10},
        }
        mode = make_url(app.config["SQLALCHEMY_DATABASE_URI"]).query.get("sslmode")
        if mode not in {"require", "verify-ca", "verify-full"}:
            raise RuntimeError("Production requires PostgreSQL TLS via sslmode")
        app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_NAME="__Host-vaim_session")
    elif not app.config["SECRET_KEY"]:
        app.config["SECRET_KEY"] = token_hex(32)
    proxy_hops = app.config["TRUST_PROXY_HOPS"]
    if proxy_hops not in {0, 1}:
        raise RuntimeError("TRUST_PROXY_HOPS must be 0 or 1")
    if proxy_hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops, x_proto=proxy_hops)
    if app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:"):
        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    with app.app_context():
        if app.config["VAIM_ENV"] == "production":
            @event.listens_for(db.engine, "connect", insert=True)
            def private_postgres_schema(connection, _record):
                previous = connection.autocommit
                connection.autocommit = True
                try:
                    connection.execute("SET search_path TO vaim")
                finally:
                    connection.autocommit = previous

        if db.engine.dialect.name == "sqlite":
            @event.listens_for(db.engine, "connect")
            def sqlite_foreign_keys(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")

    from . import admin, auth, dashboard, invoices
    for module in (auth, dashboard, invoices, admin):
        app.register_blueprint(module.bp)
    auth.register_commands(app)
    from .release import register_commands
    register_commands(app)

    @app.get("/health")
    def health():
        try:
            db.session.execute(sa.select(LoginWindow.key).limit(1))
            db.session.execute(sa.select(LoginSession.key).limit(1))
            if db.session.get(Role, "admin") is None:
                return jsonify(status="unavailable"), 503
        except sa.exc.SQLAlchemyError:
            return jsonify(status="unavailable"), 503
        return jsonify(status="ok")

    @app.context_processor
    def helpers():
        return dict(
            user=g.get("user"), can=can, statuses=STATUSES, roles=ROLES, actions=ACTIONS,
            permissions=PERMISSIONS, priorities=PRIORITIES, responsible=responsible, today=today(),
        )

    app.jinja_env.filters["money"] = format_money
    @app.template_filter("invoice_unit")
    def invoice_unit(value):
        if 11 <= value % 100 <= 14:
            return "счетов"
        return {1: "счёт", 2: "счета", 3: "счета", 4: "счета"}.get(value % 10, "счетов")

    @app.template_filter("local_time")
    def local_time(value):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M")

    @app.after_request
    def headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        if g.get("user") or request.endpoint in {"auth.login", "auth.logout"}:
            response.headers["Cache-Control"] = "no-store"
        if app.config["VAIM_ENV"] == "production" and request.is_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(400)
    @app.errorhandler(401)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    @app.errorhandler(503)
    def error(exc):
        messages = {
            400: "Запрос недействителен или срок действия формы истёк. Обновите страницу.",
            401: "Войдите в систему, чтобы продолжить.", 403: "У вас нет прав на это действие.",
            404: "Страница или документ недоступны.",
            409: "Запись уже изменилась или такое действие выполнено. Обновите страницу.",
            413: "Файл превышает допустимый размер.", 503: "Сервис временно недоступен.",
        }
        return render_template("error.html", code=exc.code, message=messages[exc.code]), exc.code

    return app
