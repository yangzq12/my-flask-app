"""Find blocked keywords inside OCR text extracted from images."""
from ocr_util import extract_text_from_image
import store


def _as_image_list(image_bytes):
    if not image_bytes:
        return []
    if isinstance(image_bytes, (list, tuple)):
        return [item for item in image_bytes if item]
    return [image_bytes]


def find_blocked_keyword_in_images(user_id, image_bytes, languages="eng"):
    """OCR every image and return the first blocked keyword hit.

    Returns a dict with:
      - keyword: blocked keyword, or None
      - texts: OCR text snippets extracted before finishing
      - errors: unique OCR errors, if any
    """
    if not store.has_blocked_keywords(user_id):
        return {"keyword": None, "texts": [], "errors": []}

    texts = []
    errors = []
    for img in _as_image_list(image_bytes):
        text, error = extract_text_from_image(img, languages=languages)
        if error and error not in errors:
            errors.append(error)
        if not text:
            continue
        texts.append(text)
        keyword = store.find_blocked_keyword(user_id, text)
        if keyword:
            return {"keyword": keyword, "texts": texts, "errors": errors}
    return {"keyword": None, "texts": texts, "errors": errors}
