# AI 报告生成平台

多智能体自动化生成数据分析报告系统（全流程：用户体系 + AI 配置 + 材料上传 + 模版 AI 解析 + ReAct 智能体生成内容 + 组装导出 Word）。

2026-10-11 最新正式导出为 [业务项目211报告-全文优化版.docx](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/业务项目211报告-全文优化版.docx)，
包含 57 个内容模块（34 篇文字、9 张表、14 个图表模块），实际渲染为 45 页，4,677,096 字节。
本轮逐段人工审定后，以确定性全文修订保存 57 个新业务版本，并通过真实 AI 导出审核、装配和程序渲染门槛。
独立复算完成 12,231 项检查、1,748 处数字绑定、5,497 项台账数字及 214 个图点，0 差异、0 跳过。
文件 SHA-256 为 `5636561c66f0fd46a93416ca98371446bf810b7247602e8e3b3a64810d49efca`。
**正式 45 页全部重新逐页原尺寸审读通过，0 缺页、0 重复页、0 统计差异、0 阻断问题；浏览器下载与交付文件逐字节一致，[最终聚合验签](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/rescreen-pass/final-delivery-verification.json)为 `pass`。**
本轮后端 855 项及 5 个子测试、离线装配 21 项、前端 21 项及 Vite 构建通过，详见
[全文优化验证记录](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/项目211报告全文优化验证记录.md)。
旧 50 页正式修订版、55 页复核版及此前 38 模块、60 页报告均为历史批次，原文件与验签保留用于比对。

## 目录结构

```
├── .env                # 所有配置（MySQL / MinIO / JWT / 端口等）
├── backend/            # FastAPI 后端
│   ├── app/
│   │   ├── main.py             # 入口
│   │   ├── config.py           # 读取 .env
│   │   ├── models.py           # user / model_config / project / project_file / chapter_task
│   │   ├── security.py         # 密码哈希、JWT、API Key 加密
│   │   ├── storage.py          # MinIO 封装
│   │   ├── llm.py              # OpenAI 兼容调用（只下发勾选的采样参数）
│   │   ├── template_parse.py   # 模版结构识别、红字识别、章节任务拆解、AI 指令回写
│   │   ├── sandbox.py          # 子进程沙箱（rlimits 限制 CPU/内存/文件，超时强杀）
│   │   ├── generate.py         # 章节任务生成：ReAct 智能体循环（run_python/finish）
│   │   ├── report_text.py      # 来源显示与正式图题
│   │   ├── report_editorial.py # 公共方法集中与受控成文显示副本
│   │   ├── assemble.py         # 原模板装配、结构校验和渲染门槛导出
│   │   └── routers/            # auth / model-configs / projects
│   └── requirements.txt
└── frontend/           # Vue 3 + Element Plus 前端
    └── src/views/      # Login / Layout / Projects / ProjectDetail / ModelConfigs
```

## 启动

```bash
# 后端（首次先创建虚拟环境并安装依赖）
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SOFFICE_PATH=/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1

# 前端（另开终端）
cd frontend
npm install
npm run dev    # 默认 5173，被占用时自动换端口
```

数据库表和 MinIO 桶在启动时自动创建，配置全部读取项目根目录的 `.env`。

当前部署面向 **Mac 本机、单个 Uvicorn worker**。后台任务在本进程执行，批量内部继续受
`GENERATE_CONCURRENCY` 限制。开发时可用 `--reload` 替代 `--workers 1`；重载会中断正在运行的任务。
首次升级启动会增量补列，不重建数据库：运行令牌、材料/指令修订号、统计配置和失败汇总，
历史修订号初值为零。启动前按正常数据库运维流程备份已有数据。

2026-10-10 本机业务复试在现有配置上仅调整 `LLM_TRUST_ENV=false` 和
`REACT_MAX_TURNS=20`：前者处理 Mac 系统代理引起的模型连接失败，后者给校验反馈后的
修正保留更多轮次。`GENERATE_CONCURRENCY=8` 保留，已有 `LLM_CA_BUNDLE`、HTTPS
证书及域名校验继续启用；没有关闭 TLS 验证，也没有修改系统代理。

2026-10-15 增补 ReAct 稳定性配置：`REACT_MAX_TURNS` 调至 30，新增
`REACT_STALE_FINISH_NUDGE=6`（连续多轮只执行代码未提交成果时注入 finish 提醒）与
`REACT_CONTEXT_WINDOW_TOKENS` / `REACT_CONTEXT_COMPRESS_RATIO` / `REACT_CONTEXT_KEEP_TURNS`
（对话历史接近模型上下文窗口时自动压缩为【前情摘要】后继续；被服务端判定超长时强制压缩重试一次）。

