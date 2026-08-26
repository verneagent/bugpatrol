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

- **解密发生在 GH Actions runner,不是 relay**:watcher 无 key 只提取候选密文写进 issue body;私钥**只**存在于 fived repo 的 **GitHub Actions secret**,由 workflow env 注入 runner,在 `build_triage_context` 里解密。Mac Studio **不存私钥**。
- **生产**:deploy 时把正式私钥设成 fived repo 的 GH secret `FIVED_WATERMARK_PRIVATE_KEY_PEM`(轮换另加 `FIVED_WATERMARK_KEYS_JSON`),并在 `.github/workflows/bugpatrol-triage.yml` 的 provision step env 注入(secret 未设 = 空串,特性关闭,无害)。
- **本地 / dev**:同名环境变量(E2E / 单测用测试 key pair),例:`~/.zshrc` 或命令行 `export`。
- **无 key = 特性关闭**:runner 端没配私钥时 `resolve_media_watermarks` 原样保留候选密文(渲染成 `[Watermark] N candidate envelope(s)`),流水线照常走,不报错不阻塞。

### 轮换流程

1. 生成新 RSA-2048 key pair,把新私钥设为 `FIVED_WATERMARK_PRIVATE_KEY_PEM`(新 payload 用它)。
2. 旧私钥以 `{"<old-keyId>": "<old PEM>"}` 写进 `FIVED_WATERMARK_KEYS_JSON`,保证旧截图仍能解。
3. app 侧 `keyId` 改为新 id 后,旧 keyId 的 payload 继续按 envelope 的 `keyId` 命中 `KEYS_JSON`。

> 私钥泄露应急:立刻换 `FIVED_WATERMARK_PRIVATE_KEY_PEM` + 在 `KEYS_JSON` 里保留一段时间旧 key,回滚到新 key 之前自动失效。

## 载体契约(embedding)

app 把加密 envelope 确定性嵌入截图。**canonical = 隐形 paired-cell pixel carrier**(用户完全感知不到,只有 BugPatrol 分析时能读出来);旧载体保留兼容:

1. **Screenshot pixel carrier(canonical, invisible)**:app 渲染 root overlay,每 bit = 相邻的深/浅两格(alpha 13),视觉上互相抵消、对页面几乎不可见(δ≈13/255 亮度)。几何是**固定 3 物理像素 cell**(`app/lib/dev/diagnosticScreenshotWatermarkPixels.ts`:CELL=3、viewBox 768×768、距角 18px),任何 DPR(2 / 2.625 / 3 / …)都落在整数像素边界,无亚像素混叠;提取端固定 scale=3 读取,并用 **±1px 偏移探测**吸收 RN 布局舍入。双角冗余(top_left + bottom_right 各嵌一份)。所有构建(含 prod)都嵌——隐形载体对用户无感知,prod 截图也能追溯。

   **三层 ECC(RS + 2D 扩展 + 置乱)**:Lark 会把截图压成 1080 宽再重编码 JPEG,cells 从 3px 缩到 2.75px、每 bit 有 JPEG 量化误差——单靠 3× 多数不够,格式升级为错误纠正载体(`rs256.py` + extractor,与 app TS builder 逐字节一致):
   - **RS(255,223)×5**:payload = `[magic 0x4D57][len 2B BE][envelope]` 零填充到 5×223=1115B,分 5 块 RS 编码 → 1275B,每块可纠 **16 个字节错**(实际经真实 Lark 渠道的每块错误 [9,10,8,8,13] 全部低于预算)。envelope 上限 **1111B**(实际 prod ≈1028B)。
   - **2D toroidal 扩展**:逻辑位 `j` 经置乱 `s=(j·8191)%10200` 后,副本 c 写在格位 `48 + s + c·10200` —— 三副本相隔 ~79 行 + ~88 列,单条横带/竖边至多翻转一份。
   - **提取端**:先读 **magic canary**(前 48 格,0.2ms)——纯色页全不可读直接平走;忙碌页多数 magic 离 0x4D57 远则自信判定无载波;5-8 位模糊时用 **RS 保护的 block-0 检查**(30ms)定夺;对齐几何读出后 RS 逐块纠错,不可读 cell 弃权。无载波图 ~300ms,干净截图 ~1.3s。
2. **QR/Data Matrix(fallback)**:BugPatrol 用 zxing-cpp 扫图,只认内容能解析成 envelope JSON 的条码(屏幕上的分享二维码被忽略),多候选取离左上角最近者。app 侧已不渲染 QR badge,此 leg 供屏幕截图里恰好有信封 JSON 条码的场景。
3. **Trailer(legacy/reference)**:图片自然结束处追加 `BUGPATROL_WM1:<b64-envelope>:BUGPATROL_WM1`。PNG/JPEG 解码器在 IEND/EOI 处停下,trailer 对用户不可见但文件里存在。
4. **PNG tEXt chunk(legacy/reference)**:关键字 `bugpatrol.watermark`,值为 base64 envelope。

提取逻辑:先扫 trailer 标记,再试 PNG tEXt/iTXt chunk,再试 pixel carrier(scale=3 优先,双角,每个 ±1px 偏移),最后扫 QR/Data Matrix。byte carrier envelope 上限 512KB。

