# 栖浦市计量资料仓

实验室向栖浦市计量资料室提交测量参考规则，计量站送交单站单日的有序观测原件。白班接入员处理退件和重投，质控员按指定目录修订发布校正报告，夜班按交接清单核算站点日值。次日复核员可在原件端口关闭后按号领取原报告和日结。

## 运行

使用 Python 3.11 标准库，从项目根目录执行。

```sh
python3 -B check_project.py
python3 -B demo.py
```

`check_project.py` 由控制台提供。也可以直接运行原回归。

```sh
python3 -B -m unittest discover -s tests -t . -v
```

演示使用独立临时库。样例中 A 与 B 同组、同优先级，覆盖 `[0, 10)`，偏移分别为 2 和 7。读数 `5, 5, 10, null` 在固定修订下依次得到 `7, 7, NO_MATCH, MISSING`。日结通过数为 2、和值为 14，两种未通过状态各计 1。即使 A 后来改为偏移 4，绑定原修订的报告仍各得 7。演示关闭原件端口及工作库后重开，核对原字节与交接顺序。

## Python 接口

调用方使用 `sqlite3.connect(path)` 打开连接，并调用 `lake.store.initialize(db)`。每个新连接均可初始化。首次初始化建立空目录修订 `0`，重复执行保留已提交业务。高层写接口拥有事务，调用时连接须无活动事务。仅在测试或诊断时可传入 `before_commit(db)` 回调，在所有业务写入之后、正式提交之前触发故障。回调须由入口控制提交与回滚。

| 模块 | 接口 | 输入与返回 |
| --- | --- | --- |
| `lake.rules.revisions` | `load_rules(db, batch_id, operations)`、`read_catalog_head(db)` | 新批返回整数修订号。同名同规范化内容返回首次修订。 |
| `lake.rules.revisions` | `enable_withdrawal(db, *, before_commit=None)`、`withdraw_rules(db, request_id, batch_id, *, before_commit=None)` | 撤回保护入口，见下文《参考批撤回》。 |
| `lake.rules.snapshots` | `get_catalog(db, revision)` | 返回按键排序的 `Rule` 元组，包含 `write_id`。 |
| `lake.intake.batches` | `admit(db, objects, batch_id, station, day, raw)` | 接纳 UTF-8 JSON 原件字节，返回批号。 |
| `lake.reports.publication` | `process(db, objects, report_id, batch_id, revision)` | 返回固定报告字典。同号同绑定须重新核验固定输入才可重投。 |
| `lake.closing.publication` | `close_day(db, close_id, day, report_ids)` | 返回固定日结字典。同号同日期及同序引用返回已发布日结。 |
| `lake.archive.delivery` | `retrieve(db, category, business_id)` | 返回核验通过的原冻结 UTF-8 JSON 字节。 |

`objects` 是 `lake.intake.objects.ObservationObjects()`。其 `close()` 停止本端口的原件读写。日结与成果领取只访问冻结成果。成果类别为 `report` 和 `daily_close`，可以使用相同业务号。日期采用 `YYYY-MM-DD`，编号、站号、组和规则键使用非空文本。

写规则用 `{"op":"put","key":"A","group":"length","priority":1,"lower":0,"upper":10,"offset":2}`。删除用 `{"op":"delete","key":"A"}`，删除缺席键也发布修订。请求内键唯一，多键请求的操作排列不影响重投。显式写入的 `write_id` 是由批号和键编码的稳定文本，未触及的规则沿用原身份。

观测原件是有序数组，每行为 `{"group":"length","reading":5,"unit":"um"}`。支持 `um` 和 `mm`，`reading` 为整数或 `null`，缺测行也须有有效单位。规范化签名保留顺序和重复次数。报告 `rows` 中每行含零起始 `position`、`status`、`rule_key`、`write_id`、`corrected`。选择范围左闭右开，优先级数值越小越优先，同优先级按规则键。日结 `report_ids` 保留交接顺序，`stations` 按站号排序。站点值以 `ok`、`total`、`missing`、`no_match` 表示。度量整数用 JSON 保存，校正和求和全程使用整数。空数组可表示无规则操作、无观测或无交接报告。

`lake.store` 提供 `ValidationError`、`ConflictError`、`NotFoundError`、`SourceUnavailableError` 和 `IntegrityError`。SQLite 操作及提交错误保留其原异常。规则批、接纳、报告与日结分别是完整事务。日结按观测批号排除重复覆盖，成果领取先核对完整摘要，再核对格式与编号。

## 参考批撤回

`enable_withdrawal(db)` 在当前目录头启用保护并返回锚点修订，本身不发布目录修订；重复启用返回同一锚点，撤回成功后不能再开启新一轮。保护期内**首次成功入仓的新规则批**是唯一可撤回的批（旧批重投沿用回执，不算新入仓），期内**首次成功签发的报告**均受保护。`withdraw_rules(db, request_id, batch_id)` 全量重算纠正目录：按修订顺序重放各批冻结的规范化操作并跳过目标批，其余批的显式等值写入、删除（含删除当时已缺席的键）仍在原修订生效，存续规则保留原 `write_id`。

- 受阻挡：返回 `{"status": "blocked", "report_id": ..., "position": ...}`。按首次签发顺序找首份报告，再按原观测顺序找首个变化行；逐行比较 `status`、`write_id`、`corrected`（含 `MISSING`、`NO_MATCH`），选规身份变化但校正值相同也算变化。目录与报告不变，不占用请求号。
- 安全撤回：发布新目录修订并推进目录头（即使纠正内容与当前目录相同），与成功回执同次提交，返回 `{"status": "withdrawn", "revision": ...}`。成功请求号与同批重投返回首次结果；同号换批或成功后再提另一笔抛 `ConflictError`；未启用、批号未知或不在保护范围抛 `ValidationError`。

### 可重开的复核事实如何交接

规则入仓时，除原有快照和回执外，规范化操作随入仓事务写入 `rule_batch_operations`，使纠正目录可以只凭冻结表重放而无需恢复原请求。报告首次签发且保护已启用时，`process` 在报告自己的提交事务内把该报告逐行规范化观测 `(position, group, reading)` 连同 SHA-256 摘要写入 `protected_report_facts`，并用自增序号固定首次签发顺序；报告原行仍冻结在成果库。撤回复核只读这两类冻结事实和 `catalog_snapshots`，绝不打开原件端口、不读取 `observation_objects` 的载荷，因此原件归冷、关库重开后仍可逐行复核。冻结事实缺失或摘要损坏时抛 `IntegrityError`。原修订、原报告、日结、V1 回执和接口全部保留原号原字节；撤回后重投已撤批仍返回它原先的入仓修订，不会重新作用于目录头。
