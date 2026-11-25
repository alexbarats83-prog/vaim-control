import re
from datetime import date, datetime
from zoneinfo import ZoneInfo


CURRENCIES = ("RUB", "USD", "EUR", "CNY")
PRIORITIES = {"normal": "Обычный", "high": "Высокий", "urgent": "Срочный"}


class InputError(ValueError):
    pass


def text(form, name, limit, required=True):
    value = form.get(name, "").strip()
    if (required and not value) or len(value) > limit or "\x00" in value:
        raise InputError("Проверьте обязательные поля и длину введённых значений.")
    return value


def integer(value):
    if not re.fullmatch(r"[0-9]{1,10}", value or "") or int(value) > 2147483647:
        raise InputError("Некорректный идентификатор.")
    return int(value)


def money(value):
    value = value.strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]{1,12}(\.[0-9]{1,2})?", value):
        raise InputError("Укажите положительное денежное значение с точностью до двух знаков.")
    whole, _, fraction = value.partition(".")
    return int(whole) * 100 + int(fraction.ljust(2, "0"))


def format_money(value):
    return f"{value // 100:,}".replace(",", "\u202f") + f",{value % 100:02d}"


def date_value(value):
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        raise InputError("Укажите корректную дату.") from None


def today():
    return datetime.now(ZoneInfo("Europe/Moscow")).date()


def choice(value, choices):
    if value not in choices:
        raise InputError("Выберите значение из списка.")
    return value
