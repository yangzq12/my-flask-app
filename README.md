# Fuck Discord — reply.py 的网页版管理后台

把原命令行脚本 `reply.py`（Discord 商品自动回复机器人）做成了带登录的网站，
并新增了**用户图片识别**功能。

## 功能

| 模块 | 说明 |
|------|------|
| 登录 | 登录名 + 密码鉴权（默认 `admin` / `admin123`） |
| 数据管理 | 商品**新增 / 修改 / 删除 / 查询**（对应截图的四个菜单） |
| 启用图片上传 | 给商品上传图片，计算感知哈希；用户发来相似图片时自动识别并回复该商品 |
| 账号管理 | 增删 Discord token（等价于 `accounts.yaml`） |
| 系统配置 | 与 `reply.py` **完全一致**的全部配置项，网页可改、引擎实时生效 |
| 引擎控制 | 一键启动/停止 Discord 监听，查看实时日志 |
| 测试匹配 | 输入文字或图片，立即查看会回复什么 |
| 对外 API | `POST /api/match`（text / image），返回匹配链接 |

## 匹配逻辑（与 reply.py 一致 + 图片增强）

1. **关键词**：移植原 `flexible_keyword_match_enhanced`，提取字母+数字核心词，
   支持 `aj6 ↔ j6` 变体、忽略大小写。命中商品名即回复其链接。
2. **图片识别**：商品图片用 dHash + pHash（感知哈希）入库；用户图片同样计算哈希，
   按汉明距离找最近商品（阈值 `IMAGE_MATCH_THRESHOLD` 可调）。
   > 感知哈希擅长识别「同一张/近重复图」。对「同款不同实拍照」识别有限，
   > 需要更强能力时可接入 CLIP 等模型。
3. **未命中**：回退店铺信息（`CUSTOM_REPLY` + `SHOP_WEBSITE`），与原逻辑一致。

## 运行

```bash
cd webapp
bash run.sh           # 自动建虚拟环境、装依赖、启动
# 默认 http://0.0.0.0:8000   登录 admin / admin123
```

环境变量：`HOST`、`PORT`、`SECRET_KEY` 可覆盖。

## 数据

- 主存储：SQLite `data/app.db`
- 商品图片：`uploads/`
- 兼容老脚本：每次改动会同步导出 `data/product_maps.yaml`（`reply.py` 仍可热重载使用）

## 与 reply.py 的关系

`reply.py` 原封不动保留。网页版用 `requests`+线程重写了监听循环（`bot_engine.py`），
逻辑等价：账号轮换、冷却、403 自动剔除、中文/链接过滤，全部可在网页配置。
