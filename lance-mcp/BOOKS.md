# PDF / EPUB 文档库

`prepare_books.py` 把本地书籍转换为 Markdown，再用现有的 `ingest_docs.py`
写入 Autumn S3 上的 LanceDB。原文件保持不变；`files` 表保存完整转换正文，
`docs` 表保存分块、向量、章节与行号，服务端读正文不依赖本地 Downloads 目录。

## 准备文档

EPUB 按 OPF spine 顺序读取正文并保留标题。PDF 优先提取文字，缺少文字的页面
通过 macOS Vision OCR 识别，保留每个物理页的标题。需要 macOS、Poppler
(`pdftotext`、`pdfinfo`、`pdftoppm`) 和 `ocr.py` 使用的 PyObjC Vision 依赖。
OCR 需要允许调用 macOS Vision 的执行环境；沙箱可能返回空结果。

```sh
cd lance-mcp
.venv/bin/python prepare_books.py \
  --source "$HOME/Downloads/tao" \
  --output /private/tmp/buda-tao-import/tao \
  --cache /private/tmp/buda-tao-ocr-cache \
  --workers 2
```

`manifest.json` 记录源文件 SHA256、转换文件名、字数、页数和短文字页。
OCR 缓存按源文件 SHA256 分目录，中断后运行相同命令可复用。
页码是 PDF 物理页码，不是书中印刷页码；OCR 文字不能视为校勘本。
少于 80 字的 PDF 页附带提取不完整的提示，不能直接当作空白页。
《太平经》附图中的複文字形不能可靠转成普通文字，保留原 PDF 供核对。

## 导入 Autumn LanceDB

使用 `k8s/tao-ingest-job.yaml` 启动 Job。将其中 `IMAGE_LANCE_MCP` 替换为
已验证的 Lance MCP 镜像，再应用 manifest。Pod 先等待上传完成标记。

```sh
kubectl apply -f /path/to/rendered-tao-ingest-job.yaml
kubectl -n autumn wait --for=condition=Ready pod -l job-name=tao-ingest --timeout=120s
TAO_INGEST_POD=$(kubectl -n autumn get pod -l job-name=tao-ingest -o jsonpath='{.items[0].metadata.name}')
kubectl -n autumn cp /private/tmp/buda-tao-import/tao "${TAO_INGEST_POD}:/input/tao"
# 核对复制后的文件 SHA256，再放行导入。
kubectl -n autumn exec "$TAO_INGEST_POD" -- touch /input/ready
kubectl -n autumn logs -f job/tao-ingest
kubectl -n autumn wait --for=condition=complete job/tao-ingest --timeout=3600s
```

Job 在集群内调用现有 `ingest_docs.py` 的 `build`、`embed_rows`、`write`：
明确从 `/input/tao` 读取文档，调用
`http://llama-embed:8080` 的 BGE-M3，并直接通过 `http://autumn-s3:9100`
写入 `s3://lancedb/tao`。旧镜像的 CLI 会将 `--s3-endpoint` 同时用于输入，
直接调用现有 API 可正确组合本地文档输入和 S3 数据库输出。
不使用 port-forward 搬运 LanceDB 数据。

这是完整替换指定库的导入流程，不要指向其他语料库。使用匿名 Autumn S3、
BGE-M3 1024 维向量及中文 ngram 1–2 全文索引；搜索支持 lexical/vector/hybrid。
重新导入时删除已完成的同名 Job，再启动新 Job 并重新复制文档；输入是 emptyDir。

发布前检查 `files` 数量与 manifest 一致、`docs` 数量与分块一致、无空向量，
并实际执行 `search_docs`、`document_outline`、`read_file` 检查引用位置。
验证通过后，从集群内有 boto3 的 Python 环境写入 ready marker：

```python
import boto3
from botocore import UNSIGNED
from botocore.config import Config

s3 = boto3.client("s3", endpoint_url="http://autumn-s3:9100",
                  region_name="us-east-1",
                  config=Config(signature_version=UNSIGNED,
                                s3={"addressing_style": "path"}))
s3.put_object(Bucket="lancedb", Key=".tao-docs-v1-ready", Body=b"verified\n")
```

首次导入曾通过本机 port-forward 尝试 S3 分片上传，转发流创建超时导致
5 MiB 分片等待约 241 秒后失败；最终改为上述 Job 路径。
集群内 1 KiB PUT 实测约 12–23 ms，不能把该次转发故障解释为存储吞吐低。

## 服务与 profile

- `k8s/lance-mcp-tao.yaml`：独立 MCP 服务，端口 5105，数据库 `s3://lancedb/tao`。
  将 `IMAGE_LANCE_MCP` 替换为已验证的现有 Lance MCP 镜像即可部署。
  启动时检查 marker，不会重复导入。
- `k8s/webui.yaml`：`MCP_SERVERS` 添加 `tao`，`DEEPWIKI_PROFILES` 配置
  `key=tao`、`label=老子通释`。工具仅使用 `mcp-tao` 的文档检索与正文读取，
  不加载其他 profile 的 skills。没有 Python 中的 profile 特例。
- 更新现有集群时只合并新 MCP 地址与 profile，保留其他在线配置。

初始目录实际为 `~/Downloads/tao`，包含《老子通释》《坐忘论》
《〈太平经〉正读》和《当代哲学经典丛书（套装共7册）》四个文件。
最后一项一并导入，但 profile 要求明确区分现代哲学观点和道教经典。
原文件另存于 `s3://docs/tao/source/`，转换正文与 manifest 存于
`s3://docs/tao/text/`，均通过匿名 boto3 `put_object` 写入。
