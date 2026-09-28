---
name: pdf-figure-reading
description: 从麻衣神相/手面相/风水 PDF 语料中读取书内插图。遇到眉形、眼形、鼻相、掌纹、阳宅图、图鉴、例图、对照图、图示、书中图片或用户要求“看书里的图”时使用。
metadata:
  hermes:
    requires_tools:
      - mcp__mayi__search_docs
      - mcp__mayi__read_page
      - mcp__mayi__page_images
      - input_image_open
      - terminal
---

# PDF 插图读取

麻衣语料里的相术图、手相图和风水图是答案证据的一部分。不要只读 OCR 文本就下结论；只要问题涉及形状、图鉴、例图、图示或用户让你对比图片，就必须调用 `page_images` 取得 PDF 里的图片。

## 工作流

1. 先用 `search_docs` 定位相关页面。使用 search hit 返回的 `file` 原样传给后续工具，不要改路径、补前缀或截短文件名。
2. 用 `read_page` 读取命中页及相邻页，确认页码范围和文字描述。
3. 用 `page_images` 拉同一页码范围的插图。页码是 1-based inclusive，例如 p27-p32 传 `page_start=27, page_end=32`。
4. `page_images` 返回的图片会显示为 `MEDIA:/opt/data/cache/images/img_<id>.png`。对每张需要分析的图片调用 `input_image_open(media_path=...)`，确认实际画面后再作答。
5. 需要把书内插图展示给用户时，用 `terminal` 将对应缓存图复制到当前工作目录下一个不会重名的文件名。当前工作目录就是本会话的 artifact 目录；复制后在回复中使用 `/artifacts/<当前会话 id>/<文件名>`。
6. 对照图片和文字回答。回答里引用书名/页码，并说明图片看到的形状特征；如果图片和 OCR 文字有差异，以图片可见内容为准，同时指出文字依据。
7. 如果用户上传了自己的面相/手相照片，需要另用 `annotate-screenshot` 标注用户图片；书内 PDF 插图仍由 `page_images` 读取。

## 调用规则

- 搜到 PDF 页码后，优先拉较窄范围：单页问题拉 1 页；跨页图鉴拉相关连续页。
- 不要用 `read_file` 读取 PDF 页；PDF 的坐标是页码，正文用 `read_page`。
- `page_images` 返回 MCP image content 后，用 `input_image_open(media_path=...)` 真正读取图片，不要只凭文件名猜测，也不要要求用户另传书中截图。
- `MEDIA:/opt/data/cache/images/...` 是容器内部缓存地址，只供 `input_image_open` 和 `terminal` 使用。禁止把它写进 Markdown 图片或链接；浏览器无法访问该路径。
- 给用户展示原图时，先复制到当前 artifact 工作目录，再只发送 `/artifacts/<当前会话 id>/...`。不要把 `/opt/data/cache/images/...` 暴露给用户。
- 如果 `page_images` 对明确有图的页返回 `no figures indexed`，先换成同一文件的窄页码重试一次；仍失败就直说该页图片索引缺失，不要扩大到全书反复调用。
- 如果检索命中的是 `.doc/.txt/.md`，它们没有 PDF 图片；只按文本回答。

## 输出要求

- 简短引用：`《书名》pN` 或 `pN-pM`。
- 对图中每个关键形状给出可观察描述，再给原文相义。
- 语料没有的判断要说没有，不要把通用相术常识伪装成书中内容。
