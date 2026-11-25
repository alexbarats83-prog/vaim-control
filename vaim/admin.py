from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from .access import ROLES, can, event, require
from .auth import email_address, password_hash
from .models import Audit, Counterparty, Role, Route, User, db
from .validation import InputError, integer, text

bp = Blueprint("admin", __name__)


def save_failure(exc):
    db.session.rollback()
    if isinstance(exc, StaleDataError):
        abort(409)
    flash(str(exc) if isinstance(exc, InputError) else "Запись с такими данными уже существует.", "error")


@bp.get("/employees")
@require("manage")
def employees():
    return render_template("employees.html", employees=db.session.scalars(select(User).order_by(User.name)).all())


@bp.route("/employees/new", defaults={"user_id": None}, methods=["GET", "POST"])
@bp.route("/employees/<int:user_id>", methods=["GET", "POST"])
@require("manage")
def employee(user_id):
    member = db.get_or_404(User, user_id) if user_id is not None else None
    if request.method == "POST":
        try:
            name = text(request.form, "name", 120)
            department = text(request.form, "department", 120)
            email = email_address(request.form.get("email", ""))
            role_ids = set(request.form.getlist("roles"))
            active = request.form.get("active") == "on"
            password = request.form.get("password", "")
            if not role_ids or not role_ids <= ROLES.keys():
                raise InputError("Назначьте хотя бы одну существующую роль.")
            if member and member.id == g.user.id and (not active or "admin" not in role_ids):
                raise InputError("Нельзя заблокировать свою учётную запись или снять с неё роль администратора.")
            values = dict(name=name, department=department, email=email, active=active,
                          roles=[db.session.get(Role, role) for role in sorted(role_ids)])
            if password or member is None:
                values["password_hash"] = password_hash(password)
            if member is None:
                member = User(**values)
                db.session.add(member)
            else:
                expected = integer(request.form.get("revision"))
                values["session_version"] = User.session_version + 1
                assigned_roles = values.pop("roles")
                result = db.session.execute(update(User).where(
                    User.id == member.id, User.session_version == expected,
                ).values(**values))
                if result.rowcount != 1:
                    abort(409)
                member.roles = assigned_roles
            db.session.flush()
            event("employee_created" if user_id is None else "employee_updated",
                  employee=member.id, enabled=active, roles=sorted(role_ids))
            db.session.commit()
            flash("Данные сотрудника сохранены.", "success")
            return redirect(url_for("admin.employees"))
        except (InputError, IntegrityError) as exc:
            save_failure(exc)
    return render_template("employee.html", member=member)


@bp.get("/roles")
@require("manage")
def roles():
    return render_template("roles.html")


@bp.get("/routes")
@require("manage")
def routes():
    return render_template("routes.html", routes=db.session.scalars(select(Route).order_by(Route.name)).all())


def read_steps():
    labels = request.form.getlist("step_name")
    targets = request.form.getlist("step_target")
    if not 1 <= len(labels) == len(targets) <= 20:
        raise InputError("Маршрут должен содержать от 1 до 20 этапов.")
    choices = request.form.getlist("step_self_approval") or ["0"] * len(labels)
    if len(choices) != len(labels) or any(value not in {"0", "1"} for value in choices):
        raise InputError("Проверьте разрешения на согласование своего счёта.")
    steps = []
    for label, target, self_approval in zip(labels, targets, choices):
        label = text({"label": label}, "label", 100)
        kind, _, key = target.partition(":")
        if kind == "role" and key in ROLES and "approve" in ROLES[key][1]:
            steps.append({"name": label, "role": key})
        elif kind == "user":
            member = db.session.get(User, integer(key))
            if member is None or not member.active or not can("comment", member):
                raise InputError("Выберите активного сотрудника.")
            steps.append({"name": label, "user": member.id, "assignee": member.name})
        else:
            raise InputError("Выберите исполнителя для каждого этапа.")
        if self_approval == "1":
            steps[-1]["allow_self_approval"] = True
    return steps


@bp.route("/routes/new", defaults={"route_id": None}, methods=["GET", "POST"])
@bp.route("/routes/<int:route_id>", methods=["GET", "POST"])
@require("manage")
def route(route_id):
    item = db.get_or_404(Route, route_id) if route_id is not None else None
    if request.method == "POST":
        try:
            name = text(request.form, "name", 120)
            steps = read_steps()
            active = request.form.get("active") == "on"
            if item is None:
                item = Route(name=name, steps=steps, active=active)
                db.session.add(item)
            else:
                if item.revision != integer(request.form.get("revision")):
                    abort(409)
                item.name, item.steps, item.active = name, steps, active
            db.session.flush()
            event("route_updated", route=item.id, version=item.revision)
            db.session.commit()
            flash("Маршрут сохранён.", "success")
            return redirect(url_for("admin.routes"))
        except (InputError, IntegrityError, StaleDataError) as exc:
            save_failure(exc)
    members = db.session.scalars(select(User).order_by(User.name))
    return render_template("route.html", item=item, members=[member for member in members if can("comment", member)])


@bp.route("/counterparties", methods=["GET", "POST"])
@require("directory")
def counterparties():
    if request.method == "POST":
        try:
            name = text(request.form, "name", 160)
            inn = text(request.form, "inn", 12)
            kpp = text(request.form, "kpp", 9, required=False)
            if not (inn.isascii() and inn.isdecimal() and len(inn) in {10, 12}) or (
                kpp and not (kpp.isascii() and kpp.isdecimal() and len(kpp) == 9)
            ):
                raise InputError("Укажите ИНН из 10 или 12 цифр и КПП из 9 цифр при наличии.")
            counterparty = Counterparty(name=name, inn=inn, kpp=kpp)
            db.session.add(counterparty)
            db.session.flush()
            event("counterparty_created", counterparty=counterparty.id)
            db.session.commit()
            flash("Контрагент добавлен.", "success")
            return redirect(url_for("admin.counterparties"))
        except (InputError, IntegrityError) as exc:
            save_failure(exc)
    items = db.session.scalars(select(Counterparty).order_by(Counterparty.name)).all()
    return render_template("counterparties.html", items=items)


@bp.get("/settings")
@require("manage")
def settings():
    return render_template("settings.html")


@bp.get("/audit")
@require("audit")
def audit():
    page = db.paginate(select(Audit).order_by(Audit.id.desc()), per_page=35, error_out=False)
    return render_template("audit.html", page=page)
