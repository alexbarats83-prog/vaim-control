import click
from flask import current_app
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .models import db
from .storage import StorageError, verify_cloud_storage


def register_commands(app):
    @app.cli.command("verify-db")
    def verify_db():
        """Check connectivity and whether PostgreSQL uses TLS."""
        try:
            db.session.execute(text("SELECT 1"))
            if db.engine.dialect.name == "postgresql":
                driver = db.session.connection().connection.driver_connection
                if not driver.pgconn.ssl_in_use:
                    raise click.ClickException("PostgreSQL подключён без TLS.")
                if current_app.config["VAIM_ENV"] == "production" and db.session.scalar(text("SELECT current_schema()")) != "vaim":
                    raise click.ClickException("Создайте закрытую схему vaim в Supabase.")
                click.echo("PostgreSQL доступен. TLS подтверждён.")
            else:
                click.echo("SQLite доступен.")
        except SQLAlchemyError:
            raise click.ClickException("Не удалось подключиться к базе. Проверьте DATABASE_URL.") from None

    @app.cli.command("verify-storage")
    def verify_storage():
        """Check private Supabase upload, download, anonymous denial and cleanup."""
        try:
            verify_cloud_storage()
        except StorageError as exc:
            raise click.ClickException(str(exc)) from None
        click.echo("Закрытый bucket проверен. Тестовый PNG загружен, скачан и удалён. Анонимный доступ закрыт.")
