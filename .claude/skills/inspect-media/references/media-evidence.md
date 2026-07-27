# Media evidence contract

- Raster input: PNG, JPEG, WebP, GIF, BMP, and TIFF readable by Pillow.
- Image previews: EXIF orientation applied, maximum side bounded, PNG preferred with JPEG fallback when the preview would exceed the model payload limit.
- PDF input: rendered and extracted with PyMuPDF. Up to eight requested pages are attached per call.
- Default PDF rendering: first four pages at 144 DPI, bounded to 2560 pixels per side.
- Model payload: at most eight images and 8 MiB per image. Base64 is transient and is not written to session state or events.
- Durable artifacts: preview/page images plus JSON metadata and extracted PDF text.

Visual inspection can reveal layout, legends, morphology, trends, and obvious artifacts. It does not replace numeric checks on the underlying matrix or plotting data.
