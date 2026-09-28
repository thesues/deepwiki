---
name: annotate-screenshot
description: 给产品截图、面相和手相照片添加红框、编号圆点、深色图例、区域标签和纹路线条标注，也可拼接 Chrome MCP 的 2x 四象限整页截图。当用户要求截图加标注、红框、图例、重制用户手册截图、制作高清整页截图，或希望在面相/手相图上区分不同部位和纹线时使用。
metadata:
  hermes:
    requires_tools:
      - annotate_image
---

# 截图与相术图片标注

沿用 rerun docs/release 用户手册的既定视觉风格。不要临时手写 Pillow 绘图逻辑；调用 `annotate_image`，或在有本地 Python 执行能力时直接 import 本目录 `scripts/annotate_lib.py`。

## 工作流

1. 先观察原图并确认其实际可见内容。历史图片先用 `input_image_open` 打开；古籍插图先用麻衣资料工具读取。
2. 规划最少且不重叠的标注：小目标用框，大片区域用区域框，细长纹路用折线。
3. 调用 `annotate_image`。默认使用 `normalized_1000` 坐标，即左上角 `(0,0)`、右下角 `(1000,1000)`；2x 高清截图传 `scale=2`。
4. 查看工具返回的预览。如果折线偏离实际纹路、编号遮住文字或图例挡住关键部位，调整坐标后重新生成。
5. 最终回答必须嵌入工具返回的 `/artifacts/<会话 id>/annotation-*.png`，并在正文中按编号解释。

不要标注未在图片中看清的线，不要把推测画成已确认事实。标注图是解读辅助，不是医学、生物识别或身份判断结果。

## 标注选择

- 按钮、输入框、菜单项、局部五官：`box` + `number`，由工具自动把编号圆点放在框左上角外侧。
- 页面或图像的大区域：`rect`；需要直接命名大片区域时再用 `chip`，不用 legend。
- 面纹、掌纹、眉形、鼻梁轮廓等细长目标：`line` + `number` + `label`。每条可见纹线单独一条折线，不能用一个大框代替多条线。
- 多个小目标或多条纹线：在图片空白处放一个 `legend`。每个编号只对应一个名称；说明太长时拆成续行。
- `circle` 仅用于没有框或折线可附着的点状目标。

## 面相与手相

### 面相

区域（额、眉、眼、鼻、口、颏、耳等）用 `box` 或 `rect`；法令、额纹、眉间纹、眼下纹等线性目标用 `line`。先按古籍原文确定要讨论的部位，再标注当前照片中确实看得清的位置。左右必须以图中人物自身的左右说明；不确定时写“画面左/右”，不要猜。

### 手相

生命线、智慧线、感情线、事业线等分别使用独立编号折线，并在图例中逐条命名。折线沿纹路中心取 3–12 个点，宁可分段也不要跨过看不清或被遮挡的部分。掌丘或手指区域可另用 `rect`，但区域编号不能复用纹线编号。

建议图例：

```json
{
  "position": [35, 700],
  "title": "掌纹标注"
}
```

配合：

```json
[
  {"kind":"line","points":[[180,420],[250,500],[300,620]],"number":1,"label":"生命线"},
  {"kind":"line","points":[[250,390],[420,450],[610,470]],"number":2,"label":"智慧线"},
  {"kind":"line","points":[[260,310],[440,330],[670,300]],"number":3,"label":"感情线"}
]
```

## 固定风格

- 红色 `(229,57,70)`；红色蒙层透明度 `36/255`；图例深灰 `(47,47,47,235)`。
- 小目标用 `box + circle + legend`；整页分区导览用 `rect + chip`。
- 2x 截图传 `scale=2`，所有默认线宽、圆点和字号自动加倍。
- 图例一句一行；过长内容拆为无编号续行并缩进两个空格；图例不得超出画面右缘。
- 编号圆点位于框左上角外侧约 `(x0-8·scale, y0-10·scale)`。
- 同一张图所有纹线仍使用统一红色，以编号和图例区分，不自行引入另一套颜色语义。

## 本地库用法

只有具备文件与 Python 执行能力时才走本地 driver；麻衣 profile 通常直接调用 `annotate_image`。driver 放当前会话 scratchpad，并用 `/opt/hermes/.venv/bin/python` 执行：

```python
import sys
sys.path.insert(0, "/opt/data/skills/mayi/annotate-screenshot/scripts")
from annotate_lib import Annotator, stitch_2x_tiles

a = Annotator("raw.png", scale=2)
a.box([1024, 368, 2918, 618])
a.circle((1008, 358), 2)
a.rect([806, 50, 2594, 948])
a.chip((30, 280), "① 数据来源：episode 列表")
a.line([(520, 320), (610, 370), (700, 460)])
a.legend((60, 780), "关注点", [
    (1, "打开数据集入口：点开菜单"),
    (None, "  无编号的续行，行首缩进两个空格"),
])
a.save("out-annotated.png")
a.save_raw("out-raw.png")
```

四象限按左上、右上、左下、右下顺序拼接：

```python
stitch_2x_tiles(
    ["top-left.png", "top-right.png", "bottom-left.png", "bottom-right.png"],
    "raw-fullpage.png",
)
```
