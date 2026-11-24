# teleagent-dashboard（多账号版）

TeleAgent 云电脑的**多账号保活 + 积分领取 + 电源管理 + 实例状态**控制台。
一个页面管几十个号：谁在线、谁掉了、谁该重连、今天积分领没领，一眼看完。

> Node.js 18+ / 单依赖 `ws` / 自带 Dockerfile，一台 1 核 1G 的机器就能跑。

---

## 这个版本做了什么

上游是单账号面板（一份 `data/jwt.txt` + 一套定时器）。这个版本重写成了多账号架构：

| 能力 | 说明 |
|---|---|
| 多账号隔离 | 每个号独立目录、独立配置、独立 AES 密钥，互不串味 |
| 启动错峰 | 默认 4 秒一个，几十个号不会同时打平台接口 |
| 批量导入 | 一行 `账号 密码 [备注]`，逐个登录验证，逐条回报成功/失败原因 |
| 批量操作 | 全部重连 / 全部领积分 / 保活全开 / 保活全关 |
| 健康度聚合 | `ok` / `warn` / `err` 三态，顶栏直接给"待处理"和"异常"计数 |
| 实例自愈 | 发现实例关机/排队自动 `instance/start`，恢复后打日志 |
| 网页登录 | 账号密码登录，滑块验证码自动识别，凭据快过期自动重登 |
| 界面重做 | 清单表 + 右侧详情抽屉 + 折叠日志，桌面/手机都适配 |

---

## 快速开始

### 方式一：Docker（推荐）

```bash
cp .env.example .env
vi .env                 # 把 TA_PANEL_PASSWORD 改成你自己的强密码
docker compose up -d --build
```

打开 `http://<服务器IP>:8590`，用 `.env` 里设的账号密码进面板。

> 所有数据（账号密文、JWT、网关节点、池 Key、用量）都在 `./data`，已挂载到容器 `/app/data`。
> 升级时重新 `docker compose up -d --build` 即可，数据不会丢。
> 对外提供 `/v1` 时请在前面加 HTTPS 反代（Caddy / Nginx / Cloudflare Tunnel）：
> 池 Key 与节点 Token 不能走明文，节点要用 `wss://`，且 `/v1` 流式必须关闭反代缓冲。

> 国内机器拉不到 `dl-cdn.alpinelinux.org` 时（`apk add` 报 TLS/连接重置），把 Dockerfile 里 apk 源换成国内镜像即可，构建产物不受影响。

### 方式二：直接跑 Node

```bash
npm install --omit=dev
node server.js           # 默认 http://127.0.0.1:8590
```

### 方式三：Windows 一键

```powershell
.\start-dashboard.ps1
```

---

## 面板登录保护

不设 `TA_PANEL_USER` / `TA_PANEL_PASSWORD` 就不启用鉴权 —— **只适合本机 127.0.0.1 访问**。
暴露到局域网或公网前务必设置，两种写法二选一：

```bash
# 明文（简单）
TA_PANEL_USER=admin
TA_PANEL_PASSWORD=你的强密码

# 或哈希（密码不落盘到 .env）
TA_PANEL_USER=admin
TA_PANEL_PASSWORD_HASH=$(node -e "console.log(require('crypto').createHash('sha256').update('admin:你的强密码').digest('hex'))")
```

哈希优先于明文。**改密码会让已登录的会话失效，需要重新登录面板。**

---

## 加账号

**单个加**：右上角「+ 添加账号」，填账号密码 → 登录验证通过才入库，失败会回滚，不留垃圾目录。

**批量加**：点「批量导入」，一行一个：

```
zhangsan  Pwd12345
lisi      Pwd67890   财务部
wangwu    Pwd00000   备用号
```

空格或逗号分隔，第三列是备注名（可省）。逐个登录，失败的逐条告诉你原因（密码错 / 被锁 / 没权限），成功的直接开始保活。

> 账号别写手机号带 `@` 后缀、别写错大小写 —— 平台登录页认的是**账号名**，不是手机号。错太多次会触发 60 分钟锁定。

---

## 数据放在哪

```
data/
├── accounts.json              # 账号索引（id / 名称 / 创建时间）
└── accounts/
    └── <账号id>/
        ├── config.json        # 该号的保活/积分/电源/重登设置
        ├── jwt.txt            # 平台会话凭据（明文 JWT）
        ├── .secret            # 该号专用的 AES-256 密钥（hex）
        └── state.json         # 领取记录等运行态
```

