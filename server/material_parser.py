"""Material ingestion and summarization helpers."""

from __future__ import annotations

import logging
import re
from io import BytesIO

from PyPDF2 import PdfReader
from google.genai import types

MATERIAL_MODEL = "gemini-2.5-flash"
LOGGER = logging.getLogger(__name__)

MAX_WORDS = 4000
SUMMARY_WORDS = 3000


def normalize_text(text: str) -> str:
    """Collapse whitespace and trim noisy blank lines."""
    text = text.replace("\x00", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def extract_pdf_text(file_bytes: bytes) -> str:
    """Try local PDF extraction before falling back to Gemini OCR."""
    reader = PdfReader(BytesIO(file_bytes))
    pages = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return normalize_text("\n\n".join(pages))


async def _generate_text_from_bytes(client, prompt: str, file_bytes: bytes, mime_type: str) -> str:
    response = await client.aio.models.generate_content(
        model=MATERIAL_MODEL,
        contents=[
            prompt,
            types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
        ],
    )
    return normalize_text(response.text or "")


async def maybe_summarize_material(client, material: str) -> str:
    """Trim oversized material while preserving the important concepts."""
    if word_count(material) <= MAX_WORDS:
        return material

    response = await client.aio.models.generate_content(
        model=MATERIAL_MODEL,
        contents=(
            "Summarize the following study material while preserving key concepts, "
            f"definitions, and relationships. Keep it under {SUMMARY_WORDS} words.\n\n"
            f"{material}"
        ),
    )
    return normalize_text(response.text or material)


async def parse_material(client, file_bytes: bytes, filename: str, mime_type: str) -> str:
    """Extract study material text from an uploaded file."""
    lower_name = filename.lower()

    if mime_type.startswith("text/") or lower_name.endswith(".txt"):
        text = normalize_text(file_bytes.decode("utf-8", errors="ignore"))
        if not text:
            raise ValueError("The uploaded text file was empty.")
        return await maybe_summarize_material(client, text)

    if mime_type == "application/pdf" or lower_name.endswith(".pdf"):
        local_text = ""
        try:
            local_text = extract_pdf_text(file_bytes)
        except Exception as exc:  # pragma: no cover - parser safety
            LOGGER.warning("PDF text extraction failed, falling back to Gemini OCR: %s", exc)
        if word_count(local_text) >= 120:
            return await maybe_summarize_material(client, local_text)

        text = await _generate_text_from_bytes(
            client,
            "Extract all text content from this document. Return the full text.",
            file_bytes,
            "application/pdf",
        )
        if not text:
            raise ValueError("No readable text could be extracted from that PDF.")
        return await maybe_summarize_material(client, text)

    if mime_type in {"image/png", "image/jpeg", "image/jpg", "image/webp"}:
        text = await _generate_text_from_bytes(
            client,
            "Extract and transcribe all text visible in this image. "
            "If it contains diagrams or figures, describe them in detail.",
            file_bytes,
            mime_type,
        )
        if not text:
            raise ValueError("No readable text could be extracted from that image.")
        return await maybe_summarize_material(client, text)

    raise ValueError("Unsupported file type. Use PDF, PNG, JPG, WEBP, or TXT.")


async def generate_topic_material(client, topic: str, description: str = "") -> str:
    """Generate a concise reference pack when the user only provides a topic."""
    prompt = f"""
Create a study-ready reference summary for the topic "{topic}".

User notes or focus:
{description or "None provided."}

Include the key concepts, core definitions, important relationships, and a few concrete examples.
Keep it concise, accurate, and under {SUMMARY_WORDS} words.
""".strip()

    response = await client.aio.models.generate_content(model=MATERIAL_MODEL, contents=prompt)
    text = normalize_text(response.text or "")
    if not text:
        raise ValueError("Could not generate study material for that topic.")
    return text
