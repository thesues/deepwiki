---
name: comfyui-media
description: 用集群里的 ComfyUI 生成图片（Z-Image）、视频（MiniMax H3）、3D 模型（TRELLIS 2 / Pixal3D）。只用本 skill assets 里预展开的现成 workflow，改参数提交，从不自己新做 workflow、从不新建节点连线。
metadata:
  hermes:
    # What this skill cannot work without, and hermes' own gate: a profile
    # lacking these never sees the skill — not in the prompt's skill index,
    # not in skills_list. `terminal` is how ComfyUI is reached (curl to the
    # cluster address; there is no MCP for it) and `file` is how the workflow
    # JSON in assets/ is read and the rendered output is written.
    #
    # Which is also how this skill is scoped to the general assistant: the
    # scripture profile carries `skills` alone and the code profile
    # `skills` + `terminal`, so neither qualifies, and neither has any use
    # for image generation. Declaring what it needs is the whole mechanism —
    # nothing here names a profile.
    requires_toolsets:
      - terminal
      - file
---

# ComfyUI 生图 / 生视频 / 3D

## 何时用
用户要生成图片、视频、3D 模型（mesh/glb），或要图生视频、参考图生视频、多视图重建 3D。

## 铁律
1. **只用 `assets/` 里的现成 workflow**，按需求选一个，绝不自己拼节点、改连线、增删节点。
2. **只用本地模型路线**。`ComfyCloud*` 开头的节点（ComfyCloudZImageTurboNode 等）需要 comfy.org 账号，本集群未登录，调了必报 Unauthorized——现成 workflow 里没有它们，也不要往任何 workflow 里加。
3. 改的只有**参数值**（prompt、seed、尺寸、文件名），不改结构。每个 workflow 可改的参数见下表。
4. 提交前 workflow JSON 里所有 `[节点id, 输出槽]` 形式的连线值保持原样——那是节点间的连线，不是数据。

## ComfyUI 地址
集群内 `http://comfyui-autumn.autumn.svc:8188`（带 basic auth 的代理是给浏览器用的，API 走集群内直连）。用 terminal 里的 curl/python 访问。

## 通用流程（三个任务都一样）
1. `skill_view` 拿到本 skill 后，读 `assets/<workflow>.json`（用 file 工具，路径相对于本 skill 目录）。
2. 按下表改参数。
3. `POST /prompt`，body 为 `{"prompt": <workflow对象>}`，可自带 `"prompt_id": "<uuid4>"`。返回 `{"prompt_id": ...}`。
4. 轮询 `GET /history/<prompt_id>`（生图每 2-3s，视频/3D 每 5-10s）。响应里该 id 存在且 `status.status_str == "success"` 即完成；`error` 字段非空则把 `node_errors` 原样报给用户。
5. 从 `outputs` 里取产物文件名，`GET /view?filename=<filename>&subfolder=<subfolder>&type=output` 下载。
6. 生图/生视频结果发给用户看；3D 结果是 glb 文件，告诉用户文件位置。

## Workflow 一览

| 需求 | workflow | 产物 |
|---|---|---|
| 文生图（快，8 步） | `z_image_turbo.json` | png |
| 文生图（精，25 步） | `z_image.json` | png |
| 文生视频（含音频） | `h3_t2v.json` | mp4 |
| 图生视频（首帧驱动） | `h3_i2v.json` | mp4 |
| 参考图生视频（1-4 张参考图） | `h3_r2v.json` | mp4 |
| 多帧参考生视频（关键帧控制） | `h3_multiframe_reference.json` | mp4 |
| 多视图转 3D（四视图 turnaround 图） | `pixal3d_multiview.json` | glb |
| 单图转 3D | `trellis2_image_to_3d.json` | glb |

## 生图（z_image_turbo / z_image）

| 节点 id | 参数 | 说明 |
|---|---|---|
| `57_27`（turbo）/ `76_67`（base） | `text` | 用户的 prompt，整段替换 |
| `57_3` / `76_69` | `seed` | 用户没指定就随机一个整数 |
| `57_13` / `76_68` | `width`, `height` | 32 的倍数；turbo 默认 1024×1024。宽图如 1344×768 |
| `57_3` / `76_69` | `steps`（turbo 固定 8，别改）、`cfg` | base 版 steps 25、cfg 4，可调 3-5 |

