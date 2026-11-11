import io
import re
import zipfile
from dataclasses import dataclass

from flask import current_app, send_file
from PIL import Image, UnidentifiedImageError

from .domain import FormError


IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


@dataclass(frozen=True)
class Upload:
    kind: str
    name: str
    media_type: str
    content: bytes


def read_upload(file, kind, *, required=False):
    if file is None or not file.filename:
        if required:
            raise FormError("请选择需要提交的文件。")
        return None
    name = file.filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:160]
    if not name or name in {".", ".."}:
        raise FormError("文件名无效。")
    limit = current_app.config["MAX_CONTENT_LENGTH"]
    content = file.stream.read(limit + 1)
    if not content or len(content) > limit:
        raise FormError("请选择非空且在上传限制内的文件。")
    media_type = "application/octet-stream"
    if kind in {"logo", "cover"}:
        try:
            with Image.open(io.BytesIO(content)) as image:
                media_type = IMAGE_TYPES.get(image.format)
                if not media_type or image.width * image.height > 20_000_000:
                    raise FormError("图片须为 PNG、JPG 或 WebP，且不超过 2,000 万像素。")
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise FormError("无法读取该图片。") from error
    if kind == "patch":
        try:
            if not name.lower().endswith(".zip"):
                raise zipfile.BadZipFile
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                if not archive.namelist():
                    raise zipfile.BadZipFile
        except (zipfile.BadZipFile, UnicodeError, ValueError, NotImplementedError) as error:
            raise FormError("补丁包须为有效且非空的 ZIP 文件。") from error
    return Upload(kind, name, media_type, content)


def save_upload(db, upload, now):
    if upload is None:
        return None
    return db.execute(
        "INSERT INTO uploads (kind, name, media_type, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (upload.kind, upload.name, upload.media_type, upload.content, now),
    ).lastrowid


def serve_upload(row, *, inline=False):
    return send_file(
        io.BytesIO(row["content"]),
        mimetype=row["media_type"] if inline else "application/octet-stream",
        download_name=row["name"],
        as_attachment=not inline,
        conditional=True,
    )
