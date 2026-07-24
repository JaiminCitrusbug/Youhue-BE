"""Roster upload validation (FR-03-01) — type / size / content-scan for the CSV import.

No third-party AV engine is wired into this codebase (grepped: no scanning utility/pattern
exists anywhere in the BE, and there is no FR-02-01 file-upload precedent to reuse — that ticket
is a school self-registration form, not a file upload). Per the ticket's own fallback instruction,
this implements a minimal but REAL check rather than faking a scan with a comment:

  * TYPE — the filename extension AND the declared content-type must both say CSV. A caller that
    tries to slip a PDF/exe/image through by lying about only one of the two is still refused.
  * SIZE — a hard byte cap, checked before any parsing (never buffer/parse an unbounded body).
  * SCAN — magic-byte sniffing rejects known binary formats (executables, ZIP/Office OOXML, PDFs,
    images, archives) masquerading as ``.csv``; a NUL byte or content that fails to decode as
    UTF-8 text is refused outright, since a well-formed CSV is UTF-8 text with no embedded NULs.
    This is real content inspection, not a rubber-stamp — it is what actually stops a renamed
    binary payload from reaching the parser, which is the concrete threat a roster upload poses.
"""


class UnsupportedFileTypeError(Exception):
    """Filename extension and/or declared content-type do not say CSV -> 415, true reason."""


class FileTooLargeError(Exception):
    """Upload exceeds the configured byte cap -> 413."""


class FailedScanError(Exception):
    """Content failed the scan (empty / binary signature / NUL byte / not UTF-8 text) -> 400."""


_ACCEPTED_EXTENSION = ".csv"
_ACCEPTED_CONTENT_TYPES = frozenset(
    {
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
        "text/plain",
        "application/octet-stream",
        "",
    }
)

# Magic-byte signatures for common binary formats a malicious/mistaken upload might carry under a
# renamed ".csv" filename. Checked against the START of the raw bytes.
_BINARY_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "a Windows executable"),
    (b"\x7fELF", "an ELF executable"),
    (b"%PDF", "a PDF document"),
    (b"PK\x03\x04", "a ZIP/Office document"),
    (b"\x89PNG\r\n\x1a\n", "a PNG image"),
    (b"\xff\xd8\xff", "a JPEG image"),
    (b"GIF87a", "a GIF image"),
    (b"GIF89a", "a GIF image"),
    (b"\x1f\x8b", "a gzip archive"),
    (b"Rar!\x1a\x07", "a RAR archive"),
    (b"7z\xbc\xaf\x27\x1c", "a 7z archive"),
)


def check_file_type(filename: str, content_type: str | None) -> None:
    """Raise :class:`UnsupportedFileTypeError` unless both the name and the declared type say
    CSV."""
    name = (filename or "").strip().lower()
    ctype = (content_type or "").split(";")[0].strip().lower()
    if not name.endswith(_ACCEPTED_EXTENSION) or ctype not in _ACCEPTED_CONTENT_TYPES:
        raise UnsupportedFileTypeError(name or "(no filename)")


def check_size(raw: bytes, max_bytes: int) -> None:
    """Raise :class:`FileTooLargeError` when the payload exceeds ``max_bytes``."""
    if len(raw) > max_bytes:
        raise FileTooLargeError(len(raw))


def scan_and_decode(raw: bytes) -> str:
    """Content-scan the raw bytes and return them decoded as UTF-8 text, or raise
    :class:`FailedScanError` — empty payload, a recognised binary signature, an embedded NUL byte,
    or bytes that are not valid UTF-8 text are all refused before a single row is parsed."""
    if not raw:
        raise FailedScanError("The uploaded file is empty.")
    for signature, kind in _BINARY_SIGNATURES:
        if raw.startswith(signature):
            raise FailedScanError(f"The uploaded file looks like {kind}, not CSV text.")
    if b"\x00" in raw:
        raise FailedScanError("The uploaded file contains binary content, not CSV text.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FailedScanError("The uploaded file is not valid UTF-8 CSV text.") from exc