2026-10-16 分析环节 JSON 自修复：模版 AI 分析（结构规划、写作指令两趟）输出非法 JSON 时，
把模型原始输出与精确解析错误回传给模型让它自己修正重出，最多
`ANALYSIS_JSON_REPAIR_ATTEMPTS` 次（默认 2，0 关闭）；每轮原始输出落盘
`backend/data/parse_output_p{项目号}[_outline][_repairN].txt`；仍失败才按原有方式报错/兜底，
不新增针对具体坏法的写死式修补规则。

模板仅支持有效的 **DOCX**；参考资料支持 DOCX/PDF，数据支持 XLSX/XLS/CSV。
数据文件旁的“统计设置”可按工作表和列索引明确单选/多选及字面分隔符；普通分类列默认不拆分。
多选默认分隔符 `/`，每条答卷记录中的重复选项去重，默认以该题非空有效作答记录数为分母。
这里“有效作答”仅指字段包含非空选项；没有质量筛选规则和独立受访者标识时，不能称为有效独立人数。
修改资料或统计设置后，需要重新分析并生成过期模块。旧内容仍可查看，过期或失败版本不能回退或导出。

2026-10-10 真实资料复核后新增统一问卷语义：系统从上传问卷原文核对各角色的完整题序、
选项域和多选模式，建立 CSV 编码与 Word 题目的结构对应，来源题号和中文选项供所有模块共用。
结构对应不等于正式施测编码确认，成文必须说明依据；复合题、额外字段及不一致版本保持待核实。
不同角色的同号题不能合并。已确定的选项域保留零频类别，比例统一使用十进制四舍五入。
没有独立受访者识别或质量筛选规则时，统计单位为提交/作答记录，不自动称有效独立人数。

正文中的中文和阿拉伯统计数字均须逐次绑定；匿名、保密和题型等文字断言须引用问卷原文。
该要求同样适用于表格及单图、组合图的标题、表注/图注和来源说明；统计设置可以支持明确的
计算方法，不能代替原件证明“所列题目均为单选/多选”或“问卷匿名填写”。缺少正式编码表等
无关限制说明也不能免除同句其他事实断言的来源要求。
统计表拒绝缺列、空值和占位成果，`note`/`notes` 在预览和 Word 中显示；题内排序采用竞争排名。
多图任务用 `charts` 数组分别保存各图证据，渲染所有子图及要求的计数、比例和分母。
图表完整性按指令中的角色和来源题号逐项核验，连续题号须全部覆盖。
导出审核通过后，多图模块逐张重绘为独立段落，按所属分节的可用宽高自然分页；新增模块接在最后一张图之后。
生成表格按实际可用宽度和内容分配列宽，长文字左对齐、续页重复表头，普通记录保持完整；
封面后的说明标题与正文相邻，图表空间足够时可同页排列。
导出图表使用较大的字号，长选项条形图采用横向标签，组合预览继续保留。
提示按任务筛选完整证据条目，完整台账位于沙箱 `./evidence.json`；参考文本证据见其中的 `references`。
`./data/reference-materials.txt` 保存台账中完整可提取的参考文本及来源位置，`./data/preceding-content.json` 保存前序有效内容，后者不能代替源证据。

每个项目只接受一个分析、生成或导出操作，忙碌时冲突修改返回 `409`。
失败重写会保留上一成功当前版本和有效报告；成功重写、回退、插删模块、分析或资料变更后需重新导出。
内容历史展示失败原因，批次展示成功/失败数量，并以 `run_id` 区分运行。

Mac 必须可使用 `/usr/bin/sandbox-exec`；Windows 使用作业对象（Job Object）隔离，无需额外组件。
代码执行按本次运行建立独立目录，Mac 上限制文件读取和写入、禁止网络，两个平台都限制进程派生、
内存、CPU 时间并执行前自检，隔离不可用时直接失败。默认脚本超时 30 秒、CPU 15 秒、内存 512 MB、
单文件 10 MB、单路日志 1 MB；使用 `.env.example` 的 `SCRIPT_SANDBOX_*` 配置调整。
注意 Windows 作业对象不提供文件读写与网络白名单，仅限制资源与进程行为。
导出还要求 LibreOffice 和 Poppler 的 `pdfinfo/pdftoppm/pdftotext`；可用 `brew install --cask libreoffice`
及 `brew install poppler` 安装。渲染门槛失败时不发布报告。
本次本机验收使用 Codex 捆绑的 LibreOffice，后端启动时显式传入 `SOFFICE_PATH`。
在 `backend` 目录重启当前本机环境，可使用以下命令；捆绑路径随运行库安装位置变化时需相应调整：

