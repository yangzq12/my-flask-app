"""匹配逻辑：商品名相似匹配 + 图片感知哈希匹配。

对外主入口 match()：给定文本和/或图片字节，返回匹配结果 dict：
  {type: 'keyword'|'image'|'shop'|'none', product: row|None, link: str}
"""
import difflib
import re
import threading

import store
from imagehash_util import compute_hash, combined_distance

MAX_IMAGE_DISTANCE = 128
_CACHE_LOCK = threading.RLock()
_TEXT_PRODUCT_CACHE = {}
_IMAGE_PRODUCT_CACHE = {}


def invalidate_product_cache(user_id=None):
    """主动清空商品匹配缓存；商品变更后可调用，版本号变化也会自动重建。"""
    with _CACHE_LOCK:
        if user_id is None:
            _TEXT_PRODUCT_CACHE.clear()
            _IMAGE_PRODUCT_CACHE.clear()
        else:
            user_id = int(user_id)
            _TEXT_PRODUCT_CACHE.pop(user_id, None)
            _IMAGE_PRODUCT_CACHE.pop(user_id, None)


# ---------------- 商品名相似度匹配 ----------------
TOKEN_RE = re.compile(r"[a-zA-Z0-9]+")


def extract_base_keywords(text):
    """提取文本中的原始核心词，不包含系统生成的型号变体。"""
    if not text:
        return []
    matches = TOKEN_RE.findall(text.lower())
    return [m for m in matches if len(m) >= 2 or m.isdigit()]


def extract_core_keywords(text):
    """提取文本中的核心词，并生成 aj4↔j4 这类型号变体。"""
    core_keywords = extract_base_keywords(text)

    variants = []
    for kw in core_keywords:
        digit_groups = re.findall(r"\d+", kw)
        letters = re.sub(r"\d+", "", kw)
        if digit_groups and letters:
            for digit in digit_groups:
                variants.append(f"{letters[-1]}{digit}")      # aj4 -> j4
                variants.append(f"a{letters[-1]}{digit}")     # j4 -> aj4
                variants.append(digit)                        # j4 -> 4

    # 处理多单词型号：Air Jordan 4 -> aj4/j4，New Balance 990 -> nb990/b990。
    for idx, kw in enumerate(core_keywords):
        if not kw.isdigit():
            continue
        prev_words = []
        lookback = idx - 1
        while lookback >= 0 and len(prev_words) < 3:
            prev = core_keywords[lookback]
            if prev.isdigit():
                break
            prev_words.insert(0, prev)
            acronym = "".join(word[0] for word in prev_words)
            if acronym:
                variants.append(f"{acronym}{kw}")
                if len(acronym) == 1:
                    variants.append(f"a{acronym}{kw}")
            lookback -= 1

    seen = set()
    keywords = []
    for kw in core_keywords + variants:
        if kw not in seen:
            seen.add(kw)
            keywords.append(kw)
    return keywords


def _normalize_text(text):
    return " ".join(extract_core_keywords(text))


def _keyword_set(text):
    return set(extract_core_keywords(text))


def _text_features(text):
    terms = _keyword_set(text)
    return {
        "norm": _normalize_text(text),
        "terms": terms,
        "base_terms": set(extract_base_keywords(text)),
        "model_terms": {t for t in terms if re.search(r"[a-zA-Z]", t) and re.search(r"\d", t)},
    }


def _score_text_features(query_features, target_features):
    query = query_features["norm"]
    target = target_features["norm"]
    query_terms = query_features["terms"]
    target_terms = target_features["terms"]
    if not query or not target or not query_terms or not target_terms:
        return 0.0

    query_base_terms = query_features["base_terms"]
    target_base_terms = target_features["base_terms"]
    overlap = query_terms & target_terms
    overlap_ratio = len(overlap) / len(target_terms)
    query_recall = len(query_base_terms & target_terms) / len(query_base_terms) if query_base_terms else 0.0
    target_recall = len(target_base_terms & query_terms) / len(target_base_terms) if target_base_terms else 0.0
    phrase_ratio = difflib.SequenceMatcher(None, query, target).ratio()

    # 型号/编码类词通常最能区分商品，例如 aj4、j4、990v6。
    model_terms = target_features["model_terms"]
    model_ratio = len(query_terms & model_terms) / len(model_terms) if model_terms else 0.0

    score = (
        query_recall * 0.35
        + target_recall * 0.30
        + overlap_ratio * 0.10
        + phrase_ratio * 0.10
        + model_ratio * 0.15
    )

    # 商品名完整出现在用户消息中时给高分，但仍参与全局比较。
    if target and target in query:
        score = max(score, 0.95)
    return score


def product_name_score(content, product_name):
    """计算用户文本与商品名的相似度，返回 0~1 的分数。"""
    return _score_text_features(_text_features(content), _text_features(product_name))


def _cached_text_products(user_id):
    user_id = int(user_id)
    version = store.product_cache_version(user_id)
    with _CACHE_LOCK:
        cached = _TEXT_PRODUCT_CACHE.get(user_id)
        if cached and cached["version"] == version:
            return cached["products"]

    products = []
    for p in store.enabled_products(user_id):
        if not p["name"] or not p["link"]:
            continue
        row = dict(p)
        features = _text_features(row["name"])
        if features["norm"] and features["terms"]:
            products.append({"row": row, "features": features})

    with _CACHE_LOCK:
        _TEXT_PRODUCT_CACHE[user_id] = {"version": version, "products": products}
        return _TEXT_PRODUCT_CACHE[user_id]["products"]


