from io import BytesIO
from unicodedata import normalize
from uuid import UUID, uuid4

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_file, url_for
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm.exc import StaleDataError

from . import storage
from .access import PAYABLE, STATUSES, can, can_approve, can_edit, eligible_approver, event, require, visible
from .models import ApprovalRun, Attachment, Audit, Counterparty, Invoice, Payment, Route, User, db
from .validation import CURRENCIES, PRIORITIES, InputError, choice, date_value, integer, money, text, today

bp = Blueprint("invoices", __name__, url_prefix="/invoices")
VIEWS = {"all": "Реестр счетов", "review": "На согласовании", "pay": "На оплату", "paid": "Оплаченные"}


def get_invoice(invoice_id):
    item = db.get_or_404(Invoice, invoice_id)
    if not visible(item, g.user):
        abort(404)
    return item


def lock(item):
    if item.revision != integer(request.form.get("revision")):
        abort(409)
    item.revision += 1
    db.session.flush()


def mutate(invoice_id, operation):
    stored = []
    committing = False
    try:
        item = get_invoice(invoice_id)
        operation(item, stored)
        db.session.flush()
        committing = True
        db.session.commit()
    except (InputError, DBAPIError, StaleDataError, storage.StorageError) as exc:
        db.session.rollback()
        commit_unknown = committing and isinstance(exc, DBAPIError) and exc.connection_invalidated
        for key in stored:
            if commit_unknown:
                current_app.logger.warning("Private document requires reconciliation: %s", key)
                continue
            try:
                storage.remove(key)
            except storage.StorageError:
                current_app.logger.warning("Orphaned private document: %s", key)
        if isinstance(exc, InputError):
            flash(str(exc), "error")
            return redirect(url_for("invoices.detail", invoice_id=invoice_id))
        if isinstance(exc, storage.StorageError):
            abort(503)
        if isinstance(exc, DBAPIError):
            code = getattr(exc.orig, "sqlstate", None)
            conflict = code in {"40001", "40P01"} or (
                isinstance(exc, OperationalError) and "database is locked" in str(exc.orig).lower()
            )
            if not isinstance(exc, IntegrityError) and not conflict:
                abort(503)
        abort(409)
    return redirect(url_for("invoices.detail", invoice_id=invoice_id))


def add_document(item, stored, payment=None, required=True):
    upload = request.files.get("document")
    if not upload or not upload.filename:
        if required:
            raise InputError("Выберите файл.")
        return
    document = storage.validate(upload)
    stored.append(document.key)
    storage.put(document)
    db.session.add(Attachment(invoice_id=item.id, payment_id=payment.id if payment else None,
                              uploader_id=g.user.id, key=document.key, name=document.name,
                              media=document.media, size=len(document.data)))
    event("uploaded", item, file=document.name)


def invoice_fields(department):
    form = request.form
    counterparty = db.session.get(Counterparty, integer(form.get("counterparty_id")))
    route = db.session.get(Route, integer(form.get("route_id")))
    if counterparty is None or route is None or not route.active:
        raise InputError("Выберите контрагента и активный маршрут.")
    amount = money(form.get("amount", ""))
    vat = money(form.get("vat", "") or "0")
    invoice_date = date_value(form.get("invoice_date"))
    due_date = date_value(form.get("due_date"))
    if not amount or vat > amount or due_date < invoice_date:
        raise InputError("Проверьте сумму, НДС и срок оплаты. Срок не может быть раньше даты счёта.")
    return dict(
        number=text(form, "number", 80), invoice_date=invoice_date, due_date=due_date,
        entity=text(form, "entity", 160), department=department,
        counterparty=counterparty, route=route, amount=amount, vat=vat,
        currency=choice(form.get("currency"), CURRENCIES), expense=text(form, "expense", 120),
        purpose=text(form, "purpose", 2000), priority=choice(form.get("priority"), PRIORITIES),
    )


