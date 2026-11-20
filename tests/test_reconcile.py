"""对账(车载里程表 vs 地图匹配里程)端到端测试。"""
import os
import sqlite3
import tempfile

os.environ.setdefault("DATABASE_PATH",
                      os.path.join(tempfile.mkdtemp(prefix="trajrec_"), "test.db"))

from fastapi.testclient import TestClient

from app import db
from app.main import app

db.init_db()
# 服务端异常向上抛出,便于断言事务回滚
client = TestClient(app, raise_server_exceptions=False)


# 路网:L 形,总里程 85 + 111 + 85 = 281 m
NET = {
    "version": "rc-v1",
    "segments": [
        {"segment_id": "S1", "start_lat": 39.900, "start_lon": 116.400,
         "end_lat": 39.900, "end_lon": 116.401, "length_m": 85.0},
        {"segment_id": "S2", "start_lat": 39.900, "start_lon": 116.401,
         "end_lat": 39.901, "end_lon": 116.401, "length_m": 111.0},
        {"segment_id": "S3", "start_lat": 39.901, "start_lon": 116.401,
         "end_lat": 39.901, "end_lon": 116.402, "length_m": 85.0},
    ],
}

# 7 个采样点,沿 S1 -> S2 -> S3,每 10s 一个
POINTS = [
    ("2026-02-01T08:00:00Z", 39.9000, 116.4000),  # seq 0
    ("2026-02-01T08:00:10Z", 39.9000, 116.4005),  # seq 1
    ("2026-02-01T08:00:20Z", 39.9000, 116.4010),  # seq 2
    ("2026-02-01T08:00:30Z", 39.9005, 116.4010),  # seq 3
    ("2026-02-01T08:00:40Z", 39.9010, 116.4010),  # seq 4
    ("2026-02-01T08:00:50Z", 39.9010, 116.4015),  # seq 5
    ("2026-02-01T08:01:00Z", 39.9010, 116.4020),  # seq 6
]


def _ensure_network():
    r = client.post("/api/networks", json=NET)
    assert r.status_code in (201, 409), r.text


def _traj(points, name=None):
    r = client.post("/api/trajectories", json={
        "name": name,
        "points": [{"timestamp": t, "lat": la, "lon": lo} for t, la, lo in points],
    })
    assert r.status_code == 201, r.text
    return r.json()["trajectory_id"]


def _match(tid):
    r = client.post("/api/match", json={"trajectory_id": tid,
                                        "network_version": "rc-v1"})
    assert r.status_code == 201, r.text
    return r.json()


def _readings(**seq_to_m):
    return {"readings": [{"seq": int(s), "odometer_m": v}
                         for s, v in seq_to_m.items()]}


def _recon(aid):
    r = client.get(f"/api/analyses/{aid}/reconciliation")
    assert r.status_code == 200, r.text
    return r.json()


# ------------------------------------------------------------------ 基本流程
def test_new_analysis_pending_without_readings_then_reconciles():
    _ensure_network()
    tid = _traj(POINTS, "recon-basic")
    aid = _match(tid)["analysis_id"]

    # 新分析尚无读数 -> 待补交
    rec = _recon(aid)
    assert rec["status"] == "pending_readings"
    assert rec["segments"] == []
    assert "待补交" in rec["message"]
    od = client.get(f"/api/trajectories/{tid}/odometer").json()
    assert od["status"] == "pending_readings" and od["readings"] == []

    # 补交读数:seq 0 / 2 / 4 / 6
    r = client.post(f"/api/trajectories/{tid}/odometer",
                    json=_readings(**{"0": 1000.0, "2": 1085.0,
                                      "4": 1196.0, "6": 1281.0}))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 1
    assert [a["analysis_id"] for a in body["affected_analyses"]] == [aid]

    rec = _recon(aid)
    assert rec["status"] == "ok"
    assert rec["readings_revision"] == 1
    assert rec["segment_count"] == 3
    assert rec["checked_count"] == 3
    assert rec["unverifiable_count"] == 0
    segs = rec["segments"]
    assert [(s["start_seq"], s["end_seq"]) for s in segs] == [(0, 2), (2, 4), (4, 6)]
    # 匹配里程 85 / 111 / 85,表计增量相同,差值 0
    for s, expected in zip(segs, (85.0, 111.0, 85.0)):
        assert s["status"] == "ok"
        assert abs(s["matched_distance_m"] - expected) < 1.0
        assert abs(s["odometer_delta_m"] - expected) < 1e-6
        assert abs(s["difference_m"] - (s["matched_distance_m"] - s["odometer_delta_m"])) < 1e-6
    assert abs(rec["matched_distance_m"] - 281.0) < 2.0
    assert abs(rec["odometer_delta_m"] - 281.0) < 1e-6
    assert abs(rec["difference_m"] - (rec["matched_distance_m"]
                                      - rec["odometer_delta_m"])) < 1e-6

    # 查询只读保存结果:不重跑匹配(分析路径仍为保存值)
    detail = client.get(f"/api/analyses/{aid}").json()
    assert [p["segment_id"] for p in detail["path"]] == ["S1", "S2", "S3"]
    assert len(detail["samples"]) == 7
    assert all(s["matched"] for s in detail["samples"])
    assert detail["samples"][0]["cumulative_matched_m"] == 0.0
    assert abs(detail["samples"][-1]["cumulative_matched_m"] - detail["total_distance_m"]) < 1e-6


