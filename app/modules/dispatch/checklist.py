from collections import defaultdict


def _segments(steps):
    groups = []
    for step in steps:
        if groups and groups[-1][0]["show"] == step["show"]:
            groups[-1].append(step)
        else:
            groups.append([step])
    return groups


def _changeovers(stages):
    rows = []
    for stage in stages:
        segments = _segments(stage["steps"])
        for front, back in zip(segments, segments[1:]):
            last, first = front[-1], back[0]
            clear_end = max(step["stage_end"] for step in front)
            rows.append({
                "stage": stage["stage"],
                "from_show": last["show"], "to_show": first["show"],
                "last": {k: last[k] for k in ("id", "title", "tech", "technician", "stage_end")},
                "next": {k: first[k] for k in ("id", "title", "tech", "technician", "start")},
                "clear_end": clear_end, "next_start": first["start"],
                "gap": first["start"] - clear_end,
                "continuous": last["tech"] == first["tech"],
            })
    return rows


def release_gate(schedule, reconcile):
    needed = {parent for job in schedule["jobs"] for parent in job["needs"]}
    pending = [job for job in schedule["jobs"] if job["status"] == "pending"]
    blocks = []
    critical = sorted(job["id"] for job in pending if job["id"] in needed)
    if critical:
        blocks.append({"code": "KEY_TEARDOWN_PENDING", "related": critical})
    others = sorted(job["id"] for job in pending if job["id"] not in needed)
    if others:
        blocks.append({"code": "PENDING_JOBS", "related": others})
    if reconcile["collisions"]:
        involved = sorted({job_id for row in reconcile["collisions"] for job_id in row[:2]})
        blocks.append({"code": "RESOURCE_COLLISION", "related": involved})
    if reconcile["premature"]:
        involved = sorted({job_id for row in reconcile["premature"] for job_id in row})
        blocks.append({"code": "DEPENDENCY_VIOLATION", "related": involved})
    if reconcile["row_gap"] or reconcile["missing"]:
        blocks.append({"code": "RECONCILE_MISMATCH", "related": reconcile["missing"]})
    return {"publishable": not blocks, "blocks": blocks}


def build(schedule, calendar, reconcile):
    groups = defaultdict(list)
    deferred = []
    for job in schedule["jobs"]:
        if job["status"] != "scheduled":
            deferred.append({k: job[k] for k in ("id", "title", "line", "status", "code", "related")})
            continue
        groups[job["stage"]].append({
            **{k: job[k] for k in ("id", "show", "title", "line", "seq", "start", "end", "stage_end", "tech", "needs")},
            "technician": calendar["technicians"][job["tech"]]["name"],
        })
    stages = [{"stage": stage, "steps": sorted(rows, key=lambda row: (row["start"], row["id"]))}
              for stage, rows in sorted(groups.items())]
    return {"date": calendar["date"], "stages": stages, "changeovers": _changeovers(stages),
            "deferred": deferred, "release": release_gate(schedule, reconcile)}
