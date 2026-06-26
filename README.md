# Fuck Discord — Discord 商品自动回复平台

把原命令行脚本 `reply.py`（Discord 商品自动回复机器人）做成了带登录的网站，
支持多用户独立使用、商品文字匹配、商品图片识别和 Discord 自动回复。

## 功能

| 模块 | 说明 |
|------|------|
| 登录 / 用户管理 | 固定管理员账号可创建普通用户；每个用户的数据和监听引擎相互独立 |
| 数据管理 | 商品新增 / 修改 / 删除 / 查询 / 备份导入导出 |
| 启用图片上传 | 给商品上传一张或多张图片，计算感知哈希；用户发来相似图片时自动识别并回复该商品 |
| 账号管理 | 每个用户独立增删 Discord token |
| 系统配置 | 每个用户独立配置监听频道、回复方式、过滤规则、图片相似度阈值等 |
| 引擎控制 | 每个用户独立启动/停止 Discord 监听 |
| 消息处理记录 | 记录已拉取消息、匹配结果、回复内容和发送状态，默认只保留最新 3000 条 |
| 测试匹配 | 输入文字或图片，立即查看会回复什么 |
| 对外 API | 登录后可 `POST /api/match`（text / image），返回当前用户自己的匹配结果 |

## 匹配逻辑（商品名相似匹配 + 图片增强）

1. **商品名**：提取用户消息和商品名中的字母/数字核心词，支持 `aj6 ↔ j6`
   变体、忽略大小写。系统会遍历全部启用商品并计算相似度，回复分数最高且超过阈值的商品链接。
2. **图片识别**：商品图片用 dHash + pHash（感知哈希）入库；用户消息里的每张图片
   同样计算哈希。系统会比较「用户全部图片 x 商品全部图片」，取每个商品的最小汉明距离
   作为该商品的最大图片相似度，再选择全局最相近且超过阈值的商品。
   `IMAGE_MATCH_THRESHOLD` 是 `0~1` 的相似度阈值，默认 `0.875`，越大越严格。
   > 感知哈希擅长识别「同一张/近重复图」。对「同款不同实拍照」识别有限，
   > 需要更强能力时可接入 CLIP 等模型。
3. **未命中**：回退店铺信息（`CUSTOM_REPLY` + `SHOP_WEBSITE`），与原逻辑一致。
4. **回复方式**：`REPLY_MODE=reply` 时直接引用回复用户消息；`REPLY_MODE=thread`
   时在用户消息下创建 Discord 线程（Thread），默认线程名为 `Share links here`，
   再把回复内容发送到该线程中。
5. **消息处理记录**：`REPLY_LOG_ENABLED=1` 时，Discord 回复成功后会记录原消息 ID、
   用户文字、用户图片 URL 与完整回复内容；重启后会跳过已记录的消息，避免重复回复。
   可在「仪表盘」页面删除记录。

## 本地运行

```bash
cd webapp
bash run.sh           # 自动建虚拟环境、装依赖、启动
# 默认 http://0.0.0.0:8000   登录 admin / admin123
```

`run.sh` 适合本地测试。服务器长期运行建议使用下面的 systemd + gunicorn 部署方式。

可用环境变量：

| 变量 | 说明 |
|------|------|
| `HOST` | 本地开发绑定地址，默认 `0.0.0.0` |
| `PORT` | 本地开发端口，默认 `8000` |
| `SECRET_KEY` | Flask session 密钥，生产环境必须换成随机长字符串 |
| `ADMIN_USERNAME` | 固定管理员账号，默认 `admin` |
| `ADMIN_PASSWORD` | 固定管理员密码，默认 `admin123` |

## 服务器部署

下面以 Ubuntu / Debian 服务器为例，假设项目部署到 `/opt/webapp`，服务直接监听
`0.0.0.0:8000` 对外访问。

### 1. 安装系统依赖

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
```

### 2. 放置项目代码

把项目上传到服务器的 `/opt/webapp`。如果你已经在服务器上有代码，可以直接进入目录：

```bash
cd /opt/webapp
```

确认目录里能看到这些文件：

```bash
ls
```

至少应包含：

```text
app.py
bot_engine.py
store.py
requirements.txt
templates/
static/
```

### 3. 创建虚拟环境并安装依赖

```bash
cd /opt/webapp
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

### 4. 准备数据目录权限

这里直接使用当前登录用户运行服务。先查看当前用户名：

```bash
whoami
```

