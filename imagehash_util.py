"""感知哈希（perceptual hash）工具：仅依赖 Pillow + numpy。

用于「识别用户的图片」：给商品上传一张图片时计算其哈希并入库；
当用户在 Discord 里发来图片时，对图片同样计算哈希，按汉明距离找最接近的商品。

实现两种哈希并组合：
  - dHash（差异哈希）：对缩放、轻微压缩鲁棒，主力。
  - pHash（基于 DCT 的感知哈希）：对亮度变化鲁棒，作为补充。
存储格式： "<dhash_hex>:<phash_hex>"，匹配时取两者汉明距离的较小值。

注意：感知哈希擅长识别「同一张图/近重复图」（如同一商品图被转发）。
对「同一商品的不同实拍照」识别能力有限——那需要 CLIP 等深度模型。
阈值（IMAGE_MATCH_THRESHOLD）可在系统配置中调整。
"""
import io

import numpy as np
from PIL import Image


def _to_gray(image, size):
    img = image.convert("L").resize(size, Image.LANCZOS)
    return np.asarray(img, dtype=np.float64)


def _bits_to_hex(bits):
    bits = bits.flatten().astype(np.uint8)
    val = 0
    for b in bits:
        val = (val << 1) | int(b)
    width = (len(bits) + 3) // 4
    return format(val, f"0{width}x")


def dhash(image, hash_size=8):
    """差异哈希：比较相邻像素，得到 hash_size*hash_size 位。"""
    pixels = _to_gray(image, (hash_size + 1, hash_size))
    diff = pixels[:, 1:] > pixels[:, :-1]
    return _bits_to_hex(diff)


def _dct2(matrix):
    """二维 DCT-II（用 numpy 手写，避免依赖 scipy）。"""
    n = matrix.shape[0]
    k = np.arange(n)
    # DCT 基矩阵
    basis = np.cos(np.pi * (2 * k[:, None] + 1) * k[None, :] / (2 * n))
    return basis @ matrix @ basis.T


def phash(image, hash_size=8, highfreq_factor=4):
    """感知哈希：对 32x32 灰度图做 DCT，取左上低频 8x8，与中位数比较。"""
    img_size = hash_size * highfreq_factor
    pixels = _to_gray(image, (img_size, img_size))
    dct = _dct2(pixels)
    low = dct[:hash_size, :hash_size]
    # 排除直流分量（low[0,0]）后取中位数
    med = np.median(low[1:, 1:])
    bits = low > med
    return _bits_to_hex(bits)


def compute_hash(image_bytes):
    """对图片字节计算组合哈希字符串： 'dhash:phash'。"""
    image = Image.open(io.BytesIO(image_bytes))
    return f"{dhash(image)}:{phash(image)}"


def hamming_hex(a, b):
    """两个等长十六进制串的汉明距离。"""
    if not a or not b or len(a) != len(b):
        return 64  # 视为很远
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def combined_distance(hash_a, hash_b):
    """比较两个 'dhash:phash' 组合串，返回两段汉明距离之和（0~128）。

    用「求和」而非「取最小」：只有 dHash 和 pHash 同时接近才算匹配，
    避免任一哈希在低对比度图片上偶然碰撞导致误判。
    """
    try:
        da, pa = hash_a.split(":")
        db, pb = hash_b.split(":")
    except (ValueError, AttributeError):
        return 128
    return hamming_hex(da, db) + hamming_hex(pa, pb)