```bash
SOFFICE_PATH=/Users/mac/.cache/codex-runtimes/codex-primary-runtime/dependencies/bin/override/soffice \
  .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

末尾纯文字模块仅在明确继承单倍行距、分节为行网格且没有显式布局/复杂内容时，
关闭该模块段落的网格吸附，使其遵循模板继承行距；不改字号、正文或分节。
检查实际装配尾部，表图、空行、签署日期、分页约束、域与脚注等均保守保留，插入链仅处理符合条件的末尾叶模块。

导出时，封面/前置说明及文末可明确识别的机构落款制作日期更新为本机装配日期，
保留模板原有年月或年月日精度。报告制作日期不等于数据采集时间；正文中的调查日期、
历史年份及来源材料日期保持原值。目录按当前章节重建，清除过期标题和页码，采用无页码目录域。

## 桌面客户端（Electron）与 GitHub Actions 发布

桌面端是独立打包的客户端：内置 Python 运行时、后端源码与前端构建产物，
默认使用 **内置 SQLite**（`userData/data/app.db`）和本地文件存储（`userData/data/storage`），
不再依赖 MySQL / MinIO。服务器部署仍可通过项目根目录 `.env` 切回 MySQL + MinIO。

### 本机打包

```bash
# macOS（Apple Silicon）
node scripts/build-desktop.mjs --platform=mac

# Windows（x64）
node scripts/build-desktop.mjs --platform=win
```

脚本会依次构建前端、整理后端运行文件、准备内置 Python（缓存在 `build/cache`）、
安装桌面端依赖，最后调用 electron-builder 输出到 `release/`：

- macOS：`AIReport-<version>-arm64.dmg`、`AIReport-<version>-arm64.zip`
- Windows：`AIReport-Setup-<version>.exe`（NSIS 安装包）、`AIReport-Portable-<version>.exe`

Electron 默认复用本机缓存（macOS 为 `~/Library/Caches/electron`，
Windows 为 `%LOCALAPPDATA%\electron\Cache`）；本机构建默认使用 npmmirror 镜像下载
Electron/electron-builder 二进制（CI 环境走官方源），也可通过 `ELECTRON_CACHE`、
`ELECTRON_BUILDER_CACHE`、`ELECTRON_MIRROR` 覆盖。

### GitHub Actions 自动发布

流水线见 [.github/workflows/release.yml](.github/workflows/release.yml)：

1. 修改 `desktop/package.json` 的 `version`。
2. 提交并推送后打标签（示例为 `v1.0.1`）：

```bash
git tag v1.0.0
git push origin v1.0.0
```

3. Actions 会分别在 `windows-latest` 与 `macos-latest` 上打包，
   完成后自动创建 Release 并把 `.exe`、`.dmg`、`.zip` 挂到
   `https://github.com/lhc75/AI-report-generation/releases`。

在 Actions 页面手动触发 `Release` 工作流时，只产出可下载的 Actions 产物，不创建 Release。
`CI` 工作流在推送到 `main` / 提交 PR 时运行前端构建、Python 语法检查与桌面端语法检查。

### 关于 / 检查更新

页面右上角「关于」实时检查 GitHub Releases 最新版本（后端 `/api/app/update`，
默认缓存 5 分钟）：

- 打开客户端/登录后自动检查一次，此后每 6 小时检查；发现新版本时在「关于」上显示红点并弹通知。
- 有更新时可查看更新说明与安装包信息。桌面端直接「下载并安装」：
  macOS 下载 zip 后由主进程在退出时替换 `/Applications` 下的应用并自动重启
  （目录不可写时回退为打开安装镜像）；Windows 安装版下载 Setup 后由 PowerShell
  等待应用退出、静默安装并在原路径重启，便携版则直接替换当前 exe 后重启
  （中文用户目录通过 UTF-16 编码传参，不受 Windows PowerShell 5.1 编码影响）。
- 浏览器部署没有桌面安装能力，按钮改为「前往下载」打开发布页。
- 桌面端版本号来自 `desktop/package.json`（Electron `app.getVersion()`），
  发布新版本前请先修改该版本号并打对应 `v<version>` 标签，否则客户端不会认为有新版本。

## 已实现功能

- **用户体系**：注册 / 登录 / JWT 鉴权；项目、文件、模型配置按用户完全隔离。
- **AI 配置页面**：多套模型配置（base_url、api_key、model、system prompt、skills）；
  temperature / top_p / top_k / min_p / presence_penalty / repetition_penalty 六个参数
  各自带勾选框，勾选后才会下发给模型；支持按 Agent（解析/数据分析/写作/图表/组装/审核）
  分别覆盖配置；API Key 加密存储，不回显；支持测试连接。
