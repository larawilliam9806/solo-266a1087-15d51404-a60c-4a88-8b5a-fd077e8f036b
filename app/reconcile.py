"""车载里程表与地图匹配里程的对账。

设计要点:
- 地图匹配完成时,按原始轨迹采样序号保存核算依据(analysis_samples):
  每个采样点是否匹配、匹配到的累计里程、未匹配时的缺口原因。
- 里程表读数按轨迹整组补交/更正;对账只读取保存的核算依据与当前整组读数,
  不重跑地图匹配。
- 对账结果随「新分析」或「整组读数更新」在同一事务内生成;
  查询只读取保存结果。
- 相邻两条读数之间为一段:若区间内存在匹配缺口,或读数倒退,则该段不可核对;
  其余段给出 表计增量、匹配里程 与 差值(匹配里程 - 表计增量)。
"""
from __future__ import annotations

import json
import sqlite3

STATUS_OK = "ok"
STATUS_PENDING = "pending_readings"

SEG_OK = "ok"
SEG_UNVERIFIABLE = "unverifiable"

REASON_MATCH_GAP = "match_gap"
REASON_REGRESSION = "reading_regression"

PENDING_MESSAGE = "待补交:该轨迹尚无里程表读数,请按轨迹编号补交至少两条读数"
LEGACY_MESSAGE = "分析 {analysis_id} 缺少按采样序号保存的核算依据,请重新匹配后再对账"


# ------------------------------------------------------------------ 读取
def load_basis(conn: sqlite3.Connection, analysis_id: int) -> dict | None:
    """读取分析的核算依据。

    旧分析没有 analysis_samples,或缺口缺少采样序号时返回 None
    (旧分析不参与对账,需重新匹配生成依据)。
    """
    analysis = conn.execute(
        "SELECT total_points FROM analyses WHERE id = ?", (analysis_id,)
    ).fetchone()
    if analysis is None:
        return None
    rows = conn.execute(
        "SELECT seq, matched, cumulative_matched_m, gap_reason FROM analysis_samples "
        "WHERE analysis_id = ? ORDER BY seq", (analysis_id,)
    ).fetchall()
    total = analysis["total_points"]
    if len(rows) != total or [r["seq"] for r in rows] != list(range(total)):
        return None

    gap_rows = conn.execute(
        "SELECT seq, start_seq, end_seq, reason FROM analysis_gaps "
        "WHERE analysis_id = ? ORDER BY seq", (analysis_id,)
    ).fetchall()
    gaps = []
    for g in gap_rows:
        # 旧分析缺口无采样序号:无缺口行且每点都有依据视为完整;
        # 有缺口行却缺序号则依据不完整。
        if g["start_seq"] is None or g["end_seq"] is None:
            return None
        gaps.append({"start_seq": g["start_seq"], "end_seq": g["end_seq"],
                     "reason": g["reason"]})

    samples = {
        r["seq"]: {
            "matched": bool(r["matched"]),
            "cumulative_matched_m": r["cumulative_matched_m"],
            "gap_reason": r["gap_reason"],
        }
        for r in rows
    }
    return {"samples": samples, "gaps": gaps, "total_samples": total}


def load_readings(conn: sqlite3.Connection, trajectory_id: int):
    """返回 (group_row, readings) ;尚未补交则 group_row 为 None。"""
    group = conn.execute(
        "SELECT trajectory_id, revision, created_at, updated_at FROM odometer_groups "
        "WHERE trajectory_id = ?", (trajectory_id,)
    ).fetchone()
    if group is None:
        return None, []
    rows = conn.execute(
        "SELECT seq, odometer_m FROM odometer_readings WHERE trajectory_id = ? ORDER BY seq",
        (trajectory_id,),
    ).fetchall()
    return group, [dict(r) for r in rows]


# ------------------------------------------------------------------ 计算
NOTE_MATCH_GAP = "区间内存在匹配缺口,无法按地图匹配里程核对"
NOTE_REGRESSION = "读数倒退,无法核对"


