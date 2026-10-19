---
name: tao-text-retrieval
description: 检索《老子通释》《道德经》《坐忘论》《太平经》及 tao 库的哲学文献。用户询问原文、章节含义、道教概念或比较这些著作时使用，读取正文并引用书名、章节、PDF 物理页码和行号。
metadata:
  hermes:
    requires_tools:
      - mcp__tao__search_docs
      - mcp__tao__read_file
---

# 老子与道教文献检索

使用 `mcp__tao__*` 工具访问独立的 tao 语料库。语料在 Autumn LanceDB，不能通过本机文件搜索读取。

1. 用 `mcp__tao__list_documents` 确认馆藏，需要章节结构时用 `mcp__tao__document_outline(id=文件路径)`。
2. 用 `mcp__tao__search_docs(query=...)` 检索问题。讨论《道德经》时优先查《老子通释》，必要时在 query 中加书名。
3. 命中只有定位和短预览。引用前用 `mcp__tao__read_file(path=命中的file,start=命中的start,end=命中的end)` 读取正文；路径原样传入，不添加前缀。单次最多读取 400 行，按需补读相邻段落。
4. 明确区分老子原文、余秋雨的解释、其他作者的观点和自己的概括。《当代哲学经典丛书》的观点不能称为道教经典。
5. 引用书名、章节和行号。PDF 转换文档的标题含 `PDF 第N页`，引用该物理页码；工具的 start/end 仍是 Markdown 行号，不能当作页码。
6. 遇到 OCR 疑似错字、複文符图或提取不完整提示，说明文字限制，不自行补造原文。没有检索到依据时直说没有；额外背景需标注为语料外知识。

简洁回答，以读到的原文支撑解释。跨佛教或其他文献比较时分别调用对应 skill 和 MCP，并分清出处。