**候选提取 + 解密(抗错读)**:±1px 偏移读错时可能产出「结构合法但密文损坏」的 envelope(base64 里 2 个字符错位仍能解析成 JSON)。**watcher 无 key 收集所有结构合法候选**写进 issue body;runner 端 `resolve_media_watermarks` 逐个用私钥试,以 **GCM auth 为 ground truth** —— 干净的那份(比如另一角)赢过解析得出但解不了密的冒牌货。迭代是**惰性 best-first** 的,干净截图只读一次就停下,无载波图 ~300ms(扁平 canary 0.2ms 直接平走)。

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

提取在**原始下载字节**上、`redactor`/`transformer` 重编码**之前**执行(resize/JPEG 转码可能毁掉低 alpha pixel carrier),且**不需要私钥**:

- **watcher 侧(无 key)**:`materialize_attachment`(resources.py,RAW bytes → 提取候选 → redact → transform → policy → store → describe)→ `Attachment.watermark` = 候选密文 JSON 数组 → issue body `- watermark-candidates: <JSON 数组>` 行。
- **runner 侧(有 key)**:`extract_media_evidence`(triage_context.py)解析候选 → `resolve_media_watermarks` 逐个 `decrypt_envelope`(GCM auth 挑干净)→ `MediaEvidence.watermark` = payload → triage context 渲染成 `- Watermark: [Watermark] keyId=...` 供 agent 读取。

图片和视频都是水印候选(resources.py `_is_watermark_candidate`)。**issue body** 只表达三态:

- **有候选(加密)** → `- watermark-candidates: <JSON 数组>`,打开 issue 看不到明文
- **扫描过、没有** → `- watermark: 未找到水印`,显式标注「已检查、不存在」,而不是整行缺失
- **未尝试**(非图片视频)→ `""`,整行省略

**解密失败 / 未知 key / 坏 payload** 不落 issue body,在 **runner context** 里变成 `水印解码失败 (<code>)`;无 key 时 runner 原样保留候选,渲染成 `[Watermark] N candidate envelope(s) (encrypted)`。watermark 任何阶段失败都不阻塞 intake / triage。

watcher / backfill / event-watcher / mail-watcher 全部接入(无 key 提取,`configured_watermark_decoder()` 已删除)。

## 验证(端到端)

- **fived jest harness**(`app/lib/__tests__/verify-watermark-e2e.test.ts`):用 app 真实代码构建 prod-mode envelope,导出加密 envelope + 匹配私钥 + **真实渲染的 darkPath/lightPath**(RS 纠错 + 置乱 + 2D 扩展几何)到 `$TMPDIR/wm-e2e-{envelope,private,paths}.*`。断言 prod payload = 15 字段(含 `uid`,不含敏感字段)。
- **BugPatrol E2E harness**(`wm_e2e_embed.py`):解析 fived 导出的真实 path 字符串,把 cell 渲染到 1080×2340 截图(双角 + 页面噪声),跑 `bugpatrol watermark decode`。PNG、JPEG q85、UI 遮挡 + JPEG q85 四种形态全部解码出完整 prod payload。**同时跑 split 流程**:watcher 无 key 提取候选 → 构造 `- watermark-candidates:` issue body 行 → `extract_media_evidence` + `resolve_media_watermarks`(测试 key)→ 断言 14 个 required 字段全在。消费的是 app 真实几何,不是 Python 重实现——TS RS 编码与 Python 解码逐字节一致由此证明。
- 单测:`tests/test_watermark.py`(55 例,含全部失败模式 + QR/pixel/trailer/tEXt carriers + 固定 3px ±1px 偏移 + 背景阶跃多数恢复 + JPEG q85 弃权 + UI edge + JPEG + **RS 16 字节/块预算 + 超预算拒读 + magic canary 布局 + 置乱置换 + flat 快路径** + **watcher 提取 / runner 解密 split + 候选优先 + 未知 key + 无 key 原样** + 流水线集成 + CLI)+ `tests/test_rs256.py`(6 例 RS 单测:300 随机 0-16 字节纠错、超预算 → None、GF 域约定)。

## 实现位置

- `bugpatrol/watermark/` — types / keys / envelope / extractor / decryptor / reporter / `rs256.py`(Reed-Solomon 编解码)/ `__init__`(公共 API `decode_image`、`WatermarkResourceDecoder`、`candidates_to_compact_json`)
- `bugpatrol/resources.py`(watcher 无 key 提取)、`intake.py`(issue body 渲染)、`triage_context.py`(`resolve_media_watermarks` runner 解密)、`backfill.py`、`watcher.py`、`event_watcher.py`、`watch_mail.py`、`__main__.py`(CLI decode)
- 测试:`tests/test_watermark.py`(55 例,含全部失败模式 + QR/pixel/trailer/tEXt carriers + 固定 3px ±1px 偏移 + 背景阶跃多数恢复 + JPEG q85 弃权 + UI edge + JPEG + RS 预算 + magic canary + split 流程 + 流水线集成 + CLI)+ `tests/test_rs256.py`(6 例)

依赖:`cryptography>=42`、`zxing-cpp>=3.1` + `numpy>=1.26`(均 lazy-import,不拖慢 import 路径;测试夹具还用 `qrcode`(dev extra)生成 QR 图)。
