"""Sandboxed file operations and dependency-free minimal DOCX generation."""

from __future__ import annotations

import os
from pathlib import Path
from xml.sax.saxutils import escape
from xml.etree import ElementTree
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile


MAX_TEXT_BYTES = 1_048_576
MAX_DOCX_PARAGRAPHS = 1_000
MAX_DOCX_BYTES = 5_242_880
MAX_DOCX_XML_BYTES = 4_194_304
_WORD_NAMESPACE = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
}


class FilesystemSandboxError(ValueError):
    """Base error for rejected or failed sandbox operations."""


class PathOutsideSandboxError(FilesystemSandboxError):
    """The requested path is absolute or resolves outside the configured root."""


class FileAlreadyExistsError(FilesystemSandboxError):
    """A write target already exists and may not be overwritten."""


class SandboxedFilesystem:
    """Perform bounded file operations beneath one canonical root directory."""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise FilesystemSandboxError(
                f"sandbox root is not a directory: {self.root}"
            )

    def resolve(self, relative_path: str) -> Path:
        """Resolve one non-empty relative path and reject sandbox escapes."""

        if not relative_path or not relative_path.strip():
            raise FilesystemSandboxError("path must be a non-empty relative path")
        if "\x00" in relative_path:
            raise FilesystemSandboxError("path contains a NUL character")
        supplied = Path(relative_path)
        if supplied.is_absolute() or supplied.anchor:
            raise PathOutsideSandboxError("absolute paths are not allowed")
        candidate = (self.root / supplied).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PathOutsideSandboxError(
                "path resolves outside the configured filesystem root"
            ) from exc
        if candidate == self.root:
            raise FilesystemSandboxError("path must name a file below the sandbox root")
        return candidate

    def read_file(self, relative_path: str) -> dict[str, str | int]:
        """Read a bounded UTF-8 text file from the sandbox."""

        target = self.resolve(relative_path)
        if not target.is_file():
            raise FilesystemSandboxError("file does not exist or is not a regular file")
        size = target.stat().st_size
        if size > MAX_TEXT_BYTES:
            raise FilesystemSandboxError(
                f"file exceeds the {MAX_TEXT_BYTES}-byte read limit"
            )
        try:
            content = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise FilesystemSandboxError("file is not valid UTF-8 text") from exc
        return {
            "path": target.relative_to(self.root).as_posix(),
            "content": content,
            "bytes": size,
        }

    def write_file(self, relative_path: str, content: str) -> dict[str, str | int]:
        """Create a new UTF-8 text file without ever overwriting a target."""

        encoded = content.encode("utf-8")
        if len(encoded) > MAX_TEXT_BYTES:
            raise FilesystemSandboxError(
                f"content exceeds the {MAX_TEXT_BYTES}-byte write limit"
            )
        target = self._prepare_new_target(relative_path)
        try:
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError as exc:
            raise FileAlreadyExistsError("refusing to overwrite existing file") from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return {
            "path": target.relative_to(self.root).as_posix(),
            "bytes": len(encoded),
        }

    def read_docx(self, relative_path: str) -> dict[str, str | int | list[str]]:
        """Read visible paragraphs from a bounded Word OOXML document."""

        if not relative_path.lower().endswith(".docx"):
            raise FilesystemSandboxError("Word input path must end with .docx")
        target = self.resolve(relative_path)
        if not target.is_file():
            raise FilesystemSandboxError("file does not exist or is not a regular file")
        size = target.stat().st_size
        if size > MAX_DOCX_BYTES:
            raise FilesystemSandboxError(
                f"Word file exceeds the {MAX_DOCX_BYTES}-byte read limit"
            )
        try:
            with ZipFile(target) as archive:
                document_info = archive.getinfo("word/document.xml")
                if document_info.file_size > MAX_DOCX_XML_BYTES:
                    raise FilesystemSandboxError(
                        "Word document XML exceeds the safe read limit"
                    )
                document = ElementTree.fromstring(
                    archive.read("word/document.xml")
                )
        except (BadZipFile, KeyError, ElementTree.ParseError) as exc:
            raise FilesystemSandboxError("file is not a valid Word document") from exc
        paragraphs = [
            "".join(
                node.text or ""
                for node in item.findall(".//w:t", _WORD_NAMESPACE)
            )
            for item in document.findall(".//w:p", _WORD_NAMESPACE)
        ]
        return {
            "path": target.relative_to(self.root).as_posix(),
            "bytes": size,
            "paragraphs": paragraphs,
        }

    def generate_docx(
        self,
        relative_path: str,
        title: str,
        paragraphs: list[str],
        sources: list[str] | None = None,
    ) -> dict[str, str | int]:
        """Create a small standards-based Word document without overwriting."""

        if not relative_path.lower().endswith(".docx"):
            raise FilesystemSandboxError("Word output path must end with .docx")
        if not title.strip():
            raise FilesystemSandboxError("title must not be empty")
        source_items = sources or []
        if len(paragraphs) + len(source_items) > MAX_DOCX_PARAGRAPHS:
            raise FilesystemSandboxError(
                f"document exceeds the {MAX_DOCX_PARAGRAPHS}-paragraph limit"
            )
        text_bytes = sum(
            len(item.encode("utf-8"))
            for item in [title, *paragraphs, *source_items]
        )
        if text_bytes > MAX_TEXT_BYTES:
            raise FilesystemSandboxError(
                f"document text exceeds the {MAX_TEXT_BYTES}-byte limit"
            )
        target = self._prepare_new_target(relative_path)
        document_xml = _document_xml(title, paragraphs, source_items)
        try:
            with ZipFile(target, mode="x", compression=ZIP_DEFLATED) as archive:
                archive.writestr("[Content_Types].xml", _CONTENT_TYPES_XML)
                archive.writestr("_rels/.rels", _PACKAGE_RELS_XML)
                archive.writestr("word/document.xml", document_xml)
                archive.writestr("word/styles.xml", _STYLES_XML)
                archive.writestr(
                    "word/_rels/document.xml.rels",
                    _DOCUMENT_RELS_XML,
                )
        except FileExistsError as exc:
            raise FileAlreadyExistsError("refusing to overwrite existing file") from exc
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return {
            "path": target.relative_to(self.root).as_posix(),
            "bytes": target.stat().st_size,
            "paragraphs": (
                1 + len(paragraphs) + len(source_items) + (1 if source_items else 0)
            ),
        }

    def _prepare_new_target(self, relative_path: str) -> Path:
        target = self.resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-resolve after mkdir so existing parent symlinks cannot redirect writes.
        target = self.resolve(relative_path)
        if target.exists():
            raise FileAlreadyExistsError("refusing to overwrite existing file")
        return target


def _text_run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def _paragraph(text: str, *, style: str | None = None) -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{properties}{_text_run(text)}</w:p>"


def _document_xml(title: str, paragraphs: list[str], sources: list[str]) -> str:
    body = [_paragraph(title, style="Title")]
    body.extend(_paragraph(item) for item in paragraphs)
    if sources:
        body.append(_paragraph("Sources", style="Heading1"))
        body.extend(_paragraph(item, style="ListBullet") for item in sources)
    body.append(
        '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" '
        'w:left="1440" w:header="720" w:footer="720" w:gutter="0"/>'
        "</w:sectPr>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>'
        + "".join(body)
        + "</w:body></w:document>"
    )


_CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
</Types>"""

_PACKAGE_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

_DOCUMENT_RELS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""

_STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/><w:basedOn w:val="Normal"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:style>
</w:styles>"""
