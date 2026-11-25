import io
import struct
import re
import warnings
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote
from uuid import uuid4

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from flask import current_app
from PIL import Image
from pypdf import PdfReader
from pypdf.errors import PyPdfError
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

from .validation import InputError

MAX_SIZE = 10 * 1024 * 1024
MEDIA = {
    ".pdf": "application/pdf", ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".jpg": "image/jpeg", ".png": "image/png",
}


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class Document:
    key: str
    name: str
    media: str
    data: bytes


def verify_xlsx(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        names = {member.filename for member in members}
        required = {"[Content_Types].xml", "_rels/.rels", "xl/workbook.xml"}
        if len(members) > 800 or len(names) != len(members) or sum(m.file_size for m in members) > 30 * 1024 * 1024:
            raise ValueError
        if not required.issubset(names):
            raise ValueError
        for member in members:
            path = PurePosixPath(member.filename)
            lower = member.filename.lower()
            if (path.is_absolute() or ".." in path.parts or "\\" in member.filename or member.flag_bits & 1
                    or lower.startswith(("xl/externallinks/", "xl/embeddings/", "xl/activex/"))
                    or lower.endswith((".bin", ".exe", ".vbs", ".js"))):
                raise ValueError
            if lower.endswith((".xml", ".rels")):
                with archive.open(member) as stream:
                    root = ElementTree.parse(stream).getroot()
                if any(node.tag.rsplit("}", 1)[-1] in {"ddeLink", "oleLink", "oleObject", "externalLink"}
                       for node in root.iter()):
                    raise ValueError
                if lower.endswith(".rels") and any(
                    link.get("TargetMode", "").lower() == "external" for link in root
                ):
                    raise ValueError
                if member.filename == "[Content_Types].xml" and not any(
                    item.get("PartName") == "/xl/workbook.xml"
                    and item.get("ContentType") == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
                    for item in root
                ):
                    raise ValueError


PDF_ACTIVE_KEYS = {"/AA", "/OpenAction", "/JavaScript", "/JS", "/EmbeddedFiles", "/EF", "/AF", "/XFA", "/RichMediaContent"}
PDF_ACTIVE_ACTIONS = {"/JavaScript", "/Launch", "/SubmitForm", "/ImportData", "/GoToR", "/GoToE", "/Rendition", "/FileAttachment", "/RichMedia"}


def verify_pdf(data):
    if not data.startswith(b"%PDF-"):
        raise ValueError
    pdf = PdfReader(io.BytesIO(data), strict=True)
    if pdf.is_encrypted or "/Pages" not in pdf.root_object:
        raise ValueError
    pending = [pdf.root_object]
    visited = set()
    for _ in range(100_000):
        if not pending:
            return
        node = pending.pop()
        if isinstance(node, IndirectObject):
            key = (node.idnum, node.generation)
            if key not in visited:
                visited.add(key)
                pending.append(node.get_object())
        elif isinstance(node, DictionaryObject):
            for key in ("/S", "/Subtype"):
                value = node.get(key)
                if isinstance(value, IndirectObject):
                    value = value.get_object()
                if isinstance(value, NameObject) and value in PDF_ACTIVE_ACTIONS:
                    raise ValueError
            if PDF_ACTIVE_KEYS.intersection(node):
                raise ValueError
            pending.extend(node.values())
        elif isinstance(node, ArrayObject):
            pending.extend(node)
    raise ValueError


def validate(upload):
    name = upload.filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    ext = Path(name).suffix.lower()
    if ext not in MEDIA or len(name) > 240 or re.search(r"[\x00-\x1f\x7f]", name):
        raise InputError("Допустимые форматы документов: PDF, XLSX, JPG и PNG.")
    data = upload.stream.read(MAX_SIZE + 1)
    if not data or len(data) > MAX_SIZE:
        raise InputError("Размер документа должен быть от 1 байта до 10 МБ.")
    try:
        if ext == ".pdf":
            verify_pdf(data)
        elif ext == ".xlsx":
            verify_xlsx(data)
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(data)) as picture:
                    if picture.format != {".jpg": "JPEG", ".png": "PNG"}[ext]:
                        raise ValueError
                    if picture.width * picture.height > 20_000_000:
                        raise ValueError
                    picture.verify()
                with Image.open(io.BytesIO(data)) as picture:
                    picture.load()
    except (ValueError, OSError, SyntaxError, RuntimeError, LookupError, TypeError, OverflowError, struct.error, PyPdfError, zipfile.BadZipFile,
            Image.DecompressionBombError, Image.DecompressionBombWarning, ElementTree.ParseError, DefusedXmlException) as exc:
        raise InputError("Содержимое документа повреждено, небезопасно или не соответствует расширению.") from exc
    return Document(uuid4().hex, name, MEDIA[ext], data)


