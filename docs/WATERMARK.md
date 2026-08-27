# 隐形水印解码 — 截图诊断元数据接入

状态:已实现(扩频明文 v2,2026-08-27)
日期:2026-08-27

## 目的

Five Degrees app 在截图里嵌入不可见诊断水印(BugPatrol / app 协作方案,payload 契约见 `app/lib/dev/diagnosticsClipboard.ts`)。BugPatrol 负责**确定性**解码:

1. **提取**(image bytes → 明文 payload):`bugpatrol/watermark/extractor.py`
2. **上报**(payload → triage 上下文):`bugpatrol/watermark/reporter.py`

整条链是**纯代码**,不走任何 prompt 识别 —— agent 只消费确定性输出。**不加密、无密钥** —— payload 直接明本写进 issue body;`uid` 只在开发环境出现(用户已确认开发环境 issue body 明文 uid 可接受)。

## 设计决策(v2,扩频明文)

用户决定:生产/开发**都不加密,直接明本**;`uid` 只在开发环境加。由此:

- **密钥层整体删除**:`keys.py`、`envelope.py`、`decryptor.py`、fived 的 `diagnosticWatermarkEncryption.ts` 全部移除。不再有 GH Actions secret / workflow env 注入 / 轮换流程。
- **keyId 字段删除**:它是加密时代的密钥轮换标记,明文下无意义 —— 格式契约已由 `schemaVersion: 2` 承担。若保留真实值 `diagnostic-watermark-v1`(22 字符)payload 会到 268B,超出 RS(255,135)×2 容量(266B);删除后 234B,余量 32B。
- **split 流程折叠回单流**:watcher 无 key 提取 → 明文 payload 直接进 issue body(`- watermark:` 行);runner 直接解析,不再解密。
- **QR/Data Matrix fallback 移除**:旧 QR leg 是给加密 envelope 大容量的兜底;明文 234B 由 pixel carrier 全包,QR 只会引入误读风险。

## 载体契约(embedding)

app 把**明文 payload JSON** 确定性嵌入截图。canonical = **扩频 paired-cell pixel carrier**(磨砂质感,只放开发环境截图;prod 也嵌——用户确认 prod 也走扩频明文)。

### 几何(nominal-canvas zero-remap)

- **名义画布 1080×2340**,所有 chip 坐标在名义画布上算。fived 渲染**全屏单 SVG**,`viewBox="0 0 1080 2340"`,`preserveAspectRatio="xMidYMid meet"`,absoluteFill。SVG 缩放到 native;Lark 把截图下采样到 1080 宽。两次缩放的因子**精确抵消** → chip 在最终 1080 宽图上落在**名义坐标原处**,decoder 直接按名义坐标读,**无需知道 native 分辨率、无需 remap**。
- **H2 pair**:每 bit 由 N=4 对 chip 承载。pair = 两个 3×3 chip,中心距 4px(水平相邻,1px 间隙):
  - bit=1 → pa(cx−2, cy) 深色、pb(cx+2, cy) 浅色
  - bit=0 → 相反
  - 深色 fill = `rgba(0,0,0,0.08)`、浅色 fill = `rgba(255,255,255,0.08)`,亮度差 δ ≈ 0.08×255 ≈ 20(与背景无关,α 不变式)。α=0.18 在真机上呈可见点阵磨砂,已降至 0.08(2026-08-27):解码在真实聊天 UI 背景下经 α 扫描(0.18→0.05)全形态仍过,**0.08 ≈ 2.3× 不易见 + 3× 余量**;极端全屏随机噪点照片背景在 <0.18 失败,非真实 app 屏幕。
- **坐标生成(LCG,TS↔Python 必须逐字节一致)**:种子 `0x5EEDCAFE`,`state=(mul(1103515245,state)+12345)&0xFFFFFFFF`,`mul` = 16-bit 分解 32 位乘法(= JS `Math.imul`)。网格间距 `g=floor(sqrt(usable/nchips))`,`usable` 四周留 2% margin,不足时 g 递减;每个格点 jitter ±g/4;Fisher-Yates 洗牌取前 nchips。bit i → `centers[4i..4i+3]`。
- **覆盖 11.6%**(4080 bit × 4 pair × 2 chip × 9px / 2527200)。与 RS 参数无关 → 用最大纠错余量。

### ECC:RS(255,135)×2

`payload = [magic 0x4D58][len 2B BE][payload]` 零填充到 2×135=270B,分 2 块 `rs_encode_msg(block, nsym=120)` → 510B 编码流 → 4080 bit。每块可纠 **60 字节错**(t=60;测试证明 α0.08 下 5/5 通道形态 + 真实聊天 UI 背景全过,含 native 1440 + 密集文本 + 照片纹理;t=56 在 text/l/1440 失败)。**纯多数投票**解码(不做弃权/擦除 —— 实测弃权反而放大错误)。payload 上限 **266B**(实测 234B,余量 32B)。

### 提取端

