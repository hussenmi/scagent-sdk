"""Deterministic image normalization and PDF rendering for model inspection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_PIXELS = 100_000_000
# Must stay at or below scagent_sdk.capabilities.results.MODEL_MEDIA_LIMIT_BYTES; pixels the
# model cannot use still cost transport, and several previews share one turn.
MODEL_MEDIA_BYTES = 2 * 1024 * 1024
# Above this a PNG preview is re-encoded as JPEG when that is genuinely smaller.
PREVIEW_TARGET_BYTES = 400 * 1024
# The model's own image pipeline resamples to roughly this longest edge, so larger previews add
# bytes without adding legibility.
DEFAULT_IMAGE_SIDE = 1568
DEFAULT_PDF_PAGES = 4


def _path(value: Any, *, suffix: str | None = None) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("path must be a non-empty string")
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"media file not found: {path}")
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError(f"media file exceeds {MAX_SOURCE_BYTES} bytes: {path}")
    if suffix is not None and path.suffix.lower() != suffix:
        raise ValueError(f"expected a {suffix} file: {path}")
    return path


def _bounded_side(value: Any) -> int:
    side = int(value or DEFAULT_IMAGE_SIDE)
    if not 512 <= side <= 4096:
        raise ValueError("max_side must be between 512 and 4096")
    return side


def _flatten(preview: Any) -> Any:
    from PIL import Image

    if preview.mode != "RGBA":
        return preview.convert("RGB")
    background = Image.new("RGB", preview.size, "white")
    background.paste(preview, mask=preview.getchannel("A"))
    return background


def _save_preview(image: Any, destination: Path, *, max_side: int) -> tuple[Path, str]:
    """Normalize to the smallest encoding that stays legible for the model.

    Dense scientific figures — scatter panels, heatmaps, antialiased legends — re-encode to
    *larger* PNGs than their source, so format is chosen by measured size rather than assumed.
    Several previews share one turn's transport, and an oversized one is pure cost: the model
    resamples to roughly `DEFAULT_IMAGE_SIDE` regardless.
    """

    from PIL import Image, ImageOps

    preview = ImageOps.exif_transpose(image).copy()
    if preview.width * preview.height > MAX_PIXELS:
        raise ValueError(f"decoded image exceeds {MAX_PIXELS} pixels")
    preview.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    if preview.mode not in {"RGB", "RGBA"}:
        preview = preview.convert("RGBA" if "A" in preview.getbands() else "RGB")
    png_path = destination.with_suffix(".png")
    preview.save(png_path, format="PNG", optimize=True)
    png_bytes = png_path.stat().st_size
    if png_bytes <= PREVIEW_TARGET_BYTES:
        return png_path, "image/png"
    jpeg_path = destination.with_suffix(".jpg")
    _flatten(preview).save(jpeg_path, format="JPEG", quality=88, optimize=True)
    if jpeg_path.stat().st_size < png_bytes:
        png_path.unlink()
        if jpeg_path.stat().st_size > MODEL_MEDIA_BYTES:
            raise ValueError("normalized image still exceeds the model media limit")
        return jpeg_path, "image/jpeg"
    jpeg_path.unlink()
    if png_bytes > MODEL_MEDIA_BYTES:
        raise ValueError("normalized image still exceeds the model media limit")
    return png_path, "image/png"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def inspect_image(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    from PIL import Image

    path = _path(arguments.get("path"))
    max_side = _bounded_side(arguments.get("max_side"))
    with Image.open(path) as image:
        source = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "format": image.format,
            "mode": image.mode,
            "width": image.width,
            "height": image.height,
            "frames": int(getattr(image, "n_frames", 1)),
        }
        preview_path, media_type = _save_preview(
            image, context.staging_dir / "image-preview", max_side=max_side
        )
    preview = {
        "relative_path": preview_path.name,
        "media_type": media_type,
        "size_bytes": preview_path.stat().st_size,
        "max_side": max_side,
    }
    details = {"source": source, "preview": preview}
    _write_json(context.staging_dir / "inspection.json", details)
    artifacts = [
        {
            "name": "image-preview",
            "relative_path": preview_path.name,
            "media_type": media_type,
        },
        {
            "name": "image-inspection",
            "relative_path": "inspection.json",
            "media_type": "application/json",
        },
    ]
    return {
        "schema_version": 1,
        "summary": (
            f"Attached a visual preview of {path.name} "
            f"({source['width']}x{source['height']}, {source['format'] or 'unknown format'}). "
            "Inspect the attached pixels before answering."
        ),
        "details": details,
        "artifacts": artifacts,
        "model_media": [
            {
                "name": "image-preview",
                "relative_path": preview_path.name,
                "media_type": media_type,
            }
        ],
    }


def _selected_pages(arguments: dict[str, Any], total: int) -> list[int]:
    raw = arguments.get("pages")
    if raw is None:
        return list(range(min(DEFAULT_PDF_PAGES, total)))
    if not isinstance(raw, list) or not raw:
        raise ValueError("pages must be a non-empty list of one-based page numbers")
    if len(raw) > 8:
        raise ValueError("at most eight PDF pages may be rendered per call")
    selected: list[int] = []
    for value in raw:
        page = int(value)
        if not 1 <= page <= total:
            raise ValueError(f"PDF page {page} is outside 1..{total}")
        if page - 1 not in selected:
            selected.append(page - 1)
    return selected


def inspect_pdf(arguments: dict[str, Any], context: Any) -> dict[str, Any]:
    import fitz
    from PIL import Image

    path = _path(arguments.get("path"), suffix=".pdf")
    max_text_chars = int(arguments.get("max_text_chars") or 30000)
    if not 1000 <= max_text_chars <= 40000:
        raise ValueError("max_text_chars must be between 1000 and 40000")
    document = fitz.open(path)
    try:
        if document.page_count < 1:
            raise ValueError("PDF has no pages")
        selected = _selected_pages(arguments, document.page_count)
        extracted_parts: list[str] = []
        text_truncated = False
        for index in range(document.page_count):
            text = document[index].get_text("text").strip()
            if not text:
                continue
            part = f"[Page {index + 1}]\n{text}"
            current = sum(len(item) + 2 for item in extracted_parts)
            remaining = max_text_chars - current
            if remaining <= 0:
                text_truncated = True
                break
            if len(part) > remaining:
                extracted_parts.append(part[:remaining])
                text_truncated = True
                break
            extracted_parts.append(part)
        extracted_text = "\n\n".join(extracted_parts)
        text_path = context.staging_dir / "extracted.txt"
        text_path.write_text(extracted_text + ("\n" if extracted_text else ""), encoding="utf-8")

        artifacts: list[dict[str, str]] = [
            {
                "name": "pdf-extracted-text",
                "relative_path": text_path.name,
                "media_type": "text/plain",
            }
        ]
        model_media: list[dict[str, str]] = []
        rendered: list[dict[str, Any]] = []
        for index in selected:
            page = document[index]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(2.0, 2.0), alpha=False)
            raw_path = context.staging_dir / f"page-{index + 1}-raw.png"
            pixmap.save(raw_path)
            with Image.open(raw_path) as page_image:
                preview_path, media_type = _save_preview(
                    page_image,
                    context.staging_dir / f"page-{index + 1}",
                    max_side=DEFAULT_IMAGE_SIDE,
                )
            raw_path.unlink(missing_ok=True)
            item = {
                "page": index + 1,
                "relative_path": preview_path.name,
                "media_type": media_type,
                "width": pixmap.width,
                "height": pixmap.height,
                "size_bytes": preview_path.stat().st_size,
            }
            rendered.append(item)
            artifacts.append(
                {
                    "name": f"pdf-page-{index + 1}",
                    "relative_path": preview_path.name,
                    "media_type": media_type,
                }
            )
            model_media.append(
                {
                    "name": f"pdf-page-{index + 1}",
                    "relative_path": preview_path.name,
                    "media_type": media_type,
                }
            )
        details = {
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "total_pages": document.page_count,
            "rendered_pages": [index + 1 for index in selected],
            "rendered": rendered,
            "extracted_text": extracted_text,
            "text_chars": len(extracted_text),
            "text_truncated": text_truncated,
            "scan_likely": not bool(extracted_text.strip()),
        }
        _write_json(context.staging_dir / "inspection.json", details)
        artifacts.append(
            {
                "name": "pdf-inspection",
                "relative_path": "inspection.json",
                "media_type": "application/json",
            }
        )
        return {
            "schema_version": 1,
            "summary": (
                f"Inspected {path.name}: {document.page_count} pages, rendered pages "
                f"{details['rendered_pages']}, extracted {len(extracted_text)} text characters. "
                "Use both the attached page images and extracted text."
            ),
            "details": details,
            "artifacts": artifacts,
            "model_media": model_media,
        }
    finally:
        document.close()
