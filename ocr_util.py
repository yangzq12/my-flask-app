"""Local OCR helpers for extracting text from user-uploaded images.

The implementation is deliberately optional: if pytesseract or the system
Tesseract binary is missing, callers get an error string instead of an
exception, so the reply engine can keep running.
"""
import io

from PIL import Image, ImageFilter, ImageOps


def _prepare_for_ocr(image):
    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")

    width, height = image.size
    max_edge = max(width, height)
    if max_edge and max_edge > 2400:
        scale = 2400 / max_edge
        image = image.resize((int(width * scale), int(height * scale)), Image.LANCZOS)
    elif max_edge and max_edge < 1200:
        image = image.resize((width * 2, height * 2), Image.LANCZOS)

    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray)
    return gray.filter(ImageFilter.SHARPEN)


def extract_text_from_image(image_bytes, languages="eng"):
    """Return ``(text, error)`` for image bytes using local Tesseract OCR."""
    if not image_bytes:
        return "", ""

    try:
        import pytesseract
    except ImportError:
        return "", "未安装 pytesseract，请先安装 Python OCR 依赖"

    try:
        image = Image.open(io.BytesIO(image_bytes))
        prepared = _prepare_for_ocr(image)
        text = pytesseract.image_to_string(
            prepared,
            lang=(languages or "eng").strip() or "eng",
            config="--psm 6",
        )
        return " ".join((text or "").split()), ""
    except pytesseract.TesseractNotFoundError:
        return "", "未找到 tesseract，可执行程序未安装或不在 PATH 中"
    except pytesseract.TesseractError as e:
        return "", f"Tesseract OCR 识别失败：{str(e)[:120]}"
    except Exception as e:
        return "", f"图片文字识别失败：{str(e)[:120]}"
