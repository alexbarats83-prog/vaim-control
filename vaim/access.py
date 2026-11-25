from functools import wraps

from flask import abort, g, redirect, request, url_for
from sqlalchemy import select

from .models import Audit, User, db

ROLES = {
    "admin": ("Администратор", {"manage", "audit", "view_all", "reports", "directory", "comment"}),
    "initiator": ("Инициатор", {"create", "directory", "comment"}),
    "manager": ("Руководитель подразделения", {"approve", "comment"}),
    "finance": ("Финансовый контролёр", {"approve", "view_all", "reports", "directory", "comment"}),
    "director": ("Директор", {"approve", "view_all", "reports", "comment"}),
    "accountant": ("Бухгалтер", {"approve", "pay", "view_all", "reports", "directory", "comment"}),
    "observer": ("Наблюдатель", {"view_all", "reports"}),
}
PERMISSIONS = {
    "manage": "Администрирование", "audit": "Общий журнал", "view_all": "Все счета",
    "reports": "Отчёты", "directory": "Контрагенты", "create": "Создание счетов",
    "approve": "Согласование по назначению", "pay": "Регистрация оплаты",
    "comment": "Комментарии к доступным счетам",
}
STATUSES = {
    "draft": "Черновик", "review": "На согласовании", "returned": "На доработке",
    "rejected": "Отклонён", "approved": "Согласован", "awaiting": "Ожидает оплаты",
    "partial": "Частично оплачен", "paid": "Оплачен", "cancelled": "Отменён",
}
PAYABLE = {"awaiting", "partial"}
CLOSED = {"paid", "rejected", "cancelled"}
ACTIONS = {
    "login": "Вход выполнен", "logout": "Выход выполнен", "employee_created": "Сотрудник создан",
    "employee_updated": "Сотрудник обновлён", "route_updated": "Маршрут сохранён",
    "counterparty_created": "Контрагент добавлен", "created": "Счёт создан",
    "edited": "Счёт изменён", "submitted": "Отправлен на согласование",
    "approve": "Этап согласован", "reject": "Счёт отклонён", "return": "Возвращён на доработку",
    "approved": "Счёт согласован", "awaiting": "Передан на оплату", "comment": "Добавлен комментарий",
    "payment": "Зарегистрирован платёж", "cancelled": "Счёт отменён", "uploaded": "Загружен документ",
    "downloaded": "Документ скачан",
}


def can(permission, user=None):
    user = user or g.user
    return bool(user and any(permission in ROLES[role][1] for role in user.role_ids))


def require(permission=None):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if g.user is None:
                if request.method in {"GET", "HEAD"}:
                    return redirect(url_for("auth.login"))
                abort(401)
            if permission and not can(permission):
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorate


def assigned(step, user, department):
    if "user" in step:
        return step["user"] == user.id and can("comment", user)
    role = step["role"]
    return role in user.role_ids and (role != "manager" or user.department == department)


def visible(invoice, user):
    return (
        invoice.initiator_id == user.id
        or can("view_all", user)
        or any(assigned(step, user, invoice.department) for run in invoice.runs for step in run.steps)
        or (invoice.round > 0 and db.session.scalar(
            select(Audit.id).where(
                Audit.invoice_id == invoice.id, Audit.actor_id == user.id,
                Audit.action.in_(("approve", "reject", "return")),
            ).limit(1)
        ) is not None)
    )


def can_edit(invoice, user):
    return invoice.initiator_id == user.id and can("create", user) and invoice.status in {"draft", "returned"}


def eligible_approver(step, invoice, user):
    return (
        (user.id != invoice.initiator_id or step.get("allow_self_approval") is True)
        and assigned(step, user, invoice.department)
    )


def can_approve(invoice, user):
    return invoice.status == "review" and eligible_approver(invoice.run.steps[invoice.current_step], invoice, user)


def responsible(invoice):
    if invoice.status == "review":
        step = invoice.run.steps[invoice.current_step]
        if "user" in step:
            user = db.session.get(User, step["user"])
            return user.name if user else step["assignee"]
        return ROLES[step["role"]][0]
    if invoice.status in PAYABLE:
        return ROLES["accountant"][0]
    return invoice.initiator.name if invoice.status in {"draft", "returned"} else "—"


def event(action, invoice=None, **detail):
    db.session.add(Audit(actor_id=g.user.id if g.get("user") else None,
                         invoice_id=invoice.id if invoice else None,
                         action=action, detail=detail))
