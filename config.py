"""集中配置：默认设置项、路径常量。

所有「与 reply.py 完全一致的配置」都在 DEFAULT_SETTINGS 中以可在网页里编辑的形式给出。
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "app.db")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
# 与原 reply.py 兼容：把关键词映射导出到该文件，老脚本仍可热重载使用
PRODUCT_MAP_FILE = os.path.join(BASE_DIR, "data", "product_maps.yaml")

# 网页会话密钥（生产环境请用环境变量覆盖）
SECRET_KEY = os.getenv("SECRET_KEY", "fuck-discord-change-me-please")

# 固定管理员登录入口。管理员用于创建/删除普通用户。
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

# 允许上传的图片类型
ALLOWED_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}

# ========== 与 reply.py 等价的全部默认配置 ==========
# 网页「系统配置」页面可逐项修改，引擎实时读取。
DEFAULT_SETTINGS = {
    # —— 基础监听配置 ——
    "TARGET_CHANNEL_ID": "",          # 监听的 Discord 频道 ID
    "CHANNEL_COOLDOWN": "300",        # 单账号回复冷却（秒）
    "SEND_INTERVAL": "5",             # 每次发送后的间隔（秒）
    "LISTEN_INTERVAL": "5",           # 轮询拉取消息的间隔（秒）
    "PROCESSED_MSG_EXPIRE": "3600",   # 已处理消息记录过期（秒）
    "RESTART_INTERVAL": "3600",       # 引擎自动重启周期（秒）
    # —— 店铺 / 回复内容 ——
    "SHOP_WEBSITE": "https://你的店铺地址.com",
    "CUSTOM_REPLY": "Visit our shop to view more products.",
    "REPLY_MODE": "reply",            # reply=直接引用回复，thread=在用户消息下开线程回复
    "THREAD_NAME": "Share links here", # 线程回复模式下的线程名称
    "MENTION_REPLIED_USER": "0",     # 1=回复消息时 @/通知被回复用户
    "REPLY_LOG_ENABLED": "1",         # 1=记录已回复消息，重启后避免重复回复
    # —— 过滤规则 ——
    "SKIP_CHINESE": "1",              # 1=中文消息不回复（与原逻辑一致）
    "SKIP_LINK_MSG": "1",             # 1=含链接/域名的消息不回复
    # —— 图片识别（新增功能）——
    "IMAGE_MATCH_ENABLED": "1",       # 1=开启用户图片识别
    "IMAGE_MATCH_THRESHOLD": "0.875", # 图片相似度阈值(0~1,越大越严格)，等价旧距离阈值 16
}

SETTING_LABELS = {
    "TARGET_CHANNEL_ID": "监听频道 ID",
    "CHANNEL_COOLDOWN": "单账号冷却(秒)",
    "SEND_INTERVAL": "发送间隔(秒)",
    "LISTEN_INTERVAL": "轮询间隔(秒)",
    "PROCESSED_MSG_EXPIRE": "消息记录过期(秒)",
    "RESTART_INTERVAL": "自动重启周期(秒)",
    "SHOP_WEBSITE": "店铺网址",
    "CUSTOM_REPLY": "默认回复语",
    "REPLY_MODE": "回复方式",
    "THREAD_NAME": "线程名称",
    "MENTION_REPLIED_USER": "回复时@用户(1/0)",
    "REPLY_LOG_ENABLED": "记录回复消息(1/0)",
    "SKIP_CHINESE": "跳过中文消息(1/0)",
    "SKIP_LINK_MSG": "跳过含链接消息(1/0)",
    "IMAGE_MATCH_ENABLED": "开启图片识别(1/0)",
    "IMAGE_MATCH_THRESHOLD": "图片相似度阈值(0-1)",
}
