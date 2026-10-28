"""从相邻页面快照生成幂等的竞品变化事件。"""
from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.core import db
from app.core.change_classifier import ChangeClassification, classify_change
from app.core.change_detector import detect_change
from app.core.snapshot_store import read_snapshot


Classifier = Callable[[str, str, str, Any], ChangeClassification]


def process_snapshot_change(
    current_snapshot_id: str,
    *,
    root: Optional[Path] = None,
    classifier: Classifier = classify_change,
) -> Dict[str, Any]:
    current = db.get_page_snapshot(current_snapshot_id)
    if not current:
        raise ValueError("current snapshot does not exist")
    previous_snapshot_id = current.get("previous_snapshot_id") or ""
    if not previous_snapshot_id:
        return {"status": "baseline", "change": None, "llm_called": False}
    previous = db.get_page_snapshot(previous_snapshot_id)
    if not previous:
        raise ValueError("previous snapshot does not exist")
    if previous["target_id"] != current["target_id"]:
        raise ValueError("snapshot chain crosses subscription targets")

    existing = db.get_competitor_change_for_snapshots(
        current["target_id"], previous_snapshot_id, current_snapshot_id
    )
    if existing:
        return {"status": "existing", "change": existing, "llm_called": False}

    before = read_snapshot(previous["snapshot_path"], root=root)
    after = read_snapshot(current["snapshot_path"], root=root)
    detection = detect_change(before, after)
    if not detection.changed:
        return {"status": "unchanged", "change": None, "llm_called": False}

    target = db.get_subscription_target(current["target_id"])
    if not target:
        raise ValueError("subscription target does not exist")
    classification = classifier(
        target["brand"], target["target_type"], current["source_url"], detection
    )
    summary = classification.summary
    if classification.facts:
        summary += "\n" + "\n".join(f"- {fact}" for fact in classification.facts)

    change_id = "chg_" + uuid.uuid4().hex[:12]
    change = db.create_competitor_change(
        change_id,
        target["sub_id"],
        current["run_id"],
        current["target_id"],
        previous_snapshot_id,
        current_snapshot_id,
        change_type=classification.change_type,
        severity=classification.severity,
        material=classification.material,
        change_ratio=detection.change_ratio,
        diff_summary=summary,
        before_excerpt=detection.before_excerpt,
        after_excerpt=detection.after_excerpt,
        llm_status=classification.llm_status,
    )
    inserted = change.get("change_id") == change_id
    db.increment_monitor_run_metrics(
        current["run_id"],
        llm_tokens=classification.llm_tokens,
        changed_count=1 if inserted and classification.material else 0,
    )
    return {
        "status": "created" if inserted else "existing",
        "change": change,
        "detection": detection.to_dict(),
        "classification": classification.to_dict(),
        "llm_called": detection.material,
    }
