---
name: issue-daily
description: 当日每人上报的 Issue 数统计（GitHub issue 为基准）。回答「今天谁发了多少个 Issue」。同时兼容 BugPatrol intake issue 与原生 GitHub issue。
---

# issue-daily — 当日每人上报 Issue 数

以 **GitHub issue 为基准**统计一个 repo 当天新建的 issue,按上报人聚合。
适合挂在 Lark 群(topic agent)里做日报,例如在 5D 群的 gitHub issue skill
里挂一个 `daily-reporters` 子命令。

## 上报人怎么识别

脚本按优先级识别「谁发的」:

1. **BugPatrol intake**——issue body 隐藏注释
   `<!-- BUGPATROL_INTAKE_META:{"reporter_open_id": "..."} -->` 里的
   `reporter_open_id`(GitHub issue 作者是 bot,真正上报人在这里)。
2. **原生**——无 intake meta 时,用 `issue.user.login`(作者即上报人)。

> 为什么不以 Lark topic 为基准:数的是消息条数(含 followup/闲聊),不是
> issue 数;还要 live Lark API + 重做 intake 已做过的 message_id 去重。

## 用法

```bash
# 今天(Asia/Shanghai 当天起点),读 fived 项目配置(仓库 + 名字映射)
python scripts/issue_daily_report.py --project projects/fived.toml

# 指定日期 / 指定 repo / 指定 gh 身份(bot)
python scripts/issue_daily_report.py --project projects/fived.toml --since 2026-08-17
python scripts/issue_daily_report.py --repo TheCloverLab/bugpatrol-todo-sandbox
python scripts/issue_daily_report.py --project projects/fived.toml --gh ~/clover/fived/scripts/gh-as-bot.sh

# 机器可读输出
python scripts/issue_daily_report.py --project projects/fived.toml --format json
```

输出 markdown 表格:上报人 | Issue 数 | Issues(带链接)。

## 名字解析优先级

1. `[lark.sender_names]`(open_id → 名字,如未配置则跳过)
2. `[lark.user_open_ids]` 反向(open_id → GitHub login)
3. 回退:裸 open_id

> ⚠️ `projects/fived.toml` 目前没有 `[lark.sender_names]`,但有
> `[lark.user_open_ids]`(8 人团队全覆盖),所以会显示成 GitHub login。
> 想显示真名,补一份 `[lark.sender_names]` 即可。

## 已知边界

- **batched issue**:多条 Lark 消息折成一个 GitHub issue 时,meta 只记录首条
  消息的上报人;同一 batch 里其他上报人会低估(当前 fived 折叠频率很低)。
- **时区**:`created_at` 是 UTC,脚本按 `--tz`(默认 Asia/Shanghai)算当天窗口。

## 集成到 5D

两选一(都不依赖原 skill 内部):

- **A. 独立 skill**:把本目录 `.claude/skills/issue-daily/` 拷到 5D 群 agent
  可用的 skill 目录,直接 `/issue-daily`。
- **B. 挂子命令**:在 5D 的 gitHub issue skill 的 Daily 派发处加一行,调用
  `python <本脚本> --project projects/fived.toml`,把输出作为 Daily 结果。

脚本无第三方依赖(stdlib + `gh`),`gh` 需能读目标 repo。
