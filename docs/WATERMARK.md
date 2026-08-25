# 隐形水印解码 — 截图诊断元数据接入

状态:已实现
日期:2026-08-25

## 目的

Five Degrees app 在截图里嵌入不可见诊断水印(BugPatrol / app 协作方案,payload 契约见 `app/lib/dev/diagnosticsClipboard.ts`)。BugPatrol 负责**确定性**解码:

1. **提取**(image bytes → 加密 envelope):`bugpatrol/watermark/extractor.py`
2. **解密**(envelope → JSON payload):`bugpatrol/watermark/decryptor.py`
3. **上报**(payload → triage 上下文):`bugpatrol/watermark/reporter.py`

整条链是**纯代码**,不走任何 prompt 识别 —— agent 只消费确定性输出。

## 密钥 / 环境变量

私钥**永远不进仓库**。只从环境 / secret 读取:

| 变量 | 用途 |
|---|---|
| `FIVED_WATERMARK_PRIVATE_KEY_PEM` | 当前主私钥,服务默认 `keyId` = `diagnostic-watermark-v1` |
| `FIVED_WATERMARK_KEYS_JSON` | (可选)keyId → PEM 的 JSON 对象,用于**密钥轮换** |

- **GitHub Actions**:两个都设成仓库 secret(`Settings → Secrets and variables → Actions`)。triage/fix runner 每 run 由 workflow 注入环境。
- **本地 / dev**:同名环境变量,例:`~/.zshrc` 或 watcher launchd 的 `EnvironmentVariables`。
- **无 key = 特性关闭**:没配私钥时解码静默返回 `watermark_private_key_missing`,流水线照常走,不报错不阻塞。

### 轮换流程

1. 生成新 RSA-2048 key pair,把新私钥设为 `FIVED_WATERMARK_PRIVATE_KEY_PEM`(新 payload 用它)。
2. 旧私钥以 `{"<old-keyId>": "<old PEM>"}` 写进 `FIVED_WATERMARK_KEYS_JSON`,保证旧截图仍能解。
3. app 侧 `keyId` 改为新 id 后,旧 keyId 的 payload 继续按 envelope 的 `keyId` 命中 `KEYS_JSON`。

> 私钥泄露应急:立刻换 `FIVED_WATERMARK_PRIVATE_KEY_PEM` + 在 `KEYS_JSON` 里保留一段时间旧 key,回滚到新 key 之前自动失效。

## 载体契约(embedding)

app 把加密 envelope 确定性嵌入截图,三种载体都支持:

1. **Screenshot pixel carrier(canonical)**:app root 渲染两个低 alpha 的 paired-cell 网格(top-left / bottom-right)。每个 bit 用相邻 light/dark cell 的亮度差编码,所以普通 iOS/Android 系统截图会天然包含水印,不依赖 app 拿到截图文件字节。BugPatrol 用 Pillow 在固定角落/scale 候选上采样并还原 envelope JSON。
2. **Trailer(legacy/reference)**:图片自然结束处追加 `BUGPATROL_WM1:<b64-envelope>:BUGPATROL_WM1`。PNG/JPEG 解码器在 IEND/EOI 处停下,trailer 对用户不可见但文件里存在。
3. **PNG tEXt chunk(legacy/reference)**:关键字 `bugpatrol.watermark`,值为 base64 envelope。

提取逻辑:先扫 trailer 标记,再试 PNG tEXt/iTXt chunk,最后试 screenshot pixel carrier。byte carrier envelope 上限 512KB;pixel carrier 受屏幕载体容量限制,当前约 4094 bytes。

## Envelope 格式

```json
{
  "v": 1,
  "keyId": "diagnostic-watermark-v1",
  "alg": "RSA-OAEP-256+AES-256-GCM",
  "data": {
    "ciphertext": "<base64 AES-256-GCM 密文(payload JSON)>",
    "iv": "<base64 12-byte nonce>",
    "tag": "<base64 16-byte GCM tag>",
    "wrappedKey": "<base64 RSA-OAEP-256 包裹的 AES-256 key>"
  }
}
```

## Payload 契约(core 字段)

解码后的 payload 必须含全部字段,`schemaVersion` 必须为 1:

`schemaVersion, keyId, watermarkId, uid, pathname, platform, appVersion, buildVersion, buildInfo, gitCommit, buildTime, modelName, osName, osVersion, capturedAt`

## CLI(agent 调用入口)

```bash
bugpatrol watermark decode --image /path/to/screenshot.png --json
```

- **成功**(找到 + 解密成功):exit 0
  ```json
  {"found": true, "confidence": 1.0, "keyId": "diagnostic-watermark-v1", "payload": { ...14 个字段... }}
  ```
- **无水印**:exit 0,`{"found": false, "confidence": 0, "error": "watermark_not_found"}`
- **密钥缺失 / 解密失败 / 未知 keyId**:exit 1,`error` 区分:`watermark_private_key_missing` / `watermark_decrypt_failed` / `watermark_key_not_found`
- **图片不存在**:exit 2,`watermark_image_not_found`

去掉 `--json` 输出人类可读的 `[Watermark] keyId=... watermarkId=...` 摘要行。

## 流水线接入点

解码在**原始下载字节**上、`redactor`/`transformer` 重编码**之前**执行(resize/JPEG 转码可能毁掉低 alpha pixel carrier):

`materialize_attachment`(resources.py,RAW bytes→解码→redact→transform→policy→store→describe)→ `Attachment.watermark` → issue body `- watermark: <值>` 行 → `extract_media_evidence` → `MediaEvidence.watermark` → triage context 渲染成 `- Watermark: <摘要>` 供 agent 读取。

图片和视频都是水印候选(resources.py `_is_watermark_candidate`),`Attachment.watermark` 承载四态:

- **找到** → 紧凑 payload JSON,渲染成 `- Watermark: [Watermark] keyId=...`
- **扫描过、没有** → `未找到水印`,issue body 显式标注「已检查、不存在」,而不是整行缺失
- **解码失败**(损坏 envelope / 未知 key / 坏 payload)→ `水印解码失败 (<code>)` 并在 stderr 打日志,不阻塞 intake
- **未尝试**(特性关闭 / 非图片视频)→ `""`,整行省略

- watcher / backfill / event-watcher / mail-watcher 全部接入,`configured_watermark_decoder()` 仅在环境配了 key 时启用(无 key 不产生噪音)。

## 实现位置

- `bugpatrol/watermark/` — types / keys / envelope / extractor / decryptor / reporter / `__init__`(公共 API `decode_image`、`WatermarkResourceDecoder`)
- `bugpatrol/resources.py`、`intake.py`、`triage_context.py`、`backfill.py`、`watcher.py`、`event_watcher.py`、`watch_mail.py`、`__main__.py`
- 测试:`tests/test_watermark.py`(37 例,含全部失败模式 + pixel/trailer/tEXt carriers + 流水线集成 + CLI)

依赖:`cryptography>=42`(lazy-import,不拖慢 import 路径)。
