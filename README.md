# WapChat QQ (老手机 WAP 版 QQ 中转站)

这是一个为诺基亚 (Nokia)、索爱 (Sony Ericsson) 等支持 WAP/XHTML Mobile 1.0 的老手机设计的 QQ 网页版中转服务端。
通过部署本项目，你可以让老手机重新连上现代的 QQ 群，实现文字聊天、看图、发图、发表情，甚至群网盘文件的互通。

![Python](https://img.shields.io/badge/Python-Flask-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Koyeb-2496ED?logo=docker&logoColor=white)
![MongoDB](https://img.shields.io/badge/MongoDB-Atlas-47A248?logo=mongodb&logoColor=white)

## 功能

- **双端架构**：一套系统同时提供两种不同的访问体验
  - **WAP/HTML 端** (访问 `/`)：为现代智能手机、触屏设备优化，包含暗色主题、图片/文件附件上传、Emoji 等功能。
  - **WML 端** (访问 `/wml/`)：原汁原味的 WML 1.1 纯净页面，专为二十年前的诺基亚、索爱等上古机器设计。并自带**自适应转换**，若用电脑访问 `/wml/` 会自动转换为可视化的 XHTML MP1.0。
- **富文本解析**：自动过滤现代设备发来的信息，将长图、表情、语音、视频、卡片消息转化为老手机能看懂的纯文字提示。
- **云端存储**：接入 MongoDB Atlas (聊天记录与账号持久化) 和 Cloudflare R2 (图片与群文件存储)。
- **管理员系统**：防爆破设计。新用户注册需管理员通过专属接口审批放行。

## 架构

- **Web 后端**：Python (Flask)
- **机器人底层**：NapCatQQ (基于 OneBot11 协议，容器内直装)
- **数据库**：MongoDB Atlas (存放用户、消息索引)
- **对象存储**：Cloudflare R2 (存放用户发的图片和文件)

## 结构

```text
├── app.py                    # 核心后端代码 (Flask 网页路由、API 与拦截逻辑)
├── Dockerfile                # 容器构建配置 (自动配置环境与 NapCatQQ 机器人)
├── requirements.txt          # Python 依赖清单
├── face_config.json          # 原生 QQ 小黄脸表情映射库
├── all_output_result_kmj.txt # 颜文字符号库
├── sensitive_words.txt       # 违禁词/敏感词拦截库
├── LICENSE                   # 开源协议
└── README.md                 # 部署教程与说明文档 (本文档)
```

## 部署

本项目已配置 `Dockerfile`，推荐使用 [Koyeb](https://www.koyeb.com/) 或 Render 等支持 Docker 的免费云平台进行部署。

### 1. 准备工作

部署前，你需要准备好以下三个免费的云服务，并拿到对应的密钥：

1. **MongoDB Atlas**：注册一个免费的云数据库集群，获取你的 `MONGO_URI` (类似 `mongodb+srv://...`)。
2. **Cloudflare R2**：开启 R2 对象存储，创建一个桶 (Bucket)，获取你的 `Account ID`、`Access Key` 和 `Secret Key`。
3. **QQ 小号**：准备一个用来挂机做机器人的 QQ 小号（本项目内置 NapCat，会占用该小号登录）。

### 2. 在 Koyeb 上创建应用

1. 登录 Koyeb，点击 **Create Service**。
2. 选择你的代码源（可以关联你的 GitHub 仓库，或者上传 ZIP 包）。
3. 在 **Builder** (构建方式) 选项中，选择 Dockerfile。
4. 在 **Environment variables (环境变量)** 区域，逐一添加以下必须的配置：

| 变量名 (Key) | 说明 / 示例值 (Value) |
| :--- | :--- |
| `FLASK_SECRET_KEY` | **(必填)** 随便敲一长串复杂的英文字母和数字。用于保护用户登录。 |
| `MONGO_URI` | **(必填)** 你的 MongoDB 数据库完整连接链接。 |
| `R2_ACCOUNT_ID` | **(必填)** Cloudflare R2 网盘的 Account ID。 |
| `R2_ACCESS_KEY` | **(必填)** Cloudflare R2 的 Access Key。 |
| `R2_SECRET_KEY` | **(必填)** Cloudflare R2 的 Secret Key。 |
| `R2_BUCKET_NAME` | **(必填)** R2 存储桶的名称 (例如: `wapchat-drive`)。 |
| `TARGET_GROUP_ID` | **(必填)** 需要互通的 QQ 群号 (纯数字，例如 `123456789`)。 |
| `BOT_QQ` | **(必填)** 挂机做机器人的 QQ 小号 (纯数字，例如 `123456789`)。用于 NapCat 启动和消息过滤。 |
| `ADMIN_SECRET_TOKEN` | **(必填)** 自定义一段复杂密码。用于你后续审批用户。 |
| `WEBHOOK_TOKEN` | **(选填)** 用于后端验证，可随便填一段字母。 |

5. 在 **Ports (端口)** 设置中，确保暴露的端口为 **`7860`** (协议选 HTTP)。
6. 点击 **Deploy** 开始部署。Koyeb 会自动读取 Dockerfile 下载 Linux 版的 QQ 机器人并安装 Python 依赖。

### 3. 扫码登录机器人

当 Koyeb 提示部署成功并开始运行后：

1. 点击 Koyeb 控制台的 **Terminal** (终端) 或查看部署日志。
2. 在日志中，NapCatQQ 启动时会刷出一个 **二维码的链接**。
3. 拿出手机，登录你的 QQ 小号，**扫描终端里的二维码**完成登录。
4. 扫码成功后，网站也就可以正常访问了。

## 使用

- **访问方式**：
  - 现代浏览器/新手机访问：`https://你的域名/`
  - 老式诺基亚 WAP 浏览器访问：`https://你的域名/wml/`
- **用户注册**：用户访问你的网站，点击“注册”，输入账号密码后进入待审核状态。
- **管理员审批 (API 手动方式)**：注册后账号处于 `pending` (待审核) 状态，无法登录。你需要通过浏览器访问以下专属链接来放行：
  `https://你的域名/admin/approve?account=刚注册的账号名&token=你设置的ADMIN_SECRET_TOKEN`
- **删号**：如果有人捣乱，访问：
  `https://你的域名/admin/reject?account=对方账号&token=你设置的ADMIN_SECRET_TOKEN`

### 推荐：使用 WapChat Verify 部署专属独立审核站

手动输入链接审批太麻烦？且无法知道是谁注册的？我们提供了配套的 **WapChat Verify** 独立审核站项目。
它是一个基于 Cloudflare Workers 构建的无服务器后台系统。

- **防爆破/防刷屏**：带有自动 IP 限制与蜜罐防护。
- **收集申请理由**：用户在主站注册报错后，可引导其前往该独立验证站填写“申请理由”。
- **可视化管理后台**：为你提供了一个后台界面，可直观查看申请者的留言、IP 地址、地理位置，并支持一键审批放行或拉黑清理。

👉 **你可以进入其独立的 GitHub 仓库获取最新版本：[WapChat-Verify](https://github.com/amahteru/wapchat-verify)。**

## 协议

本项目采用 [MIT 许可证](./LICENSE) 开源。
