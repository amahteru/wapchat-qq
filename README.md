# WapChat QQ (老手机 WAP 版 QQ 中转站)

这是一个为诺基亚 (Nokia)、索爱 (Sony Ericsson) 等支持 WAP/XHTML Mobile 1.0 的老手机设计的 QQ 网页版中转服务端。
通过部署本项目，你可以让老手机重新连上现代的 QQ 群，实现文字聊天、看图、发图、发表情，甚至群网盘文件的互通。

![Python](https://img.shields.io/badge/Python-Flask-3776AB?logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Koyeb-2496ED?logo=docker&logoColor=white)
![MongoDB](https://img.shields.io/badge/MongoDB-Atlas-47A248?logo=mongodb&logoColor=white)

## 功能

- **双端架构**：一套系统同时提供两种不同的访问体验
  - **XHTML 端** (访问 `/`)：XHTML MP 1.0 页面，面向支持 XHTML 的老手机浏览器（如诺基亚 S60、Opera Mini、UC 浏览器），包含夜间模式、图片/文件上传、颜文字等功能。
  - **WML 端** (访问 `/wml/`)：WML 1.1 页面，面向只支持 WML 的更老机型（如诺基亚 S40、索爱）。不支持 WML 的浏览器访问时会自动跳转到 XHTML 端。
- **富文本解析**：自动过滤现代设备发来的信息，将长图、表情、语音、视频、卡片消息转化为老手机能看懂的纯文字提示。
- **云端存储**：只需一个 MongoDB Atlas，聊天记录、账号、图片和网盘文件都存在里面。
- **QQ 群内审批**：新用户注册时填写申请理由，机器人在群里通知，群主或管理员直接在 QQ 里回复即可放行，无需任何管理密码或后台网站。

## 架构

- **Web 后端**：Python (Flask)
- **机器人底层**：NapCatQQ (基于 OneBot11 协议，容器内直装)
- **数据库**：MongoDB Atlas (存放用户、消息、图片与网盘文件)

## 结构

```text
├── app.py                    # 入口：Flask 应用配置与全部网页路由
├── common.py                 # 公共部分：数据库、聊天记录、图片、敏感词等
├── qq.py                     # 与 NapCat 机器人通信、查询群成员昵称
├── accounts.py               # 登录注册、昵称、IP 定位、群内审批指令
├── sending.py                # 网页用户发言：拦截检查并转发到 QQ 群
├── webhook.py                # 接收 QQ 群消息与通知并转写成网页消息
├── templates/                # 页面模板 (base.html 为公共骨架，_macros.html 为共用片段)
├── Dockerfile                # 容器构建配置 (自动配置环境与 NapCatQQ 机器人)
├── start.sh                  # 容器启动脚本 (生成机器人配置并启动服务)
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

部署前只需要准备两样东西：

1. **MongoDB Atlas**：注册一个免费的云数据库集群，获取你的 `MONGO_URI` (类似 `mongodb+srv://...`)。记得在 Network Access 里放行部署平台的出口 IP (通常填 `0.0.0.0/0`)。
2. **QQ 小号**：准备一个用来挂机做机器人的 QQ 小号，并把它拉进要互通的群（本项目内置 NapCat，会占用该小号登录）。

### 2. 在 Koyeb 上创建应用

1. 登录 Koyeb，点击 **Create Service**。
2. 选择你的代码源（可以关联你的 GitHub 仓库，或者上传 ZIP 包）。
3. 在 **Builder** (构建方式) 选项中，选择 Dockerfile。
4. 在 **Environment variables (环境变量)** 区域添加以下三项：

| 变量名 (Key) | 说明 / 示例值 (Value) |
| :--- | :--- |
| `MONGO_URI` | 你的 MongoDB 数据库完整连接链接。 |
| `TARGET_GROUP_ID` | 需要互通的 QQ 群号 (纯数字，例如 `123456789`)。 |
| `BOT_QQ` | 挂机做机器人的 QQ 小号 (纯数字，例如 `123456789`)。 |

登录会话密钥和内部 Webhook 令牌都会自动生成，不需要填写。

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
- **用户注册**：用户访问你的网站，输入账号、密码和申请理由后点击“注册”，账号进入待审核状态，暂时无法登录。
- **审批**：机器人会把申请（账号、地域、理由）发到 QQ 群里。**群主或群管理员**在群里发送下列指令即可，普通群成员发送无效：

| 指令 | 作用 |
| :--- | :--- |
| `通过 账号名` | 通过审核，对方可以登录了。 |
| `拒绝 账号名` | 拒绝并删除待审核的账号。 |
| `删号 账号名` | 删除任意账号（包括已通过的），用于清理捣乱的人。 |
| `待审核` | 列出当前所有待审核的账号和理由。 |
| `删文件 文件名` | 从网盘中删除同名文件。 |

这些指令和机器人的回复只出现在 QQ 群里，不会转发到网页聊天室。

### 存储容量

MongoDB Atlas 免费集群共 512MB。本项目会自动清理旧数据：聊天记录保留最近 5000 条，图片保留最近 500 张，网盘文件总量超过 250MB 时从最旧的开始删除。单个上传文件上限为 15MB。

## 协议

本项目采用 [MIT 许可证](./LICENSE) 开源。