- **HTTPS 证书**：模型请求使用系统证书库和 Mozilla CA，保持证书与域名校验。
  macOS/Windows 原生验证器可尝试补全服务端漏发的中间证书，但不能保证成功；Linux 上服务端需要
  正确配置完整链（例如 Nginx 使用 `fullchain.pem`）。遇到私有 CA 或代理证书，
  可在 `.env` 设置 `LLM_CA_BUNDLE=/path/to/trusted-ca.pem` 后重启后端；相对路径
  以项目根目录为基准。证书校验错误会直接返回修复提示，不进行无效重试。
  `cdn.juapi.net` 当前漏发 Let’s Encrypt YE2 中间链，本项目已提供从官方取得并
  经 Mozilla 信任根验证的 `backend/certs/letsencrypt-ye2-chain.pem`；本机 `.env`
  已通过 `LLM_CA_BUNDLE` 加载。证书来源、指纹和更新说明见
  [证书链说明](backend/certs/README.md)。服务端仍应修复完整链；本地配置始终保留
  证书、有效期和域名校验。
- **模型连接与代理**：`LLM_TRUST_ENV` 默认为 `true`，HTTPX 会读取环境及 Mac 系统代理。
  若系统代理导致模型连接中断、且模型服务可直连，在项目 `.env` 设置
  `LLM_TRUST_ENV=false` 并重启后端。该设置仅影响模型请求和模型连接测试，仍使用
  `LLM_CA_BUNDLE` 与 HTTPS 证书/域名校验，不修改系统代理。
- **第一步流程**：创建项目 → 上传模版（DOCX）、参考资料（DOCX/PDF，多份）、
  数据（Excel/CSV，多份，存 MinIO）→ 点击"分析"：
  - 模版中已有**红色字体指令**时，直接识别并按章节拆解为写作任务入库；
  - 没有红字时，调用配置的模型，结合参考资料和数据摘要生成指令，以红色字体
    写回模版（生成"带红字指令的解析模版"，可下载），同时拆解章节任务入库。
  - 章节任务以树形展示，标注类型（文字 / 表格 / 图片）和状态。

#### 模版 AI 解析的通用规则（template_parse.py）

- **指令约定**：AI 只为每个标题生成四类红色指令——【填写：…】（文字）、
  【生成表格"表名"：…】（表格，写明行列结构与数据来源）、
  【生成图片（图形类型）：…】（图片，写明图形/口径/数据来源）、
  【说明：…】（删除/替换备注，不产生写作任务）。
- **标题不动**：所有 Word 标题原样保留；AI 给出指令的章节会清空旧正文/图片/表格后
  写入红字指令；AI 未涉及的章节保持原样（安全兜底，避免误删）。
- **前置小节**：第一个标题之前的"说明/前言/序言"等小节可由 AI 整体替换正文，
  封面、落款、目录不受影响。
- **同名标题**：如多个"小结"，AI 输出按出现顺序分配，互不串扰。
- **材料上下文**：数据提示采用精简结构说明，只列工作表、提交记录数、列名和现有编码对照，
  不再附带另一套可能截断的逐题统计摘要。参考资料提供有界文本摘要，生成阶段每份最多
  20000 字符；超长摘要标明截断。可提取的 DOCX 段落、表格单元格或 PDF 页文本分别保存为
  完整参考证据记录，模型须按统一台账查询所需原文，不能把摘要未展示视为资料不存在。
- **编码对照**：数据结构说明自动识别"XX编码 + XX名称"列对，输出资料已有的编码→名称对照
  （如 education_supervisor=…（教育主管部门问卷））。成文、表格和图表沿用资料中的中文群体名称、
  已对应的题干及原选项；对应不明确时说明边界，不能自行给编码赋予题意。
- **统一逐题统计**：台账识别"键：值；键：值；…"打包答案列，按问卷群体和题目标识分别统计，
  普通分类列默认不拆分；打包答案沿用 `/` 多选约定，手动统计设置可覆盖题型和字面分隔符。
  单选、多选共用计数函数，多选每条记录内去重，默认分母为该题非空有效作答记录数。
  频次、比例、样本记录数和分母分别保存；百分比采用十进制四舍五入，不能把这些字段混合匹配，
  也不能合并不同问卷中的同号题或自行根据参考题号推定 CSV 题意。
- **完整台账与任务索引**：生成提示结合章节路径、指令和补充要求，选取相关的完整证据条目，
  为提交总数、群体计数和参考文本保留展示预算；不会从中间截断单条记录或引文。
  索引明确提示省略条数，全部统计事实、参考文本证据和统一问卷结构仍保存在沙箱
  `./evidence.json`，可按群体、题目标识或来源问卷题号查询。
