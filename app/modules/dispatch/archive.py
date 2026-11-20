from datetime import datetime, timezone
from time import perf_counter

from app.config import RULE_VERSION
from app.errors import DomainError
from app.manifest import manifest
from app.pipeline import process
from app.store import fetch, publication, record_publication, save

GATE_REASONS = {
    "KEY_TEARDOWN_PENDING": "关键拆景未落位",
    "PENDING_JOBS": "工序未落位",
    "RESOURCE_COLLISION": "资源碰撞未清",
    "DEPENDENCY_VIOLATION": "前置工序违序",
    "RECONCILE_MISMATCH": "占用对账不符",
}


def present(record):
    return {**record, "publication": publication(record["id"])}


def create(source, label):
    started = perf_counter()
    digest = manifest(source)
    run_id = digest["digest"]
    try:
        _, record = fetch(run_id)
    except DomainError as exc:
        if exc.code != "RUN_NOT_FOUND":
            raise
    else:
        return present({**record, "reused": True})
    result = process(source)
    saved = save(source, {"id": run_id, "manifest": digest, "sample": label,
                          "elapsed_ms": round((perf_counter() - started) * 1000, 2), "result": result})
    return present(saved)


def rerun(run_id):
    source, original = fetch(run_id)
    if manifest(source) != original["manifest"]:
        raise DomainError("RUN_VERSION_CHANGED", "原批次与当前规则版本不一致", 409)
    result = process(source)
    if result != original["result"]:
        raise DomainError("RUN_DRIFT", "复算结果与原始快照不一致", 409)
    return present({**original, "reused": True})


def release(run_id):
    _, record = fetch(run_id)
    gate = record["result"]["dispatch"]["release"]
    if not gate["publishable"]:
        detail = "；".join(f'{GATE_REASONS[block["code"]]}：{"、".join(block["related"])}'
                          for block in gate["blocks"])
        raise DomainError("RELEASE_BLOCKED", f"未达发布条件（{detail}）", 409)
    entry = {"published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "rule_version": RULE_VERSION, "digest": record["manifest"]["digest"]}
    _, inserted = record_publication(run_id, entry)
    return present({**record, "reused": not inserted})