def _cached_image_products(user_id):
    user_id = int(user_id)
    version = store.product_cache_version(user_id)
    with _CACHE_LOCK:
        cached = _IMAGE_PRODUCT_CACHE.get(user_id)
        if cached and cached["version"] == version:
            return cached["images"]

    images = []
    for row in store.products_with_image_hash(user_id):
        image_hash = row["product_image_hash"]
        if image_hash:
            images.append({
                "row": dict(row),
                "product_id": row["id"],
                "image_hash": image_hash,
            })

    with _CACHE_LOCK:
        _IMAGE_PRODUCT_CACHE[user_id] = {"version": version, "images": images}
        return _IMAGE_PRODUCT_CACHE[user_id]["images"]


def _minimum_keyword_score(content):
    query_base_terms = set(extract_base_keywords(content))
    query_terms = _keyword_set(content)
    has_model = any(re.search(r"[a-zA-Z]", t) and re.search(r"\d", t) for t in query_terms)
    if len(query_base_terms) <= 1 and not has_model:
        return 0.65
    return 0.45


def match_keyword(content, user_id=None):
    """对一段文本和所有商品名做相似度匹配，返回最佳商品 row，否则 None。"""
    if not content:
        return None
    user_id = store.default_user_id() if user_id is None else int(user_id)
    best, best_score = None, 0.0
    min_score = _minimum_keyword_score(content)
    query_features = _text_features(content)
    for entry in _cached_text_products(user_id):
        score = _score_text_features(query_features, entry["features"])
        if score > best_score:
            best, best_score = entry["row"], score

    # 分数过低说明只是碰巧有少量字符/词相似，避免误回商品链接。
    return best if best is not None and best_score >= min_score else None


# ---------------- 图片匹配 ----------------
def _as_image_list(image_bytes):
    if not image_bytes:
        return []
    if isinstance(image_bytes, (list, tuple)):
        return [item for item in image_bytes if item]
    return [image_bytes]


def image_similarity(distance):
    """把组合哈希距离转换为 0~1 相似度，距离越小相似度越高。"""
    if distance is None:
        return None
    distance = max(0, min(MAX_IMAGE_DISTANCE, distance))
    return round(1 - distance / MAX_IMAGE_DISTANCE, 4)


def image_similarity_threshold(value):
    """读取 0~1 图片相似度阈值；兼容旧的 0~128 距离阈值。"""
    try:
        raw = float(value)
    except (TypeError, ValueError):
        raw = 0.875
    if raw > 1:
        raw = 1 - max(0, min(MAX_IMAGE_DISTANCE, raw)) / MAX_IMAGE_DISTANCE
    return max(0.0, min(1.0, raw))


def image_distance_threshold(value):
    """把 0~1 相似度阈值转换成内部使用的最大 hash 距离。"""
    similarity = image_similarity_threshold(value)
    return int(round((1 - similarity) * MAX_IMAGE_DISTANCE))


def match_image(image_bytes, threshold=None, user_id=None):
    """对用户多张图片和商品多张图片做匹配，threshold 为 0~1 相似度阈值。"""
    user_id = store.default_user_id() if user_id is None else int(user_id)
    if threshold is None:
        threshold = store.get_setting("IMAGE_MATCH_THRESHOLD", "0.875", user_id=user_id)
    max_distance = image_distance_threshold(threshold)

    query_hashes = []
    for img in _as_image_list(image_bytes):
        try:
            query_hashes.append(compute_hash(img))
        except Exception as e:
            print(f"[matcher] 图片哈希计算失败: {e}")

    if not query_hashes:
        return None, None

    best, best_dist = None, None
    product_best_dist = {}
    product_best_row = {}
    for entry in _cached_image_products(user_id):
        image_hash = entry["image_hash"]
        dist = min(combined_distance(query_hash, image_hash) for query_hash in query_hashes)
        product_id = entry["product_id"]
        if product_id not in product_best_dist or dist < product_best_dist[product_id]:
            product_best_dist[product_id] = dist
            product_best_row[product_id] = entry["row"]

    for product_id, dist in product_best_dist.items():
        if best_dist is None or dist < best_dist:
            best, best_dist = product_best_row[product_id], dist

    if best is not None and best_dist is not None and best_dist <= max_distance:
        return best, best_dist
    return None, best_dist


# ---------------- 统一入口 ----------------
def match(content="", image_bytes=None, source="web-test", record_match_log=True, user_id=None):
    """综合匹配：先按商品名找最相近商品，未命中且有图片再图片匹配，仍未命中回退店铺信息。"""
    user_id = store.default_user_id() if user_id is None else int(user_id)
    settings = store.get_settings(user_id)
    content = (content or "").strip()
    image_list = _as_image_list(image_bytes)
    result = {"type": "none", "product": None, "link": "", "distance": None, "similarity": None}

    # 1) 商品名相似匹配
    if content:
        p = match_keyword(content, user_id=user_id)
        if p:
            result.update(type="keyword", product=p, link=p["link"])

    # 2) 图片
    if result["type"] == "none" and image_list and settings.get("IMAGE_MATCH_ENABLED", "1") == "1":
        p, dist = match_image(
            image_list,
            settings.get("IMAGE_MATCH_THRESHOLD", "0.875"),
            user_id=user_id,
        )
        result["distance"] = dist
        result["similarity"] = image_similarity(dist)
        if p:
            result.update(type="image", product=p, link=p["link"])

    # 3) 回退店铺信息
    if result["type"] == "none":
        shop_reply = f'{settings.get("CUSTOM_REPLY", "")}\n{settings.get("SHOP_WEBSITE", "")}'.strip()
        result.update(type="shop", link=shop_reply)

    if record_match_log:
        store.log_match(
            user_id=user_id,
            source=source,
            query_text=content,
            had_image=bool(image_list),
            match_type=result["type"],
            matched_code=result["product"]["code"] if result["product"] else "",
            matched_link=result["link"],
        )
    return result
