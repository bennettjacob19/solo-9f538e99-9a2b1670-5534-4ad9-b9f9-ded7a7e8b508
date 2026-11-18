# 本地服务发现目录（svcdir）

一个运行在 Flask + SQLite 上的小型服务发现目录：运维人员可以在网页上修改
「服务名 → 目标地址」的映射、创建命名备份、并从一个备份恢复。另有 `/resolve`
查询 API 供客户端使用。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
```

- 网页：<http://localhost:8000/>
- API：`GET http://localhost:8000/resolve?name=orders-api`
- 健康检查：`GET /healthz`
- SQLite 数据与备份都位于命名卷 `svcdir-data` 的 `/data/directory.sqlite3`
  （备份是同一数据库里的版本快照，所以备份与恢复天然原子）。

### 本地运行

```bash
python3 -m pip install -r requirements.txt
SVCDIR_DB=./data/directory.sqlite3 python3 -m svcdir   # 开发用内置服务器
# 或： gunicorn --bind 0.0.0.0:8000 --workers 1 --threads 8 svcdir.__main__:app
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SVCDIR_DB` | `data/directory.sqlite3` | SQLite 文件路径 |
| `SVCDIR_RESTORE_LEASE_SECONDS` | `30` | 恢复租约时长（进程异常死亡后的兜底回收上限） |
| `PORT` | `8000` | 仅用于 `python -m svcdir` |

## 页面与 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 网页：目录、修改表单、备份列表、恢复按钮 |
| GET | `/resolve?name=<服务名>` | 返回单个服务的地址 |
| GET | `/resolve` | 返回整个当前目录 |
| GET | `/api/state` | 当前版本、完整目录（含每条服务的编辑令牌）、恢复状态、备份列表 |
| GET | `/api/backups` | 备份列表 |
| POST | `/api/services` | `{"service": ..., "address": ..., "note": ..., "token": ...}` 新增/修改；`token` 可选 |
| DELETE | `/api/services/<名>` | 删除；`?token=...` 可选（也接受 JSON 体 `{"token": ...}`） |
| POST | `/api/backups` | `{"name": ...}` 创建命名备份 |
| GET | `/api/backups/<名>/validate` | 校验备份（不修改目录） |
| POST | `/api/backups/<名>/restore` | 从备份恢复 |

浏览器表单走 `POST /services`、`/services/<名>/delete`、`/backups`、
`/backups/<名>/restore`，成功后 303 跳回页面并显示结果或错误。

查询响应示例：

```json
{"ok": true, "version": 6, "restore_in_progress": false,
 "name": "orders-api", "address": "10.0.0.12:8080"}
```

`/resolve` 永远只返回完整、已提交的目录版本，不含编辑令牌等内部状态。

## 一致性与恢复语义

实现要点（对应需求逐条）：

- **备份是同一提交点的完整目录。** 目录从不原地修改：每次修改都写入一个
  *完整的新版本*（先复制当前版本的全部行，再改一行），最后一条
  `UPDATE meta SET value=<新版本>` 才是发布。备份行只固定「版本号」，因此
  创建备份期间持续写入不会让备份变成半个目录。
- **恢复前校验。** 恢复分为三步：①在一个写事务里登记「恢复中」租约；②只读
  事务里校验备份（条目数、每行服务名/地址合法性、重复服务名、SHA-256 摘要与
  备份记录比对）；③一个原子写事务创建完整的新版本并切换 `current_version`。
  校验失败 → 直接返回 422，目录一个字节都不动。
- **恢复期间查询仍返回当前完整目录。** `current_version` 只在最后一步切换，
  查询永远读它；整个查询在读事务快照里完成，因此不会读到混排的数据。
- **新修改明确拒绝，已受理的修改先结算。** 登记恢复意图需要写锁，所有已受理
  的修改必然先提交完成；之后到达的修改看到有效租约，返回 `409
  restore_in_progress`（网页也会明确提示）。
- **恢复后新查询只见备份地址；先前受理的查询不混用两版。** 切换后新查询用新
  版本；一个在切换前开始的查询持有自己的读快照（`store.read_view()`），
  即使内部重复读取也仍是同一版本。
- **切换中断后重启只呈现原版或完整备份版。** 新版本与版本号切换在同一事务内
  提交；崩在提交前 → 仍是原版，崩在提交后 → 是完整备份版，旧版本的行永不删除。
  恢复者用租约 + 操作系统文件锁（`<db>.restore.lock`）标记存活：进程死亡时
  内核释放锁，重启后立即回收被遗弃的「恢复中」标记，不必等租约到期。
- **并发恢复。** 文件锁 + 数据库内的 `restore_state` 双重互斥，第二个恢复请求
  得到 409。

## 编辑令牌（防止旧表单覆盖新地址）

网页为每条已有服务各渲染一个「编辑」表单（行内修改地址）和删除按钮，两者都带
一个隐藏的**编辑令牌**；保存与删除时都会校验。令牌是不透明字符串，不是版本号：

- 令牌 = HMAC(数据库中持久化的随机密钥, 全局恢复纪元, 服务名, 该服务的代次)。
  代次与纪元都在 SQLite 里，所以**重启不重置令牌**，读取和刷新页面也不会改变它。
