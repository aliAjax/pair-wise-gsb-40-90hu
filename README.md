# 海上搜救协调系统

标准库实现的独立协调原型，使用 SQLite 保存事件、搜救资源、搜索区域、线索、离线批次和时间线。

## 运行

要求 Python 3.11+（在当前 Python 3.9 环境也可运行）。

```bash
python3 app.py --init --seed
python3 app.py
```

默认地址为 `http://127.0.0.1:8206`，数据库默认为 `maritime_sar.db`。`--db`、`--host`、`--port` 可覆盖默认值。

## 主要接口

写操作使用 JSON，并需要 `X-User` 与 `X-Role` 请求头。角色包括 `coordinator`、`operator`、`field`、`analyst`、`viewer`。

- `GET /health`、`GET /api/state`
- `POST /api/incidents`：创建遇险事件并识别重复报警
- `POST /api/assets`：登记资源
- `POST /api/areas`：创建搜索区域
- `POST /api/areas/split`：把搜索区域按方位角拆成连续扇区（同一区域只能拆一次）
- `POST /api/assignments`：按能力、海况和航程分配资源
- `POST /api/areas/reassign`：改派区域（`expected_area_version` 乐观锁，并发改派只有一人成功）
- `POST /api/sectors/coverage`：上报扇区覆盖，按 `client_event_id` 去重；海况或航程不合格只进待核清单，不改变覆盖范围
- `GET /api/reviews?status=pending|resolved|all`：待核清单
- `POST /api/reviews/resolve`：复核待核项（`confirmed` 采纳覆盖 / `rejected` 驳回）
- `POST /api/clues`、`POST /api/clues/verify`
- `POST /api/assets/withdraw`：撤回资源并释放任务
- `POST /api/incidents/transfer`、`POST /api/incidents/close`
- `POST /api/offline/batch`：幂等合并离线记录；支持 `sector_coverage` 事件，批次恢复后重算已结束区域覆盖，冲突项进待核清单，重放批次不生成重复扇区/覆盖记录
- `GET /api/incidents/{id}/timeline`

## 扇区与失联恢复

- 搜索区域拆成按方位角等分的连续扇区（`AREA-xx-S01…`），`sectors.status` 为 `pending`（待补扫）或 `covered`（已覆盖）。
- 覆盖上报携带客户端幂等编号；重复编号返回原记录，不重复计数。
- 海况超出资源能力或扇区超出资源航程时，上报只写待核清单（`coverage_unqualified`），扇区仍为待补扫；协调员/分析师复核后可采纳或驳回。
- 区域已结束才到达的覆盖（含离线恢复批次）产生 `ended_area_coverage` 待核项，系统同时重算该区域覆盖率，冲突留待人工复核。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整协调流程、重复报警、错误位置、资源并发占用、离线幂等和权限拒绝。

## 局限

身份依赖调用方传入的用户和角色头；坐标使用球面距离近似；不会自动计算漂移概率区；文件附件、气象服务、真实通信链路和地理围栏未包含在内。