@bp.get("/")
@require()
def listing():
    view = request.args.get("view", "all")
    if view not in VIEWS:
        abort(404)
    if view == "pay" and not (can("pay") or can("view_all")):
        abort(403)
    stmt = select(Invoice).order_by(Invoice.id.desc())
    filters = {"review": {"review"}, "pay": PAYABLE, "paid": {"paid"}}
    if view in filters:
        stmt = stmt.where(Invoice.status.in_(filters[view]))
    status = request.args.get("status")
    if view == "all" and status in STATUSES:
        stmt = stmt.where(Invoice.status == status)
    query = request.args.get("q", "").strip()[:80]
    if query and "\x00" not in query:
        stmt = stmt.where(Invoice.number.icontains(query, autoescape=True) | Invoice.counterparty.has(
            Counterparty.name.icontains(query, autoescape=True)
        ))
    items = [item for item in db.session.scalars(stmt) if visible(item, g.user)]
    page = request.args.get("page", 1, type=int)
    page = max(1, min(page, (len(items) + 24) // 25 or 1))
    return render_template("invoices.html", items=items[(page - 1) * 25:page * 25], count=len(items),
                           title=VIEWS[view], page=page, view=view)


@bp.route("/new", defaults={"invoice_id": None}, methods=["GET", "POST"])
@bp.route("/<int:invoice_id>/edit", methods=["GET", "POST"])
@require("create")
def edit(invoice_id):
    item = get_invoice(invoice_id) if invoice_id is not None else None
    if item and not can_edit(item, g.user):
        abort(403)
    if request.method == "POST":
        try:
            fields = invoice_fields(item.department if item else g.user.department)
            if item is None:
                item = Invoice(**fields, initiator_id=g.user.id)
                db.session.add(item)
                db.session.flush()
                event("created", item)
            else:
                lock(item)
                for key, value in fields.items():
                    setattr(item, key, value)
                event("edited", item)
            db.session.commit()
            flash("Счёт сохранён.", "success")
            return redirect(url_for("invoices.detail", invoice_id=item.id))
        except InputError as exc:
            db.session.rollback()
            flash(str(exc), "error")
        except StaleDataError:
            db.session.rollback()
            abort(409)
    return render_template("invoice_form.html", item=item, currencies=CURRENCIES,
                           counterparties=db.session.scalars(select(Counterparty).order_by(Counterparty.name)).all(),
                           routes=db.session.scalars(select(Route).where(Route.active).order_by(Route.name)).all())


@bp.get("/<int:invoice_id>")
@require()
def detail(invoice_id):
    item = get_invoice(invoice_id)
    return render_template(
        "invoice.html", item=item, editable=can_edit(item, g.user), approvable=can_approve(item, g.user),
        payable=can("pay") and item.status in PAYABLE, payment_token=str(uuid4()),
        history=db.session.scalars(select(Audit).where(Audit.invoice_id == item.id).order_by(Audit.id.desc())).all(),
        payments=db.session.scalars(select(Payment).where(Payment.invoice_id == item.id).order_by(Payment.id)).all(),
        documents=db.session.scalars(select(Attachment).where(Attachment.invoice_id == item.id).order_by(Attachment.id)).all(),
    )


def submit(item):
    if not item.route.active or not item.route.steps:
        raise InputError("Выберите активный маршрут с этапами.")
    active = db.session.scalars(select(User).where(User.active)).all()
    for step in item.route.steps:
        if not any(eligible_approver(step, item, user) for user in active):
            raise InputError("В маршруте есть этап без активных исполнителей с необходимыми правами.")
    item.round += 1
    item.current_step = 0
    item.status = "review"
    db.session.add(ApprovalRun(invoice_id=item.id, number=item.round, route_id=item.route.id,
                               route_name=item.route.name, route_revision=item.route.revision, steps=item.route.steps))
    event("submitted", item, round=item.round, route=item.route.name, version=item.route.revision)


@bp.post("/<int:invoice_id>/action")
@require()
def action(invoice_id):
    command = request.form.get("action")

    def operation(item, _stored):
        lock(item)
        if command in {"submit", "cancel"}:
            if not can_edit(item, g.user):
                abort(403)
            if command == "submit":
                submit(item)
            else:
                item.status = "cancelled"
                event("cancelled", item)
        elif command in {"approve", "reject", "return"}:
            if not can_approve(item, g.user):
                abort(403)
            comment = text(request.form, "comment", 2000, required=command != "approve")
            step = item.run.steps[item.current_step]
            event(command, item, round=item.round, step=step.get("name") or item.current_step + 1,
                  comment=comment, initiator_action=g.user.id == item.initiator_id)
            if command in {"reject", "return"}:
                item.status = "rejected" if command == "reject" else "returned"
            else:
                item.current_step += 1
                if item.current_step == len(item.run.steps):
                    event("approved", item)
                    item.status = "awaiting"
                    event("awaiting", item)
        elif command == "comment":
            if not can("comment"):
                abort(403)
            event("comment", item, comment=text(request.form, "comment", 2000))
        else:
            abort(400)

    return mutate(invoice_id, operation)


@bp.post("/<int:invoice_id>/payments")
@require("pay")
def pay(invoice_id):
    def operation(item, stored):
        lock(item)
        if item.status not in PAYABLE:
            abort(409)
        form = request.form
        amount = money(form.get("amount", ""))
        payment_date = date_value(form.get("date"))
        order = text(form, "order_number", 80)
        order_key = normalize("NFKC", order).casefold()
        try:
            token = str(UUID(form.get("request_id", "")))
        except ValueError:
            abort(400)
        if not 0 < amount <= item.amount - item.paid or payment_date > today():
            raise InputError("Проверьте сумму остатка и дату платежа.")
        if len(order_key) > 160:
            raise InputError("Проверьте номер платёжного поручения.")
        payment = Payment(invoice_id=item.id, actor_id=g.user.id, amount=amount, date=payment_date,
                          order_number=order, order_key=order_key, request_id=token)
        db.session.add(payment)
        db.session.flush()
        add_document(item, stored, payment=payment, required=False)
        item.paid += amount
        item.status = "paid" if item.paid == item.amount else "partial"
        event("payment", item, amount=amount, order=order, payment=payment.id, status=item.status)
    return mutate(invoice_id, operation)


@bp.post("/<int:invoice_id>/documents")
@require("create")
def upload(invoice_id):
    def operation(item, stored):
        if not can_edit(item, g.user):
            abort(403)
        lock(item)
        add_document(item, stored)
    return mutate(invoice_id, operation)


@bp.get("/<int:invoice_id>/documents/<int:document_id>")
@require()
def download(invoice_id, document_id):
    item = get_invoice(invoice_id)
    document = db.get_or_404(Attachment, document_id)
    if document.invoice_id != item.id:
        abort(404)
    try:
        data = storage.get(document.key)
    except storage.StorageError:
        abort(503)
    event("downloaded", item, file=document.name)
    db.session.commit()
    return send_file(BytesIO(data), mimetype=document.media, as_attachment=True, download_name=document.name)