- 保存某服务只递增**该服务**的代次，所以其他服务的表单不受影响；令牌里还绑定了
  服务名，令牌无法挪用到别的服务。
- 服务被改动、删除、删除后重建，或从备份恢复（递增全局纪元，使所有服务的旧令牌
  一次性失效）之后，旧表单都会得到 `409 stale_edit`，响应（JSON 的
  `error.current`，网页的冲突提示）显示**当前已发布状态**，并且**不会产生新的
  目录版本**。由于令牌与地址无关，即使地址又被改回表单里的原值，旧表单仍然冲突。
- 令牌校验与发布新版本在**同一个写事务**里完成（`BEGIN IMMEDIATE`）：并发提交
  同一份表单时只有一个成功，已受理的写入不会在校验之后被插队。
- **旧的无令牌 API 仍可用。** `POST /api/services`（增或改）、
  `DELETE /api/services/<名>` 不传 `token` 时照常工作（供脚本使用），但该写入会
  递增对应服务的代次，因此会让该服务所有已打开的旧表单失效。
- 网页底部不带令牌的表单只用于**新增**：若同名服务已存在，它会提示冲突而不是
  覆盖，避免成为绕过令牌的入口（要修改请用目录行内的编辑表单）。
- 校验失败的恢复（例如备份损坏）**不会**改动令牌，目录保持可写；恢复进行期间
  仍按原有语义拒绝写入（`409 restore_in_progress`）。

## 测试

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pytest -q
```

关键交错场景覆盖在 `tests/test_svcdir.py`：

- `test_writes_rejected_while_restore_in_progress_and_settled_before`
- `test_query_accepted_before_switch_keeps_one_version`
- `test_concurrent_writers_during_restore_never_mix`
- `test_cross_process_readers_and_writer_during_restore`（独立进程 + WAL 快照）
- `test_corrupt_backup_is_rejected_and_directory_unchanged`
- `test_backup_with_missing_rows_is_rejected`
- `test_interrupted_switch_presents_original_or_complete_backup`
- `test_killed_restorer_leaves_original_and_restart_can_retry`（SIGKILL 恢复进程）
- `test_expired_restore_lease_is_reclaimed_after_restart`
- `test_dead_restorer_is_reclaimed_immediately_on_restart`
- `test_restore_can_run_after_prior_abandoned_marker`
- `test_live_restore_lease_survives_sibling_process_initialization`
- `test_backup_row_pins_one_version_during_continuous_writes`
- `test_http_rejects_writes_and_serves_reads_during_restore`
- `test_api_flow` / `test_html_page_and_forms` / `test_html_escapes_hostile_names`
- `test_api_validation_errors_are_json` / `test_edge_case_inputs_are_rejected_cleanly`

编辑令牌相关：

- `test_stale_form_cannot_overwrite_newer_edit`（后提交的旧表单被拒、无新版本）
- `test_stale_form_conflicts_even_if_address_reverted`（地址改回原值仍冲突）
- `test_delete_then_recreate_invalidates_old_form`
- `test_restore_invalidates_all_old_forms_even_if_same_values`
- `test_failed_restore_does_not_invalidate_tokens`
- `test_other_services_edit_does_not_invalidate_token`
- `test_token_survives_restart` / `test_token_survives_restart_through_http`
- `test_token_check_and_publish_are_atomic`（并发提交只有一个成功）
- `test_legacy_tokenless_api_still_works_and_invalidates_forms`
- `test_html_add_form_refuses_to_overwrite_existing_service` /
  `test_legacy_api_is_not_affected_by_add_form_restriction`
- `test_html_conflict_shows_current_state_and_creates_no_version` /
  `test_html_conflict_after_restore_renders_current_state` /
  `test_html_stale_delete_conflict`
- `test_legacy_database_without_token_schema_is_migrated`
- `test_token_does_not_leak_into_resolve`

## 布局

```
svcdir/errors.py     错误类型 → HTTP 状态码
svcdir/store.py      目录版本、命名备份、校验、恢复（核心逻辑）
svcdir/web.py        Flask 路由：网页 + API（编辑令牌校验、冲突展示）
svcdir/templates/    页面模板
tests/test_svcdir.py 自测（含并发交错）
Dockerfile / docker-compose.yml
```

## 已知限制与未完成项

- **未在 Docker 中实际构建/运行。** 本环境没有 Docker（`docker` 命令不存在），
  因此 Dockerfile 与 compose 文件只做了静态检查（compose YAML 已解析校验）。
  应用本身已用 gunicorn 在真实 HTTP 上跑通。`docker compose up --build` 即可
  按上方说明启动，数据落在命名卷 `svcdir-data`。
- 单容器部署，恢复互斥依赖同一文件系统上的 `flock`；若把数据目录放到不支持
  `flock` 的网络文件系统，会退化为「租约到期才算失效」。
- 备份保存在同一 SQLite 数据库中，可防止误改目录，但不能防磁盘/文件损坏；
  需要异地容灾应另行导出备份文件。
- 未做用户认证与 HTTPS——按需求这是本地运维工具，请只在内网使用。
- 恢复的「已受理查询」保证依赖单个查询请求只在一个读事务里读完（当前实现如此）；
  没有为客户端提供跨多个独立 HTTP 请求的版本固定参数。
