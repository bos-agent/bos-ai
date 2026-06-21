from __future__ import annotations

from typing import Any, Literal, TypeAlias, TypedDict, cast

# Message content types and validation owned by the agent core. These are the
# shape of the agent's input (``Agent.ask(content=...)``) and turn-context
# system prompt. The module is a stdlib-only leaf: outer rings (``bos.protocol``)
# re-export these inward so existing call sites keep working.


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
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return cast("list[dict[str, Any]]", list(content))


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
