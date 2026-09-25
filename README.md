# 充电设备降级容量核算

面向业务人员的纯服务端系统：采集设备告警、功率曲线与维修窗口，按可追溯
规则核算服务区每个 15 分钟时间片的有效接待容量，并把同一配电回路的
共同上限纳入核算，避免“枪在线但已降功率”被简单在线统计高估。

## 架构

代码按领域模型、应用服务、持久化与接口边界分层；时间、标识、签名密钥
均通过可替换端口接入（`ports/`），测试注入固定时钟与确定性标识以稳定
复现状态变化。运行数据（SQLite、签名密钥）不写入源码目录。

```
service_09251_007/
├── domain/          # 纯函数领域层：模型、时间片、核算引擎
│   ├── models.py        # 充电枪/回路/告警区间/维修窗口/规则版本/切片结果
│   ├── timeutil.py      # UTC epoch 与 15 分钟时间片对齐
│   └── capacity.py      # 核算引擎（见下方规则）
├── ports/           # 可替换端口：时钟、标识、HMAC 签名
├── persistence/     # SQLite：事件日志为唯一事实来源，派生切片可重建
│   ├── db.py            # WAL、schema、快照不可变触发器、默认规则
│   └── repositories.py  # 事件装配（告警区间/功率序列）与各仓储
├── application/     # 应用服务：导入/查询/维修/规则/快照/重放/恢复
└── api/             # 标准库 http.server 的 REST 边界
```

### 核算规则（每个时间片、每把枪，全部因子留痕）

1. **维修窗口**覆盖的片内分片容量置零（跨午夜只是普通 `[start, end)` 区间）；
2. **活动告警**按规则版本的类别系数降额（默认温控 0.6 / 配电 0.7 / 通信 0.8 /
   未知 0.5）；多个告警重叠时取**最严格值**，不连乘；
3. **实测功率**只在存在降额告警时收紧估值（`min(规则估值, 片内实测最大)`），
   无告警时低功率仅代表需求不足，不压低能力；只收紧不放松；
4. **回路限额**：同一配电回路按片内分片瞬时求和，超过回路上限时按比例
   （pro-rata）分摊到各枪，回路合计不超过上限。

片内分片按秒级时间加权平均成 15 分钟切片值；每个切片记录命中的规则版本
与全部影响因子（告警、维修、实测、回路分摊），构成可追溯证据链。

### 重放与不可变快照

- 事件（告警发起/解除、功率采样、维修登记/取消）以 `event_id` 幂等落库，
  整批原子生效；告警解除补录、窗口调整、规则升级都会计算**受影响区间**
  并在同一事务内重放派生切片；
- 规则版本带 `effective_from`：历史切片保留原规则结果，只重放生效时点之后；
- 快照在发布时先重放再签名（HMAC-SHA256，规范化 JSON），数据库触发器
  强制 `snapshots` 表不可 UPDATE/DELETE；后续重放只产生更高序号的新快照，
  差异说明 API 逐片对比并关联因果事件（两次快照水位之间的事件日志）；
- 派生表 `capacity_slices` 只是物化缓存，`POST /api/v1/admin/rebuild`
  可随时从事件日志整体重建（SQLite WAL + busy_timeout 保证可恢复）。

## 运行

```bash
# 环境变量：CAPACITY_DB_PATH（默认 var/capacity.db）、CAPACITY_HOST、
# CAPACITY_PORT（默认 8092）、CAPACITY_SIGNING_KEY（hex，缺省生成并落库）
python3 -m service_09251_007
```

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/devices` | 登记回路（`limit_kw`）与充电枪（`rated_kw`），自然键幂等 |
| POST | `/api/v1/events/batch` | 批量导入事件；`event_id` 去重幂等，整批原子，返回重放区间 |
| GET | `/api/v1/capacity` | 实时查询 `station_id/start/end`，逐片返回枪级容量与解释因子 |
| POST | `/api/v1/maintenance` | 登记维修窗口（返回 `version`） |
| PUT | `/api/v1/maintenance/{id}` | 乐观并发更新，`expected_version` 不符返回 409 与当前版本 |
| POST | `/api/v1/rules` | 发布新版核算规则并重放 `effective_from` 之后区间 |
| POST | `/api/v1/snapshots` | 发布区间容量快照并签名（不可变） |
| GET | `/api/v1/snapshots/{id}` | 读取快照内容与签名 |
| POST | `/api/v1/snapshots/{id}/verify` | 验签 |
| GET | `/api/v1/snapshots/diff` | `from_id/to_id` 差异说明：变化切片、原因、因果事件 |
| POST | `/api/v1/admin/rebuild` | 从事件日志重建全部派生切片（恢复路径） |

事件类型：`alarm_raised` / `alarm_cleared`（`alarm_id` 配对，解除可补录
历史时刻）、`power_samples`（片内实测功率）、`maintenance_upsert` /
`maintenance_cancelled`。时间一律使用带时区的 ISO 8601。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：重叠降级取最严格、片内时间加权、跨午夜维修、回路限额分摊、实测
功率收紧、批量导入幂等与原子性、维修并发不丢更新（8 线程 × 5 次 CAS）、
解除补录重放、规则升级分区生效、派生态重建恢复、快照不可变/验签/差异
说明，以及 HTTP 端到端全流程。

## 编译检查

```bash
python3 -m compileall -q service_09251_007 tests
```