- 离线回归：`cd backend && .venv/bin/python test_template_parse_offline.py`；
  完整模板与资料回归使用自建合成资料，见下方隔离验证命令。

## 第二步：按章节任务生成内容（generate.py + sandbox.py，ReAct 智能体）

- **ReAct 智能体**：所有任务统一走「思考 → 行动 → 观察」循环。模型每轮输出一个
  JSON 动作：`run_python`（在沙箱执行代码，可用 pandas/openpyxl/matplotlib，
  数据文件在 ./data/）或 `finish`（提交成果）。执行结果作为【观察】反馈给模型。
  **默认不设轮次上限**（`REACT_MAX_TURNS=0`），由`REACT_MAX_SECONDS`（默认 1 小时）
  时间预算与卡死检测终止；同一代码或同一失败观察连续重复（`REACT_STUCK_REPEAT_LIMIT`）、
  连续执行失败（`REACT_MAX_RUN_FAILURES`）才判定任务失败，不再因固定轮数提前放弃。
  长时间只执行代码不提交成果时（`REACT_STALE_FINISH_NUDGE`）与剩余轮次不足时会注入
  系统提醒；对话历史估算接近模型窗口（`REACT_CONTEXT_WINDOW_TOKENS` ×
  `REACT_CONTEXT_COMPRESS_RATIO`）时自动压缩为【前情摘要】后继续，压缩时保留最近的
  【历史失败记录】，服务端判定超长时强制压缩并重试该轮一次。
- **失败经验继承**：任务失败时，最近几轮的具体错误（代码执行异常、finish 校验打回）
  会写入失败版本的 `failure_digest`；同一任务、同一指令版本下次生成时自动注入
  “【上一次生成失败经验】”，要求模型先反思再换做法。复核模型不可用等基础设施故障
  不会写入，避免误导。
- **文件提交**：文字、表格可以先把完整成果 JSON 保存到沙箱 `./out/result.json`，
  再用 `finish` 的 `action_input` 提交 `{"result_file":"out/result.json"}`。
  系统直接读取文件并执行同一证据校验，不从观察日志复制成果；日志只显示最多 3000 字符的摘要。
  成果路径必须是 `out/` 内的相对 JSON 路径，受文件大小限制，拒绝目录穿越及符号链接、硬链接。
- **成果校验**：文字不少于 50 字；表格必须是含 columns/rows 且非空的 JSON；
  图片必须已在沙箱产出 ./out/chart.png（dpi=150，中文字体链 Hiragino Sans GB →
  PingFang SC 等），校验不通过作为观察打回，模型修正后重新 finish。
  文字成果还会检查是否混入智能体协议或过程性笔记（thought/action 字段、
  【观察】标记等），混入即打回，防止工作笔记写进报告正文。
  `evidence_id`、`metric`、`cell_evidence` 等内部字段仅用于元数据绑定，不得写入读者可见的
  正文、表格、表注或图注；来源说明应使用资料文件、问卷群体、题号和统计口径。
- **模型分工**：按章节任务类型自动选用对应 Agent 的模型配置——文字→写作 Agent，
  表格→数据分析 Agent，图片→图表 Agent；未配置时回退到全局默认配置。
  system prompt 与 ReAct 协议合并为单条 system 消息（兼容只接受一条 system 的推理服务）。
- **并发上限**：全局信号量限制同时执行的生成任务数（`GENERATE_CONCURRENCY`，
  默认 5，本次本机配置保留 8），单任务生成/重写/插入/全部生成共用；超出的任务标记为"排队中"等待执行，
  避免同时启动过多 LLM 请求和沙箱子进程拖垮系统。导出前的逐模块 AI 复核使用
  `REVIEW_CONCURRENCY`（默认 5）做受限并行，模型网关不稳时可调小（如 2-3）；
  导出内的定向修复/重生成与修复后复评也并行执行，分别沿用 `GENERATE_CONCURRENCY`
  与 `REVIEW_CONCURRENCY`；
  导出过程中 `GET /api/projects/{id}` 会返回 `progress` 字段供前端展示进度条。
- **材料缓存**：参考资料摘要与数据结构按材料修订号、文件信息、统计设置和算法版本缓存，
  批量生成时不再每个任务重复下载和解析全部文件；资料或统计设置变化后失效。
- **状态恢复**：服务重启时把卡在 生成中/排队中 的任务复位为失败、卡在 分析中
  的项目复位为失败，避免中间态锁死。
