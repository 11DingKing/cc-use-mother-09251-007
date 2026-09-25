# 充电设备降级容量核算

服务区充电枪可能因温控、配电或通信异常**降功率**而非完全停机，按在线枪数统计会高估接待能力。
本服务采集设备告警、功率曲线与维修窗口，按可追溯规则逐 15 分钟时间片计算有效容量，并把同一
配电回路的共同上限纳入核算；告警解除、曲线补录、维修更新与规则升级都会**重放受影响区间**，
而已签署发布的容量快照**物理不可变**。

## 分层结构

```
service_09251_007/
├── domain/
│   ├── timeline.py      # 15 分钟半开区间 [start,end) 的取整/枚举
│   ├── models.py        # 回路、枪、降级区间、规则版本、逐片核算结果
│   └── engine.py        # 纯函数规则引擎（无 IO、无当前时间，可对任意历史区间重放）
├── application/
│   └── service.py       # 用例编排：受影响区间 → 重放 → 落盘 / 签署 / 差异
├── infrastructure/
│   ├── db.py            # SQLite schema、WAL、不可变快照触发器
│   └── repository.py    # 行/对象转换、事务、幂等与追加日志
├── interfaces/
│   └── http_api.py      # JSON over HTTP（标准库，无第三方依赖）
├── ports.py             # Clock 时间端口（测试注入 FixedClock）
└── bootstrap.py         # 组装
```

## 核算规则（每个 15 分钟片）

1. **逐枪降级因子**：片内按告警/维修边界切小段，按覆盖时长加权；同一枪上多重降级默认取
   `min`（最严格），规则升级可改为 `multiply`。每段都记录命中的事件号（`kind/ref/factor`），
   结果可追溯。
2. **功率曲线佐证**：片内实测样本数达到阈值（默认 2）时，以最高输出/额定功率作为曲线因子，
   与事件因子再取 min。样本不足不采信曲线。
3. **回路共同上限**：成员枪有效容量求和后受回路 `limit_kw` 封顶，并标记 `binding` 与成员明细。
4. **规则版本**：按时间片选择生效版本，逐片记录 `rule_version`；升级后重放生效区间。

## 重放与不可变

- 任何写入（告警上报/解除、维修窗口、曲线补录、规则升级、批量导入）都计算受影响时间片范围
  并重算覆盖落盘到 `capacity_slices`；每次重放在 `replays` 台账留痕。
- "先持续中、后解除"的事件会把重放窗口延伸到历史重放过的最远终点，清掉过期未来片。
- `snapshots` 表带 `BEFORE UPDATE/DELETE` 触发器（RAISE ABORT），已发布快照无法被任何
  SQL 路径改写；快照内容附带规范化 SHA-256 与规则版本列表。

## 幂等与并发

- 告警以 `event_ref` 为主键，配单调 `seq`：重复投递忽略，迟到旧 seq 不覆盖新状态。
- 批量导入以 `batch_id` 占位，重复批次整体返回 `duplicate`。
- 维修窗口更新写入只追加的 `maintenances_events(window_ref, op_seq)` 唯一表，再按序折叠
  到主行：并发两路不同 `op_seq` 的更新都不会丢；同一 `op_seq` 重投幂等、内容不同报 409；
  可选 `expected_version` 做乐观锁。
- 写入统一 `BEGIN IMMEDIATE` 事务 + WAL + busy_timeout；HTTP 每工作线程独立连接。

## API

| 方法/路径 | 说明 |
| --- | --- |
| `POST /imports` | 批量导入拓扑/告警/维修/曲线/规则（`batch_id` 幂等） |
| `POST /topology` | 回路与枪 |
| `POST /alarms` | 告警上报与解除（`ended_at` 为空表示持续中） |
| `POST /maintenances` | 新建维修窗口（支持跨午夜、`scope=gun\|circuit`） |
| `POST /maintenances/events` | 追加维修更新（`op_seq`、可选 `expected_version`） |
| `POST /curves` | 功率曲线补录 |
| `POST /rules` | 规则升级并重放生效区间 |
| `POST /replays` | 手动重放指定区间 |
| `GET  /capacity?start=&end=&scope=&target_id=` | 实时查询（缺片自动重放） |
| `POST /snapshots` | 快照签署（返回 SHA-256） |
| `GET  /snapshots/{id}` | 读取快照 |
| `GET  /snapshots/{a}/diff/{b}` | 两份快照逐片差异说明 |
| `GET  /replays` | 重放台账 |
| `GET  /health` | SQLite 完整性检查 |

## 运行

```bash
python3 -m service_09251_007 --db data/capacity.db --port 8080
```

运行数据只写 `--db` 指定位置（默认 `data/`，已加入 `.gitignore`），不写源码目录。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q service_09251_007 tests
```

测试覆盖：重叠降级取最严因子与按段追溯、部分时间片时长加权、跨午夜维修、回路范围维修、
回路级限额封顶/放松、事件与批次幂等、并发维修更新不丢失与乐观锁冲突、关闭重开后状态恢复、
快照触发器级不可变与重复签署冲突、快照差异说明、曲线补录、规则升级分版本重放，以及
HTTP API 端到端往返。