def _segment_note(reasons: list[str], gap_reasons: list[str]) -> str | None:
    if not reasons:
        return None
    notes: list[str] = []
    if REASON_MATCH_GAP in reasons:
        detail = "、".join(gap_reasons) if gap_reasons else "无匹配"
        notes.append(f"{NOTE_MATCH_GAP}({detail})")
    if REASON_REGRESSION in reasons:
        notes.append(NOTE_REGRESSION)
    return ";".join(notes)


def compute_reconciliation(analysis_id: int, trajectory_id: int,
                           basis: dict | None, readings: list[dict] | None,
                           revision: int | None, now: str) -> dict:
    """按核算依据 + 当前整组读数计算对账结果(纯函数,不写库)。"""
    if not readings:
        return {
            "analysis_id": analysis_id,
            "trajectory_id": trajectory_id,
            "status": STATUS_PENDING,
            "readings_revision": None,
            "computed_at": now,
            "message": PENDING_MESSAGE,
            "segment_count": 0,
            "checked_count": 0,
            "unverifiable_count": 0,
            "odometer_delta_m": 0.0,
            "matched_distance_m": 0.0,
            "difference_m": 0.0,
            "segments": [],
        }

    samples = (basis or {}).get("samples", {})
    gaps = (basis or {}).get("gaps", [])
    segments: list[dict] = []

    for k in range(len(readings) - 1):
        a, b = readings[k], readings[k + 1]
        start, end = a["seq"], b["seq"]
        delta = b["odometer_m"] - a["odometer_m"]

        reasons: list[str] = []
        gap_reasons: list[str] = []

        def mark_gap(reason: str | None, _reasons=reasons,
                     _gap_reasons=gap_reasons) -> None:
            if REASON_MATCH_GAP not in _reasons:
                _reasons.append(REASON_MATCH_GAP)
            if reason and reason not in _gap_reasons:
                _gap_reasons.append(reason)

        # 端点本身未匹配
        for seq in (start, end):
            sm = samples.get(seq)
            if sm is None or not sm["matched"]:
                mark_gap(None if sm is None else sm.get("gap_reason"))

        # 区间内未匹配的采样点
        for seq in range(start + 1, end):
            sm = samples.get(seq)
            if sm is None or not sm["matched"]:
                mark_gap(None if sm is None else sm.get("gap_reason"))

        # 区间内的匹配缺口(含相邻两个已匹配点之间的 disconnected 缺口)
        for g in gaps:
            if g["start_seq"] >= start and g["end_seq"] <= end:
                mark_gap(g["reason"])

        if delta < 0 and REASON_REGRESSION not in reasons:
            reasons.append(REASON_REGRESSION)

        seg = {
            "seq": k,
            "start_seq": start,
            "end_seq": end,
            "status": SEG_UNVERIFIABLE if reasons else SEG_OK,
            "verifiable": not reasons,
            "reason": reasons[0] if reasons else None,
            "reasons": reasons,
            "gap_reasons": gap_reasons,
            "note": _segment_note(reasons, gap_reasons),
            # 仅可核对段给出三个数值;不可核对段留空并注明原因
            "odometer_delta_m": None,
            "matched_distance_m": None,
            "difference_m": None,
        }
        if not reasons:
            sa, sb = samples[start], samples[end]
            matched = sb["cumulative_matched_m"] - sa["cumulative_matched_m"]
            seg["odometer_delta_m"] = round(delta, 3)
            seg["matched_distance_m"] = round(matched, 3)
            seg["difference_m"] = round(matched - delta, 3)
        segments.append(seg)

    checked = [s for s in segments if s["status"] == SEG_OK]
    odo_total = round(sum(s["odometer_delta_m"] for s in checked), 3)
    matched_total = round(sum(s["matched_distance_m"] for s in checked), 3)
    return {
        "analysis_id": analysis_id,
        "trajectory_id": trajectory_id,
        "status": STATUS_OK,
        "readings_revision": revision,
        "computed_at": now,
        "message": None,
        "segment_count": len(segments),
        "checked_count": len(checked),
        "unverifiable_count": len(segments) - len(checked),
        "odometer_delta_m": odo_total,
        "matched_distance_m": matched_total,
        "difference_m": round(matched_total - odo_total, 3),
        "segments": segments,
    }


