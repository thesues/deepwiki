---
name: autumn-codebase
description: 阅读 autumn-rs 的 Rust 分布式存储代码，回答实现、架构、调用链、故障定位与代码行为问题。通过 code-index MCP 查找符号和读取源码，以文件路径、符号名与行号提供依据。
metadata:
  hermes:
    requires_tools:
      - mcp__code_index__search_code
      - mcp__code_index__read_file
---

# Autumn 代码理解

使用 `mcp__code_index__*` 工具读取远端代码索引，不把索引路径当作本地 checkout。

1. 用 `mcp__code_index__search_code` 查实现，用 `mcp__code_index__search_docs` 查设计说明。
2. 搜索命中是定位，不是完整源码。用 `mcp__code_index__read_file(path,start,end)` 读目标行，或用 `mcp__code_index__get_symbol(id)` 读整个符号。
3. 原样使用返回的 `file`，例如 `autumn-rs/crates/...`；不要添加 `code/` 或猜测文件系统前缀。单次读取至多 400 行，优先函数或相关分支。
4. 根据问题调用 `mcp__code_index__find_callers`、`mcp__code_index__find_callees` 或 `mcp__code_index__trace_call_path` 追踪调用关系；拿到足够证据就停止搜索。
5. 画架构图时先 `skill_view('archify')`，依照其流程生成并展示图。
6. 区分源码事实、推断和需要运行才能验证的行为。引用文件路径、符号与行号；索引没有内容时说明缺失，不编造实现或测试结果。

用中文简洁回答。代码工具不用于查经文；其他领域的问题选择各自的 skill 和 MCP。
