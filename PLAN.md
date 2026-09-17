# deepwiki — 项目计划

buda → deepwiki。定位从"佛典检索的 hermes webui"改为：**多项目的
DeepWiki 式站点** —— 首页像 deepwiki.com 一样列出若干"项目卡片"，点开一个
项目就进入对话/检索界面。项目本质仍是 agent + provider，但 agent 的身份
（system brief、toolsets、MCP servers、skills、工作目录）从全局一份变成
**per-profile**：选一个项目 = 选一个 AgentProfile，加载它对应的 skills。
**profile 的数据（skills/docs/code/workspace）放 autumnfs，FUSE 挂载；
hermes 的会话存储留在 PVC。**

UI 基本不动，删掉背景壁纸即可。

## 名字迁移（rename 本身）

- [x] GitHub 仓库 `thesues/buda` → `thesues/deepwiki`，本地 origin 指向新地址
- [ ] 目录 `~/upstream/buda` → `~/upstream/deepwiki`（含 CLAUDE.md 里的旧名）
- [ ] 代码/文档/镜像/k8s 里的 `buda` → `deepwiki`：README、ARCHITECTURE.md、
      `BUDA_ENDPOINTS`/`BUDA_AGENT_CACHE_MAX` 等 env 前缀、docker 镜像名、
      k8s 资源名、webui 日志前缀。语料库路径 `mnt/autumn/docs/buda/` 是
      autumn 里的**数据位置**，保持不动（skill 与出处标注引用它）。

## 0. 存储：profile 数据上 autumnfs，hermes home 留在 PVC

现在 webui 是这个仓库里唯一**不**碰 autumn 的负载（README 明说，这也是它
不被 WIRE lockstep 约束的原因）：HERMES_HOME（state.db、sessions、
config.yaml）在一块 ebs-ssd PVC 上。

分工（修正后的决定）：

- **HERMES_HOME 留在 ebs-ssd PVC 上**：state.db 是 SQLite，FUSE 上的锁/写
  语义不值得赌；sessions 与 config.yaml 同居一处也简单。webui 仍不因数据
  而依赖 autumn 的 WIRE lockstep（豁免声明维持）。
- **profile 数据下沉到 autumnfs**：每个 profile 的 skills / docs / code /
  workspace 都是普通文件读写，FUSE 完全够；pod 重建不丢，改文件不用进 pod。

- [ ] autumnfs 目录规划（一个命名空间，webui 自己的 credential；
      HERMES_HOME 不在此处，见上）：
      ```
      fs/deepwiki/profiles/<key>/   每个 profile 一个文件夹（见 §1/§5）
      ├── skills/      该 profile 的 skills（预置好，见 §5）
      ├── docs/        该 profile 的 md / 语料 / 知识文件
      ├── code/        该 profile 的代码（如有）
      └── workspace/   agent 运行时的工作目录（产出、临时文件）
      ```
- [ ] k8s/webui.yaml 改造：参照 memory-mcp.yaml 的**单容器同挂载**形状
      （sidecar 传播在这个集群不工作；bidirectional propagation 泄漏挂载
      曾打挂 5 个节点——注释里都有）。webui 从"无挂载无凭证"变为
      privileged + fs.cred；WIRE lockstep 的豁免声明按"读 autumnfs 的部分
      受影响、HERMES_HOME 部分不受影响"改写。
- [ ] 启动顺序沿用 memory-mcp 的教训：等**真实读**而不是 mountpoint 出现
      （FUSE INIT 未完成时 ls 也能成功）；崩溃残留的挂载先 `fusermount3 -uz`
      再挂；`--direct-read false` 必须。
- [ ] PVC `hermes-webui-data` 保留（HERMES_HOME 继续住上面），但内容收窄：
      profiles 相关数据不再写入 PVC。

## 1. AgentProfile：把"agent 的身份"参数化（后端核心）

现在 `build_agent` 给每个 agent 同样的全局身份：`CHAT_DIRECTIVE`（env，佛典
brief）、config.yaml 的全局 toolsets、**所有** enabled 的 MCP server、同一个
skills 目录。多项目下这是污染：佛法 agent 会看到 code-index 工具，反之亦然。

- [ ] 新增 `AgentProfile`（纯配置，镜像 `Endpoint` 的做法）：`key`、`label`、
      `directive`、`toolsets`（可选，缺省继承 config.yaml）、`mcp_servers`
      （config 注册集合的**子集**）、`workspace`（该 profile 在 autumnfs 上
      的文件夹，agent 的 cwd + skills 来源）、可选 `endpoints` 子集（如视频
      agent 钉多模态模型）。
