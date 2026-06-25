"""Discord 监听引擎（reply.py 的网页版后台线程实现）。

与 reply.py 等价的能力：
  - 轮询目标频道消息
  - 账号轮换 + 冷却 + 403 自动剔除
  - 中文 / 含链接消息过滤（可配置）
  - 关键词模糊匹配命中回复商品链接，未命中回复店铺信息
新增能力：
  - 用户发图片时，下载图片做感知哈希匹配，命中则回复对应商品链接

用 requests + threading 实现（不依赖 aiohttp），可在网页里启动/停止。
所有配置、账号、商品均实时从 SQLite 读取。
"""
import threading
import time
import re
from datetime import datetime

import requests

import store
import matcher

CHINESE_RE = re.compile(r"[一-鿿]")


class BotEngine:
    def __init__(self):
        self._thread = None
        self._stop = threading.Event()
        self.running = False
        self.status_msg = "未启动"
        self.last_error = ""
        self.processed = {}          # msg_id -> ts
        self.account_cursor = 0
        self.log_lines = []          # 最近日志（环形）
        self._lock = threading.Lock()

    # ---------- 日志 ----------
    def log(self, msg):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        with self._lock:
            self.log_lines.append(line)
            if len(self.log_lines) > 200:
                self.log_lines = self.log_lines[-200:]
        print(line)

    def get_logs(self):
        with self._lock:
            return list(self.log_lines)

    # ---------- 启停 ----------
    def start(self):
        if self.running:
            return False, "引擎已在运行"
        accounts = store.list_accounts()
        if not accounts:
            return False, "没有可用账号，请先在「账号管理」添加 Discord token"
        if not store.get_setting("TARGET_CHANNEL_ID"):
            return False, "未配置监听频道 ID，请先在「系统配置」填写 TARGET_CHANNEL_ID"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self.running = True
        self.status_msg = "运行中"
        return True, "引擎已启动"

    def stop(self):
        if not self.running:
            return False, "引擎未运行"
        self._stop.set()
        self.running = False
        self.status_msg = "已停止"
        self.log("🛑 收到停止指令")
        return True, "引擎已停止"

    def status(self):
        return {
            "running": self.running,
            "status_msg": self.status_msg,
            "last_error": self.last_error,
            "processed": len(self.processed),
        }

    # ---------- 账号轮换 ----------
    def _next_account(self, skip=None):
        skip = skip or []
        accounts = [a for a in store.list_accounts() if a["name"] not in skip]
        if not accounts:
            return None
        now = time.time()
        n = len(accounts)
        for i in range(n):
            idx = (self.account_cursor + i) % n
            acc = accounts[idx]
            if float(acc["next_available_time"] or 0) <= now:
                self.account_cursor = (idx + 1) % n
                return acc
        return None

    # ---------- 发送回复 ----------
    def _send_reply(self, channel_id, reply_to_msg_id, content, token, acc_name):
        try:
            headers = {"Authorization": token, "Content-Type": "application/json"}
            payload = {
                "content": content,
                "message_reference": {
                    "message_id": reply_to_msg_id,
                    "channel_id": channel_id,
                    "guild_id": None,
                },
                "allowed_mentions": {"replied_user": False},
            }
            resp = requests.post(
                f"https://discord.com/api/v9/channels/{channel_id}/messages",
                headers=headers, json=payload, timeout=10,
            )
            if resp.status_code == 403:
                store.delete_account(acc_name)
                self.log(f"❌ 账号[{acc_name}]被踢出(403)，已删除")
                return False
            return resp.status_code == 200
        except Exception as e:
            self.log(f"⚠️ 发送回复异常：{str(e)[:40]}")
            return False

    def _reply_with_rotation(self, channel_id, username, content, reply_to_msg_id):
        skip = []
        accounts = store.list_accounts()
        for _ in range(max(1, len(accounts))):
            acc = self._next_account(skip)
            if not acc:
                self.log("⏳ 所有账号冷却中/无效，无法回复")
                return
            token = acc["token"]
            if not token:
                store.delete_account(acc["name"])
                skip.append(acc["name"])
                continue
            ok = self._send_reply(channel_id, reply_to_msg_id, content, token, acc["name"])
            if ok:
                cooldown = int(store.get_setting("CHANNEL_COOLDOWN", "300") or 300)
                store.update_account_usage(
                    acc["name"], time.time() + cooldown,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                self.log(f"📤 账号[{acc['name']}]回复 @{username} | msg:{reply_to_msg_id}")
                return
            skip.append(acc["name"])
        self.log(f"❌ 所有账号尝试完毕，无法回复 msg:{reply_to_msg_id}")

    # ---------- 下载图片 ----------
    def _download_image(self, url, token):
        try:
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            self.log(f"⚠️ 下载图片失败：{str(e)[:40]}")
        return None

    # ---------- 主循环 ----------
    def _run(self):
        self.log("🚀 引擎启动，开始监听")
        last_id = None
        restart_timer = 0
        try:
            while not self._stop.is_set():
                settings = store.get_settings()
                channel_id = settings.get("TARGET_CHANNEL_ID", "")
                listen_interval = int(settings.get("LISTEN_INTERVAL", "5") or 5)
                restart_interval = int(settings.get("RESTART_INTERVAL", "3600") or 3600)
                filter_domain = settings.get("FILTER_DOMAIN", "")
                skip_chinese = settings.get("SKIP_CHINESE", "1") == "1"
                skip_link = settings.get("SKIP_LINK_MSG", "1") == "1"
                send_interval = int(settings.get("SEND_INTERVAL", "5") or 5)

                # 自动重启计时（这里只是重置游标 + 日志，不真正退出进程）
                restart_timer += listen_interval
                if restart_timer >= restart_interval:
                    restart_timer = 0
                    self.processed.clear()
                    self.log("🔄 达到重启周期，已清理消息缓存")

                accounts = store.list_accounts()
                if not accounts:
                    self.log("❌ 无可用账号，引擎停止")
                    break
                listen_token = accounts[0]["token"]

                # 拉取消息
                try:
                    url = f"https://discord.com/api/v9/channels/{channel_id}/messages?limit=10"
                    if last_id:
                        url += f"&after={last_id}"
                    headers = {"Authorization": listen_token, "User-Agent": "Mozilla/5.0"}
                    resp = requests.get(url, headers=headers, timeout=15)

                    if resp.status_code == 403:
                        store.delete_account(accounts[0]["name"])
                        self.log(f"🔄 监听账号[{accounts[0]['name']}]被踢，切换中")
                        time.sleep(listen_interval)
                        continue
                    if resp.status_code != 200:
                        self.log(f"❌ 监听失败 状态码:{resp.status_code}")
                        time.sleep(listen_interval)
                        continue

                    msgs = resp.json()
                    msgs = msgs if isinstance(msgs, list) else []
                    if not msgs:
                        time.sleep(listen_interval)
                        continue

                    # Discord 返回为倒序，最后一条是最新
                    last_id = msgs[0]["id"]
                    for msg in reversed(msgs):
                        if self._stop.is_set():
                            break
                        if not isinstance(msg, dict):
                            continue
                        if msg.get("author", {}).get("bot") or msg["id"] in self.processed:
                            continue
                        self.processed[msg["id"]] = time.time()

                        content = (msg.get("content") or "").strip()
                        author = msg.get("author", {})
                        username = author.get("username", "未知用户") if isinstance(author, dict) else "未知用户"
                        attachments = msg.get("attachments", []) or []
                        has_image = any(
                            (a.get("content_type", "").startswith("image")
                             or re.search(r"\.(png|jpe?g|gif|webp|bmp)$", a.get("filename", ""), re.I))
                            for a in attachments
                        )

                        if skip_chinese and CHINESE_RE.search(content):
                            continue
                        if str(msg.get("channel_id")) != str(channel_id):
                            continue
                        if skip_link and ("http" in content or (filter_domain and filter_domain in content)):
                            continue
                        if not content and not has_image:
                            continue

                        # 取图片字节（如有）
                        image_bytes = None
                        if has_image:
                            img_url = next(
                                (a.get("url") for a in attachments
                                 if a.get("content_type", "").startswith("image")
                                 or re.search(r"\.(png|jpe?g|gif|webp|bmp)$", a.get("filename", ""), re.I)),
                                None,
                            )
                            if img_url:
                                image_bytes = self._download_image(img_url, listen_token)

                        result = matcher.match(content=content, image_bytes=image_bytes, source="discord")
                        reply = result["link"]
                        if reply:
                            kind = {"keyword": "关键词", "image": "图片", "shop": "店铺"}.get(result["type"], result["type"])
                            self.log(f"🎯 {kind}匹配 @{username}: {content[:20]}")
                            self._reply_with_rotation(msg.get("channel_id"), username, reply, msg["id"])
                            time.sleep(send_interval)

                except requests.RequestException as e:
                    self.last_error = str(e)[:60]
                    self.log(f"⚠️ 网络异常：{str(e)[:40]}")

                time.sleep(listen_interval)
        except Exception as e:
            self.last_error = str(e)[:120]
            self.log(f"💥 引擎异常退出：{str(e)[:80]}")
        finally:
            self.running = False
            self.status_msg = "已停止"
            self.log("👋 引擎已停止")


# 全局单例
engine = BotEngine()
