from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import MetaData

db = SQLAlchemy(metadata=MetaData(naming_convention={
    "ix": "ix_%(column_0_label)s", "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}))


def utc_now():
    return datetime.now(timezone.utc)


user_roles = db.Table(
    "user_roles",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id"), primary_key=True),
    db.Column("role_id", db.String(24), db.ForeignKey("roles.id"), primary_key=True),
)


class Role(db.Model):
    __tablename__ = "roles"
    id = db.Column(db.String(24), primary_key=True)


class User(db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(254), unique=True, nullable=False)
    name = db.Column(db.String(120), nullable=False)
    department = db.Column(db.String(120), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    roles = db.relationship(Role, secondary=user_roles, lazy="selectin")

    @property
    def role_ids(self):
        return {role.id for role in self.roles}


class Counterparty(db.Model):
    __tablename__ = "counterparties"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    inn = db.Column(db.String(12), nullable=False)
    kpp = db.Column(db.String(9), nullable=False, default="")
    __table_args__ = (db.UniqueConstraint("inn", "kpp"),)


class Route(db.Model):
    __tablename__ = "routes"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    revision = db.Column(db.Integer, nullable=False, default=1)
    active = db.Column(db.Boolean, nullable=False, default=True)
    steps = db.Column(db.JSON, nullable=False)
    __mapper_args__ = {"version_id_col": revision}


class Invoice(db.Model):
    __tablename__ = "invoices"
    id = db.Column(db.Integer, primary_key=True)
    number = db.Column(db.String(80), nullable=False)
    invoice_date = db.Column(db.Date, nullable=False)
    entity = db.Column(db.String(160), nullable=False)
    department = db.Column(db.String(120), nullable=False)
    counterparty_id = db.Column(db.Integer, db.ForeignKey("counterparties.id"), nullable=False)
    amount = db.Column(db.BigInteger, nullable=False)
    currency = db.Column(db.String(3), nullable=False)
    vat = db.Column(db.BigInteger, nullable=False)
    expense = db.Column(db.String(120), nullable=False)
    purpose = db.Column(db.Text, nullable=False)
    due_date = db.Column(db.Date, nullable=False)
    priority = db.Column(db.String(12), nullable=False)
    initiator_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    route_id = db.Column(db.Integer, db.ForeignKey("routes.id"), nullable=False)
    status = db.Column(db.String(24), nullable=False, default="draft", index=True)
    paid = db.Column(db.BigInteger, nullable=False, default=0)
    revision = db.Column(db.Integer, nullable=False, default=1)
    round = db.Column(db.Integer, nullable=False, default=0)
    current_step = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    counterparty = db.relationship(Counterparty)
    initiator = db.relationship(User)
    route = db.relationship(Route)
    runs = db.relationship("ApprovalRun", order_by="ApprovalRun.number", lazy="selectin")
    __mapper_args__ = {"version_id_col": revision, "version_id_generator": False}
    __table_args__ = (
        db.CheckConstraint("amount > 0 AND vat >= 0 AND vat <= amount", name="amounts"),
        db.CheckConstraint("paid >= 0 AND paid <= amount", name="paid"),
    )

    @property
    def run(self):
        return self.runs[-1] if self.runs else None


class ApprovalRun(db.Model):
    __tablename__ = "approval_runs"
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), primary_key=True)
    number = db.Column(db.Integer, primary_key=True)
    route_id = db.Column(db.Integer, db.ForeignKey("routes.id"), nullable=False)
    route_name = db.Column(db.String(120), nullable=False)
    route_revision = db.Column(db.Integer, nullable=False)
    steps = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)


class Payment(db.Model):
    __tablename__ = "payments"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    date = db.Column(db.Date, nullable=False, index=True)
    amount = db.Column(db.BigInteger, nullable=False)
    order_number = db.Column(db.String(80), nullable=False)
    order_key = db.Column(db.String(160), nullable=False)
    request_id = db.Column(db.String(36), nullable=False, unique=True)
    actor = db.relationship(User)
    __table_args__ = (
        db.UniqueConstraint("invoice_id", "order_key"),
        db.CheckConstraint("amount > 0", name="positive_amount"),
    )


class Attachment(db.Model):
    __tablename__ = "attachments"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), nullable=False, index=True)
    payment_id = db.Column(db.Integer, db.ForeignKey("payments.id"))
    uploader_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    key = db.Column(db.String(40), nullable=False, unique=True)
    name = db.Column(db.String(240), nullable=False)
    media = db.Column(db.String(100), nullable=False)
    size = db.Column(db.Integer, nullable=False)


class Audit(db.Model):
    __tablename__ = "audit_events"
    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id"), index=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(40), nullable=False)
    detail = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
    actor = db.relationship(User)


class LoginWindow(db.Model):
    __tablename__ = "login_windows"
    key = db.Column(db.String(66), primary_key=True)
    count = db.Column(db.Integer, nullable=False)
    expires_at = db.Column(db.BigInteger, nullable=False, index=True)


class LoginSession(db.Model):
    __tablename__ = "login_sessions"
    key = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    expires_at = db.Column(db.BigInteger, nullable=False, index=True)