def test_correction_replaces_group_and_updates_all_analyses():
    _ensure_network()
    tid = _traj(POINTS, "recon-correct")
    a1 = _match(tid)["analysis_id"]
    a2 = _match(tid)["analysis_id"]

    client.post(f"/api/trajectories/{tid}/odometer",
                json=_readings(**{"0": 100.0, "6": 400.0}))
    for aid in (a1, a2):
        rec = _recon(aid)
        assert rec["status"] == "ok"
        assert abs(rec["odometer_delta_m"] - 300.0) < 1e-6

    # 整组更正(PUT 同义):整组替换,不影响匹配结果
    r = client.put(f"/api/trajectories/{tid}/odometer",
                   json=_readings(**{"0": 100.0, "3": 250.0, "6": 400.0}))
    assert r.status_code == 200 and r.json()["revision"] == 2
    for aid in (a1, a2):
        rec = _recon(aid)
        assert rec["readings_revision"] == 2
        assert rec["segment_count"] == 2
        assert abs(rec["odometer_delta_m"] - 300.0) < 1e-6
        assert client.get(f"/api/analyses/{aid}").json()["path"] == \
            client.get(f"/api/analyses/{aid}").json()["path"]

    cur = client.get(f"/api/trajectories/{tid}/odometer").json()
    assert cur["revision"] == 2
    assert [r["seq"] for r in cur["readings"]] == [0, 3, 6]


def test_gap_range_is_unverifiable_and_others_still_checked():
    _ensure_network()
    pts = [
        ("2026-02-02T08:00:00Z", 39.9000, 116.4000),  # 0 匹配
        ("2026-02-02T08:00:10Z", 39.9000, 116.4008),  # 1 匹配
        ("2026-02-02T08:00:20Z", 39.9050, 116.4000),  # 2 超距缺口
        ("2026-02-02T08:00:30Z", 39.9010, 116.4012),  # 3 匹配
        ("2026-02-02T08:00:40Z", 39.9010, 116.4018),  # 4 匹配
    ]
    tid = _traj(pts, "recon-gap")
    aid = _match(tid)["analysis_id"]
    client.post(f"/api/trajectories/{tid}/odometer",
                json=_readings(**{"0": 10.0, "1": 60.0, "2": 150.0,
                                  "3": 240.0, "4": 300.0}))
    rec = _recon(aid)
    segs = rec["segments"]
    assert [(s["start_seq"], s["end_seq"]) for s in segs] == [
        (0, 1), (1, 2), (2, 3), (3, 4)]
    ok = segs[0]
    assert ok["status"] == "ok"
    # 1->2 与 2->3 均跨缺口:不可核对,附原因,且不给出三个数值
    for s in segs[1:3]:
        assert s["status"] == "unverifiable"
        assert s["reason"] == "match_gap"
        assert "out_of_range" in s["gap_reasons"]
        assert s["matched_distance_m"] is None
        assert s["difference_m"] is None
    assert segs[3]["status"] == "ok"
    assert rec["checked_count"] == 2
    assert rec["unverifiable_count"] == 2
    # 汇总只累计可核对段:(0,1) 50 + (3,4) 60
    assert abs(rec["odometer_delta_m"] - 110.0) < 1e-6


def test_reading_regression_is_unverifiable():
    _ensure_network()
    tid = _traj(POINTS, "recon-regress")
    aid = _match(tid)["analysis_id"]
    client.post(f"/api/trajectories/{tid}/odometer",
                json=_readings(**{"0": 500.0, "3": 400.0, "6": 900.0}))
    segs = _recon(aid)["segments"]
    assert segs[0]["status"] == "unverifiable"
    assert segs[0]["reason"] == "reading_regression"
    assert segs[1]["status"] == "ok"
    assert segs[1]["difference_m"] is not None


