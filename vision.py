"""Karen's eyes: questions about the screen or an image file, answered by a vision model on Groq.

Nothing is saved: the screenshot or photo is shrunk, sent with the question, and dropped.
"""

import base64
import io
import os
from pathlib import Path

from net import friendly_error, http

VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".heic", ".tif", ".tiff"}
PROMPT = ("You are the eyes of a voice assistant. Answer the question about the image in a few short, plain "
          "sentences that can be read aloud. Copy important text, numbers, amounts, dates and error messages "
          "exactly. If you can't see or read something clearly, say so instead of guessing.")


def _jpeg(image, max_side: int = 1600) -> bytes:
    image = image.convert("RGB")
    image.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def ask_image(jpeg: bytes, question: str) -> dict:
    """Ask the vision model about one JPEG image."""
    key = os.getenv("GROQ_API_KEY", "")
    if not key:
        return {"error": "GROQ_API_KEY is missing in .env"}
    content = [
        {"type": "text", "text": question.strip() or "What is in this image? Read out any important text."},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
    ]
    try:
        r = http.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": VISION_MODEL, "max_tokens": 700, "temperature": 0.2,
                  "messages": [{"role": "system", "content": PROMPT}, {"role": "user", "content": content}]},
            timeout=(15, 90),
        )
    except Exception as e:  # noqa: BLE001
        return {"error": friendly_error(e)}
    if r.status_code != 200:
        try:
            message = r.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = r.text[:200]
        return {"error": f"vision model failed ({r.status_code}): {message}"}
    answer = (r.json()["choices"][0]["message"].get("content") or "").strip()
    return {"answer": answer} if answer else {"error": "the vision model returned nothing"}


def look_at_screen(question: str = "") -> dict:
    """Look at what's on the PC screen right now and answer a question about it."""
    from PIL import ImageGrab

    try:
        shot = ImageGrab.grab()  # the main screen, where Karen's user is working
    except OSError as e:
        return {"error": f"couldn't capture the screen: {e}"}
    result = ask_image(_jpeg(shot), question or "Describe what's on this screen and read any important text.")
    return {"looked_at": "the screen", **result}


def describe_image(path: str, question: str = "") -> dict:
    """Answer a question about an image file (photo, screenshot, scanned page)."""
    from agent import resolve

    p = resolve(path)
    if not p.is_file():
        return {"error": f"no file at {p}"}
    return describe_image_bytes(p.read_bytes(), p.name, question)


def describe_image_bytes(data: bytes, name: str, question: str = "") -> dict:
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError):
        return {"error": f"{name} isn't an image Karen can open"}
    return {"image": name, **ask_image(_jpeg(image), question)}


def is_image(name: str) -> bool:
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS
