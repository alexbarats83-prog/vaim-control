from collections import defaultdict
from datetime import date

from flask import Blueprint, abort, g, render_template, request
from sqlalchemy import select

from .access import CLOSED, PAYABLE, can, can_approve, can_edit, require, visible
from .models import Audit, Invoice, Payment, db
from .validation import InputError, date_value, today

bp = Blueprint("dashboard", __name__)


def invoices():
    return [item for item in db.session.scalars(select(Invoice).order_by(Invoice.id.desc())) if visible(item, g.user)]


def tasks_for(items):
    return [item for item in items if can_edit(item, g.user) or can_approve(item, g.user)
            or (can("pay") and item.status in PAYABLE)]


def period():
    end = today()
    try:
        start = date_value(request.args["start"]) if request.args.get("start") else date(end.year, end.month, 1)
        end = date_value(request.args["end"]) if request.args.get("end") else end
    except InputError:
        abort(400)
    if start > end:
        abort(400)
    return start, end


def metrics(items, start, end):
    groups = defaultdict(lambda: dict(review=0, awaiting=0, payments=0))
    summary = dict(review=0, awaiting=0, paid=0, overdue=0)
    by_id = {item.id: item for item in items}
    totals = defaultdict(int)
    for item in items:
        bucket = groups[item.currency]
        if item.status == "review":
            summary["review"] += 1
            bucket["review"] += item.amount
        if item.status in PAYABLE:
            summary["awaiting"] += 1
            bucket["awaiting"] += item.amount - item.paid
        if item.status not in CLOSED and item.due_date < today():
            summary["overdue"] += 1
    if by_id:
        payments = db.session.execute(
            select(Payment.invoice_id, Payment.amount, Payment.date)
            .where(Payment.invoice_id.in_(by_id), Payment.date <= end)
            .order_by(Payment.date, Payment.id)
        )
        for invoice_id, amount, paid_date in payments:
            item = by_id[invoice_id]
            totals[invoice_id] += amount
            if paid_date >= start:
                groups[item.currency]["payments"] += amount
                if totals[invoice_id] == item.amount:
                    summary["paid"] += 1
    return summary, dict(sorted(groups.items()))


@bp.get("/")
@require()
def home():
    items = invoices()
    start, end = period()
    summary, groups = metrics(items, start, end)
    todo = tasks_for(items)
    recent = db.session.scalars(select(Audit).where(Audit.invoice_id.in_([item.id for item in items]))
                                .order_by(Audit.id.desc()).limit(7)).all() if items else []
    return render_template("home.html", summary=summary, groups=groups, start=start, end=end,
                           tasks=todo[:5], recent=recent)


@bp.get("/tasks")
@require()
def tasks():
    return render_template("tasks.html", items=tasks_for(invoices()))


@bp.get("/reports")
@require("reports")
def reports():
    start, end = period()
    summary, groups = metrics(invoices(), start, end)
    return render_template("reports.html", summary=summary, groups=groups, start=start, end=end)