1. 打开图 → 转 L → 宽度 ≠ 1080 则 LANCZOS 缩到 1080 宽。
2. 同 seed 生成 LCG centers。
3. 每 bit:读 4 对 chip 的 3×3 均值,`delta = bv − av`(pa 在 cx−2、pb 在 cx+2),多数投票取符号(平局 → 0)。
4. 组装 510B → 每块 `rs_correct_msg(..., 120)`,任一失败 → 无载波/拒读。
5. `[magic 0x4D58][len]` → payload bytes → JSON。

无载波图快速失败:magic 不符或 RS 解不出即返回 None。

## Payload 契约(v2,明文)

`schemaVersion` 必须为 `2`。字段(uid 仅开发环境,prod 省略):

`schemaVersion, appVersion, buildVersion, buildTime, modelName, osName, osVersion, capturedAt` + `uid`(dev only)

删除:`keyId`(加密产物)、`watermarkId`、`pathname`、`platform`、`buildInfo`、`gitCommit` 及全部 testing 字段(nickname/socialId/pageDebug/rawDeviceId 等)。

## CLI(agent 调用入口)

```bash
bugpatrol watermark decode --image /path/to/screenshot.png --json
```

- **成功**:exit 0,`{"found": true, "confidence": 1.0, "payload": { ...8 个字段... }}`
- **无水印**:exit 0,`{"found": false, "confidence": 0, "error": "watermark_not_found"}`
- **图片不存在**:exit 2,`watermark_image_not_found`

去掉 `--json` 输出人类可读的 `[Watermark] schemaVersion=2 appVersion=...` 摘要行。

## 流水线接入点

提取在**原始下载字节**上、`redactor`/`transformer` 重编码**之前**执行(resize/JPEG 转码会毁掉低 alpha pixel carrier):

- **watcher 侧**:`materialize_attachment`(resources.py,RAW bytes → 提取明文 payload → redact → transform → policy → store → describe)→ `Attachment.watermark` = payload compact JSON → issue body `- watermark: <JSON>` 行。
- **runner 侧**:`extract_media_evidence`(triage_context.py)解析 `- watermark:` → `MediaEvidence.watermark` = payload → triage context 渲染成 `- Watermark: [Watermark] appVersion=... uid=...` 供 agent 读取。**无解密、无 key。**

issue body 三态:

- **有水印** → `- watermark: <明文 payload JSON>`
- **扫描过、没有** → `- watermark: 未找到水印`,显式标注「已检查、不存在」
- **未尝试**(非图片视频)→ `""`,整行省略

坏载体 → `水印解码失败 (<code>)`。watermark 任何阶段失败都不阻塞 intake / triage。watcher / backfill / event-watcher / mail-watcher 全部接入。

## 验证(端到端)

- **单测载体**(`tests/test_watermark.py`):trailer / PNG tEXt / 扩频 pixel 三载波 round-trip、prod(无 uid)payload、**半 native 缩放**(540 宽渲染 → 提取器 LANCZOS 回 1080)、RS 预算边界(60 字节错纠正 / 61 → 拒读)、坏载体 `ERROR_BAD_ENVELOPE`、watcher 提取 / runner 解析 / intake 渲染 / triage context / CLI / 明文契约,33 例。
- **RS 单测**(`tests/test_rs256.py`):clean 直通、60 字节错纠正、300 例随机损坏在预算内纠正、远超预算拒读、前缀保留、域约定。
- **fived jest harness**(`app/lib/__tests__/verify-watermark-e2e.test.ts`):用 app 真实代码构建明文 payload + path 几何(dev 带 uid / prod 无 uid),导出到 `$TMPDIR/wm-e2e-*`。
- **BugPatrol E2E harness**(`wm_e2e_embed.py`):**直接解析 fived jest 导出的真实 `darkPath`/`lightPath`**(非重推导),渲染到名义 1080×2340 截图,跑 `bugpatrol watermark decode --json` + watcher→runner split 流程。5 种形态:PNG、JPEG q85、UI 遮挡、UI+q85、**native 1170×2532 q85**(Lark 1080 下采样往返)。全部解出完整明文 payload,与 fived 导出**逐字节相等**。

## 实现位置

- `bugpatrol/watermark/` — types / extractor / reporter / `rs256.py`(Reed-Solomon 编解码)/ `__init__`(公共 API `decode_image`、`WatermarkResourceDecoder`)
- `bugpatrol/resources.py`(watcher 提取明文)、`intake.py`(issue body 渲染)、`triage_context.py`(runner 解析)、`__main__.py`(CLI decode)
- fived:`app/lib/dev/diagnosticScreenshotWatermarkPixels.ts`(扩频 path builder)、`app/components/dev/DiagnosticScreenshotWatermark.tsx`(全屏单 SVG)、`app/lib/dev/diagnosticsClipboard.ts`(8 字段明文 payload)
- 测试:`tests/test_watermark.py` + `tests/test_rs256.py` + fived `app/lib/__tests__/verify-watermark-e2e.test.ts`

依赖:`cryptography` 不再需要(无加密)。PIL + rs256 自带。
