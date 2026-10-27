# DSH External Service（Docker MVP）

这是按 `dsh-external-service-architecture.docx` 落地的第一版后台 Chatbot 闭环，所有运行依赖通过 Docker Compose 管理。

## 已实现的架构边界

- API Gateway/BFF：Principal 由 `X-User-Id`、`X-Tenant-Id` 传入；前端获取的 `umctoken` 使用 `Authorization: Bearer <UMC_TOKEN>` 按请求透传，原始 Token 不写入 Session 或普通日志，仅保留短指纹引用。
- Session Router：普通会话读写接口先校验 tenant/user 所有权；对话审计接口默认同样隔离，只有显式 allowlist 的管理员审计范围才可跨账号/租户读取。
- Session Persistence：共享 PostgreSQL 的追加式 `session_event`，按 `conversation_id + seq` 回放。
- WS 主通道：支持 `subscribe`、`resume`、`message`、`ack`、`cancel`；SSE 提供 `afterSeq` 重放和降级订阅。
- 幂等：`clientMessageId` 有唯一约束，重复提交不会重复创建用户事件。
- Runtime/Instance Manager：每个 conversation 一个独立逻辑租约，状态包含 `CLEAN_IDLE/STARTING/READY/BUSY/DRAINING/CLEANING/DEAD`。
- LLM Adapter：未配置模型时使用 mock，配置 `LLM_BASE_URL`/`LLM_API_KEY` 后调用 OpenAI-compatible streaming API（默认模型名 `qwen3.8-flash`）。回答生成和 JSON Skill 分类共用这组配置；流式接收会忽略合法的空 `choices` 统计帧，不将其误判为回答失败。
- Customer 千问配置：北京地域使用 `https://dashscope.aliyuncs.com/compatible-mode/v1`，模型 ID 为 `qwen3.8-flash`，key 仅放入未跟踪的 `.env` 或受保护的运行配置。`Qwen/Qwen3.8-Flash-Next` 是开源权重仓库 ID，不是该托管接口的模型 ID。[官方模型卡](https://huggingface.co/Qwen/Qwen3.8-Flash-Next)说明 Qwen3.8-Flash 是基于 Flash-Next、增加生产功能的官方托管版本；[官方 API 示例](https://www.alibabacloud.com/blog/qwen3-8-flash-next-a-new-architecture-towardsultimate-cost-efficiency_603501)列明地域端点和调用名。不要把托管服务宣称为逐权重一致的开源部署。Customer 配置不应用于 Admin 或共享知识库服务。
- Conversation languages: English and Arabic use native generation; Urdu, Malayalam and Tagalog use a validated English bridge. See [Multilingual runtime](doc/multilingual-runtime.md).
- 会话初始化：新建会话后持久化一条 `assistant.welcome` 英阿双语欢迎事件，并通过 WS/SSE 的序号回放机制展示；该事件不参与正常问答完成判定。
- 配置与 Skill API：提供版本化配置更新和运维 Skill CRUD，终端测试界面只选择会话，不上传 Skill。
- OCR Tool Gateway：只由 DSH Runtime 调用的内部工具服务，转发到 PaddleOCR-VL-1.6 产线 API；不向前端暴露 OCR 业务接口。
- Knowledge Tool Gateway：只由 DSH Runtime 调用的内部知识库工具服务，转发到 77 服务器 `18085` 的 `public/knowledge` 匿名只读接口；目录、文件和检索请求不向上游携带 Authorization。DSH 逻辑默认 `top_k=32`，向上游传递 BM25、图谱和向量三路检索模式；当前 77 代理对单次 `top_k` 的校验上限为 20，因此网关仅将线路参数安全截断到 `KNOWLEDGE_UPSTREAM_MAX_TOP_K=20`，避免上游 422；不开放上传、解析、删除、移动或原文件下载。
- Platform Data Access Gateway：只由 DSH Runtime 调用的内部平台接口服务，连接同一台 77 服务器的 UMC Data Access 根地址；仅转发申请详情、ISBN 查询和受控新增草稿三个已发布端点，调用必须携带当前请求的 UMC Token。正式提交 `type=1` 在网关层拒绝。

## 链路审计与数据留存

Backend 会把每轮用户/助手对话、Skill 路由、DSH Tool 调用与结果、LLM 请求元数据、完整流式回答和可用的 `reasoning_content` 写入 PostgreSQL 的 `audit_record` 表。生成期间的 `assistant.chunk` 仅通过 Broker 实时发送，不再为每个模型片段执行远端数据库提交；最终 `assistant.message` 和 `llm.response` 是可回放、可审计的权威结果。审计记录按 `requestId`、`runtimeId`、会话和类别建立索引，Token、密码、Authorization、API Key 等凭据形态字段会在审计副本中脱敏；原始 UMC Token 不写入数据库。Backend 后台清理任务按运行配置 `AUDIT_RETENTION_DAYS`（默认 30 天）和 `AUDIT_CLEANUP_INTERVAL_SECONDS`（默认 3600 秒）周期删除过期审计记录，用户可在控制台“运行配置 → 链路审计”热更新这两个值；该策略只清理审计表，不删除用户可见的会话历史。为降低长首 token 等待期间的空白感，WS 还会发送 `assistant.status` 安全进度事件（路由、知识检索、OCR、UMC 查询、整理和生成回答）；这些事件只包含阶段和本地化短提示，不包含原始 LLM reasoning、系统提示词或敏感参数。

测试控制台的“对话审计”页调用 `GET /api/v1/conversations/{conversationId}/audit`。默认按会话所有权校验，普通账号只能查看自己的租户和账号记录；左侧列出可访问的会话摘要和执行状态，点击会话后右侧按时间顺序显示完整执行链路。每条记录可展开查看脱敏后的 Payload，包含用户/助手内容、Skill 路由、Tool 参数与结果、LLM 请求/思考/回答和异常信息。

如果部署的是隔离的 Chatbot 管理员控制台，可通过运行环境显式开启全局审计范围：`AUDIT_ADMIN_ENABLED=true`，并将管理员的 UMC User ID 填入 `AUDIT_ADMIN_USER_IDS`（多个 ID 用逗号分隔）。仅命中 allowlist 的账号会收到 `scope=admin`，可查看任意账号和租户的审计；`*` 仅适用于已由网关隔离的管理员专用部署。部署环境中的管理员 allowlist 作为可信引导配置，优先于数据库里历史保存的关闭值，避免升级后因旧运行配置继续看不到记录。审计管理员开关和 allowlist 只能由已具备全局审计权限的管理员在控制台修改，默认关闭，不会因为选择 `admin` Portal 自动放大权限。未开启时接口返回 `scope=owner`。

## 测试控制台密码

测试控制台启动时会在 PostgreSQL `config_entry`（`scope=system`、`key=console_password`）中初始化固定控制台密码。页面登录成功后仅获得短期 HttpOnly Cookie；测试 API 和 WebSocket 均要求该 Cookie，密码不会回显到配置页、响应或普通日志。密码遗失时，使用受控的数据库管理员账号查询：

```sql
SELECT value->>'value' AS console_password
FROM config_entry
WHERE scope = 'system' AND key = 'console_password';
```

该字段按需求可恢复，但应限制 PostgreSQL 账号和备份的访问权限；不要把查询结果写入前端代码或提交到 Git。

完整的控制台登录、接口保护和审计范围说明见 [`doc/console-permissions.md`](doc/console-permissions.md)。

当前 MVP 的运行面使用后端容器内的逻辑 Runtime Lease，接口已将实例管理边界独立出来；后续可以把 `RuntimeManager` 的启动/清理实现替换为 Docker/Kubernetes 动态容器调度，而不改变会话、事件和 WebSocket 契约。

## 对话语言与欢迎语

DSH identifies the message language independently of the Portal locale. English and Arabic retain native generation. Urdu, Malayalam and Tagalog requests use private English working text with the existing business flow, followed by validated output conversion. Short references inherit the previous response language; explicit supported-language requests are honored. Ambiguous or unsupported input receives a clarification/failure receipt rather than a guessed business interpretation. Intermediate English is excluded from public messages and stream chunks. The existing console test-generation page remains English/Arabic; multilingual runtime checks are documented separately.

每个新会话的首个持久化事件为以下双语欢迎语：

> Hello! 👋 I’m your AI assistant for the National Media Authority (NMA). Tell me about your work or publishing needs, and I’ll help you find the right services.
>
> مرحباً! 👋 أنا مساعدك الذكي من الهيئة الوطنية للإعلام (NMA). أخبرني عن عملك أو احتياجاتك للنشر، وسأساعدك في اختيار الخدمات المناسبة.

## PaddleOCR-VL-1.6

OCR Gateway 默认指向 `http://ocr-vl-api:8080`。`docker compose up -d` 会启动官方 PaddleOCR-VL API 和底层 NVIDIA/CUDA VLM 推理容器，模型固定为 `PaddleOCR-VL-1.6-0.9B`（可通过 `OCR_MODEL_NAME` 配置）：

```bash
docker compose up -d
```

OCR 是 DSH 的正式内部工具依赖，未配置可用 NVIDIA GPU 时 OCR 链路不会被标记为就绪。官方服务提供 `POST /layout-parsing`，其中 `file` 接受 URL 或 Base64，`fileType=0` 表示 PDF、`fileType=1` 表示图片。生产环境可通过 `OCR_VLM_BACKEND` 在 `vllm`/`fastdeploy` 间选择，并按显卡调整 `OCR_GPU_DEVICE`。

可在容器内分别检查 Gateway 存活和完整 OCR 链路就绪状态：

```bash
docker compose exec ocr-gateway python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8100/healthz').read().decode())"
docker compose exec ocr-gateway python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8100/readyz').read().decode())"
```

DSH Runtime 的本地工具边界可用以下开发标记验证（生产环境由 DSH Harness 原生 tool-call 替换）：

```bash
curl -X POST http://localhost:8000/api/v1/conversations/{conversationId}/messages \
  -H "X-User-Id: demo-user" \
  -H "Content-Type: application/json" \
  -d '{"content":"/tool ocr.layout_parsing {\"file\":\"https://example.com/sample.pdf\",\"fileType\":0}","clientMessageId":"ocr-001"}'
```

## 77 知识库 Tool

知识库服务只在 Docker 网络内暴露给 `backend`，外部客户端通过 DSH Runtime 间接调用。首期使用附件中推荐的匿名只读接口：目录树、文件元数据和目录检索。上游地址由 `KNOWLEDGE_BASE_URL` 配置，默认是 `http://77.242.240.158:18085/api/platform/api/v1`。

DSH Runtime 的开发标记示例（DSH 外层请求仍可带当前会话的 UMC Token，但知识库 public 上游不要求该 Token）：

```bash
curl -X POST http://localhost:8000/api/v1/conversations/{conversationId}/messages \
  -H "X-User-Id: demo-user" \
  -H "Authorization: Bearer <UMC_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"content":"/tool knowledge.search {\"query\":\"media regulation\",\"folder_id\":\"34a89aa7b43b473d8326cf6540fc3894\",\"top_k\":32}","clientMessageId":"kb-001"}'
```

可用工具名：

- `knowledge.search`：必须传 `query` 和 `folder_id`；默认召回 32 条，三路模式由 `KNOWLEDGE_RETRIEVAL_MODES=bm25,graph,vector` 配置并作为兼容提示随请求传给代理。最终是否启用某一路由由 18085 上游版本决定；当前 public 返回中的 `retrieval_mode=vector`/`completed_channels=["vector"]` 只能说明向量单路完成，不能当作三路均已执行。
- `knowledge.folders_tree`：获取当前租户可见目录树。
- `knowledge.files`：读取目录文件元数据，可选 `folder_id`、`recursive`。
- `knowledge.files_page`：分页读取文件元数据，`page_size` 范围 10–100。

附件中的鉴权管理接口（上传、解析、删除、移动、原文件下载等）不在 DSH Tool 范围内；如未来开放写操作，应另行接入平台身份与权限校验。

## UMC Data Access（更新版匿名代理）

上游统一地址：`http://77.242.240.158:18085/api/platform/api/v1/public/data-access`。它是匿名 Backend 代理，但调用方必须携带自己的 UMC Bearer Token；DSH 不再配置或转发其他平台 Token。

### UMC Portal 环境切换

客户门户和后台门户是两套前后端系统，DSH 通过 Backend 环境变量统一选择上游 Base URL：

```dotenv
UMC_PORTAL=customer
UMC_CUSTOMER_BASE_URL=https://umc-customerportal.sol.daypop.ai
UMC_ADMIN_BASE_URL=https://umc-adminportal.sol.daypop.ai
```

将 `UMC_PORTAL` 改为 `admin` 并重建 Backend，即切换到 Admin Portal；`customer` 使用 Customer Portal；`public` 也使用 Customer Portal Base URL，供公开入口场景标识。Customer URL 可以写成带 `/login` 的页面地址，DSH 会自动去掉该后缀，再拼接 `/api/User/Login`、`/api/Document/Upload` 和 `/api/Document/Dowload`。`UMC_LOGIN_URL`、`UMC_DOCUMENT_BASE_URL` 仍可作为兼容旧环境的显式覆盖项，留空时完全由上述开关决定。运行配置页会显示当前开关和两套 Base URL，并提供 `customer`、`admin`、`public` 三个选项。

### UMC 客户登录 Token

当前选定门户获取 UMC Token 使用：`POST <UMC_BASE_URL>/api/User/Login`。请求体由对应门户前端组装，字段为 `loginProvider`（邮箱）、`providerKey`（前端加密后的密码）和 `loginType`（PC 端为 `2`）；登录成功后从响应中的 `token` 获取 UMC Bearer Token。密码不应以明文 `providerKey` 发送，也不应写入 DSH 配置或日志。

`/api/v1/login/umc/access-token` 是 FF AI 平台的会话交换接口，不是客户门户登录接口，DSH 不调用该接口。拿到客户门户 Token 后，按请求放入 `Authorization: Bearer <UMC_TOKEN>`，由 DSH 透传给知识库和 UMC Data Access 代理。

当前内部 Tool 映射：

- `umc.application_detail` → `POST /data-access/application-detail` → `nma-application-detail`
- `umc.book_by_isbn` → `POST /data-access/book-by-isbn` → `nma-book-by-isbn`
- `umc.add_application` → `POST /data-access/add-application` → `nma-add-new-application`

其中新增申请只允许受控草稿（`type=3`、`isTest=true`）；正式提交 `type=1` 会被 DSH 网关拒绝。新增草稿会产生真实持久化数据，必须经过用户确认。三类 Tool 都只使用当前请求的 `umctoken`，不把 Token 写入数据库或事件。

开发环境可通过 DSH Runtime 工具边界验证，例如：

```bash
curl -X POST http://localhost:8000/api/v1/conversations/{conversationId}/messages \
  -H "X-User-Id: demo-user" \
  -H "Authorization: Bearer <UMC_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"content":"/tool umc.book_by_isbn {\"isbn\":\"9781302000011\"}","clientMessageId":"isbn-001"}'
```

## 77 平台 Swagger Tool

平台 Swagger 与知识库使用相同服务器 IP，但挂载路径不同：

- Swagger/OpenAPI：`http://77.242.240.158:18085/api/platform/api/v1/openapi.json`
- 平台网关（Docker 内）：`http://platform-gateway:8102`
- 兼容应用分页：`POST /api/MyRequest/ApplicationPage`，请求体 `{"pageIndex":1,"pageSize":100}`；如启用，仍只转发当前请求的 UMC Token。
- 更新版 Data Access 工具见上节；申请详情、ISBN 和新增草稿使用 `umc.application_detail`、`umc.book_by_isbn`、`umc.add_application`，不会配置固定平台 Token。

新的 UMC 代理不要求 DSH 配置平台 Token 或 Data Gateway Token。生产调用方可在登录后将 `umctoken` 在每次 HTTP 请求的 `Authorization` 头中传入；浏览器 WebSocket 则在连接后发送一次 `{"type":"auth","umctoken":"..."}`。本地 Docker 测试控制台会使用 `.env` 中的既有测试账号自动完成客户门户登录，再按同样方式透传短期 Token；未配置账号时，DSH 只返回“需要认证”，不生成个人账户结果。

## 启动

```bash
cp .env.example .env
docker compose up --build -d
```

### 本机轻量启动（不拉 OCR，保留远端知识库）

开发本机不需要加载 PaddleOCR-VL 的 GPU 镜像时，使用独立的轻量 Compose 文件：

```bash
docker compose --env-file .env.lite -f docker-compose.lite.yml up --build -d
```

该模式使用 `.env.lite` 中配置的远程 PostgreSQL，启动前端、Backend、Redis、Platform Gateway 和 Knowledge Gateway。Knowledge Gateway 复用 `.env.example` 的 `KNOWLEDGE_*` 配置，连接既有远端 UMC 知识库；它不运行本地知识库数据库。该模式不会声明或拉取 `ocr-gateway`、`ocr-vl-api`、`ocr-vlm-server` 或本地 PostgreSQL，因此 OCR Tool 会返回不可用，普通聊天、会话历史、LLM、知识库检索和 UMC 平台接口仍可使用。默认 `.env.lite` 使用 `18180`，避免占用常见的本地 `18080` 端口。

当前选定 UMC Portal 的附件上传后，DSH 使用对应 Base URL 和当前回合的 UMC Bearer Token 从 `/api/Document/Dowload` 读取文件，再交给内部 OCR。Token 不写入会话事件；事件仅保存文件引用、文件名、MIME 类型和 PDF/图片类型。轻量模式不启动 OCR，因此附件消息会明确提示文档解析尚不可用，而不会生成脱离附件内容的回答。

测试控制台的对话页提供“附件（可仅提交附件）”区域：页面打开时，Backend 使用 `.env` 中配置的既有 UMC 测试账号调用当前选定门户的 `/api/User/Login`，按门户格式加密密码并在内存缓存短期 Token；前端只显示脱敏账号和状态，不要求手工粘贴 Token。选择本地 PDF/图片后，前端通过同源 Backend 自动调用 `/api/v1/umc/documents/upload`，由 Backend 转发至当前 Portal 的 `/api/Document/Upload`（优先 multipart 字段 `file`；兼容旧门户返回 `data=[]` 时自动回退 `files`），使用当前内存会话；上传返回的对象引用会自动放入附件消息，问题文本可以留空后发送。Backend 下载对象后向 PaddleOCR-VL 传纯 Base64（不带 data URL 前缀）和 `fileType`，符合官方 serving API 的解码方式。密码和原始 Token 不写入前端构建产物、会话事件或普通日志。

轻量和全量模式使用同一个 Compose 项目名与数据卷，因此会话历史可在两种模式间保留。若此前已运行全量模式，切换前先停止不需要的服务：

```bash
docker compose stop ocr-gateway ocr-vl-api ocr-vlm-server
```

### Docker 离线镜像 Release

GitHub Release 提供当前 Compose 全部 9 个 `linux/amd64` 镜像的 zstd 压缩分卷包，包括 DSH 自建服务、PostgreSQL、Redis 和 PaddleOCR-VL-1.6 GPU 离线镜像。每个分卷小于 GitHub 的 2 GiB 单附件限制，并配套 SHA-256 清单与恢复脚本。

生成发布资产：

```powershell
.\scripts\export-docker-release.ps1 -ZstdPath "D:\path\to\zstd.exe"
```

下载同一 Release 的全部 `dsh-docker-images-linux-amd64*` 文件和 `import-docker-release.ps1` 到同一目录后恢复：

```powershell
.\import-docker-release.ps1 -AssetDirectory . -ZstdPath "D:\path\to\zstd.exe"
docker compose up -d
```

恢复脚本会先校验每个分卷和完整压缩包的 SHA-256，再执行 `docker image load`；默认会清理重组产生的中间文件。

访问：

- 测试控制台：本机轻量配置当前为 http://localhost:18180；启动本机 DSH 网关后统一使用 http://localhost:18087。直接端口可通过 `FRONTEND_PORT` 覆盖。
- 运行配置：在控制台“运行配置”页维护 LLM API Key、DB/Redis URL、知识库/Swagger/OCR Tool URL、Tool 开关和可编辑的全局系统提示词；系统提示词会追加到每轮请求，但内置语言、安全和证据规则仍优先。UMC 会话由 Backend 使用环境变量中的既有测试账号自动获取，页面只显示脱敏状态，不提供手工 Token 输入。API Key/DB/Redis 只显示配置状态，不回显密文。
- Skill 路由：默认 `SKILL_ROUTER_MODE=llm`，用于直接调试新路由；`keyword` 保留历史确定性路由，`shadow` 只审计新路由而不改变实际执行。运行时从 Redis 中的已发布、启用 Skill 读取单一业务域、路由别名和正向示例；按当前问题与最近几条用户消息计分，选得分最高的一个业务域，再交给 LLM 从该业务域的 Skill 中选择具体项。反向示例和 Skill 内容只给 LLM 判断边界，不参与本地打分。无领域候选、模型超时、输出非法或置信度不足时，统一使用运行配置 `SKILL_ROUTER_FALLBACK_SKILL_ID` 指定的知识库兜底 Skill，默认 `general_knowledge`；候选必须已发布、启用，且只绑定 `knowledge.search`。模型分类不接收 Tool，也不能在 shadow 模式触发业务执行。PostgreSQL 是事实来源，Redis 是运行时缓存。
- Skills 配置：在控制台“Skills 配置”页以列表查看 system Skill，可点击“新增 Skill”创建或点击“编辑”修改名称、业务域、路由别名、正反向示例、允许调用的 Tools、依赖条件、状态、启用开关和行为指令；只有 `PUBLISHED` 且启用的 Skill 内容会注入对应路由，保存后对后续请求生效。依赖条件目前保留为自由文本元数据，暂不参与运行时过滤。绑定 Tool 是允许调用，不表示每轮都会调用。
- Tools 配置：在控制台“Tools 配置”页读取 OpenAPI/Swagger operation，导入并维护模型可读的 Tool 定义。后端以规范化 `HTTP 方法 + 路径` 建立唯一约束，同一个接口不能注册两次；只有启用且发布的 Tool 才能被已发布 Skill 绑定。
- 本地 Skill 同步：`python scripts/sync_skills_to_77.py` 默认只预览许可证相关 Skill；设置 `DSH_77_CONSOLE_PASSWORD` 后追加 `--publish` 才会通过 77 控制台 API 写入并发布，脚本不会修改系统 Prompt。
- 多语言业务测试：在控制台“多语言业务测试”页从 `/umc` 知识库目录生成 English/العربية 测试集，并执行 DSH 端到端路由、检索、Tool 和 5 分制评分。
- DSH API Swagger：http://localhost:8000/docs
- DSH OpenAPI JSON：http://localhost:8000/openapi.json
- 健康检查：http://localhost:8000/healthz

`knowledge-gateway` 和 `ocr-gateway` 只在 Docker 网络内供 DSH Runtime 使用，不直接发布宿主机 Swagger 端口；外部调用应通过 DSH API/Runtime 边界完成。

停止：

```bash
docker compose down
```

### 测试控制台 API

页面使用以下开发接口，均经过 DSH Principal 边界：

- `GET/PATCH /api/v1/config?scope=system`：读取或保存运行配置；敏感值由服务端遮罩，空白 Secret 不会覆盖原值。
- `GET /api/v1/skills?scope=system`、`POST /api/v1/skills`、`PUT /api/v1/skills/{skill_id}`：读取、新增或编辑 system Skill；Skill 内容在运行时按路由、版本、`PUBLISHED` 状态和启用开关加载。
- `GET /api/v1/tools`、`GET /api/v1/tools/swagger?swaggerUrl=...`、`POST /api/v1/tools/import`、`PUT /api/v1/tools/{tool_name}`：读取系统默认能力和业务 Tool Registry、扫描 Swagger、导入 operation 和维护业务 Tool；知识库/OCR 由运行配置动态提供，不落业务 Tool Registry，导入时会拒绝重复 HTTP 接口。
- `POST /api/v1/test-cases/generate`：从实时知识库目录生成 English/阿语跨业务用例，默认 `top_k=32`、`bm25,graph,vector`。
- `POST /api/v1/test-cases/run`：并发执行最多 40 条用例，返回路由、工具、完成状态、回答和 5 分制评分。

DB/Redis URL 只作为部署参数保存，修改后需要重新创建 backend 容器；LLM、知识库、平台和 OCR Tool 的地址/密钥在后续请求中热更新。生产环境应关闭开发 Principal 回退并由受信任 API Gateway 注入 `X-User-Id`/`X-Tenant-Id`。

如需删除本项目数据库和 Redis 数据，明确确认后再执行 `docker compose down -v`。

## 变更记录

- 2026-08-28：PaddleOCR-VL-1.6 改为 Docker 默认必需服务，固定 `PaddleOCR-VL-1.6-0.9B`，增加完整链路就绪检查并修复官方 VLM 服务网络解析；新增英/阿语输出策略和会话双语欢迎事件。
- 2026-08-28：增加 Docker Release 离线包导出/恢复工具，支持 9 个 Compose 镜像的 zstd 压缩、GitHub 附件分卷和 SHA-256 完整性校验。
- 2026-08-27：完成 Docker DSH 基线、请求级 UMC Token 匿名代理、草稿写入确认、知识库 `top_k` 兼容与 public 代理鉴权修复，并执行 Term1/Term2 全量回归。

逐提交说明、验证记录和兼容性变化见 [CHANGELOG.md](CHANGELOG.md)。

## WS 示例

```json
{"type":"subscribe","conversationId":"conv_xxx","afterSeq":0}
{"type":"message","conversationId":"conv_xxx","content":"你好","clientMessageId":"client-001"}
```

## 首期验收路径（G1/G2）

1. 打开测试对话台并连接 WS。
2. 新建会话，发送一条消息，观察 `user.message`、`turn.started`、`assistant.status`、`assistant.chunk`、`assistant.message` 和 `turn.completed`。`assistant.status` 会在外部 Tool 或 LLM 等待期间提供可安全展示的进度提示。
3. 刷新或通过 `afterSeq`/`resume` 重放历史事件。
4. 使用相同 `clientMessageId` 重复提交，确认服务返回 `duplicate: true` 且不会新增用户事件。
