import io
import zipfile

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter
from werkzeug.datastructures import FileStorage

from vaim import create_app, storage
from vaim.validation import InputError

SECRET_FIXTURE = "sb_" "secret_fixture"
PUBLISHABLE_FIXTURE = "sb_" "publishable_fixture"


def pdf(active=False):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    if active:
        writer.add_js("app.alert('test')")
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def xlsx(external=False):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr("[Content_Types].xml",
                         '<Types><Override PartName="/xl/workbook.xml" '
                         'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                         '</Types>')
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("_rels/.rels",
                         '<Relationships><Relationship Target="https://example.invalid" TargetMode="External"/>'
                         '</Relationships>' if external else "<Relationships/>")
    return result.getvalue()


def jpeg():
    result = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(result, format="JPEG")
    return result.getvalue()


@pytest.mark.parametrize(("name", "content", "media"), [
    ("invoice.pdf", pdf(), "application/pdf"),
    ("invoice.xlsx", xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("invoice.jpg", jpeg(), "image/jpeg"),
])
def test_allowed_document_formats(name, content, media):
    document = storage.validate(FileStorage(io.BytesIO(content), filename=name))
    assert document.name == name and document.media == media
    assert document.data == content


@pytest.mark.parametrize(("name", "content"), [("active.pdf", pdf(active=True)), ("external.xlsx", xlsx(external=True))])
def test_active_or_external_content_is_rejected(name, content):
    with pytest.raises(InputError):
        storage.validate(FileStorage(io.BytesIO(content), filename=name))


def test_supabase_private_storage_contract(monkeypatch):
    app = create_app({
        "TESTING": True, "VAIM_ENV": "testing", "SUPABASE_URL": "https://storage.example.invalid",
        "SUPABASE_SECRET_KEY": SECRET_FIXTURE,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
    })
    calls = []
    document = storage.Document("a" * 32, "invoice.pdf", "application/pdf", b"test-data")

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        req = httpx.Request(method, url)
        if "/bucket/" in url:
            return httpx.Response(200, json={"public": False}, request=req)
        return httpx.Response(200, content=document.data, request=req)

    monkeypatch.setattr(storage.httpx, "request", request)
    with app.app_context():
        storage.put(document)
        assert storage.get(document.key) == document.data
        storage.remove(document.key)
    root = "https://storage.example.invalid/storage/v1"
    bucket = f"{root}/bucket/documents"
    object_url = f"{root}/object/documents/{document.key}"
    assert [(method, url) for method, url, _ in calls] == [
        ("GET", bucket), ("POST", object_url), ("GET", bucket),
        ("GET", f"{root}/object/authenticated/documents/{document.key}"), ("DELETE", object_url),
    ]
    for method, _, options in calls:
        expected = {"apikey": SECRET_FIXTURE}
        if method == "POST":
            expected.update({"Content-Type": document.media, "x-upsert": "false"})
            assert options["content"] == document.data
        assert options["headers"] == expected
        assert options["follow_redirects"] is False


@pytest.mark.parametrize("public", [True, None, "false"])
@pytest.mark.parametrize("operation", ["put", "get"])
def test_cloud_rejects_unconfirmed_private_bucket(monkeypatch, public, operation):
    app = create_app({"TESTING": True, "SUPABASE_URL": "https://storage.example.invalid",
                      "SUPABASE_SECRET_KEY": SECRET_FIXTURE})
    methods = []

    def request(method, url, **kwargs):
        methods.append(method)
        return httpx.Response(200, json={"public": public}, request=httpx.Request(method, url))

    monkeypatch.setattr(storage.httpx, "request", request)
    document = storage.Document("a" * 32, "sample.png", "image/png", b"sample")
    with app.app_context(), pytest.raises(storage.StorageError):
        if operation == "put":
            storage.put(document)
        else:
            storage.get(document.key)
    assert methods == ["GET"]


@pytest.mark.parametrize(("credential", "anonymous_status"), [(None, 403), (PUBLISHABLE_FIXTURE, 403), (PUBLISHABLE_FIXTURE, 200)])
def test_verify_storage_checks_anonymous_access_and_cleans_up(monkeypatch, credential, anonymous_status):
    app = create_app({"TESTING": True, "SUPABASE_URL": "https://storage.example.invalid",
                      "SUPABASE_SECRET_KEY": SECRET_FIXTURE, "SUPABASE_PUBLISHABLE_KEY": credential})
    objects = {}
    anonymous_paths = []

    def request(method, url, **kwargs):
        req = httpx.Request(method, url)
        if "/bucket/" in url:
            return httpx.Response(200, json={"public": False}, request=req)
        key = url.rsplit("/", 1)[-1]
        assert kwargs["follow_redirects"] is False
        headers = kwargs["headers"]
        if headers.get("apikey") != SECRET_FIXTURE:
            assert headers in ({}, {"apikey": credential})
            assert method == "GET" and key in objects
            anonymous_paths.append((req.url.path.rsplit("/", 1)[0], headers.get("apikey")))
            status = anonymous_status if headers.get("apikey") == credential else 403
            return httpx.Response(status, request=req)
        if method == "POST":
            objects[key] = kwargs["content"]
        elif method == "DELETE":
            objects.pop(key)
        else:
            return httpx.Response(200, content=objects[key], request=req)
        return httpx.Response(200, request=req)

    monkeypatch.setattr(storage.httpx, "request", request)
    result = app.test_cli_runner().invoke(args=["verify-storage"])
    assert result.exit_code == (0 if anonymous_status == 403 else 1), result.output
    assert objects == {}
    if anonymous_status == 403:
        roles = [None, credential] if credential else [None]
        assert anonymous_paths == [
            (f"/storage/v1/object/{segment}documents", key)
            for segment in ("", "authenticated/", "public/")
            for key in roles
        ]


@pytest.mark.parametrize(("variable", "value"), [
    ("SUPABASE_SECRET_KEY", "legacy-jwt-placeholder"),
    ("SUPABASE_SECRET_KEY", PUBLISHABLE_FIXTURE),
    ("SUPABASE_PUBLISHABLE_KEY", SECRET_FIXTURE),
])
def test_supabase_rejects_wrong_key_type_without_echoing_value(variable, value):
    with pytest.raises(RuntimeError, match=variable) as exc:
        create_app({variable: value})
    assert value not in str(exc.value)


def test_storage_does_not_use_legacy_environment_key(monkeypatch):
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "legacy-placeholder")
    app = create_app({"SUPABASE_URL": "https://storage.example.invalid"})
    monkeypatch.setattr(storage.httpx, "request", lambda *args, **kwargs: pytest.fail("Unexpected network request"))
    with app.app_context(), pytest.raises(storage.StorageError, match="SUPABASE_SECRET_KEY"):
        storage.require_private_bucket()


def test_storage_denies_redirect_without_forwarding_secret(monkeypatch, caplog):
    app = create_app({"SUPABASE_URL": "https://storage.example.invalid", "SUPABASE_SECRET_KEY": SECRET_FIXTURE})
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(307, headers={"Location": "https://other.example.invalid"})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(storage.httpx, "request", client.request)
        with app.app_context(), pytest.raises(storage.StorageError) as exc:
            storage.require_private_bucket()
    assert len(requests) == 1
    assert requests[0].url.host == "storage.example.invalid"
    assert SECRET_FIXTURE not in caplog.text + str(exc.value)
