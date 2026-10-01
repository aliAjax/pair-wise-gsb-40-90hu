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
- `POST /api/areas/split`：把区域拆成首尾相接的连续扇区（重复拆分幂等，数量变化返回 409）
- `GET /api/areas/{id}/sectors`：列出区域扇区
- `POST /api/coverage`：资源上报扇区覆盖，按 `client_report_id` 去重；海况/航程不合格只进待核清单，不改变覆盖
- `POST /api/sectors/reassign`：扇区改派（乐观版本控制，并发改派只有一人成功）
- `GET /api/coverage/reviews`：待核/复核清单
- `POST /api/coverage/review`：协调员批准/驳回待核项；区域已结束时批准保留为冲突终态
- `POST /api/assignments`：按能力、海况和航程分配资源
- `POST /api/clues`、`POST /api/clues/verify`
- `POST /api/assets/withdraw`：撤回资源并释放任务（含扇区任务）
- `POST /api/incidents/transfer`、`POST /api/incidents/close`
- `POST /api/offline/batch`：幂等合并离线记录，支持 `clue`、`timeline`、`sector_split`、`coverage` 事件；
  合并后重算涉及区域的覆盖，已结束区域的差异生成冲突项留待复核，批次/上报重放均不产生重复扇区或覆盖记录
- `GET /api/incidents/{id}/timeline`

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整协调流程、重复报警、错误位置、资源并发占用、离线幂等、权限拒绝、连续扇区拆分、覆盖去重、待核清单、并发改派和离线恢复冲突复核。

## 局限

身份依赖调用方传入的用户和角色头；坐标使用球面距离近似；不会自动计算漂移概率区；文件附件、气象服务、真实通信链路和地理围栏未包含在内。
