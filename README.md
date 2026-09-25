# 数字人文文本校勘

这是一个 Python 标准库实现的校勘工作台，使用 SQLite 保存作品、版本、残片、转录、段落、异文、注释、修订层和快照，并通过 `http.server` 暴露 JSON API。

## 启动与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8114`，地址 <http://127.0.0.1:8114>。首次启动创建一个带缺页残片和不可辨标记的示例。数据库可通过 `COLLATION_DB` 指定，端口可通过 `PORT` 指定。

## 业务规则

- 版本类型限定为 `version`、`fragment`、`transcription`。
- 段落和版本必须属于同一作品，同一版本不能重复对齐同一段落。
- 只有负责人或被单独授权的编辑可以修改对应版本；其他用户只有查看权限。
- `[缺页]`、`[不可辨]`、`[残损]` 等标记会参与校勘稿导出和缺口统计，不匹配的方括号会拒绝保存。
- 每次新增或修改异文都会产生递增修订号和 JSON 快照；提交必须携带 `expected_revision`，旧页面不能覆盖新层。
- 锁定段落由负责人执行，锁定后任何新修订都会被拒绝。
- 负责人可把当前各段落修订固化为**定本**：每次发布生成递增版本号和说明，并记录当时的对齐、异文、注释、缺口数量及各段落修订头；定本以不可变快照保存，历史定本只读、可随时回看。
- 再次发布定本必须基于上一定本之后的新修订；当前内容（修订头或各项数量）没有变化时拒绝重复发布，旧内容不会被当成新定本。
- 定本与草稿修订按先后构成时间线：已并入某版的修订标注对应版本号，最新定本之后的修订标注为草稿。

## 主要接口

- `POST /api/users`、`POST /api/works`
- `POST /api/works/{id}/witnesses`、`POST /api/witnesses/{id}/editors`
- `POST /api/works/{id}/passages`、`POST /api/works/{id}/access`
- `POST /api/alignments`
- `POST /api/variants`、`POST /api/variants/{id}/revisions`
- `GET /api/passages/{id}/snapshots/{revision}?user_id=...`
- `POST /api/passages/{id}/lock`
- `GET /api/works/{id}/collation?user_id=...`
- `POST /api/works/{id}/editions`（负责人发布定本，body 含 `note`、`user_id`）
- `GET /api/works/{id}/editions?user_id=...`（定本列表、草稿修订、可否发布、时间线）
- `GET /api/editions/{id}?user_id=...`（回看只读定本快照）

导出接口把版本对齐、异文、注释、残损缺口和锁定状态组合成可复核的校勘稿。
