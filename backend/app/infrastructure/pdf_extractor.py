"""Fixed worker child operation. Imports the PDF parser only after resource limits."""

import io
import json
import logging
import resource
import sys

from app.domain.retrieval import PDF_RAW_BYTES
from app.domain.web import MAX_SOURCE_BYTES
from app.infrastructure.web_extraction import bounded_source_text

_PROCESS_MEMORY_BYTES = 256 * 1024 * 1024
_PDF_STREAM_BYTES = 4 * 1024 * 1024
_MAX_PAGES = 100


def _limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_AS, (_PROCESS_MEMORY_BYTES, _PROCESS_MEMORY_BYTES))


def _parse(body: bytes) -> dict[str, object]:
    from pypdf import PdfReader, apply_configuration
    from pypdf.errors import LimitReachedError
    from pypdf.generic import DictionaryObject

    def outcome(status: str, error_code: str) -> dict[str, object]:
        return {"status": status, "text": "", "title": None, "error_code": error_code}

    try:
        with apply_configuration(
            maximum_declared_stream_length=PDF_RAW_BYTES,
            array_based_stream_maximum_output_length=_PDF_STREAM_BYTES,
            array_maximum_element_count=10000,
            dictionary_maximum_element_count=10000,
            zlib_maximum_output_length=_PDF_STREAM_BYTES,
            lzw_maximum_output_length=_PDF_STREAM_BYTES,
            run_length_maximum_output_length=_PDF_STREAM_BYTES,
            brotli_maximum_output_length=_PDF_STREAM_BYTES,
            stream_decoding_work_maximum_length=16 * 1024 * 1024,
            stream_parsing_work_maximum_count=200000,
            page_tree_maximum_entries=1000,
            page_tree_maximum_depth=30,
            page_tree_maximum_page_entries=20000,
            xform_maximum_invocations_per_extraction=100,
            jbig2dec_binary=None,
        ):
            reader = PdfReader(io.BytesIO(body), strict=True)
            if reader.is_encrypted:
                return outcome("unsupported", "pdf_encrypted")
            pages = reader.root_object["/Pages"]
            if not isinstance(pages, DictionaryObject):
                return outcome("extraction_failed", "pdf_invalid")
            declared = pages.get("/Count")
            if isinstance(declared, int) and declared > _MAX_PAGES:
                return outcome("unsupported", "pdf_page_limit")
            if len(reader.pages) > _MAX_PAGES:
                return outcome("unsupported", "pdf_page_limit")
            parts: list[str] = []
            partial = False
            empty_page = False
            for index, page in enumerate(reader.pages):
                contents = page.get_contents()
                if contents is not None and len(contents.get_data()) > _PDF_STREAM_BYTES:
                    return outcome("unsupported", "pdf_stream_limit")
                page_text = (page.extract_text() or "").strip()
                empty_page = empty_page or not page_text
                parts.append(page_text)
                text, partial = bounded_source_text("\n".join(parts).strip())
                if partial:
                    break
                if len(text.encode("utf-8")) >= MAX_SOURCE_BYTES:
                    partial = index + 1 < len(reader.pages)
                    break
            text, limited = bounded_source_text("\n".join(parts).strip())
            if not text:
                return outcome("unsupported", "pdf_no_extractable_text_ocr_unsupported")
            metadata = reader.metadata
            title = metadata.title if metadata is not None else None
            return {
                "status": "partial" if partial or limited or empty_page else "success",
                "text": text,
                "title": title[:1000] if isinstance(title, str) and title.strip() else None,
                "error_code": (
                    "extracted_text_limit"
                    if partial or limited
                    else "pdf_pages_without_extractable_text"
                    if empty_page
                    else None
                ),
            }
    except LimitReachedError, MemoryError:
        return outcome("unsupported", "pdf_resource_limit")
    except Exception:
        return outcome("extraction_failed", "pdf_invalid")


def main() -> None:
    _limits()
    logging.disable(logging.CRITICAL)
    body = sys.stdin.buffer.read(PDF_RAW_BYTES + 1)
    if len(body) > PDF_RAW_BYTES:
        result = {"status": "oversized", "text": "", "title": None, "error_code": "pdf_input_limit"}
    else:
        result = _parse(body)
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