def test_validation_and_transactionality():
    _ensure_network()
    tid = _traj(POINTS, "recon-validation")
    aid = _match(tid)["analysis_id"]
    client.post(f"/api/trajectories/{tid}/odometer",
                json=_readings(**{"0": 1000.0, "6": 1281.0}))
    before = _recon(aid)

    # 至少两条
    r = client.post(f"/api/trajectories/{tid}/odometer",
                    json={"readings": [{"seq": 0, "odometer_m": 1.0}]})
    assert r.status_code == 422
    # 采样序号不可重复
    r = client.post(f"/api/trajectories/{tid}/odometer",
                    json={"readings": [{"seq": 1, "odometer_m": 1.0},
                                       {"seq": 1, "odometer_m": 2.0}]})
    assert r.status_code == 422
    # 累计米数非负
    r = client.post(f"/api/trajectories/{tid}/odometer",
                    json={"readings": [{"seq": 0, "odometer_m": -1.0},
                                       {"seq": 6, "odometer_m": 2.0}]})
    assert r.status_code == 422
    # 采样序号超出轨迹范围
    r = client.post(f"/api/trajectories/{tid}/odometer",
                    json={"readings": [{"seq": 0, "odometer_m": 1.0},
                                       {"seq": 99, "odometer_m": 2.0}]})
    assert r.status_code == 422
    # 不存在的轨迹
    r = client.post("/api/trajectories/99999/odometer",
                    json=_readings(**{"0": 0.0, "1": 1.0}))
    assert r.status_code == 404

    # 全部失败后原读数与对账结果保持不变
    after = _recon(aid)
    assert after == before
    cur = client.get(f"/api/trajectories/{tid}/odometer").json()
    assert cur["revision"] == 1
    assert [r["odometer_m"] for r in cur["readings"]] == [1000.0, 1281.0]


def test_legacy_analysis_without_basis_reports_needs_rematch():
    _ensure_network()
    tid = _traj(POINTS, "recon-legacy")
    aid = _match(tid)["analysis_id"]
    # 模拟升级前的旧分析:删除核算依据与对账结果
    conn = sqlite3.connect(db.db_path())
    try:
        conn.execute("DELETE FROM analysis_samples WHERE analysis_id = ?", (aid,))
        conn.execute("DELETE FROM reconciliations WHERE analysis_id = ?", (aid,))
        conn.execute("UPDATE analysis_gaps SET start_seq = NULL, end_seq = NULL "
                     "WHERE analysis_id = ?", (aid,))
        conn.commit()
    finally:
        conn.close()

    # 原报告照常可查
    detail = client.get(f"/api/analyses/{aid}")
    assert detail.status_code == 200
    assert [p["segment_id"] for p in detail.json()["path"]] == ["S1", "S2", "S3"]

    # 对账提示重新匹配
    rec = _recon(aid)
    assert rec["status"] == "needs_rematch"
    assert "重新匹配" in rec["message"]

    # 重新匹配后生成依据,可正常对账
    aid2 = _match(tid)["analysis_id"]
    client.post(f"/api/trajectories/{tid}/odometer",
                json=_readings(**{"0": 0.0, "6": 281.0}))
    assert _recon(aid)["status"] == "needs_rematch"       # 旧分析仍提示
    rec2 = _recon(aid2)
    assert rec2["status"] == "ok"
    assert abs(rec2["difference_m"]) < 2.0


def test_readings_applied_to_later_analysis_and_interleaving():
    """读数先于匹配 / 匹配晚于读数,对账都对应当前整组读数。"""
    _ensure_network()
    tid = _traj(POINTS, "recon-interleave")
    # 先提交读数
    client.post(f"/api/trajectories/{tid}/odometer",
                json=_readings(**{"0": 50.0, "3": 200.0, "6": 350.0}))
    # 再匹配:新分析直接带上对账结果
    aid = _match(tid)["analysis_id"]
    rec = _recon(aid)
    assert rec["status"] == "ok"
    assert rec["readings_revision"] == 1
    assert rec["segment_count"] == 2

    # 分析后再次更正读数:所有相关分析一起更新
    client.post(f"/api/trajectories/{tid}/odometer",
                json=_readings(**{"0": 0.0, "6": 281.0}))
    rec = _recon(aid)
    assert rec["readings_revision"] == 2
    assert rec["segment_count"] == 1