假设输出是 `ubuntu`，则执行：

```bash
cd /opt/webapp
mkdir -p data uploads
sudo chown -R ubuntu:ubuntu /opt/webapp
```

如果你的用户名不是 `ubuntu`，把命令里的 `ubuntu:ubuntu` 换成你的用户名。例如当前用户是 `root`：

```bash
sudo chown -R root:root /opt/webapp
```
后面的 systemd 配置里 `User` / `Group` 也要使用同一个用户名。

### 5. 创建 systemd 服务

```bash
sudo nano /etc/systemd/system/webapp.service
```

填入以下内容。请把 `SECRET_KEY` 和 `ADMIN_PASSWORD` 换成你自己的值：

```ini
[Unit]
Description=Discord Reply Web Platform
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/webapp
Environment=SECRET_KEY=replace-with-a-long-random-string
Environment=ADMIN_USERNAME=admin
Environment=ADMIN_PASSWORD=replace-with-your-admin-password
ExecStart=/opt/webapp/.venv/bin/gunicorn -w 1 --threads 8 -b 0.0.0.0:8000 app:app
Restart=always
RestartSec=5
User=ubuntu
Group=ubuntu

[Install]
WantedBy=multi-user.target
```

这里必须注意：

- `-b 0.0.0.0:8000` 表示直接对外监听服务器的 8000 端口。
- 建议使用 `-w 1 --threads 8`。
- 不建议开多个 gunicorn worker，例如 `-w 4`。
- 原因是 Discord 监听引擎在进程内存中运行；多个 worker 会产生多个互相独立的引擎状态，可能导致监听状态混乱。

### 6. 启动并设置开机自启

```bash
sudo systemctl daemon-reload
sudo systemctl enable webapp
sudo systemctl start webapp
```

查看状态：

```bash
sudo systemctl status webapp
```

查看实时日志：

```bash
sudo journalctl -u webapp -f
```

如果服务崩溃，`Restart=always` 会让 systemd 自动拉起。

### 7. 放行端口并访问

如果服务器启用了防火墙，需要放行 8000 端口。例如使用 UFW：

```bash
sudo ufw allow 8000/tcp
```

现在可以访问：

```text
http://你的服务器IP:8000
```

如果你使用云服务器，还需要在云厂商安全组里放行 TCP `8000` 端口。

### 8. 首次登录和创建用户

用 systemd 里配置的固定管理员账号登录：

```text
用户名：admin
密码：你在 ADMIN_PASSWORD 里配置的密码
```

管理员登录后进入「用户管理」，创建普通用户。普通用户登录后可以独立配置：

- 商品数据
- Discord token
- 监听频道
- 回复方式
- 图片识别阈值
- 屏蔽关键字
- 消息处理记录
- 自己的监听引擎

### 9. 更新代码

更新代码后执行：

```bash
cd /opt/webapp
.venv/bin/pip install -r requirements.txt
sudo systemctl restart webapp
```

查看是否启动成功：

```bash
sudo systemctl status webapp
```

### 10. 备份数据

至少需要备份：

```text
/opt/webapp/data/app.db
/opt/webapp/uploads/
```

示例：

```bash
cd /opt/webapp
tar -czf /opt/webapp_backup_$(date +%Y%m%d_%H%M%S).tar.gz data uploads
```

恢复时停止服务，解压覆盖 `data/` 和 `uploads/`，再启动服务：

```bash
sudo systemctl stop webapp
cd /opt/webapp
tar -xzf /path/to/webapp_backup_xxx.tar.gz
sudo chown -R ubuntu:ubuntu /opt/webapp
sudo systemctl start webapp
```

## 常用运维命令

```bash
sudo systemctl start webapp      # 启动
sudo systemctl stop webapp       # 停止
sudo systemctl restart webapp    # 重启
sudo systemctl status webapp     # 状态
sudo journalctl -u webapp -f     # 实时日志
```

## 数据

- 主存储：SQLite `data/app.db`
- 商品图片：`uploads/`
- 商品备份包：每个登录用户只能导出/导入自己的商品和商品图片
- 兼容老脚本：每次改动会同步导出 `data/product_maps.yaml`（`reply.py` 仍可热重载使用）

## 与 reply.py 的关系

`reply.py` 原封不动保留。网页版用 `requests`+线程重写了监听循环（`bot_engine.py`），
逻辑等价：账号轮换、冷却、403 自动剔除、中文/链接过滤，全部可在网页配置。