**这几样东西等于号的控制权，别打包外发、别传网盘、别进公开仓库。**
`.gitignore` 已经把 `data/` 排除掉了。

- 备份：直接打包 `data/` 整个目录带走，换机器解压即可，不用重新登录
- 清空重来：停容器 → `rm -rf data` → 起容器，回到全新状态
- 单号删除：面板上「详情 → 删除账号」

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8590` | 监听端口 |
| `TA_HOST` | `127.0.0.1`（Docker 镜像内为 `0.0.0.0`） | 绑定地址；裸 node 直跑默认只监听本机，容器内由 Dockerfile 覆盖 |
| `TA_DATA_DIR` | `./data` | 数据目录，容器里固定 `/app/data` |
| `TA_PANEL_USER` | — | 面板登录用户名，不设=不启用鉴权 |
| `TA_PANEL_PASSWORD` | — | 面板登录明文密码 |
| `TA_PANEL_PASSWORD_HASH` | — | `sha256("用户名:密码")` 的 hex，优先于明文 |
| `TA_PANEL_HINT` | — | 设任意值则在登录页显示用户名提示 |
| `TA_OFFLINE` | — | 设任意值则进离线模式：不连平台，只做本地自检 |

---

## 接口一览

所有写操作都要先过面板鉴权。

```
GET    /api/status                      全局：机器状态 + 账号清单 + 汇总
GET    /api/logs                        最近 200 条日志
GET    /api/accounts                    账号列表
POST   /api/accounts                    新增单个账号
POST   /api/accounts/bulk               批量导入
POST   /api/batch                       批量操作 {action, ids}
GET    /api/accounts/:id                单号详情
DELETE /api/accounts/:id                删号
POST   /api/accounts/:id/keepalive/toggle     保活开关
POST   /api/accounts/:id/keepalive/reconnect  立即换票重连
POST   /api/accounts/:id/desktop/open         取云电脑登录票据 URL
POST   /api/accounts/:id/points/refresh       刷积分
POST   /api/accounts/:id/points/claim         领今日积分
POST   /api/accounts/:id/power/restart        重启实例
POST   /api/accounts/:id/power/rebuild        重置实例（需 ?confirm=RESET）
POST   /api/accounts/:id/power/cancel         取消排队中的实例
POST   /api/accounts/:id/service-status       查平台服务状态
POST   /api/accounts/:id/relogin              用已存密码重登
POST   /api/accounts/:id/logout               清除该号凭据
GET    /api/accounts/:id/config               读配置
POST   /api/accounts/:id/config               改配置（白名单字段）
POST   /api/notify                            企微通知设置
POST   /api/notify/test                       发测试消息
```

`action` 取值：`reconnect` / `toggle` / `claim` / `relogin`。

---

## 给接手改代码的人

几个踩过的坑，别再踩一遍：

1. **登录是双层信封**：密码用随机 AES-128-GCM 加密，key+iv 用 RSA-OAEP(SHA256) 封装，AAD 里绑了 `loginName` / `keyId` / `timestamp` / `requestId` / `clientId`。**账号名必须 `toLowerCase()`**，服务端按小写账号解密，大小写不对就是"用户名或密码不正确"。

2. **换 token 要带上登录响应的 cookie**。回跳时服务端会连发三条同名 `operationToken`：第一条有真值，后两条是 `expires=1970` 的删除指令。用 `Object.assign` 盲目合并会被空值覆盖掉真 token —— 必须跳过空值和过期指令。这是最难查的一个 bug，日志表现为"跳转链未拿到 operationToken"。

3. **滑块用模板匹配，别用连通域**。`captcha/get` 返回原图 + `jigsawImageBase64` 拼图块。拼图块只有一小片不透明像素（约 900 个），拿它的 alpha 掩码在原图上逐列滑、只在不透明像素上算 RGB 平均绝对差，最小值就是缺口。实测 10/10 通过、平均 1.4 轮。早先"找最大白色连通域"的写法会被图片里的白色高光/文字骗到。

4. **一个号只启动一次业务循环**。凭据加载成功会触发 `onToken` 回调，新增账号时别再手动拉一遍保活和调度，否则定时器翻倍、积分重复领。代码里用 `ctx.started` 兜住。

5. **调度定时器重排前先 `clearTimeout`**，不然越跑越多。

---

## 网关池（多机分布式）

面板同时可以作为多台 Windows 机器的统一 OpenAI 兼容入口。Windows 节点上运行
`TeleAgent-OpenAI/teleagent_gateway.py` + `pool_agent.py`，节点主动出站连面板，
所以**服务器不需要能访问节点**（NAT 后也能用）。

```
OpenAI 客户端 ──HTTPS──> 面板 /v1/*（池 API Key）
                             ├── ws /api/pool/agent <── pool_agent.py（Windows）
                             └── /api/gateway/*（面板 Cookie）
```

### 使用步骤

1. 面板顶栏切到「网关池」→「添加节点」，填名称/权重/最大并发。
2. 创建成功会显示**一次性 Token** 和启动命令，复制到节点。
3. 节点上先启动本地网关（见 TeleAgent-OpenAI/README.md），再运行：

   ```bat
   python pool_agent.py --server wss://你的面板域名/api/pool/agent ^
     --node-id n_xxx --token sk-node-xxx ^
     --gateway-url http://127.0.0.1:8899 --gateway-key <TG_API_KEY>
   ```

   或把字段写进 `pool-agent.json` 后双击 `start-pool-agent.bat`（掉线自动重连）。
4. 面板「网关池」→「新建 Key」，拿到一次性的 `sk-pool-...`。
5. 客户端配置：`base_url=https://你的面板域名/v1`，`api_key=sk-pool-...`。

### 行为说明

- 路由：在线且上游正常、未达并发上限的节点中选最少连接，权重高者优先；单次请求最多换 2 个节点。
- 只在**上游还没开始返回**时重试；已经向客户端写出字节后不重试。
- 超时：等待开始 60s、空闲 90s、总时长 30min；客户端断开立即取消上游。
- 用量：非流式读 `usage`；流式自动注入 `stream_options.include_usage`，缺失时按字符数/4 估算并计入 `estTokens`。
- 限额：池 Key 可设 RPM 与每日 token 上限，超限返回 429；不填即只统计不拦截。
- 节点 45s 无心跳判离线并自动摘除，可通过面板通知配置推送。
- 节点 Token 与池 Key 都只存 sha256，明文只显示一次。

### 池管理接口（面板 Cookie 鉴权）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/gateway/overview` | 节点、Key、今日用量 |
| POST | `/api/gateway/nodes` | 新建节点，返回一次性 Token |
| PATCH/DELETE | `/api/gateway/nodes/:id` | 改名/权重/并发/启停/轮换 Token、删除 |
| POST | `/api/gateway/nodes/:id/disconnect` | 强制断开 |
| GET/POST | `/api/gateway/keys` | 列表 / 新建（返回一次性 Key） |
| PATCH/DELETE | `/api/gateway/keys/:id` | 改名/启停/限额、删除 |
| GET | `/v1/models` | 合并在线节点模型（池 Key 鉴权） |
| POST | `/v1/chat/completions` | 统一入口，支持流式（池 Key 鉴权） |

> 一个 `TA_DATA_DIR` 只能跑一个 dashboard 实例，多实例会互相覆盖节点在线状态与用量。
> 建议把面板放在 TLS 反代后，节点用 `wss://` 连接。

---

## 已知限制

- 平台接口是抓包还原的，**非官方、随时可能改**，改了就得跟着修
- 滑块求解成功率约 86%/次，靠最多 6 轮重试兜底，不是 100%
- 密码连续错误会触发平台 **60 分钟锁定**，批量导入前先确认账号密码是对的
- `power/rebuild` 会**重置实例系统盘，实例内数据全丢**，代码里强制要求 `confirm=RESET`
- 保活本质上是绕过平台的休眠策略，**账号风险自担**

---

## 安全须知

1. `8590` 别直接裸奔公网。至少反代一层 TLS，或者走 Cloudflare Tunnel / 内网穿透
2. `data/` 目录里有明文的 JWT 和加密后的账号密码，**拿到 `data/` 等于拿到号**
3. 面板密码别用弱口令，`TA_PANEL_PASSWORD_HASH` 方式更好，密码不进 `.env`
4. 多台机器部署时，`data/` 不要共享挂载，会互相覆盖运行态

---

## License

上游仓库未声明 License。自用没问题，对外分发请自行确认合规性。