def test_readings_update_during_match_uses_current_group(monkeypatch):
    """匹配计算完成前读数更新落库:新分析的对账须对应当前整组读数。"""
    from app import main as main_module
    from app import matcher
    from app.schemas import OdometerReadingIn, OdometerSubmit

    _ensure_network()
    tid = _traj(POINTS, "recon-interleave-strict")
    orig_match = matcher.NetworkIndex.match
    fired = {"done": False}

    def match_then_submit(self, points):
        result = orig_match(self, points)
        if not fired["done"]:
            fired["done"] = True
            # 模拟另一个客户端在匹配落库前完成读数更新
            main_module._submit_odometer(tid, OdometerSubmit(readings=[
                OdometerReadingIn(seq=0, odometer_m=0.0),
                OdometerReadingIn(seq=6, odometer_m=281.0),
            ]))
        return result

    monkeypatch.setattr(matcher.NetworkIndex, "match", match_then_submit)
    try:
        aid = _match(tid)["analysis_id"]
    finally:
        monkeypatch.undo()

    rec = _recon(aid)
    assert rec["status"] == "ok"
    assert rec["readings_revision"] == 1
    assert abs(rec["difference_m"]) < 2.0


def test_disconnected_gap_between_matched_points_is_unverifiable():
    """相邻两个已匹配采样点之间若有 disconnected 缺口,该段也不可核对。"""
    split = {
        "version": "rc-split",
        "segments": [
            {"segment_id": "X1", "start_lat": 39.700, "start_lon": 116.300,
             "end_lat": 39.700, "end_lon": 116.301, "length_m": 85.0},
            {"segment_id": "X2", "start_lat": 39.710, "start_lon": 116.310,
             "end_lat": 39.710, "end_lon": 116.311, "length_m": 85.0},
        ],
    }
    assert client.post("/api/networks", json=split).status_code in (201, 409)
    pts = [
        ("2026-02-03T08:00:00Z", 39.7000, 116.3002),
        ("2026-02-03T08:00:10Z", 39.7000, 116.3008),
        ("2026-02-03T08:00:20Z", 39.7100, 116.3102),
        ("2026-02-03T08:00:30Z", 39.7100, 116.3108),
    ]
    tid = _traj(pts, "recon-disconnected")
    r = client.post("/api/match", json={"trajectory_id": tid,
                                        "network_version": "rc-split"})
    assert r.status_code == 201, r.text
    aid = r.json()["analysis_id"]
    # 两端点均匹配,缺口为 disconnected
    assert r.json()["gaps"][0]["reason"] == "disconnected"
    client.post(f"/api/trajectories/{tid}/odometer",
                json=_readings(**{"0": 0.0, "1": 85.0, "2": 200.0, "3": 285.0}))
    segs = _recon(aid)["segments"]
    assert [s["status"] for s in segs] == ["ok", "unverifiable", "ok"]
    assert segs[1]["reason"] == "match_gap"
    assert segs[1]["gap_reasons"] == ["disconnected"]
    assert segs[1]["matched_distance_m"] is None


def test_reconciliation_of_missing_analysis_is_404():
    assert client.get("/api/analyses/99999/reconciliation").status_code == 404


def test_save_failure_keeps_original_data_and_result(monkeypatch):
    """对账结果保存失败时,读数与分析结果一并回滚。"""
    from app import reconcile

    _ensure_network()
    tid = _traj(POINTS, "recon-rollback")
    aid = _match(tid)["analysis_id"]
    client.post(f"/api/trajectories/{tid}/odometer",
                json=_readings(**{"0": 1000.0, "6": 1281.0}))
    before_rec = _recon(aid)
    before_readings = client.get(f"/api/trajectories/{tid}/odometer").json()

    def boom(conn, computed):
        raise RuntimeError("disk full")

    monkeypatch.setattr(reconcile, "save_reconciliation", boom)
    r = client.post(f"/api/trajectories/{tid}/odometer",
                    json=_readings(**{"0": 0.0, "3": 100.0, "6": 200.0}))
    assert r.status_code == 500
    monkeypatch.undo()

    assert _recon(aid) == before_rec
    assert client.get(f"/api/trajectories/{tid}/odometer").json() == before_readings


def test_new_analysis_and_reconciliation_take_effect_together(monkeypatch):
    """新分析落库失败(含对账)时,不会留下半份分析。"""
    from app import reconcile

    _ensure_network()
    tid = _traj(POINTS, "recon-atomic-match")
    before = client.get("/api/analyses", params={"trajectory_id": tid}).json()

    def boom(conn, computed):
        raise RuntimeError("disk full")

    monkeypatch.setattr(reconcile, "save_reconciliation", boom)
    r = client.post("/api/match", json={"trajectory_id": tid,
                                        "network_version": "rc-v1"})
    assert r.status_code == 500
    monkeypatch.undo()

    after = client.get("/api/analyses", params={"trajectory_id": tid}).json()
    assert after == before