- **沙箱**：Mac Seatbelt（`sandbox-exec`）限制必要运行库和本次材料的读取，仅允许本次目录写入，
  禁止网络和派生进程；保留 CPU/内存/文件大小限制、超时 killpg 和有界日志。
  Windows 用作业对象（Job Object）实现：单进程上限阻断派生子进程、内存硬上限、用户态 CPU
  时间限额，超时或超限终止整个进程树（venv 重定向器场景直接启动基础解释器）。
  父进程读取图表产物也拒绝目录穿越、符号链接和硬链接，隔离或自检失败不回退为普通子进程。
- **分析可重复**：再次点击"分析"做增量合并——按章节路径匹配已有任务，指令未变的
  模块保留已生成内容，指令变化的标回待生成，消失的模块级联删除，新增模块插入；
  "全部生成"只跑待生成/失败的任务，即从上次停下的地方继续。
- **任务编辑**：章节标题及文字、表格、图片模块都有“编辑”按钮。标题节点可改标题，
  内容模块可改标题及完整生成指令；保存只修改任务，随后点击“生成”应用新指令。
  已有内容和历史保留，受影响内容标为过期，重新生成前不能回退为有效版本或导出。
  只改章节标题无需重生成子模块，重新导出即可。再次“分析”会恢复模板解析出的标题和指令。
  编辑保留原模板位置，指令含“生成表格/生成图片”时自动调整模块类型；忙碌时禁止编辑。
- **内容操作**：每个内容任务支持 生成 / 查看内容（文字、表格、图片预览）/ 重写
  （可附自定义提示词）/ 插入新任务（sort 顺延并自动生成）/ 删除（级联清理
  子任务、内容版本和 MinIO 图片）。每次生成/重写产生新版本，可在版本列表中
  切换查看或回退到历史版本。
- **入口**：工具栏"全部生成"（后台批量跑，任务状态 pending → queued → generating
  → done/failed，前端轮询刷新）；也可逐任务单独生成。

测试：

```bash
cd backend
.venv/bin/python test_generate_offline.py   # 沙箱/契约/回退/插入/级联删除
.venv/bin/python test_step3_offline.py      # 增量合并/材料缓存/批量并发/ReAct 循环
```

## 第三步：证据审核与模板保真导出（assemble.py）

- **项目证据快照**：分析阶段发布项目级事实台账；指纹包含材料、统计配置及算法版本。
  正文 claim 的逐数字 `statistics`、表格 `cell_metrics`、图表 `metric/value_evidence`
  分别绑定人数、比例、样本量或分母和具体证据。生成及审核仅读取快照，必要时在内存重建。
  旧元数据只在单位、上下文和证据唯一明确时兼容补全，否则按具体模块阻止导出。
- **导出前审核**：先确定性复算人数、比例、分母、单选/多选口径、表格/图表数据和
  跨模块冲突；再使用独立 review Agent 检查证据支持、群体/题目混淆和过度外推。
  能唯一定位的文本或表格单元格修正会保存为新版本并复核，无法确认时直接阻止导出。
  复核、定向修正与整稿重生成组成智能体循环：默认不设轮次上限（`REVIEW_MAX_ROUNDS=0`），
  由 `REVIEW_MAX_SECONDS` 时间预算和 `REVIEW_NO_PROGRESS_LIMIT` 无进展检测终止；
  每轮会把前几轮已提出但未解决的问题带回模型，要求先复算、换一种做法而不是重复同样修改。
  顶层 caption 开头重复的图号在复核前确定性折叠，不再作为 error 占用修复轮次。
- **来源显示**：生成提示要求使用清楚的中文来源说明。已知CSV文件名及紧邻的“CSV”
  工作表标记在导出显示副本中合并为“调查问卷数据”；多文件使用不同中文序号。
  “CSV字段/CSV对应”等叙述规范为“数据字段/数据字段对应”，邻接的文件指代一并调整。
  原始类别中的裸 `CSV`、实际文件路径和证据元数据保持原样；先校验原证据再制作显示副本，
  不覆盖历史正文或证据。上传的原始CSV数据保留，报告不内嵌CSV数据文件。
- **成文表达**：保存正文与证据先通过严格审核，再制作受控显示副本。只有前置方法真实确认全部
  适用边界时才将编码、记录单位及多选通则集中一次；未知说明、条件题、题型差异及小基数限制保留。
  正式图题使用调查对象和主题，题号与选项仍由原保存内容和证据追溯。公共条款只按精确无数字
  白名单处理，统计数字序列、表格数据和逐点绑定保持；引号内选项、标点及主题中的数字受保护。
  模型提示同步减少编排自述和重复题名，装配manifest记录原/显示正文、证据哈希及编辑策略。