# ------------------------------------------------------------------ 保存
def save_reconciliation(conn: sqlite3.Connection, computed: dict) -> None:
    """写入(覆盖)某分析的对账结果。调用方负责外层事务。"""
    aid = computed["analysis_id"]
    conn.execute(
        "DELETE FROM reconciliation_segments WHERE reconciliation_id IN "
        "(SELECT id FROM reconciliations WHERE analysis_id = ?)", (aid,)
    )
    conn.execute("DELETE FROM reconciliations WHERE analysis_id = ?", (aid,))
    cur = conn.execute(
        "INSERT INTO reconciliations (analysis_id, status, readings_revision, computed_at, "
        "segment_count, checked_count, unverifiable_count, odometer_delta_m, "
        "matched_distance_m, difference_m) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (aid, computed["status"], computed.get("readings_revision"),
         computed["computed_at"], computed["segment_count"], computed["checked_count"],
         computed["unverifiable_count"], computed["odometer_delta_m"],
         computed["matched_distance_m"], computed["difference_m"]),
    )
    rid = cur.lastrowid
    conn.executemany(
        "INSERT INTO reconciliation_segments (reconciliation_id, seq, start_seq, end_seq, "
        "status, reason, reasons, gap_reasons, odometer_delta_m, matched_distance_m, "
        "difference_m) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(rid, s["seq"], s["start_seq"], s["end_seq"], s["status"], s["reason"],
          json.dumps(s["reasons"], ensure_ascii=False),
          json.dumps(s["gap_reasons"], ensure_ascii=False),
          s["odometer_delta_m"], s["matched_distance_m"], s["difference_m"])
         for s in computed["segments"]],
    )


def fetch_reconciliation(conn: sqlite3.Connection, analysis_id: int) -> dict | None:
    """读取保存的对账结果;不存在返回 None。"""
    row = conn.execute(
        "SELECT * FROM reconciliations WHERE analysis_id = ?", (analysis_id,)
    ).fetchone()
    if row is None:
        return None
    seg_rows = conn.execute(
        "SELECT seq, start_seq, end_seq, status, reason, reasons, gap_reasons, "
        "odometer_delta_m, matched_distance_m, difference_m "
        "FROM reconciliation_segments WHERE reconciliation_id = ? ORDER BY seq",
        (row["id"],),
    ).fetchall()
    segments = []
    for s in seg_rows:
        d = dict(s)
        d["reasons"] = json.loads(d["reasons"] or "[]")
        d["gap_reasons"] = json.loads(d["gap_reasons"] or "[]")
        d["verifiable"] = d["status"] == SEG_OK
        d["note"] = _segment_note(d["reasons"], d["gap_reasons"])
        segments.append(d)
    return {
        "analysis_id": analysis_id,
        "status": row["status"],
        "message": PENDING_MESSAGE if row["status"] == STATUS_PENDING else None,
        "readings_revision": row["readings_revision"],
        "computed_at": row["computed_at"],
        "segment_count": row["segment_count"],
        "checked_count": row["checked_count"],
        "unverifiable_count": row["unverifiable_count"],
        "odometer_delta_m": row["odometer_delta_m"],
        "matched_distance_m": row["matched_distance_m"],
        "difference_m": row["difference_m"],
        "segments": segments,
    }


def recompute_for_trajectory(conn: sqlite3.Connection, trajectory_id: int,
                             now: str) -> tuple[list[dict], list[int]]:
    """按轨迹当前整组读数重算所有具备依据的分析(调用方负责事务)。

    返回 (已更新分析列表, 因缺少依据而跳过的分析编号列表)。
    """
    group, readings = load_readings(conn, trajectory_id)
    revision = group["revision"] if group else None
    updated: list[dict] = []
    skipped: list[int] = []
    analyses = conn.execute(
        "SELECT id FROM analyses WHERE trajectory_id = ? ORDER BY id", (trajectory_id,)
    ).fetchall()
    for a in analyses:
        aid = a["id"]
        basis = load_basis(conn, aid)
        if basis is None:
            skipped.append(aid)
            continue
        computed = compute_reconciliation(
            aid, trajectory_id, basis, readings if group else None, revision, now
        )
        save_reconciliation(conn, computed)
        updated.append(computed)
    return updated, skipped
