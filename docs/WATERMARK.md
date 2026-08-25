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

app 把加密 envelope 确定性嵌入截图。**canonical = 隐形 paired-cell pixel carrier**(用户完全感知不到,只有 BugPatrol 分析时能读出来);旧载体保留兼容:

1. **Screenshot pixel carrier(canonical, invisible)**:app 渲染 root overlay,每 bit = 相邻的深/浅两格(alpha 13),视觉上互相抵消、对页面几乎不可见(δ≈13/255 亮度)。几何是**固定 3 物理像素 cell**(`app/lib/dev/diagnosticScreenshotWatermarkPixels.ts`:CELL=3、viewBox 768×768、距角 18px),任何 DPR(2 / 2.625 / 3 / …)都落在整数像素边界,无亚像素混叠;提取端固定 scale=3 读取,并用 **±1px 偏移探测**吸收 RN 布局舍入。双角冗余(top_left + bottom_right 各嵌一份)。所有构建(含 prod)都嵌——隐形载体对用户无感知,prod 截图也能追溯。

   **3× 比特交错 + 多数投票**:envelope 逻辑位 `j` 在连续 3 个格位各写一份(`j//3` 逻辑位、`j%3` 副本),占满 128×256 格栅(上限从 4094 降到 **1364 字节**,实际 prod envelope ≈1028B)。提取端逐 bit 在 3 副本间多数投票。这样两类真实干扰都能扛:
   - **背景 UI 边翻转极性**:底下一段 >13 luma 阶跃会反转被它跨过的 cell 对的极性(错值)。交错(而非顺序块)让局部 blob 分散到不同字节位,每字节总有 2 份干净投票。
   - **JPEG 洗掉单元**:q85 量化会把个别 3px 对的 luma delta 压到检测阈值(0.25)以下 → 该 bit 读为「不可读」。读 span 时不可读单元**弃权**,只要某个逻辑位的 3 个副本里 ≥1 个可读就能投票恢复,而不是整条载波作废。
2. **QR/Data Matrix(fallback)**:BugPatrol 用 zxing-cpp 扫图,只认内容能解析成 envelope JSON 的条码(屏幕上的分享二维码被忽略),多候选取离左上角最近者。app 侧已不渲染 QR badge,此 leg 供屏幕截图里恰好有信封 JSON 条码的场景。
3. **Trailer(legacy/reference)**:图片自然结束处追加 `BUGPATROL_WM1:<b64-envelope>:BUGPATROL_WM1`。PNG/JPEG 解码器在 IEND/EOI 处停下,trailer 对用户不可见但文件里存在。
4. **PNG tEXt chunk(legacy/reference)**:关键字 `bugpatrol.watermark`,值为 base64 envelope。

提取逻辑:先扫 trailer 标记,再试 PNG tEXt/iTXt chunk,再试 pixel carrier(scale=3 优先,双角,每个 ±1px 偏移),最后扫 QR/Data Matrix。byte carrier envelope 上限 512KB。

**候选解码(抗错读)**:±1px 偏移读错时可能产出「结构合法但密文损坏」的 envelope(base64 里 2 个字符错位仍能解析成 JSON)。解码端收集**所有**结构合法候选,逐个用私钥试,以 **GCM auth 为 ground truth** —— 干净的那份(比如另一角)赢过解析得出但解不了密的冒牌货。迭代是**惰性 best-first** 的,干净截图只读一次就停下(~170ms/图),无载波图 ~30ms。

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

## 验证(端到端)

- **fived jest harness**(`app/lib/__tests__/verify-watermark-e2e.test.ts`):用 app 真实代码构建 prod-mode envelope,导出加密 envelope + 匹配私钥 + **真实渲染的 darkPath/lightPath**(3× 交错几何)到 `$TMPDIR/wm-e2e-{envelope,private,paths}.*`。断言 prod payload = 15 字段(含 `uid`,不含敏感字段)。
- **BugPatrol E2E harness**(`wm_e2e_embed.py`):解析 fived 导出的真实 path 字符串,把 cell 渲染到 1080×2340 截图(双角 + 页面噪声),跑 `bugpatrol watermark decode`。PNG、JPEG q85、UI 遮挡 + JPEG q85 四种形态全部解码出完整 prod payload。消费的是 app 真实几何,不是 Python 重实现。
- 单测:`tests/test_watermark.py`(47 例,含全部失败模式 + QR/pixel/trailer/tEXt carriers + 固定 3px ±1px 偏移 + 背景阶跃多数恢复 + JPEG q85 弃权 + UI edge + JPEG + 流水线集成 + CLI)。

## 实现位置

- `bugpatrol/watermark/` — types / keys / envelope / extractor / decryptor / reporter / `__init__`(公共 API `decode_image`、`WatermarkResourceDecoder`)
- `bugpatrol/resources.py`、`intake.py`、`triage_context.py`、`backfill.py`、`watcher.py`、`event_watcher.py`、`watch_mail.py`、`__main__.py`
- 测试:`tests/test_watermark.py`(47 例,含全部失败模式 + QR/pixel/trailer/tEXt carriers + 固定 3px ±1px 偏移 + 背景阶跃多数恢复 + JPEG q85 弃权 + UI edge + JPEG + 流水线集成 + CLI)

依赖:`cryptography>=42`、`zxing-cpp>=3.1` + `numpy>=1.26`(均 lazy-import,不拖慢 import 路径;测试夹具还用 `qrcode`(dev extra)生成 QR 图)。
