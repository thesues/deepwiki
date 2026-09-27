---
name: vllm-omni-h3
description: 用集群里的 vLLM-Omni MiniMax H3 生成带原生立体声音频的视频。支持文生视频和单张首帧图生视频；输入图片先持久化到 Autumn S3，再由客户端取回 bytes 传给 vLLM Video API。
metadata:
  hermes:
    requires_toolsets:
      - terminal
      - file
---

# vLLM-Omni MiniMax H3

## 何时用

用户要通过 vLLM-Omni 生成 H3 视频、明确要求和 ComfyUI 比较，或消息里含有
`input/webui/...` 的上传图片 object key。

## 服务约束

- API：`http://vllm-omni-h3.autumn.svc:8000/v1/videos/sync`
- 当前服务是单张 RTX 4090，加载 `FL2VA` 模型目录并使用 model-level CPU offload；
  仅 DiT 使用在线 FP8，Qwen encoder 与两个 VAE 保持 BF16，以适配 24GiB 显存。
- 支持 `t2va` 文生视频，以及 `fl2va` 单首帧图生视频。当前服务不提供 Ref2VA。
- 单服务一次只执行一个 diffusion request；长请求要耐心等待，不要重复提交。
- 24 GB 起步形状是 1024×576、5 秒。输出固定 24 FPS，时长会对齐到 H3 合法帧网格。

## 必须使用脚本

调用同目录 `scripts/h3_client.py`，不要手写 curl multipart。脚本负责：

1. 新的本地输入图先 PUT 到 `s3://input/webui/<session-id>/<uuid>.<ext>`；
2. 只把 object key 写进元数据和会话，不把凭证、内部 endpoint 或图片 base64 写入历史；
3. 推理前从 Autumn S3 GET 图片 bytes，再作为 `input_reference` multipart 上传给
   vLLM。不要把 `s3://` 字符串直接传给模型——Video API 不承诺支持 S3 URI；
4. 将 MP4 与旁路 JSON 元数据写进当前会话 artifact 目录。

每次调用都必须选择一个从未使用过的输出路径，例如
`/opt/data/artifacts/media-<run-uuid>/h3.mp4`。同一提示词重跑也要换新的 `run-uuid`；禁止覆盖
已经生成或已经回复给用户的 MP4/JSON。客户端会在推理开始前拒绝任何已存在的输出路径。

文生视频：

```bash
python /opt/data/skills/media/vllm-omni-h3/scripts/h3_client.py \
  --prompt '镜头和音频描述' \
  --output /opt/data/artifacts/media-<run-uuid>/h3.mp4 \
  --session-id '<当前会话 id>'
```

新本地图生视频（会先持久化图片）：

```bash
python /opt/data/skills/media/vllm-omni-h3/scripts/h3_client.py \
  --prompt '让主体自然运动，描述环境声' \
  --input ./first-frame.png \
  --output /opt/data/artifacts/media-<run-uuid>/h3-i2v.mp4 \
  --session-id '<当前会话 id>'
```

重放 WebUI 已上传的图片：

```bash
python /opt/data/skills/media/vllm-omni-h3/scripts/h3_client.py \
  --prompt '让主体自然运动，描述环境声' \
  --input-object-key 'input/webui/<session-id>/<uuid>.png' \
  --output /opt/data/artifacts/media-<run-uuid>/h3-i2v.mp4 \
  --session-id '<当前会话 id>'
```

默认参数是 1024×576、5 秒、20 steps、seed 1101、flow shift 12、audio flow
shift 3。只有用户明确要求时才改。与 ComfyUI 做 A/B 时必须同时写明两边实际输出
尺寸、steps、turbo、时长、seed；ComfyUI 的 `0.4 MP` 并不等于 1024×576。

## 历史和生命周期

- S3 object key 是不可变 UUID key，记录在 `<output>.json`，也要在回复中保留。
- 历史重放只使用 object key，脚本每次重新 GET；不依赖 ComfyUI input 目录或 pod 本地盘。
- 默认不自动删除输入。只要会话还可回放，就不能 GC 对应 `input/webui/<session-id>/`
  前缀。未来会话删除工作流可以按该前缀显式清理；当前不要做隐式过期。
- 每次输出放新的 `/opt/data/artifacts/media-<run-uuid>/`，回复使用对应的 `/artifacts/...`
  相对链接；任何旧输出路径都不能被后续生成复用。

## 失败处理

- `503` 或连接失败：检查 `vllm-omni-h3` readiness，不要连续重提。
- S3 `404`：object key 不存在；请用户重新上传，不能悄悄改用本地临时文件。
- 超时：服务端同步超时为 1800 秒；报告请求参数和已等待时间。
- 结果必须是非空 MP4；脚本会验证 `ftyp`，失败时不会把错误 JSON 当视频交付。