turbo 版没有负面提示词（negative 是 ConditioningZeroOut）；base 版负面提示词在 `76_71` 的 `text`。

## 生视频（MiniMax H3，均带原生立体声）

**时长**：改 `PrimitiveFloat` 节点的 `value`（秒数，5-15）。帧数由 `ComfyMathExpression` 按 17k+5 网格自动算，不用碰。
**分辨率**：`ResolutionSelector` 的 `aspect_ratio`（如 `"16:9 (Widescreen)"`）和 `megapixels`（0.4≈736×416，0.98≈1344×768 官方 768p）。
**seed**：`RandomNoise` 的 `noise_seed`。
**turbo 开关**：`PrimitiveBoolean` 的 `value`（true=8 步 turbo LoRA，快；false=20 步全质量）。默认 false。

- **h3_t2v**：prompt 在 `MiniMaxH3ImageToVideo`（节点 `140_131`）的 `text`。视频 prompt 建议含镜头/时间线/音频描述，模板原值是好范例。
- **h3_i2v**：输入图 `LoadImage`（节点 `114`）的 `image`。图先 `POST /upload/image`（multipart 表单，字段 `image`），用返回的文件名。prompt 在 `105_104`。首帧图会被自动缩放，不必预处理。
- **h3_r2v**：prompt 在 `PrimitiveStringMultiline`（节点 `138`），参考图语法 `<Picture 1>` `<Picture 2>` 按上传顺序对应 `ref_images.ref_image_0/1/...`（LoadImage 节点 `137`、`139`）。参考图 1-4 张。
- **h3_multiframe_reference**：`136` 的 ref_images 是主体参考，`147/162/169` 三个 `MiniMaxH3AddGuide` 各挂一张关键帧图（`frame_idx` 由 `PrimitiveFloat` 秒数换算）。四个 LoadImage：`164`（主体）、`157`、`159`、`167`（关键帧）。

视频生成 5s/480p 约几分钟，15s/720p 可到十几分钟，轮询间隔放宽，别提前放弃。

## 3D

- **trellis2_image_to_3d**（单图）：`LoadImage`（节点 `122`）的 `image`。内置抠图（birefnet）+ MoGe 深度估计，背景杂乱的图也能用。`PrimitiveBoolean`（`316`）false=Pixal3D 路线、true=TRELLIS2 路线，默认 false 即可。输出 `Save3DAdvanced`（`322`）。
- **pixal3d_multiview**（多视图）：输入一张**四视图 turnaround 图**（前/左/后/右 从左到右拼一张），`LoadImage`（`364`）。⚠️ 四个 `ImageCropV2` 的 `crop_region`（x/y/width/height 像素矩形）是按模板样图 6640×2688 写死的，**换图必须按用户图的实际尺寸重算**四个裁剪框。没有 turnaround 图就先用 `z_image_turbo` 生成或让用户提供。输出 `Save3DAdvanced`（`372`）。

## 输入图上传

```bash
curl -s -F "image=@/path/to/img.png" -F "type=input" -F "overwrite=true" \
  http://comfyui-autumn.autumn.svc:8188/upload/image
# 返回 {"name": "img.png", "subfolder": "", "type": "input"} —— name 填进 LoadImage
```

## 结果下载

```bash
curl -s -o /tmp/out.png \
  "http://comfyui-autumn.autumn.svc:8188/view?filename=<filename>&subfolder=<subfolder>&type=output"
```

## 排错
- 400 + `node_errors`：通常是改参数时碰断了连线（把 `[id, slot]` 改成了字面量），恢复原值重提。
- 400 + `Required input is missing`：漏了某个必填参数，对照 asset 原文件补回。
- 长时间无结果：`GET /queue` 看是否在跑；`GET /history` 一直查不到 id 说明提交没成功，重提。
- OOM：视频把 megapixels 降到 0.4、时长降到 5s；3D 换 trellis2 单图路线。
