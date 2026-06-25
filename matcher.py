"""匹配逻辑：关键词模糊匹配（移植自 reply.py）+ 图片感知哈希匹配。

对外主入口 match()：给定文本和/或图片字节，返回匹配结果 dict：
  {type: 'keyword'|'image'|'shop'|'none', product: row|None, link: str}
"""
import re

import store
from imagehash_util import compute_hash, combined_distance


# ---------------- 关键词模糊匹配（与 reply.py 完全一致的算法）----------------
def extract_core_keywords(text):
    """提取文本中的核心关键词（字母+数字组合），并生成 aj4↔j4 变体。"""
    if not text:
        return []
    pattern = re.compile(r"[a-zA-Z0-9]+")
    matches = pattern.findall(text.lower())
    core_keywords = [m for m in matches if len(m) >= 2]

    variants = []
    for kw in core_keywords:
        digits = re.findall(r"\d+", kw)
        if digits:
            digit = digits[0]
            letters = re.sub(r"\d+", "", kw)
            if letters:
                variants.append(f"{letters[-1]}{digit}")      # aj4 -> j4
                variants.append(f"a{letters[-1]}{digit}")     # j4 -> aj4
    return core_keywords + variants


def flexible_keyword_match(content, text_maps):
    """增强版灵活关键词匹配：核心词 + 变体 + 忽略大小写。返回链接或 None。"""
    if not content or not text_maps:
        return None
    core_keywords = extract_core_keywords(content)
    if not core_keywords:
        return None

    for kw, link in text_maps.items():
        if not kw:
            continue
        target_kw = kw.lower()
        if target_kw in core_keywords:
            return link
        for variant in extract_core_keywords(target_kw):
            if variant in core_keywords:
                return link
    return None


def match_keyword(content):
    """对一段文本做关键词匹配，命中则返回对应商品 row，否则 None。"""
    if not content:
        return None
    for p in store.enabled_products():
        if not p["name"] or not p["link"]:
            continue
        if flexible_keyword_match(content, {p["name"]: p["link"]}):
            return p
    return None


# ---------------- 图片匹配 ----------------
def match_image(image_bytes, threshold=None):
    """对图片字节做感知哈希匹配，返回 (商品 row, 距离) 或 (None, None)。"""
    if threshold is None:
        threshold = int(store.get_setting("IMAGE_MATCH_THRESHOLD", "16") or 16)
    try:
        query_hash = compute_hash(image_bytes)
    except Exception as e:
        print(f"[matcher] 图片哈希计算失败: {e}")
        return None, None

    best, best_dist = None, None
    for p in store.products_with_image_hash():
        dist = combined_distance(query_hash, p["image_hash"])
        if best_dist is None or dist < best_dist:
            best, best_dist = p, dist

    if best is not None and best_dist is not None and best_dist <= threshold:
        return best, best_dist
    return None, best_dist


# ---------------- 统一入口 ----------------
def match(content="", image_bytes=None, source="web-test"):
    """综合匹配：先关键词，未命中且有图片再图片匹配，仍未命中回退店铺信息。"""
    settings = store.get_settings()
    content = (content or "").strip()
    result = {"type": "none", "product": None, "link": "", "distance": None}

    # 1) 关键词
    if content:
        p = match_keyword(content)
        if p:
            result.update(type="keyword", product=p, link=p["link"])

    # 2) 图片
    if result["type"] == "none" and image_bytes and settings.get("IMAGE_MATCH_ENABLED", "1") == "1":
        p, dist = match_image(image_bytes, int(settings.get("IMAGE_MATCH_THRESHOLD", "16") or 16))
        result["distance"] = dist
        if p:
            result.update(type="image", product=p, link=p["link"])

    # 3) 回退店铺信息
    if result["type"] == "none":
        shop_reply = f'{settings.get("CUSTOM_REPLY", "")}\n{settings.get("SHOP_WEBSITE", "")}'.strip()
        result.update(type="shop", link=shop_reply)

    # 记录日志
    store.log_match(
        source=source,
        query_text=content,
        had_image=bool(image_bytes),
        match_type=result["type"],
        matched_code=result["product"]["code"] if result["product"] else "",
        matched_link=result["link"],
    )
    return result