- **原模板装配**：不再新建空白 Word 或让组装 Agent 决定版式。系统优先使用解析模板，
  否则使用原始上传模板，按标题完整路径、任务类型、原始指令和出现顺序定位稳定
  `slot_key`，将文字、表格和图片原位替换；用户插入模块仅按 `anchor_task_id`
  相邻插入。用户编辑的标题、可明确识别的制作落款日期和当前目录按前述规则更新；
  其余封面文字、分页、section、样式、编号、书签及未替换的图片/表格保持原样。
- **五道门槛**：只有证据、一致性、模板 slot、DOCX 结构和 LibreOffice 渲染门槛全部
  通过才上传 `{uid}/{pid}/export/` 并写入 `report_path`。渲染会检查空白页、缺字、
  页面边界溢出和页面栅格化完整性。
- **入口**：`POST /api/projects/{id}/assemble`（与生成任务共用并发上限，
  状态 assembling → analyzed）；`GET /api/projects/{id}/report` 预签名下载。
  前端工具栏"导出报告"/"下载报告"按钮。

测试：`.venv/bin/python test_reliability_offline.py`（事实台账、严格证据契约、审核修正/
阻断）和 `.venv/bin/python test_assemble_offline.py`（多 section、目录、书签、重复
指令、slot、插入模块、原位装配）。

## 隔离验收

以下离线脚本自行建立临时 SQLite、模拟对象存储和用户，使用合成 DOCX/XLSX/CSV，
禁止未模拟的模型网络请求；结束会清理测试数据，不依赖固定用户或下载目录。

```bash
cd backend
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python test_template_parse_offline.py
.venv/bin/python test_step3_offline.py
.venv/bin/python test_generate_offline.py
.venv/bin/python test_assemble_offline.py
.venv/bin/python test_reliability_offline.py
.venv/bin/python test_skills_offline.py
.venv/bin/python test_real_fixture_offline.py
# 读取 .env 的 MySQL 连接，只创建/删除 ai_report_test_<随机ID> 独立库
.venv/bin/python tests/mysql_lifecycle_check.py
# 显式真实模型验证：只读默认模型配置，合成资料写到独立测试库，记录脱敏结果
.venv/bin/python tests/real_model_check.py
cd ../frontend
npm test
npm run build
```

MySQL 验收需要创建/删除独立测试库权限。`pytest` 的 Mac 沙箱测试真实执行 Python，
模拟模型流程调用实际 LibreOffice/Poppler 渲染，不调用真实模型。真实模型脚本设置有限超时，
服务不可达、生成不合规或审核不通过均记为失败，不绕过证据或渲染门槛。

截至 2026-10-11，本轮全文优化的代码、业务内容和文件验证范围如下，完整记录见
[全文优化验证记录](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/项目211报告全文优化验证记录.md)：

| 验证范围 | 本轮结果与限制 |
| --- | --- |
| 冻结代码后完整后端 pytest | **855 项及 5 个子测试通过**，3 条既有弃用警告，16.49 秒；[本轮汇总](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/rescreen-pass/final-code-validation.json)。 |
| 本轮离线装配 | **21 项通过**，自建用户、隔离临时库、模拟存储与模型。 |
| 本轮前端测试与构建 | **21 项通过，Vite 构建成功**；既有大包体提示保留。 |
| 五组离线、独立 MySQL、专项真实 Mac 沙箱 | 沿用前轮记录，本轮仅重跑离线装配组；没有把历史结果称为本轮新跑，也没有使用业务库做测试。 |
| 原资料独立复算 | **57 个有效模块、12,231 项检查、1,748 处数字绑定、5,497 项台账数字、214 图点，0 差异、0 跳过**；[本轮审计](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/rescreen-pass/final-independent-audit.json)。 |
| 正常业务导出 | 人工审定后的确定性全文修订保存 57 个新版本；[真实 AI 导出审核](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/rescreen-pass/export-model-review.json)为 `ok`，装配与程序渲染通过。 |
| 正式文件及最终验签 | 45 页、4,677,096 字节，SHA 如开头所列；45 页全部重新审读，下载逐字节一致，[最终聚合验签](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/rescreen-pass/final-delivery-verification.json)为 `pass`。第 19 页一次简短范围提示重复保留为非阻断观察。 |

原 7 份资料、任务结构与指令、材料修订号及旧版本历史保留。公共方法集中于前置说明，
结果保留题义、基数和必要限制，结论与建议去除重复列数和技术待办。
表 6 将恒定题号/主题集中为表注，保留 7 列；表 9 只集中恒定题号，保留可变调查内容及 8 列。
两表显示副本同步重映射数字与证据位置，源单元格、原选项元数据及统计值保持；真实开放填写不作删减。
文字与表格预览和 Word 共用格式器，保存编辑仍使用原文。

### 历史：57 模块、50 页正式表达修订

