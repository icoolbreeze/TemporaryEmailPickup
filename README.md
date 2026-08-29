# TemporaryEmailPickup

> 在 Windows 上一键**批量注册 [Creative Fabrica](https://www.creativefabrica.com) / [Studio AI](https://studio.creativefabrica.com) 账号**的桌面工具。
> 邮箱管理、指纹浏览器、验证码识别、Studio 积分读取、注册结果统计，全部内置在一个 Tkinter 应用里。

rootsh.com 十分钟临时邮箱（或长期 Outlook）只是脚手架；真正在跑的是 **CF 自动注册 + Studio 积分同步**这一整条流水线。

---

## 这个工具能做什么

- **批量申请 Creative Fabrica 账号**：每个邮箱在独立的浏览器配置中完成登录/注册、验证码识别、Cloudflare/CAPTCHA 等待、Studio 跳转、积分读取
- **多邮箱并行管理**：rootsh 十分钟临时邮箱 + Outlook 长期邮箱在同一窗口内统一显示，独立会话、Cookie、收件游标互不干扰
- **Studio 积分自动同步**：登录成功后自动从 Studio 读取 Coins，新注册账号默认 5000 积分，读取到网站实际值后覆盖
- **指纹浏览器池**：内置 Brave / Chrome / Donut Browser / VirtualBrowser 四种后端；隔离会话每个邮箱独立配置，复用会话在登录前自动登出旧账号
- **后台 FIFO 队列**：连续点击多个邮箱的「后台注册」会按点击顺序排队执行，不会并发抢占同一个浏览器实例
- **本地数据持久化**：邮箱、积分、Donut `profile_id`、VirtualBrowser worker 分配、收件列表全部写入 `%LOCALAPPDATA%\TemporaryEmailPickup\`
- **Windows DPAPI 加密**：Outlook 刷新令牌通过当前 Windows 用户凭据加密，换用户无法解密

---

## 快速开始

环境：Windows 10/11，Python 3.10+，可选 Donut Browser 或 VirtualBrowser（只用 Brave/Chrome 也可以）。

```powershell
# 1. 安装依赖
python -m pip install -r requirements.txt

# 2. 启动应用（无黑色控制台窗口）
start.vbs
#   或带控制台输出：
python app.py

# 3. 第一个 CF 账号的最小流程
#   a) 顶部「新建随机邮箱」 → 左侧出现一个新邮箱
#   b) 选中该行 → 右侧「自动打开浏览器」 → 看到 CF 登录页
#   c) 邮箱地址会自动复制到剪贴板，可直接粘贴到登录/注册表单
#   d) 遇到 Cloudflare 人机验证时，程序会暂停并保持窗口 10 分钟，
#      在浏览器里手动完成后流程会自动继续
#   e) 注册成功后，该行「积分」列会先显示 5000，再被 Studio 实际余额覆盖
```

> 旧版入口 `start.bat` 仍保留作兼容，转交给 `start.vbs`；如果系统出现极短闪现，请直接用 `start.vbs`。

---

## 核心功能详解

### 1. 邮箱

| 类型 | 来源 | 有效期 | 用途 |
|------|------|--------|------|
| rootsh 临时邮箱 | `bccto.cc`（rootsh.com 公开接口） | 10 分钟，可续期 | 一次性 CF 注册、临时接码 |
| Outlook 长期邮箱 | 用户粘贴 `邮箱----密码----OAuth客户端ID----刷新令牌` | 长期 | 已有 Outlook 账号复用 |

- 每个邮箱独立 `client`、独立 Cookie、独立收件游标
- 列表支持 `Ctrl`/`Shift` 多选，按「删除选中邮箱」可一次销毁多个
- rootsh 邮箱到期后保留在列表中，点「续期 10 分钟」可恢复
- 「立即取件」手动刷新；进入 CF 注册验证码等待阶段时，程序每 10 秒自动轮询
- 双击邮件读取正文，可保存原始 `.eml` 或下载整个邮箱
- Outlook 导入时**只验证访问权限、不读取邮件列表**；优先 Microsoft Graph `Mail.Read`，权限不匹配时回退到 OAuth2 IMAP；删除邮箱只从本地列表移除，不会触碰 Microsoft 账号

### 2. Creative Fabrica / Studio AI 自动注册（核心）

> 这是工具的真正主功能。所有邮箱行的「后台注册」列、`积分` 列、底部「后台注册状态」反馈条，都围绕这条流水线。

- 「后台注册」按钮：在可见的浏览器窗口中完成 登录→验证→注册→Studio 跳转→积分更新 全流程，结束后自动关闭后台驱动并持久化积分
- 「后台注册状态」反馈条：持续显示正在处理的邮箱、排队数量、已成功/失败/待验证/结果未确认的统计，**不会被下一项任务的普通状态信息覆盖**
- 邮箱行的状态：保留「注册成功」「注册失败」「注册成功但积分未读取」等明确结果；已注册邮箱的「后台注册」按钮会被隐藏，重启后也不会再现
- 验证码识别：监听该行邮箱，自动从 Creative Fabrica 邮件主题或正文中提取 4–8 位数字验证码并填入表单（`cf_browser.extract_verification_code`）
- 登录失败自动转注册：使用同一邮箱作为账号和密码，无需二次确认
- Studio 积分：登录或注册成功后跳转到 `studio.creativefabrica.com`，从任务栏文本或可访问标签中提取 Coins（支持 `K`/`M` 后缀）；如果 Studio 子域没有继承主站会话，会打开登录弹窗并使用同一凭据重新登录
- 旧数据兼容：已经带积分的邮箱自动识别为已注册，无需重新走流程

### 3. 浏览器与会话策略

顶部「浏览器」下拉框：Brave / Chrome / Donut / VirtualBrowser。选择会写入 `settings.json`，所有邮箱统一使用。

顶部「会话策略」下拉框：

- **隔离会话（默认）**：每个邮箱拥有独立的浏览器用户数据目录、Donut Wayfern 配置或 VirtualBrowser worker，Cookie / 登录状态 / Local Storage / 缓存互不干扰
- **复用会话**：所有邮箱依次复用同一个浏览器数据目录。每次登录或注册前，程序会先打开 CF 账户页尝试退出当前账号；无法退出则停止后续流程，避免串号。**FIFO 队列一次只处理一个浏览器任务**，可连续点击多个「后台注册」加入队列；已有可见窗口时队列会等待窗口关闭

切换浏览器/会话策略前需要关闭本程序已打开的浏览器窗口，并等待正在执行的注册任务结束。程序**不会**在所选浏览器缺失或 Donut API 不可用时静默回退。

#### Donut Browser

1. 启动 Donut，下载至少一个 Wayfern 版本，接受 Wayfern 使用条款
2. Donut 的「设置 → 集成 → 本地 API」启用 API（默认 `http://127.0.0.1:10108`）
3. 在本程序顶部点「Donut API」，填写地址和 Token；Token 通过 Windows DPAPI 加密保存，也可由 `DONUT_API_URL` / `DONUT_API_KEY` 环境变量提供
4. 隔离会话下首次打开某个邮箱时，程序会创建专用 Wayfern 配置并保存 `profile_id`，以后复用同一配置和指纹

程序通过 Donut REST API 启动配置，再让 Selenium 连接到返回的 CDP 端口；兼容 0.28.1 的 `remote_debugging_port` 和新版的 `cdp_port`。`/run` / `/kill` / CDP 自动化能力取决于当前 Donut 授权。

#### VirtualBrowser worker 池

- 池子位于 `%LOCALAPPDATA%\VirtualBrowser\Workers`，由 VirtualBrowser UI 创建，编号命名
- 「VB worker 池」设置固定数量（默认 3）；程序只使用最小的 N 个 worker，按「最近最少使用」分配空闲 worker，**绝不并发复用同一个 worker**
- 每次把空闲 worker 分配给新邮箱前，会**退出该 worker 中现有的 CF 会话并清除该站点的 Cookie / Local Storage / Session Storage**，然后才开始新流程
- 指纹本身仍属于该 worker——本工具适用于你**有权管理**的账号和流程，**不**应被视为规避 CF 规则或 CAPTCHA 的手段
- VirtualBrowser 没有公开的「创建环境」接口，补充 worker 需在 VirtualBrowser UI 中创建，本程序会自动发现

### 4. Cloudflare / CAPTCHA 处理

- 首次进入 CF 登录页或后续步骤若显示 Cloudflare / Turnstile 验证，程序会**保持窗口**并等待最多 10 分钟（`HUMAN_VERIFICATION_TIMEOUT_SECONDS = 600`）
- 在浏览器里手动通过验证后，登录/注册流程会自动继续，**不会**因「缺少登录表单」误报为「浏览器失败」
- 后台任务检测到验证时**主动停止**并把状态改为「需要手动验证」；复用会话的注册队列会**暂停**，避免下一账户覆盖验证现场
- 同一行的「自动打开」即可在可见窗口中继续处理；关闭该窗口后队列自动恢复

### 5. 浏览器窗口只读跟踪

「自动打开」和「手动打开」的窗口会被程序以每 3 秒一次的频率做**只读**检查（不会驱动或干扰窗口操作）：

- 识别到退出链接 → 自动把邮箱标记为「已注册」并显示「网站已登录」
- 停在 Studio 页面 → 读取积分并更新「积分」列
- 窗口被关闭 → 状态改为「浏览器已关闭」，释放对应的 VirtualBrowser worker

手动打开的 Brave/Chrome 窗口通过 `--remote-debugging-port=0` 暴露 127.0.0.1 上的调试端口，程序据此附加只读会话；VirtualBrowser 的手动窗口不参与跟踪。

### 6. 数据持久化

| 数据 | 位置 | 内容 |
|------|------|------|
| 邮箱 | `%LOCALAPPDATA%\TemporaryEmailPickup\mailboxes.json` | 地址、域名、隔离会话 key、Donut `profile_id`/CDP、积分、有效期、注册状态、rootsh Cookie/取件游标、邮件元数据 |
| 设置 | `%LOCALAPPDATA%\TemporaryEmailPickup\settings.json` | 浏览器选择、会话策略、VB 池大小、Donut API 凭据、窗口/分割条位置 |
| Chrome 配置 | `%LOCALAPPDATA%\TemporaryEmailPickup\browser_profiles` | 每个邮箱的隔离用户数据 |
| Brave 配置 | `%LOCALAPPDATA%\TemporaryEmailPickup\browser_profiles_brave` | 每个邮箱的隔离用户数据 |
| Donut 配置 | `%LOCALAPPDATA%\DonutBrowser` | 由 Donut 管理 |
| VirtualBrowser 配置 | `%LOCALAPPDATA%\VirtualBrowser\Workers` | 由 VirtualBrowser 管理 |

- 创建、续期、取件、删除邮件、删除邮箱、正常退出后**立即**写盘
- 邮件正文按需从 rootsh 读取，不长期保留到本地
- 销毁邮箱**不**删除 Donut / VirtualBrowser 指纹配置，也**不**强制关闭已打开的浏览器窗口
- 关闭程序只会关闭本机网络会话，不会调用额外的销毁接口；临时邮箱按网站规则自然到期
- Outlook 密码和刷新令牌**不**明文保存：使用 Windows DPAPI 加密后写入 `mailboxes.json`，换用户后无法解密

---

## 界面与状态

- 邮箱列表与详情之间、收件箱与邮件预览之间的分割位置自动保存；启动时按当前窗口大小等比恢复
- 主标题「临时邮箱管理器」下方有「后台注册状态」反馈条，常驻显示注册队列统计
- 邮箱列表列：邮箱地址 / 剩余 / 邮件 / **积分** / 状态 / **后台注册** / 浏览器打开
- 每行直接提供「自动打开」「手动打开」按钮，**无需**先在顶部切换模式

---

## 手动端到端测试

```powershell
python -u e2e_receive_test.py
```

脚本会申请一个新邮箱，等待四分钟，向该地址发送邮件后输出发件人/主题/正文，最后自动销毁测试邮箱。用来验证 rootsh 接口与本地环境是否正常。

---

## 隐私与免责

- 临时邮箱（rootsh）**不**适合接收密码、财务、身份信息等敏感内容
- Creative Fabrica / Studio AI 的注册条款与 Studio 积分政策可能随时变化；本工具与 Creative Fabrica **没有**官方关系
- VirtualBrowser 池的指纹重置功能（退出旧账号、清除站点数据）**仅**用于你**有权管理**的账号与流程，请勿用于规避第三方平台的使用条款或 CAPTCHA
- 站点接口变化时客户端也需要随之更新；本仓库不提供对 Creative Fabrica / Studio AI 长期稳定运行的承诺
- Outlook 邮箱按只读收件权限处理，**不**会从 Microsoft 账号或远程邮箱中删除任何内容

---

## 开发与测试

```powershell
# 运行各模块的单元测试
python -m pytest test_rootsh_client.py
python -m pytest test_outlook_client.py
python -m pytest test_donut_browser.py
python -m pytest test_cf_browser.py
python -m pytest test_virtual_browser.py
python -m pytest test_app_queue.py
```

模块结构：

| 文件 | 职责 |
|------|------|
| `app.py` | Tkinter 主应用、UI 布局、邮箱生命周期、注册队列、Studio 积分同步 |
| `cf_browser.py` | Creative Fabrica 登录/注册流程、验证码识别、Cloudflare 处理、Studio 积分解析 |
| `rootsh_client.py` | rootsh.com 临时邮箱的会话与收件 API |
| `outlook_client.py` | Outlook Microsoft Graph + OAuth2 IMAP 读信 |
| `donut_browser.py` | Donut Browser 本地 API 客户端与配置管理 |
| `virtual_browser.py` | VirtualBrowser worker 发现与 LRU 分配 |
| `secret_store.py` | Windows DPAPI 加/解密 |
| `e2e_receive_test.py` | 手工驱动的端到端测试脚本 |