- [ ] 声明来源：**hermes config.yaml 里新增 `profiles:` 段**（与
      `mcp_servers` / `platform_toolsets` 同一份文件——改配置不改代码，
      重启生效；hermes 不认识的键会被它忽略，不影响它自己的解析）。env
      `DEEPWIKI_PROFILES`（JSON，同 `BUDA_ENDPOINTS` 风格）只作为覆盖入口，
      便于 k8s 里用 ConfigMap 注入。两者都没有时落一个内置默认 profile
      （现 CHAT_DIRECTIVE 的佛典 agent，保证老部署不破坏）。
      不做"添加项目"入口——profiles 写在配置文件里，提前定好。
- [ ] **每个 profile 一个文件夹**（§0 的 `profiles/<key>/`）：该 profile 的
      skills、md、代码、工作目录都在里面。agent 的 file/terminal 工具以
      `profiles/<key>/workspace`（或 docs/code）为 cwd——佛法 agent 看不见
      code 项目的文件，反之亦然，隔离是目录级的，不靠 prompt 自觉。
      （这些是文件不是 SQLite，放 FUSE 上没有锁语义问题。）
- [ ] `build_agent(session_id, ep, profile)`：`enabled_toolsets` /
      `mcp_server_names` 按 profile 过滤（config.yaml 照旧全量注册连接，
      哪些进 agent 由 profile 决定）；cwd 指向 profile 文件夹。
- [ ] `AgentPool.acquire` 缓存签名加 `profile.key`：换身份 = miss = 重建，
      复用 endpoint signature 的既有机制。
- [ ] `run_turn` 的 `system_message` 取 `profile.directive`；`CHAT_DIRECTIVE`
      env 保留为默认 profile 的回退。
- [ ] skills 加载：确认 hermes 的 skills toolset 读哪个目录、支持哪种
      per-agent 指定（先查 hermes 源码——per-agent skills 目录、session/new
      参数、还是别的）。目标：agent 只看到 `profiles/<key>/skills/` 里的。
      不确定就先落 `skills` 字段并把路径传给 hermes 已有的注入点。

## 2. 会话携带 profile（API 层）

- [ ] session 增加 `agent`（profile key）字段；`/api/chat/start` 接受并回显
      实际使用的 key，stale-fallback 规则与 endpoint 一致（存的 key 不在
      服务端列表里 → 回默认 profile）。
- [ ] 会话列表/持久化沿用 hermes store（在 PVC 上，§0），`agent` 随
      session 存取。
- [ ] 并发语义不变：per-endpoint `maxConcurrent` 照旧；profile 不另设限制。

## 3. 首页：项目卡片（前端）

像 deepwiki.com 首页那样，但**不抄它的 UI**，保持现在的风格：

- [ ] 打开站点先看到项目卡片（从 `/api/status` 拿 profile 列表：佛法项目、
      理解 code、文本→解说视频……），点卡片 = 用该 profile 开新会话。
- [ ] 进入会话后 UI 就是现在的聊天界面，composer 上显示当前项目（代替/
      并列 endpoint picker）；可以返回首页换项目开新会话。
- [ ] 现有 endpoint 逻辑（单 endpoint 徽章 / 多 endpoint select、localStorage
      偏好、stale 回退）不动。

## 4. 删壁纸（前端小活）

- [ ] 删 `static/img/mural.jpg` 及 style.css 里的 mural 背景、scrim、噪点层，
      用纯色/极简底色。

## 5. 预置 profiles 与 skills（部署内容）

skills **提前在 autumnfs 的 profile 文件夹里放好**（`profiles/<key>/skills/`），
镜像不再同步 skills 进 HERMES_HOME——webui.yaml 里那段 find/cp/sweep 的
同步脚本退役。改 skill = 改 autumnfs 里的文件，与解耦一致。

- [ ] 三套预置（用户选定，不做运行时添加入口）：
      `buddhist-canon-retrieval`（已有，搬进 `profiles/buda/skills/`）、
      code-understanding（代码讲解 / deepwiki 式问答）、
      text-to-explainer-video（文本直接生成解说视频的 agent skill）。
- [ ] k8s/docker 里声明三个 profile（config.yaml 的 `profiles:` 段，经
      ConfigMap 或启动 seed），各自绑定 skills 文件夹与 MCP 子集；MCP
      server 全量在 config.yaml 注册。
- [ ] memory-mcp 照旧是佛法 profile 的检索后端；code 项目若需要自己的索引
      源，作为新 MCP server 注册进 config.yaml，由 profile 选择。

## 6. 测试与文档

- [ ] tests：profile 解析（含 stale 回退）、build_agent 过滤、pool 签名、
      `/api/chat/start` 回显；endpoint_picker.mjs 同级补 profile 选择逻辑。
- [ ] ARCHITECTURE.md / README.md 更新：多 profile 模型、存储新分工
      （profile 数据在 autumnfs，hermes home 在 PVC）、与 hermes config
      的关系、buda→deepwiki 改名说明（旧 env 名是否保留别名，写清楚）。
