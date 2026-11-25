import io
import zipfile

import httpx
import pytest
from PIL import Image
from pypdf import PdfWriter
from werkzeug.datastructures import FileStorage

from vaim import create_app, storage
from vaim.validation import InputError


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
        "SUPABASE_SERVICE_KEY": "test-key", "SUPABASE_BUCKET": "private-bucket",
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
    assert [method for method, _, _ in calls] == ["GET", "POST", "GET", "GET", "DELETE"]
    for _, _, options in calls:
        assert options["headers"]["Authorization"] == "Bearer test-key"
    assert calls[1][1].endswith("/storage/v1/object/private-bucket/" + document.key)
    assert calls[1][2]["headers"]["x-upsert"] == "false"


@pytest.mark.parametrize("public", [True, None, "false"])
def test_cloud_upload_rejects_unconfirmed_private_bucket(monkeypatch, public):
    app = create_app({"TESTING": True, "SUPABASE_URL": "https://storage.example.invalid",
                      "SUPABASE_SERVICE_KEY": "test-key"})
    methods = []

    def request(method, url, **kwargs):
        methods.append(method)
        return httpx.Response(200, json={"public": public}, request=httpx.Request(method, url))

    monkeypatch.setattr(storage.httpx, "request", request)
    with app.app_context(), pytest.raises(storage.StorageError):
        storage.put(storage.Document("a" * 32, "sample.png", "image/png", b"sample"))
    assert methods == ["GET"]


@pytest.mark.parametrize("anonymous_status", [403, 200])
def test_verify_storage_checks_anonymous_access_and_cleans_up(monkeypatch, anonymous_status):
    app = create_app({"TESTING": True, "SUPABASE_URL": "https://storage.example.invalid",
                      "SUPABASE_SERVICE_KEY": "test-key"})
    objects = {}

    def request(method, url, **kwargs):
        req = httpx.Request(method, url)
        if "/bucket/" in url:
            return httpx.Response(200, json={"public": False}, request=req)
        key = url.rsplit("/", 1)[-1]
        if not kwargs.get("headers"):
            return httpx.Response(anonymous_status, request=req)
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
