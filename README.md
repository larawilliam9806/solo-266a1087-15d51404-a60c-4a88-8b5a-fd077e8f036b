# 车辆轨迹匹配 API

基于 **FastAPI + SQLite** 的地图匹配(Map Matching)服务:登记带版本的有向路网,提交带 UTC 时刻的轨迹,将轨迹点吸附到 50 米内的候选路段,结合路网连通性用 HMM + Viterbi 生成全局最优的连续路径,无法连通或超距的区间标记为缺口。

## 快速启动

### Docker Compose(推荐)

```bash
docker compose up --build
```

服务监听 `http://localhost:8000`,SQLite 数据持久化在命名卷 `traj-data`(`/data/app.db`)。交互式文档见 `http://localhost:8000/docs`。

### 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload
```

运行测试:

```bash
.venv/bin/python -m pytest tests/ -q
```

## 匹配算法

1. **候选生成**:每个 GPS 点投影到 50 米(`MATCH_RADIUS_M`)以内的有向路段,取投影点作为吸附位置(每点最多保留 8 个最近候选)。
2. **连通性**:候选 a → b 的路径距离 = a 沿路段到终点 + 有向图最短路径(Dijkstra,按源点缓存)+ b 在目标路段上的偏移。端点坐标一致(1e-6 度内)即视为同一图节点;方向不可逆行。
3. **全局最优(HMM + Viterbi)**:
   - 发射概率:点到路段距离的高斯似然(σ = 15m);
   - 转移概率:`exp(-|路网路径距离 − 两点球面直线距离| / 30m)`,不连通则禁止。
   - 同一时刻存在多个候选时,取全局概率最大的路径而非逐点最近邻,保证路径连续。
4. **连续路径展开**:相邻匹配点之间按最短路径补全中间路段,生成完整连续路径;进入/离开时刻按里程比例在相邻采样点时刻间插值。
5. **缺口**:
   - `out_of_range` — 点距所有路段超过 50 米,无候选;
   - `disconnected` — 前后点均有候选,但有向路网不连通,无法生成连续路径。

## 版本语义

- 路网按版本登记,版本号全局唯一,登记后**不可变**;修订路网 = 登记新版本。
- 每次匹配显式指定 `network_version`,分析全程只读取该版本的快照,**不混用版本**;分析期间登记新版本不影响进行中的匹配。
- 分析结果连同版本号一起落库,路网修订后旧分析仍可按原版本查询(`GET /api/analyses?network_version=...`)。

## 里程对账(车载里程表 vs 匹配里程)

结算巡检里程前,用**车载里程表读数**核对地图匹配算出的里程。匹配结果与读数解耦:
读数补交/更正后**无需重跑地图匹配**,对账查询直接读取保存的结果。

### 流程

1. **匹配时保存核算依据**:新分析落库时按原轨迹采样序号逐点保存
   `analysis_samples`(是否匹配、匹配到的累计里程、未匹配时的缺口原因),
   以及带起止采样序号的 `gaps`。核算只依赖这份依据。
2. **按轨迹编号整组补交/更正读数**:`POST/PUT /api/trajectories/{id}/odometer`
   - 至少两条,用**不同的原轨迹采样序号**(`seq`,从 0 起)定位,累计米数(`odometer_m`)须非负;
   - 整组替换该轨迹当前读数,补交与更正语义相同,`revision` 递增;
   - 读数须落在该轨迹采样序号范围内(否则 422)。
3. **对账**:`GET /api/analyses/{id}/reconciliation` 按分析编号查询保存的结果。
   沿采样顺序**逐对核对相邻读数**,每段给出起止序号:
   - `ok`:表计增量 `odometer_delta_m`、匹配里程 `matched_distance_m`、
     差值 `difference_m = 匹配里程 - 表计增量`;
   - `unverifiable`:区间内存在匹配缺口(`match_gap`,附具体缺口原因)或读数倒退
     (`reading_regression`)时,注明无法核对的原因,不给出上述三个数值;
   - 汇总只累计可核对段。

### 状态与语义

| status | 含义 |
| --- | --- |
| `pending_readings` | 新分析尚无读数,提示待补交 |
| `ok` | 读数齐全,已按当前整组读数对账 |
| `needs_rematch` | 旧分析缺少按采样序号保存的核算依据;原报告照常可查,对账提示重新匹配 |

- **一致性**:新建分析、整组读数更新与受影响的对账结果在**同一事务**内生效;
  保存失败会回滚,保留原读数与对账结果。
- **交错安全**:匹配落库与读数更新都用 `BEGIN IMMEDIATE` 串行化,
  对账在事务内按**当前整组读数**计算,因此交错执行时结果仍对应当前读数。
- **旧数据兼容**:升级前生成的分析没有核算依据,`GET /api/analyses/{id}` 照常返回
  (`basis_available=false`),对账返回 `needs_rematch`,重新匹配即可生成依据。

示例:

```bash
# 按轨迹 1 整组补交读数:采样序号 0/2/4/6 对应累计里程
curl -X POST localhost:8000/api/trajectories/1/odometer \
  -H 'Content-Type: application/json' \
  -d '{"readings": [
        {"seq": 0, "odometer_m": 1000.0},
        {"seq": 2, "odometer_m": 1085.0},
        {"seq": 4, "odometer_m": 1196.0},
        {"seq": 6, "odometer_m": 1281.0}]}'

