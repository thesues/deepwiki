---
name: classical-physiognomy
description: 检索麻衣神相、手相、面相、阳宅风水、周易和八字等传统文献。用户询问书中记载、图谱，或要求对照上传图片解读传统相术时使用；结合原文、插图与可见标注作答。
metadata:
  hermes:
    requires_tools:
      - mcp__mayi__search_docs
      - mcp__mayi__read_page
---

# 传统相术与风水文献

只用 `mcp__mayi__*` 检索麻衣语料。通用 agent 同时有多种检索工具，不能把同名的 tao、佛典或代码工具用于此库。

1. 用 `mcp__mayi__list_documents` 确认文献，再用 `mcp__mayi__search_docs` 定位记载。
2. PDF 命中的 start/end 是物理页码，用 `mcp__mayi__read_page(file,page_start,page_end)` 读取整页。其他文档的坐标是行号，使用 `mcp__mayi__read_file(path,start,end)`。
3. 遇到书内图谱、例图、眉形、眼形、鼻相、掌纹或风水图，先 `skill_view('pdf-figure-reading')`，再用 `mcp__mayi__page_images` 获取图像。按照该 skill 用 `input_image_open` 真正查看图片，不能仅凭 OCR 描述图中形状。
4. 用户上传面相或手相图片时，先 `skill_view('annotate-screenshot')`。使用 `annotate_image` 标注清晰可见的部位和纹线，回复中展示标注图；不把推测画成已确认的特征。
5. 引用书名、页码或章节及原文片段。把相术解释明确标为传统文献说法，不凭外貌断定人的真实性格、健康或命运。
6. 图像不可读或当前模型无法查看图片时，说明需要支持图片的模型，不把未查看的图像当作依据。语料没有的直说没有，补充常识时注明来源区别。

用中文简洁回答，优先说明实际读到的文字与看到的图像。
