from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Literal, TypeAlias, TypedDict

from .message_types import MessageType


class ContentSource(TypedDict):
    kind: Literal["url", "path"]
    value: str


class TextPart(TypedDict):
    type: Literal["text"]
    text: str


class ImagePart(TypedDict):
    type: Literal["image"]
    source: ContentSource


class FilePart(TypedDict):
    type: Literal["file"]
    mime_type: str
    source: ContentSource


MessageContentPart: TypeAlias = TextPart | ImagePart | FilePart
MessageContent: TypeAlias = str | list[MessageContentPart]


def is_message_content_type(content_type: MessageType | str) -> bool:
    return str(content_type) == MessageType.MESSAGE


def validate_envelope_content(content: Any, content_type: MessageType | str) -> None:
    if is_message_content_type(content_type):
        validate_message_content(content)
        return
    if not isinstance(content, str):
        raise TypeError("Non-message envelopes require string content.")


def validate_message_content(content: Any) -> None:
    if isinstance(content, str):
        return
    if not isinstance(content, list):
        raise TypeError("Message content must be a string or a list of BOS content parts.")
    for part in content:
        validate_message_part(part)


def validate_message_part(part: Any) -> None:
    if not isinstance(part, dict):
        raise TypeError("Message content parts must be objects.")
    part_type = part.get("type")
    if part_type == "text":
        if not isinstance(part.get("text"), str):
            raise TypeError("Text parts require a string `text` field.")
        return
    if part_type == "image":
        _validate_source(part.get("source"), allow_path=True)
        return
    if part_type == "file":
        if not isinstance(part.get("mime_type"), str) or not part["mime_type"].strip():
            raise TypeError("File parts require a non-empty string `mime_type` field.")
        _validate_source(part.get("source"), allow_path=True)
        return
    raise TypeError(f"Unsupported BOS message part type: {part_type!r}")


def content_as_parts(content: MessageContent) -> list[dict[str, Any]]:
    validate_message_content(content)
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def content_to_plain_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    chunks: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            chunks.append(str(part))
            continue
        part_type = part.get("type")
        if part_type == "text":
            chunks.append(part.get("text", ""))
        elif part_type == "image":
            chunks.append("[image]")
        elif part_type == "file":
            mime_type = part.get("mime_type") or "file"
            chunks.append(f"[file:{mime_type}]")
        else:
            chunks.append(f"[{part_type or 'content'}]")
    return " ".join(chunk for chunk in chunks if chunk).strip()


def content_preview(content: Any, limit: int = 120) -> str:
    text = content_to_plain_text(content)
    if len(text) <= limit:
        return text
    return text[: max(limit - 3, 0)] + "..."


def content_length(content: Any) -> int:
    return len(content_to_plain_text(content))


def image_source_to_model_url(source: Any) -> str:
    _validate_source(source, allow_path=True)
    source_kind = source["kind"]
    source_value = source["value"]
    if source_kind == "url":
        return source_value

    path = Path(source_value).expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"Image path does not exist: {path}")

    mime_type, _ = mimetypes.guess_type(path.name)
    if not mime_type or not mime_type.startswith("image/"):
        raise ValueError(f"Image path must resolve to an image MIME type: {path}")

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _validate_source(source: Any, *, allow_path: bool) -> None:
    if not isinstance(source, dict):
        raise TypeError("Image/file parts require a `source` object.")
    allowed_kinds = {"url", "path"} if allow_path else {"url"}
    if source.get("kind") not in allowed_kinds:
        if allow_path:
            raise TypeError("Content source `kind` must be `url` or `path`.")
        raise TypeError('Image parts currently require `source.kind == "url"`.')
    if not isinstance(source.get("value"), str) or not source["value"].strip():
        raise TypeError("Content source `value` must be a non-empty string.")
