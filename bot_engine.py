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
MESSAGE_RECORD_MAX_ROWS = 3000
LINK_RE = re.compile(
    r"""(?ix)
    (?:
        \b(?:https?://|www\.)[^\s<>()\[\]{}"']+
      |
        (?<![@\w.-])
        (?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+
        (?:com|net|org|io|co|cn|us|uk|de|jp|kr|ca|au|app|shop|store|xyz|top|club|site|online|link|me|info|biz|cc|tv|dev|gg|to|ru|fr|it|nl|es|in|id|br|mx)
        (?::\d{2,5})?
        (?:[/?#][^\s<>()\[\]{}"']*)?
    )
    """
)


def contains_link(text):
    return bool(LINK_RE.search(text or ""))


def is_image_attachment(attachment):
    return (
        attachment.get("content_type", "").startswith("image")
        or re.search(r"\.(png|jpe?g|gif|webp|bmp)$", attachment.get("filename", ""), re.I)
    )


class BotEngine:
    PROCESSED_MAX_ENTRIES = 5000
    PENDING_MAX_ENTRIES = 1000
    MESSAGE_RECORD_CLEANUP_INTERVAL = 300

    def __init__(self, user_id):
        self.user_id = int(user_id)
        self._thread = None
        self._stop = threading.Event()
        self.running = False
        self.status_msg = "未启动"
        self.last_error = ""
        self.processed = {}          # msg_id -> ts
        self.pending_replies = {}    # msg_id -> reply task waiting for account cooldown
        self.account_cursor = 0
        self.log_lines = []          # 最近日志（环形）
        self._lock = threading.Lock()
        self._next_record_cleanup = 0

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
        accounts = store.active_accounts(self.user_id)
        if not accounts:
            return False, "没有可用账号，请先在「账号管理」添加有效 Discord token"
        if not store.get_setting("TARGET_CHANNEL_ID", user_id=self.user_id):
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
            "pending_replies": len(self.pending_replies),
        }

    def _int_setting(self, settings, key, default, minimum=0):
        try:
            value = int(settings.get(key, default) or default)
        except (TypeError, ValueError):
            value = default
        return max(minimum, value)

    # ---------- 账号轮换 ----------
    def _ready_account_available(self):
        now = time.time()
        for acc in store.active_accounts(self.user_id):
            if float(acc["next_available_time"] or 0) <= now:
                return True
        return False

    def _next_account(self, skip=None):
        skip = skip or []
        accounts = [a for a in store.active_accounts(self.user_id) if a["name"] not in skip]
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

    def _start_thread_from_message(self, channel_id, message_id, token, acc_name):
        thread_name = (
            store.get_setting("THREAD_NAME", "Share links here", user_id=self.user_id)
            or "Share links here"
        ).strip()
        thread_name = thread_name[:100] or "Share links here"
        try:
            headers = {"Authorization": token, "Content-Type": "application/json"}
            payload = {"name": thread_name, "auto_archive_duration": 1440}
            resp = requests.post(
                f"https://discord.com/api/v9/channels/{channel_id}/messages/{message_id}/threads",
                headers=headers, json=payload, timeout=10,
            )
            if resp.status_code in (200, 201):
                data = resp.json() if resp.content else {}
                thread_id = data.get("id")
                if thread_id:
                    return thread_id
            self.log(f"⚠️ 账号[{acc_name}]创建线程失败 状态码:{resp.status_code}")
        except Exception as e:
            self.log(f"⚠️ 创建线程异常：{str(e)[:40]}")
        return None

    def _send_channel_message(self, channel_id, content, token, mention_user_id=None):
        headers = {"Authorization": token, "Content-Type": "application/json"}
        payload = {"content": content}
        if mention_user_id:
            payload["content"] = f"<@{mention_user_id}> {content}"
            payload["allowed_mentions"] = {"users": [str(mention_user_id)]}
        resp = requests.post(
            f"https://discord.com/api/v9/channels/{channel_id}/messages",
            headers=headers, json=payload, timeout=10,
        )
        return resp

    # ---------- 发送回复 ----------
    def _send_reply(self, channel_id, reply_to_msg_id, content, token, acc_name, user_id=None, thread_id=None):
        try:
            mention_replied_user = store.get_setting(
                "MENTION_REPLIED_USER", "0", user_id=self.user_id
            ) == "1"
            reply_mode = (
                store.get_setting("REPLY_MODE", "reply", user_id=self.user_id) or "reply"
            ).strip().lower()
            if reply_mode == "thread":
                target_thread_id = thread_id or self._start_thread_from_message(
                    channel_id, reply_to_msg_id, token, acc_name
                )
                if target_thread_id:
                    resp = self._send_channel_message(
                        target_thread_id,
                        content,
                        token,
                        user_id if mention_replied_user else None,
                    )
                    if resp.status_code == 200:
                        return {
                            "mode": "thread",
                            "reply_channel_id": target_thread_id,
                            "account_name": acc_name,
                        }
                    self.log(f"⚠️ 账号[{acc_name}]线程回复失败 状态码:{resp.status_code}，不发送直接回复")
                    return None
                self.log(f"⚠️ 账号[{acc_name}]无法创建/获取线程，不发送直接回复")
                return None

            headers = {"Authorization": token, "Content-Type": "application/json"}
            payload = {
                "content": content,
                "message_reference": {
                    "message_id": reply_to_msg_id,
                    "channel_id": channel_id,
                    "guild_id": None,
                },
                "allowed_mentions": {"replied_user": mention_replied_user},
            }
            resp = requests.post(
                f"https://discord.com/api/v9/channels/{channel_id}/messages",
                headers=headers, json=payload, timeout=10,
            )
            if resp.status_code == 403:
                store.mark_account_invalid(self.user_id, acc_name, "发送回复返回 403")
                self.log(f"❌ 账号[{acc_name}]发送返回403，已标记失效")
                return None
            if resp.status_code == 200:
                return {
                    "mode": "reply",
                    "reply_channel_id": channel_id,
                    "account_name": acc_name,
                }
            return None
        except Exception as e:
            self.log(f"⚠️ 发送回复异常：{str(e)[:40]}")
            return None

    def _reply_with_rotation(self, channel_id, username, content, reply_to_msg_id, user_id=None, thread_id=None):
        skip = []
        accounts = store.active_accounts(self.user_id)
        for _ in range(max(1, len(accounts))):
            acc = self._next_account(skip)
            if not acc:
                self.log("⏳ 所有账号冷却中/无效，无法回复")
                return None, "cooldown" if store.active_accounts(self.user_id) else "no_account"
            token = acc["token"]
            if not token:
                store.mark_account_invalid(self.user_id, acc["name"], "Token 为空")
                skip.append(acc["name"])
                continue
            send_info = self._send_reply(
                channel_id, reply_to_msg_id, content, token, acc["name"],
                user_id=user_id, thread_id=thread_id,
            )
            if send_info:
                cooldown = int(store.get_setting("CHANNEL_COOLDOWN", "300", user_id=self.user_id) or 300)
                store.update_account_usage(
                    self.user_id, acc["name"], time.time() + cooldown,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                )
                self.log(f"📤 账号[{acc['name']}]回复 @{username} | msg:{reply_to_msg_id}")
                return send_info, "sent"
            skip.append(acc["name"])
        self.log(f"❌ 所有账号尝试完毕，无法回复 msg:{reply_to_msg_id}")
        return None, "failed"

    def _queue_pending_reply(self, task, reply_log_enabled=True):
        msg_id = str(task["message_id"])
        if msg_id in self.pending_replies:
            return
        task["queued_at"] = time.time()
        self.pending_replies[msg_id] = task
        if reply_log_enabled:
            store.log_message_record(
                user_id=self.user_id,
                channel_id=task["channel_id"],
                message_id=msg_id,
                author_id=task.get("author_id"),
                username=task["username"],
                user_content=task["user_content"],
                had_image=task["had_image"],
                image_urls=task["image_urls"],
                reply_content=task["reply_content"],
                reply_mode="",
                reply_channel_id="",
                account_name="",
                match_type=task["match_type"],
                matched_code=task["matched_code"],
                matched_link=task["matched_link"],
                reply_status="pending",
            )
        self.log(f"⏳ 账号冷却中，已加入待回复队列 @{task['username']} | msg:{msg_id}")

    def _record_successful_reply(self, task, send_info, reply_log_enabled):
        msg_id = str(task["message_id"])
        self.processed[msg_id] = time.time()
        if reply_log_enabled:
            store.log_message_record(
                user_id=self.user_id,
                channel_id=task["channel_id"],
                message_id=msg_id,
                author_id=task.get("author_id"),
                username=task["username"],
                user_content=task["user_content"],
                had_image=task["had_image"],
                image_urls=task["image_urls"],
                reply_content=task["reply_content"],
                reply_mode=send_info.get("mode", ""),
                reply_channel_id=send_info.get("reply_channel_id", ""),
                account_name=send_info.get("account_name", ""),
                match_type=task["match_type"],
                matched_code=task["matched_code"],
                matched_link=task["matched_link"],
                reply_status="sent",
            )

    def _record_failed_reply(self, task, reason, reply_log_enabled):
        if not reply_log_enabled:
            return
        msg_id = str(task["message_id"])
        store.log_message_record(
            user_id=self.user_id,
            channel_id=task["channel_id"],
            message_id=msg_id,
            author_id=task.get("author_id"),
            username=task["username"],
            user_content=task["user_content"],
            had_image=task["had_image"],
            image_urls=task["image_urls"],
            reply_content=task["reply_content"],
            reply_mode="",
            reply_channel_id="",
            account_name="",
            match_type=task["match_type"],
            matched_code=task["matched_code"],
            matched_link=task["matched_link"],
            reply_status="failed",
            skip_reason=reason,
        )

    def _record_skipped_message(self, msg, username, content, image_urls, reason, reply_log_enabled):
        if not reply_log_enabled:
            return
        author = msg.get("author", {}) if isinstance(msg.get("author"), dict) else {}
        store.log_message_record(
            user_id=self.user_id,
            channel_id=msg.get("channel_id"),
            message_id=msg.get("id"),
            author_id=author.get("id"),
            username=username,
            user_content=content,
            had_image=bool(image_urls),
            image_urls=image_urls,
            reply_content="",
            reply_mode="",
            reply_channel_id="",
            account_name="",
            match_type="skipped",
            matched_code="",
            matched_link="",
            reply_status="skipped",
            skip_reason=reason,
        )

    def _cleanup_runtime_state(self, processed_expire, reply_log_enabled):
        now = time.time()
        expire_seconds = max(60, int(processed_expire or 3600))

        for msg_id, ts in list(self.processed.items()):
            try:
                expired = now - float(ts or 0) > expire_seconds
            except (TypeError, ValueError):
                expired = True
            if expired:
                self.processed.pop(msg_id, None)

        for msg_id, task in list(self.pending_replies.items()):
            try:
                queued_at = float(task.get("queued_at") or now)
            except (TypeError, ValueError):
                queued_at = now
            if now - queued_at > expire_seconds:
                self.pending_replies.pop(msg_id, None)
                self.processed[msg_id] = now
                self._record_failed_reply(task, "pending_expired", reply_log_enabled)
                self.log(f"⌛ 待回复过期，已移除 msg:{msg_id}")

        if len(self.processed) > self.PROCESSED_MAX_ENTRIES:
            overflow = len(self.processed) - self.PROCESSED_MAX_ENTRIES
            def processed_ts(msg_id):
                try:
                    return float(self.processed.get(msg_id, 0) or 0)
                except (TypeError, ValueError):
                    return 0

            oldest = sorted(self.processed, key=processed_ts)[:overflow]
            for msg_id in oldest:
                self.processed.pop(msg_id, None)

        if len(self.pending_replies) > self.PENDING_MAX_ENTRIES:
            overflow = len(self.pending_replies) - self.PENDING_MAX_ENTRIES
            def pending_ts(msg_id):
                try:
                    return float(self.pending_replies[msg_id].get("queued_at", 0) or 0)
                except (KeyError, TypeError, ValueError):
                    return 0

            oldest = sorted(self.pending_replies, key=pending_ts)[:overflow]
            for msg_id in oldest:
                task = self.pending_replies.pop(msg_id, None)
                if task:
                    self.processed[msg_id] = now
                    self._record_failed_reply(task, "pending_overflow", reply_log_enabled)
            self.log(f"🧹 待回复队列超过上限，已清理 {overflow} 条")

    def _cleanup_message_records(self):
        now = time.time()
        if now < self._next_record_cleanup:
            return
        deleted = store.prune_message_records(self.user_id, max_rows=MESSAGE_RECORD_MAX_ROWS)
        self._next_record_cleanup = now + self.MESSAGE_RECORD_CLEANUP_INTERVAL
        if deleted:
            self.log(f"🧹 已清理消息处理记录 {deleted} 条")

    def _process_pending_replies(self, reply_log_enabled, send_interval):
        if not self.pending_replies or not self._ready_account_available():
            return
        for msg_id, task in list(self.pending_replies.items()):
            if self._stop.is_set():
                break
            if reply_log_enabled and store.has_replied_message(self.user_id, task["channel_id"], msg_id):
                self.pending_replies.pop(msg_id, None)
                self.processed[msg_id] = time.time()
                continue
            send_info, reason = self._reply_with_rotation(
                task["channel_id"],
                task["username"],
                task["reply_content"],
                msg_id,
                user_id=task.get("user_id"),
                thread_id=task.get("thread_id"),
            )
            if send_info:
                self._record_successful_reply(task, send_info, reply_log_enabled)
                self.pending_replies.pop(msg_id, None)
                time.sleep(send_interval)
                continue
            if reason == "cooldown":
                break
            self._record_failed_reply(task, reason, reply_log_enabled)
            self.pending_replies.pop(msg_id, None)
            self.processed[msg_id] = time.time()

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
                settings = store.get_settings(self.user_id)
                channel_id = settings.get("TARGET_CHANNEL_ID", "")
                listen_interval = self._int_setting(settings, "LISTEN_INTERVAL", 5, minimum=1)
                restart_interval = self._int_setting(settings, "RESTART_INTERVAL", 3600, minimum=0)
                processed_expire = self._int_setting(settings, "PROCESSED_MSG_EXPIRE", 3600, minimum=60)
                skip_chinese = settings.get("SKIP_CHINESE", "1") == "1"
                skip_link = settings.get("SKIP_LINK_MSG", "1") == "1"
                reply_log_enabled = settings.get("REPLY_LOG_ENABLED", "1") == "1"
                send_interval = self._int_setting(settings, "SEND_INTERVAL", 5, minimum=0)
                self._cleanup_runtime_state(processed_expire, reply_log_enabled)
                self._cleanup_message_records()

                # 自动重启计时（这里只是重置游标 + 日志，不真正退出进程）
                restart_timer += listen_interval
                if restart_interval > 0 and restart_timer >= restart_interval:
                    restart_timer = 0
                    self.processed.clear()
                    self.log("🔄 达到重启周期，已清理消息缓存")

                accounts = store.active_accounts(self.user_id)
                if not accounts:
                    self.log("❌ 无有效账号，引擎停止")
                    break
                listen_token = accounts[0]["token"]
                self._process_pending_replies(reply_log_enabled, send_interval)
                if self._stop.is_set():
                    break

                # 拉取消息
                try:
                    url = f"https://discord.com/api/v9/channels/{channel_id}/messages?limit=10"
                    if last_id:
                        url += f"&after={last_id}"
                    headers = {"Authorization": listen_token, "User-Agent": "Mozilla/5.0"}
                    resp = requests.get(url, headers=headers, timeout=15)

                    if resp.status_code == 403:
                        store.mark_account_invalid(
                            self.user_id, accounts[0]["name"], "监听消息返回 403"
                        )
                        self.log(f"🔄 监听账号[{accounts[0]['name']}]返回403，已标记失效并切换")
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
                        msg_id = msg["id"]
                        msg_channel_id = msg.get("channel_id")
                        if (
                            msg.get("author", {}).get("bot")
                            or msg_id in self.processed
                            or str(msg_id) in self.pending_replies
                            or (
                                reply_log_enabled
                                and store.has_replied_message(self.user_id, msg_channel_id, msg_id)
                            )
                        ):
                            continue
                        self.processed[msg_id] = time.time()

                        content = (msg.get("content") or "").strip()
                        author = msg.get("author", {})
                        username = author.get("username", "未知用户") if isinstance(author, dict) else "未知用户"
                        user_id = author.get("id") if isinstance(author, dict) else None
                        thread = msg.get("thread") if isinstance(msg.get("thread"), dict) else {}
                        thread_id = thread.get("id") if thread else None
                        attachments = msg.get("attachments", []) or []
                        image_urls = [a.get("url") for a in attachments if is_image_attachment(a) and a.get("url")]
                        has_image = bool(image_urls)

                        blocked_keyword = store.find_blocked_keyword(self.user_id, content)
                        if blocked_keyword:
                            self._record_skipped_message(
                                msg, username, content, image_urls,
                                f"blocked_keyword:{blocked_keyword}", reply_log_enabled,
                            )
                            self.log(f"屏蔽关键字命中 @{username}: {blocked_keyword}")
                            continue
                        if skip_chinese and CHINESE_RE.search(content):
                            self._record_skipped_message(
                                msg, username, content, image_urls, "chinese", reply_log_enabled,
                            )
                            continue
                        if str(msg.get("channel_id")) != str(channel_id):
                            self._record_skipped_message(
                                msg, username, content, image_urls, "channel_mismatch", reply_log_enabled,
                            )
                            continue
                        if skip_link and contains_link(content):
                            self._record_skipped_message(
                                msg, username, content, image_urls, "link", reply_log_enabled,
                            )
                            continue
                        if not content and not has_image:
                            self._record_skipped_message(
                                msg, username, content, image_urls, "empty", reply_log_enabled,
                            )
                            continue

                        # 取全部图片字节（如有）
                        image_bytes = []
                        for img_url in image_urls:
                            data = self._download_image(img_url, listen_token)
                            if data:
                                image_bytes.append(data)

                        result = matcher.match(
                            content=content,
                            image_bytes=image_bytes,
                            source="discord",
                            record_match_log=False,
                            user_id=self.user_id,
                        )
                        reply = result["link"]
                        if reply:
                            record_match_type = "none" if result["type"] == "shop" else result["type"]
                            kind = {"keyword": "商品名", "image": "图片", "shop": "不匹配"}.get(result["type"], result["type"])
                            self.log(f"🎯 {kind}匹配 @{username}: {content[:20]}")
                            reply_task = {
                                "channel_id": msg_channel_id,
                                "message_id": msg_id,
                                "username": username,
                                "reply_content": reply,
                                "user_id": user_id,
                                "thread_id": thread_id,
                                "author_id": user_id,
                                "user_content": content,
                                "had_image": bool(image_bytes),
                                "image_urls": image_urls,
                                "match_type": record_match_type,
                                "matched_code": result["product"]["code"] if result["product"] else "",
                                "matched_link": result["link"],
                            }
                            send_info, reason = self._reply_with_rotation(
                                msg_channel_id, username, reply, msg_id,
                                user_id=user_id, thread_id=thread_id,
                            )
                            if send_info:
                                self._record_successful_reply(reply_task, send_info, reply_log_enabled)
                                time.sleep(send_interval)
                            elif reason == "cooldown":
                                self.processed.pop(msg_id, None)
                                self._queue_pending_reply(reply_task, reply_log_enabled)
                            else:
                                self._record_failed_reply(reply_task, reason, reply_log_enabled)
                        elif reply_log_enabled:
                            store.log_message_record(
                                user_id=self.user_id,
                                channel_id=msg_channel_id,
                                message_id=msg_id,
                                author_id=user_id,
                                username=username,
                                user_content=content,
                                had_image=bool(image_bytes),
                                image_urls=image_urls,
                                reply_content="",
                                reply_mode="",
                                reply_channel_id="",
                                account_name="",
                                match_type="none",
                                matched_code="",
                                matched_link="",
                                reply_status="skipped",
                                skip_reason="no_reply",
                            )

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


class BotEngineManager:
    def __init__(self):
        self._engines = {}
        self._lock = threading.Lock()

    def for_user(self, user_id):
        user_id = int(user_id)
        with self._lock:
            if user_id not in self._engines:
                self._engines[user_id] = BotEngine(user_id)
            return self._engines[user_id]

    def status(self, user_id):
        return self.for_user(user_id).status()

    def running(self, user_id):
        return self.for_user(user_id).running

    def clear_state(self, user_id):
        bot = self.for_user(user_id)
        bot.processed.clear()
        bot.pending_replies.clear()

    def stop_user(self, user_id):
        bot = self.for_user(user_id)
        if bot.running:
            bot.stop()


# 全局引擎管理器：每个登录用户一个独立监听实例
engine = BotEngineManager()