以下至接口说明之前的 50 页、786 项回归及下载/验签结果，仅对应此前正式修订批次，
不代表最新 45 页全文优化版。旧记录保留，完整详情见
[正式修订验证记录](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/项目211报告正式修订验证记录.md)：

| 验证范围 | 已完成结果与限制 |
| --- | --- |
| 本轮完整后端 pytest | **786 项及 5 个子测试通过**，3 条既有依赖/启动事件弃用警告，14.00 秒；[本轮日志](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/editorial-pass/final-layout-backend-pytest.log)。新增 85 项回归（54 项显示/图题、31 项末尾文字排版）已包含在总数中。 |
| 本轮离线装配 | **21 项通过**，自建用户、隔离临时库、模拟存储与模型；[本轮日志](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/editorial-pass/final-layout-offline-assemble.log)。 |
| 五组离线回归 | 沿用前轮 154 项全部通过的记录；本轮只重跑 assemble 21 项，未将旧结果称为新跑。 |
| 前端测试与 Vite 构建 | 沿用前轮 **20 项和生产构建通过**记录，本轮未重跑、未改前端；[测试](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-10/report-quality-v3-57/frontend-tests.log)、[构建](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-10/report-quality-v3-57/frontend-build.log)。 |
| 独立 MySQL 生命周期 | 沿用前轮独立随机测试库 **14 项通过**记录，本轮未重跑，未使用业务库做测试；[验证记录](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-10/mysql-lifecycle-final-result.json)。 |
| 本轮原资料独立源审计 | **18,745 项检查、1,997 处绑定、5,497 项台账数字、214 个图点、78 处原题引用，0 差异、0 跳过**；[本轮审计](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/editorial-pass/final-independent-audit.json)。 |
| 本轮正常导出与下载 | 真实模型重写 10 模块，7 标题正常编辑；导出通过真实审核。下载与批准对象一致，57 模块、9 表 134 数据行及 48 子图完整，可见 CSV/内部编码/协议及内嵌 CSV 均为 0；[结构核验](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/editorial-pass/final-docx-structure-verification.json)、[下载验签](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/editorial-pass/final-download-verification.json)。 |
| 本轮新 Word 全页验收 | **50 页全部逐张原尺寸审读通过**，0遗漏、0重复、0统计差异、0阻断问题；[最终交付验签](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/editorial-pass/final-delivery-verification.json)为`pass`，旧55页和51页初稿记录不替代本版。 |

业务项目 211 保持用户确认的 57 个有效当前内容模块；本轮没有重新分析替换章节。
公共方法集中在前置说明，必要局部限制保留；图题、重复题名及模糊指代已修正。
统计、表格数据与图表证据保持，真实生成历史仍可查看。原 7 份资料保留。
本轮首次 51 页导出在人工检查发现末页仅两行，已保留为初稿；修复末尾文字网格吸附后正常重新审核、导出为 50 页，全部最新 50 页已重新审读通过。
逐页证据：[1—17页](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/editorial-pass/qa-final-pages-1-17.json)、[18—34页](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/editorial-pass/qa-final-pages-18-34.json)、[35—50页](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-11/editorial-pass/qa-final-pages-35-50.json)。少量逐项列数、表头较高及表9原填写提示冗长的非阻断观察保留在审读记录中。

旧 **57 模块、55 页报告、后端 701 项及 5 个子测试**为上一轮，
[旧 57 模块验证记录](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-10/项目211实际资料验证记录-57模块.md)
与 [旧 55 页交付验签](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-10/report-quality-v3-57/final-delivery-verification.json)保留作历史比对。
此前 **38 模块、60 页报告、后端 522 项及 5 个子测试**也属于历史批次，见
[旧 38 模块验证记录](/Users/mac/Documents/java/ai报告生成/output/repair-2026-10-10/项目211实际资料验证记录.md)。
资料仍缺少正式施测编码字典、独立受访者标识和质量筛选规则；报告保持资料适用边界，
不将作答记录称为有效独立人数。

接口兼容保留内容预览及下载路径；新增 `GET/PUT /api/projects/{id}/files/{file_id}/statistics-config`，
读取工作表、列索引及配置，保存 `fields` 覆盖项。章节返回 `history_count/latest_error/is_stale`，
内容版本返回 `can_rollback/is_stale`，操作启动返回 `run_id`，项目返回最近运行汇总。

任务编辑接口为 `PATCH /api/projects/{id}/tasks/{task_id}`，提交 `title`，内容模块还需提交
非空 `instruction`；返回 `changed/task_type/status`。兼容迁移新增任务的
`template_title/template_instruction`，旧行默认空，首次修改补全原模板定位信息。
更新代码后重启后端以执行兼容加列，再刷新前端。
