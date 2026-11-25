# VAIM Control v0.1.2

Веб-приложение ВАИМ для согласования счетов и регистрации оплаты. Первый модуль охватывает путь от черновика до полного погашения, закрытые вложения, задачи по ролям и аудит.

## Локальный запуск

Нужен Python 3.12. Из корня репозитория выполните:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
python -c 'from secrets import token_hex; print(token_hex(32))'
```

Впишите полученное значение в `SECRET_KEY` в локальном `.env`. При отсутствии `DATABASE_URL` используется `instance/vaim.sqlite3`, документы помещаются в `instance/documents`.

```bash
python -m flask --app vaim db upgrade
python -m flask --app vaim create-admin
python -m flask --app vaim run --port 5000
```

Команда создания администратора интерактивно запрашивает email, имя и пароль. Локальный адрес приложения — `http://127.0.0.1:5000`. По умолчанию администратор настраивает процесс. Для создания счетов назначьте роль инициатора, для регистрации оплаты — роль бухгалтера.

## Рабочий цикл

1. Администратор создаёт сотрудников и маршрут. В каждом этапе можно выбрать конкретного сотрудника или роль. Для согласования своего счёта нужно отдельное разрешение этапа. Руководитель, назначенный по роли, получает этапы своего подразделения.
2. Инициатор добавляет контрагента, сохраняет счёт, прикрепляет документы и отправляет счёт на согласование.
3. Исполнители проходят этапы по порядку. При возврате или отклонении требуется комментарий. После возврата инициатор может изменить реквизиты и отправить счёт заново.
4. Бухгалтер регистрирует платежи по дате, сумме и номеру поручения. Остаток определяет статус счёта. Приложение регистрирует факт оплаты, банковский перевод выполняется отдельно.

## Тесты и демонстрационные данные

```bash
python -m pytest -q
```

Тесты по умолчанию используют временные SQLite-базы. Для повторного прогона на PostgreSQL задайте `VAIM_TEST_POSTGRES_URL` для отдельной тестовой базы. Пользователю базы нужно право создавать схемы. Один тест использует одну временную схему и удаляет её после прогона. CI запускает оба варианта.

Для демонстрации используйте отдельную пустую SQLite-базу:

```bash
export VAIM_ENV=testing
export DATABASE_URL=sqlite:////tmp/vaim-demo.sqlite3
python -m flask --app vaim db upgrade
python -m flask --app vaim seed-demo
python -m flask --app vaim run --port 5001
```

Команда печатает тестовый пароль. Доступны `admin@demo.invalid`, `initiator@demo.invalid`, `manager@demo.invalid`, `finance@demo.invalid`, `director@demo.invalid`, `accountant@demo.invalid` и `observer@demo.invalid`. Команда требует `VAIM_ENV=testing`, пустую базу и SQLite.

## Подготовка облака

Пошаговая настройка нового проекта описана в [инструкции Supabase и Render Free](docs/DEPLOYMENT.md). Запуск в Render использует PostgreSQL по TLS, закрытый bucket `documents` и серверный ключ Supabase `sb_secret_` в переменной `SUPABASE_SECRET_KEY`. Миграции и первый администратор создаются отдельно до запуска сервиса. Команды `python -m flask --app vaim verify-db` и `python -m flask --app vaim verify-storage` проверяют настройки по локальному окружению.

## Документация

- [Архитектура](docs/ARCHITECTURE.md)
- [База данных](docs/DATABASE.md)
- [Безопасность](docs/SECURITY.md)
- [Render и Supabase](docs/DEPLOYMENT.md)
- [Дорожная карта](docs/ROADMAP.md)
- [Технические решения](docs/DECISIONS.md)
- [История версий](CHANGELOG.md)
