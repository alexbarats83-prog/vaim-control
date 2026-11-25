# Настройка Supabase и бесплатного Render

Инструкция рассчитана на новый проект и отдельную тестовую установку. Для действующей базы сначала сделайте резервную копию и согласуйте перенос данных. Если версия 0.1.0 использовала схему `public`, отдельно перенесите её таблицы и историю Alembic в `vaim`. Новая установка создаёт таблицы прямо в `vaim`.

## 1. Supabase в браузере

1. Откройте [панель Supabase](https://supabase.com/dashboard), нажмите **New project** и выберите организацию. Задайте имя, ближайший доступный регион и длинный пароль базы. Сохраните пароль в менеджере паролей. Это пароль PostgreSQL, отдельный от пароля администратора приложения.
2. Откройте **SQL Editor → New query**, вставьте код и нажмите **Run**. Команды выделяют закрытую схему под приложение и отзывают доступ у ролей клиентского API.
   ```sql
   CREATE SCHEMA IF NOT EXISTS vaim AUTHORIZATION postgres;
   REVOKE ALL ON SCHEMA vaim FROM PUBLIC, anon, authenticated;
   ```
   Оставьте `vaim` вне **Exposed schemas**. В отдельном проекте VAIM можно отключить Data API через **Integrations → Data API** или **Project Settings → Data API**, в зависимости от версии панели. Для Storage используется отдельный API.
3. Вверху панели нажмите **Connect**. Выберите **Session pooler** и скопируйте строку URI. Используйте указанные в ней имя пользователя с суффиксом проекта, хост и порт **5432**. Shared Session pooler поддерживает IPv4 и сессии PostgreSQL. Он подходит для Render Free и для выполнения миграций с компьютера без IPv6. При доступном IPv6 для миграций можно выбрать **Direct connection**. Режим **Transaction pooler**, порт **6543**, здесь не используйте.
4. В **Database → Settings → SSL Configuration** включите требование TLS. Строка ниже задаёт шифрование как обязательное. Для проверки подлинности сервера предпочтительны `sslmode=verify-full` и доверенный CA для выбранного endpoint. Скачайте сертификат из панели там, где он применим, и проверьте цепочку и имя хоста командой `verify-db`. Для хоста с общедоверенным сертификатом libpq поддерживает `sslrootcert=system`. Для отдельного CA укажите `sslrootcert=/путь/к/ca.crt` локально и путь к Secret File в Render.
   ```text
   postgresql+psycopg://postgres.PROJECT_REF:DATABASE_PASSWORD@POOLER_HOST:5432/postgres?sslmode=require
   ```
   Замените только значения из панели. Если пароль содержит символы URL-разметки, например `@`, `:`, `/`, `#` или `%`, процентно закодируйте его в URI.
5. В **Storage** выберите созданный bucket `documents`. Для нового проекта создайте его через **New bucket**, оставьте **Public bucket** выключенным, установите предел **10 MB**. Если панель позволяет перечислить MIME-типы, разрешите `application/pdf`, `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `image/jpeg` и `image/png`.
6. В **Storage → Policies** оставьте для этого bucket доступ клиентских ролей `anon` и `authenticated` закрытым для SELECT, INSERT, UPDATE и DELETE. Для нового bucket с RLS приложения отдельные разрешающие политики не нужны. Перепроверьте, что общие политики на `storage.objects` не охватывают этот bucket. Пользователи входят в VAIM, а сервер повторно проверяет права на счёт и сам обращается к Storage. Серверный ключ обходит RLS, поэтому держите его только в менеджере секретов и окружении сервера.
7. В **Project Settings → API Keys → Publishable and secret API keys** создайте отдельный ключ приложения в разделе **Secret keys** и сохраните его в `SUPABASE_SECRET_KEY`. Ключ начинается с `sb_secret_` и передаётся сервером только в заголовке `apikey`. Для необязательной проверки роли `anon` используйте ключ `sb_publishable_` из раздела **Publishable key**. Получите URL проекта из **Connect** или настроек API. Обычно он выглядит как `https://PROJECT_REF.supabase.co`.

Официальные справки: [ключи Supabase](https://supabase.com/docs/guides/getting-started/api-keys), [подключение PostgreSQL](https://supabase.com/docs/guides/database/connecting-to-postgres), [приватные bucket](https://supabase.com/docs/guides/storage/buckets/fundamentals), [политики Storage](https://supabase.com/docs/guides/storage/security/access-control).

## 2. Первичная подготовка с компьютера

1. Установите Python 3.12, скачайте нужную версию репозитория из GitHub через **Code → Download ZIP**, распакуйте архив и откройте терминал в папке с `README.md`.
2. Создайте окружение командой `python -m venv .venv`. В macOS/Linux активируйте его через `source .venv/bin/activate`. В Windows PowerShell используйте `.\.venv\Scripts\Activate.ps1`. Установите пакеты командой `python -m pip install -r requirements.txt`.
3. Рядом с `README.md` создайте локальный файл `.env` по образцу `.env.example`. Он исключён из Git. Для общего компьютера используйте личный каталог с доступом только вашей учётной записи. Ниже перечислены переменные. Для `SECRET_KEY` сгенерируйте случайное значение командой `python -c "import secrets; print(secrets.token_hex(32))"` и храните его вместе с остальными секретами.

   | Переменная | Значение |
   | --- | --- |
   | `VAIM_ENV` | `production` для облачных PostgreSQL и Storage. |
   | `DATABASE_URL` | Строка подключения из предыдущего раздела с TLS. |
   | `SUPABASE_URL` | HTTPS URL проекта. |
   | `SUPABASE_SECRET_KEY` | Серверный ключ `sb_secret_`, только в локальном окружении или секретах Render. |
   | `SUPABASE_PUBLISHABLE_KEY` | Необязательный ключ `sb_publishable_` для локальной команды `verify-storage`. Проверяет роль `anon` отдельно от запросов без ключа. |
   | `SUPABASE_BUCKET` | `documents`. |
   | `SECRET_KEY` | Случайный ключ подписи сессий из 64 hex-символов. |
   | `TRUST_PROXY_HOPS` | `0` локально. Установите `1` на Render за его HTTPS-прокси. |
   | `WEB_CONCURRENCY` | `1` на Render Free. |
4. Проверьте подключение, затем выполните миграции и создайте первого администратора. Для существующей установки применяйте миграции до переключения версии и после резервной копии.
   ```bash
   python -m flask --app vaim verify-db
   python -m flask --app vaim db upgrade
   python -m flask --app vaim db current
   python -m flask --app vaim create-admin
   python -m flask --app vaim verify-storage
   ```
   `verify-db` подтверждает подключение с TLS и схему `vaim`. После `db current` ожидается одна строка с `(head)`. Введите email и имя администратора, затем дважды введите длинный уникальный пароль. Терминал скрывает ввод. Команда `verify-storage` проверяет закрытость bucket, загружает временный PNG, сравнивает скачанные байты, проверяет отказ чтения без учётных данных и удаляет объект. Она не меняет счета или записи PostgreSQL. Если удаление не подтверждено, команда напечатает только идентификатор временного файла для ручной проверки.

## 3. Render в браузере

1. После одобрения запуска откройте [панель Render](https://dashboard.render.com), нажмите **New → Web Service**, подключите GitHub и выберите репозиторий `vaim-control` и согласованную ветку. Первый запуск может начаться при нажатии **Create Web Service**.
2. В конфигурации выберите **Python**, **Free**, регион рядом с Supabase и корневую папку репозитория. Поле **Build Command**: `python -m pip install -r requirements.txt`. Поле **Start Command**: `gunicorn -c gunicorn.conf.py wsgi:app`. В **Health Check Path** укажите `/health`. Выключите автоматические деплои. Файл `render.yaml` содержит эквивалентные настройки для варианта **New → Blueprint**.
3. В **Environment Variables** внесите значения из таблицы. Задайте `PYTHON_VERSION=3.12.14`. При проверке TLS через отдельный CA добавьте файл `ca.crt` в **Secret Files** и укажите в `DATABASE_URL` путь `/etc/secrets/ca.crt`. Для Blueprint Render сам генерирует `SECRET_KEY`. Значение должно быть стабильным между запусками. `PORT` предоставляет Render, а Gunicorn читает его автоматически.
4. Убедитесь, что все команды первичной подготовки уже выполнены. На Free миграции и создание администратора выполняются с вашего компьютера. Конфигурация не использует Pre-Deploy Command, Shell, постоянный диск Render и создание демонстрационных пользователей. Нажмите **Create Web Service** только после одобрения.
5. В **Events** дождитесь успешного запуска. Откройте адрес сервиса с добавлением `/health`. Ожидаемый результат: `{"status":"ok"}`. Войдите первым администратором. На синтетических данных проверьте создание, согласование, загрузку и скачивание. Для проверки прав используйте отдельного пользователя без доступа к этому счёту. Если `/health` вернул 503, проверьте `DATABASE_URL`, подключение и результат миграций.
6. Для следующих обновлений сначала используйте резервную копию и примените совместимую миграцию с компьютера. После этого в Render выберите **Manual Deploy → Deploy latest commit**. Миграции PostgreSQL сериализуются блокировкой Alembic, а старт веб-процесса только запускает Gunicorn.

Free подходит для пилота и демонстрации. Сервис засыпает после простоя и использует временную файловую систему. PostgreSQL и вложения находятся в Supabase. Для реальных финансовых документов нужны эксплуатационные гарантии, резервное копирование базы и Storage, проверка восстановления и антивирусная обработка по политике компании. [Ограничения Render Free](https://render.com/docs/free) · [Развёртывания и Pre-Deploy Command](https://render.com/docs/deploys).

## Обновление с 0.1.1

Перед запуском новой версии добавьте `SUPABASE_SECRET_KEY` в окружение Render и локальный `.env`, замените `SUPABASE_ANON_KEY` на `SUPABASE_PUBLISHABLE_KEY`, если используете проверку роли `anon`. После проверки новой версии удалите старую переменную `SUPABASE_SERVICE_KEY`. Существующие значения в Render проверьте вручную даже при использовании Blueprint.

Установите `SUPABASE_BUCKET=documents`. Если прежняя установка записывала файлы в другой bucket, до переключения скопируйте объекты с сохранением имён и проверьте скачивание. Пока перенос не завершён, оставьте прежний bucket в настройках. Метаданные VAIM хранят ключ объекта, а имя bucket берут из окружения.

## Docker

```bash
docker build -t vaim-control:0.1.2 .
docker run --rm --env-file /secure/path/vaim.env vaim-control:0.1.2 flask --app vaim db upgrade
docker run --rm --env-file /secure/path/vaim.env -p 8000:8000 vaim-control:0.1.2
```

В промышленном режиме поставьте контейнер за HTTPS-прокси. Задавайте `TRUST_PROXY_HOPS=1` только когда перед Gunicorn ровно один доверенный прокси, который дополняет заголовки адреса и HTTPS. Если сборка идёт через корпоративный TLS-прокси, передайте доверенную цепочку через BuildKit: `docker build --secret id=pip_ca,src=/path/to/ca-bundle.crt -t vaim-control:0.1.2 .`.