def cloud_url(path):
    return f'{current_app.config["SUPABASE_URL"].rstrip("/")}/storage/v1/{path}'


def bucket_path():
    return quote(current_app.config["SUPABASE_BUCKET"], safe="")


def object_path(key):
    if not re.fullmatch(r"[0-9a-f]{32}", key):
        raise StorageError("Invalid private object key")
    return f"object/{bucket_path()}/{key}"


def cloud_request(method, path, **kwargs):
    key = current_app.config["SUPABASE_SERVICE_KEY"]
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    headers.update(kwargs.pop("headers", {}))
    try:
        response = httpx.request(method, cloud_url(path), headers=headers, timeout=15, **kwargs)
        response.raise_for_status()
        return response
    except httpx.HTTPError as exc:
        current_app.logger.warning("Private storage request failed (%s)", type(exc).__name__)
        raise StorageError("Supabase Storage недоступен или отклонил запрос.") from None


def require_private_bucket():
    response = cloud_request("GET", f"bucket/{bucket_path()}")
    try:
        bucket = response.json()
    except ValueError:
        raise StorageError("Supabase вернул некорректные настройки bucket.") from None
    if not isinstance(bucket, dict) or bucket.get("public") is not False:
        raise StorageError("Выберите закрытый bucket Supabase Storage.")


def put(document):
    if current_app.config["SUPABASE_URL"]:
        require_private_bucket()
        cloud_request("POST", object_path(document.key), content=document.data,
                      headers={"Content-Type": document.media, "x-upsert": "false"})
    else:
        object_path(document.key)
        path = Path(current_app.config["UPLOAD_DIRECTORY"])
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            with (path / document.key).open("xb") as stream:
                stream.write(document.data)
        except OSError as exc:
            raise StorageError("Локальное хранилище недоступно.") from exc


def get(key):
    path = object_path(key)
    if current_app.config["SUPABASE_URL"]:
        require_private_bucket()
        return cloud_request("GET", path).content
    try:
        return (Path(current_app.config["UPLOAD_DIRECTORY"]) / key).read_bytes()
    except OSError as exc:
        raise StorageError("Локальное хранилище недоступно.") from exc


def remove(key):
    path = object_path(key)
    if current_app.config["SUPABASE_URL"]:
        cloud_request("DELETE", path)
    else:
        try:
            (Path(current_app.config["UPLOAD_DIRECTORY"]) / key).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError("Не удалось удалить файл.") from exc


def check_anonymous_access(key):
    path = object_path(key)
    roles = [{}]
    anon_key = current_app.config["SUPABASE_ANON_KEY"]
    if anon_key:
        if anon_key == current_app.config["SUPABASE_SERVICE_KEY"]:
            raise StorageError("SUPABASE_ANON_KEY должен принадлежать роли anon.")
        roles.append({"apikey": anon_key, "Authorization": f"Bearer {anon_key}"})
    for endpoint in (path, f"object/public/{bucket_path()}/{key}"):
        for headers in roles:
            try:
                response = httpx.request("GET", cloud_url(endpoint), headers=headers, timeout=15)
            except httpx.HTTPError:
                raise StorageError("Не удалось проверить анонимный доступ.") from None
            if response.status_code not in {400, 401, 403, 404}:
                raise StorageError("Анонимное скачивание не отклонено ожидаемым образом.")


def verify_cloud_storage():
    """Test an isolated object and remove it even if a later check fails."""
    if not current_app.config["SUPABASE_URL"] or not current_app.config["SUPABASE_SERVICE_KEY"]:
        raise StorageError("Укажите SUPABASE_URL и SUPABASE_SERVICE_KEY.")
    require_private_bucket()
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(buffer, format="PNG")
    document = Document(uuid4().hex, "vaim-storage-check.png", "image/png", buffer.getvalue())
    try:
        put(document)
        if get(document.key) != document.data:
            raise StorageError("Скачанный файл отличается от загруженного.")
        check_anonymous_access(document.key)
    finally:
        try:
            remove(document.key)
        except StorageError as exc:
            raise StorageError(f"Не подтверждено удаление тестового объекта {document.key}.") from exc