# 按分析编号查对账结果(直接读保存结果,不重跑匹配)
curl localhost:8000/api/analyses/1/reconciliation
```

响应示例(节选):

```json
{
  "analysis_id": 1,
  "trajectory_id": 1,
  "status": "ok",
  "readings_revision": 1,
  "segment_count": 3,
  "checked_count": 3,
  "unverifiable_count": 0,
  "odometer_delta_m": 281.0,
  "matched_distance_m": 281.0,
  "difference_m": 0.0,
  "segments": [
    {"seq": 0, "start_seq": 0, "end_seq": 2, "status": "ok", "verifiable": true,
     "reason": null, "reasons": [], "gap_reasons": [], "note": null,
     "odometer_delta_m": 85.0, "matched_distance_m": 85.0, "difference_m": 0.0},
    {"seq": 1, "start_seq": 2, "end_seq": 4, "status": "unverifiable", "verifiable": false,
     "reason": "match_gap", "reasons": ["match_gap"], "gap_reasons": ["out_of_range"],
     "note": "区间内存在匹配缺口,无法按地图匹配里程核对(out_of_range)",
     "odometer_delta_m": null, "matched_distance_m": null, "difference_m": null}
  ]
}
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/networks` | 登记带版本的有向路网(路段编号、起终点坐标、长度) |
| GET | `/api/networks` | 列出路网版本 |
| GET | `/api/networks/{version}` | 查看某版本路网(含全部路段) |
| POST | `/api/trajectories` | 提交轨迹(UTC 时刻 + 坐标,按时间排序存储) |
| GET | `/api/trajectories/{id}` | 查看轨迹 |
| POST | `/api/match` | 在指定版本上匹配,返回匹配路段、进入/离开时刻、里程、缺口 |
| GET | `/api/analyses` | 查询历史分析(可按 `network_version`、`trajectory_id` 过滤;含 `reconciliation_status`) |
| GET | `/api/analyses/{id}` | 查看分析结果(含按采样序号保存的核算依据 `samples`) |
| GET | `/api/analyses/{id}/reconciliation` | 按分析编号查询保存的对账结果(别名 `/api/reconciliations/{id}`) |
| POST/PUT | `/api/trajectories/{id}/odometer` | 按轨迹编号整组补交/更正读数(别名 `/api/odometer-readings/{id}`) |
| GET | `/api/trajectories/{id}/odometer` | 查看轨迹当前整组读数 |
| GET | `/health` | 健康检查 |

### 示例

```bash
# 1. 登记路网 v1
curl -X POST localhost:8000/api/networks -H 'Content-Type: application/json' -d '{
  "version": "v1",
  "segments": [
    {"segment_id": "S1", "start_lat": 39.900, "start_lon": 116.400,
     "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
    {"segment_id": "S2", "start_lat": 39.900, "start_lon": 116.401,
     "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0}
  ]
}'

# 2. 提交轨迹(时刻必须为带时区的 UTC ISO8601)
curl -X POST localhost:8000/api/trajectories -H 'Content-Type: application/json' -d '{
  "name": "trip-1",
  "points": [
    {"timestamp": "2026-01-01T08:00:00Z", "lat": 39.9000, "lon": 116.4000},
    {"timestamp": "2026-01-01T08:00:10Z", "lat": 39.9000, "lon": 116.4008},
    {"timestamp": "2026-01-01T08:00:20Z", "lat": 39.9008, "lon": 116.4010}
  ]
}'

# 3. 匹配(轨迹 1,路网版本 v1)
curl -X POST localhost:8000/api/match -H 'Content-Type: application/json' \
  -d '{"trajectory_id": 1, "network_version": "v1"}'
```

响应示例:

```json
{
  "analysis_id": 1,
  "trajectory_id": 1,
  "network_version": "v1",
  "total_points": 7,
  "matched_points": 6,
  "total_distance_m": 183.0,
  "path": [
    {"seq": 0, "segment_id": "S1", "enter_time": "2026-01-01T08:00:00.000Z",
     "leave_time": "2026-01-01T08:00:20.000Z", "distance_m": 85.0}
  ],
  "gaps": [
    {"seq": 0, "start_time": "2026-01-01T08:00:30.000Z",
     "end_time": "2026-01-01T08:00:50.000Z", "reason": "out_of_range"}
  ]
}
```

## 项目结构

```
app/
  main.py       FastAPI 路由与持久化
  matcher.py    HMM + Viterbi 匹配引擎(候选、有向图最短路、缺口检测、按采样序号核算依据)
  reconcile.py  里程对账计算与读写(核算依据、整组读数、对账结果)
  geo.py        球面距离、点到线段投影
  db.py         SQLite 连接与建表(含旧库自动补列)
  schemas.py    请求校验(Pydantic,含读数整组校验)
  util.py       UTC 时间处理
tests/test_api.py       端到端测试(连续路径、两类缺口、多候选全局最优、版本隔离)
tests/test_reconcile.py 对账测试(待补交、补交/更正、缺口与倒退、事务回滚、旧分析兼容)
Dockerfile / docker-compose.yml
```

## 配置

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `./data/app.db`(容器内 `/data/app.db`) | SQLite 数据库文件路径 |

匹配参数(`app/matcher.py` 顶部常量):吸附半径 50m、发射 σ 15m、转移衰减 β 30m、每点最大候选数 8。
