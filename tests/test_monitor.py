from __future__ import annotations

import ast
import copy
import hashlib
import json
from pathlib import Path

import pytest

import tax_radar_au.monitor as monitor_module
from tax_radar_au.errors import MonitorError
from tax_radar_au.monitor import _https_url, _iso_date, _iso_timestamp, _load_observation, compare, render_markdown, validate_review, write_queue
from tax_radar_au.util import SourceSnapshot, sample_path, sha256_json


ROOT = Path(__file__).resolve().parents[1]


def _queue():
    return compare(
        baseline_path=sample_path("baseline", "sample-sources.json"),
        observation_path=sample_path("observations", "sample-register-observation.json"),
        mapping_path=sample_path("mappings", "sample-source-skill-map.json"),
    )


def _expected_queue_digest(queue: dict) -> str:
    payload = {key: value for key, value in queue.items() if key != "queue_digest"}
    return "sha256:" + sha256_json(payload)


def _matching_decision(queue: dict) -> dict:
    open_item = next(item for item in queue["items"] if item["state"] == "OPEN")
    return {
        "schema_version": "au-tax-technical-review.v2",
        "run_id": queue["run_id"],
        "queue_digest": queue["queue_digest"],
        "reviewer_ref": "demo-tax-reviewer",
        "reviewed_at": "2026-08-09T00:00:00Z",
        "decisions": [
            {
                "item_id": open_item["item_id"],
                "decision": "ESCALATE_TECHNICAL_REVIEW",
                "rationale": "Synthetic fixture requires technical-tax assessment.",
                "evidence_note": "Reviewed the fabricated source metadata and fixture link.",
            }
        ],
    }


def _matching_decision_for_all_open_items(queue: dict) -> dict:
    decision = _matching_decision(queue)
    template = decision["decisions"][0]
    decision["decisions"] = [
        {**template, "item_id": item["item_id"]}
        for item in queue["items"]
        if item["state"] == "OPEN"
    ]
    assert decision["decisions"]
    return decision


def _validate_payloads(
    tmp_path: Path, queue: dict, decision: dict
) -> dict:
    queue_path = tmp_path / "impact-queue.json"
    decision_path = tmp_path / "technical-review.json"
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    decision_path.write_text(json.dumps(decision), encoding="utf-8")
    return validate_review(queue_path=queue_path, decision_path=decision_path)


def _json_with_conflicting_duplicate(
    payload: dict, *, target: dict, key: str, conflicting_value: object
) -> str:
    """Serialise a payload while retaining a conflicting first JSON member."""
    encoded = json.dumps(payload)
    original = f"{json.dumps(key)}: {json.dumps(target[key])}"
    assert encoded.count(original) == 1
    duplicate = f"{json.dumps(key)}: {json.dumps(conflicting_value)}, {original}"
    return encoded.replace(original, duplicate, 1)


def _payload(kind: str, name: str) -> dict:
    return json.loads(sample_path(kind, name).read_text(encoding="utf-8"))


def _compare_fixtures(tmp_path: Path, **mutated: dict) -> dict:
    """Run compare() over the shipped samples with any of the three replaced.

    Every classification rule below needs a fixture the shipped demo does not
    contain, so each case starts from the real samples and changes one thing.
    """
    defaults = {
        "baseline": ("baseline", "sample-sources.json"),
        "observation": ("observations", "sample-register-observation.json"),
        "mapping": ("mappings", "sample-source-skill-map.json"),
    }
    paths = {}
    for name, parts in defaults.items():
        payload = mutated[name] if name in mutated else _payload(*parts)
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[name] = path
    return compare(baseline_path=paths["baseline"], observation_path=paths["observation"], mapping_path=paths["mapping"])


def _only(queue: dict, change_kind: str) -> dict:
    matches = [item for item in queue["items"] if item["change_kind"] == change_kind]
    assert len(matches) == 1, f"expected one {change_kind} item, got {[item['change_kind'] for item in queue['items']]}"
    return matches[0]


def _clear_compilation(entry: dict) -> dict:
    for field in ("observed_compilation_number", "observed_compilation_date", "observed_register_document_id"):
        entry[field] = None
    return entry


def _independent_changed_item(
    template: dict,
    *,
    change_kind: str,
    state: str,
    mapping_status: str,
    observed_compilation: bool,
) -> dict:
    item = copy.deepcopy(template)
    item["item_id"] = (
        "impact:independent-"
        f"{change_kind.lower()}-{mapping_status.lower()}-"
        f"{'observed' if observed_compilation else 'null'}"
    )
    item["change_kind"] = change_kind
    item["state"] = state
    item["source"]["observed_compilation"] = (
        copy.deepcopy(template["source"]["observed_compilation"])
        if observed_compilation
        else None
    )
    item["mapping_status"] = mapping_status
    if mapping_status != "MAPPED":
        item["impact_candidates"] = []
    return item


def _independent_scope_item(template: dict, *, change_kind: str, suffix: str) -> dict:
    item = copy.deepcopy(template)
    item.update(
        item_id=f"impact:independent-{change_kind.lower()}-{suffix}",
        state="BLOCKED",
        change_kind=change_kind,
        impact_candidates=[],
        mapping_status="NOT_EVALUATED",
        limitations=["Synthetic scope evidence requires operator resolution."],
    )
    if change_kind == "INCOMPLETE_SCOPE":
        item["source"] = None
    else:
        item["source"]["observed_compilation"] = None
    return item


def _bind_independent_queue(
    queue: dict, *, complete: bool, items: list[dict]
) -> tuple[dict, dict]:
    queue = copy.deepcopy(queue)
    queue["observation"]["complete"] = complete
    queue["items"] = copy.deepcopy(items)
    queue["run_status"] = (
        "BLOCKED"
        if any(item["state"] == "BLOCKED" for item in items)
        else "REVIEW_REQUIRED" if items else "NO_CHANGE_DETECTED"
    )
    queue["queue_digest"] = _expected_queue_digest(queue)
    return queue, _matching_decision_for_all_open_items(queue)


def test_queue_digest_is_present_and_deterministic() -> None:
    first = _queue()
    second = _queue()

    assert first["queue_digest"] == _expected_queue_digest(first)
    assert second["queue_digest"] == first["queue_digest"]


def test_queue_digest_and_source_digests_include_the_mapping_snapshot(
    tmp_path: Path,
) -> None:
    original_mapping = _payload("mappings", "sample-source-skill-map.json")
    changed_mapping = copy.deepcopy(original_mapping)
    changed_mapping["mapping_version"] += "-changed-bytes"

    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = _compare_fixtures(first_dir, mapping=original_mapping)
    second = _compare_fixtures(second_dir, mapping=changed_mapping)

    assert set(first["source_digests"]) == {"baseline", "observation", "mapping"}
    assert first["source_digests"]["mapping"] != second["source_digests"]["mapping"]
    assert first["run_id"] != second["run_id"]
    assert first["queue_digest"] != second["queue_digest"]


@pytest.mark.parametrize(
    "field",
    [
        "source.title",
        "source.evidence_url",
        "source.baseline_compilation.number",
        "source.baseline_compilation.date",
        "source.observed_compilation.number",
        "source.observed_compilation.date",
        "source.observed_compilation.document_id",
        "impact_candidates",
        "impact_candidates.review_question",
        "mapping_status",
        "limitations",
    ],
)
def test_queue_digest_rejects_tampered_review_evidence(
    tmp_path: Path, field: str
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    item = queue["items"][0]
    source = item["source"]

    if field == "source.title":
        source["title"] += " amended"
    elif field == "source.evidence_url":
        source["evidence_url"] = "https://example.test/replaced-evidence"
    elif field == "source.baseline_compilation.number":
        source["baseline_compilation"]["number"] = "999"
    elif field == "source.baseline_compilation.date":
        source["baseline_compilation"]["date"] = "2099-07-02"
    elif field == "source.observed_compilation.number":
        source["observed_compilation"]["number"] = "999"
    elif field == "source.observed_compilation.date":
        source["observed_compilation"]["date"] = "2099-08-02"
    elif field == "source.observed_compilation.document_id":
        source["observed_compilation"]["document_id"] = "C2099C00999"
    elif field == "impact_candidates":
        added = copy.deepcopy(item["impact_candidates"][0])
        added["mapping_id"] += ":added-after-review"
        item["impact_candidates"].append(added)
    elif field == "impact_candidates.review_question":
        item["impact_candidates"][0]["review_question"] += " Changed."
    elif field == "mapping_status":
        item["mapping_status"] = "UNMAPPED_SOURCE"
    elif field == "limitations":
        item["limitations"].append("Additional synthetic limitation.")
    else:  # pragma: no cover - the parameter list controls this branch.
        raise AssertionError(field)

    with pytest.raises(MonitorError):
        _validate_payloads(tmp_path, queue, decision)


@pytest.mark.parametrize(
    "location",
    [
        "queue",
        "source_digests",
        "baseline",
        "observation",
        "item",
        "source",
        "baseline_compilation",
        "observed_compilation",
        "candidate",
    ],
)
def test_exact_queue_schemas_reject_additional_properties_before_digesting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, location: str
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    item = queue["items"][0]
    targets = {
        "queue": queue,
        "source_digests": queue["source_digests"],
        "baseline": queue["baseline"],
        "observation": queue["observation"],
        "item": item,
        "source": item["source"],
        "baseline_compilation": item["source"]["baseline_compilation"],
        "observed_compilation": item["source"]["observed_compilation"],
        "candidate": item["impact_candidates"][0],
    }
    targets[location]["unexpected"] = "must be rejected"

    def digest_must_not_run(value: object) -> str:
        raise AssertionError(f"digest ran before exact schema validation: {value!r}")

    monkeypatch.setattr(monitor_module, "sha256_json", digest_must_not_run)

    with pytest.raises(MonitorError, match="invalid shape|must contain exactly"):
        _validate_payloads(tmp_path, queue, decision)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not-a-list", "limitations must be a list"),
        ([""], "non-empty string"),
        ([7], "non-empty string"),
    ],
)
def test_exact_queue_limitations_schema_is_enforced(
    tmp_path: Path, value: object, message: str
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    queue["items"][0]["limitations"] = value

    with pytest.raises(MonitorError, match=message):
        _validate_payloads(tmp_path, queue, decision)


def test_exact_queue_rejects_a_missing_mapping_digest(tmp_path: Path) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    del queue["source_digests"]["mapping"]

    with pytest.raises(MonitorError, match="source_digests.*invalid shape"):
        _validate_payloads(tmp_path, queue, decision)


def test_exact_queue_rejects_a_non_string_run_status_cleanly(
    tmp_path: Path,
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    queue["run_status"] = ["REVIEW_REQUIRED"]

    with pytest.raises(MonitorError, match="unsupported run_status"):
        _validate_payloads(tmp_path, queue, decision)


def test_exact_queue_rejects_a_non_string_candidate_id_cleanly(
    tmp_path: Path,
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    queue["items"][0]["impact_candidates"][0]["mapping_id"] = ["map:bad"]

    with pytest.raises(MonitorError, match="non-empty string"):
        _validate_payloads(tmp_path, queue, decision)


@pytest.mark.parametrize("location", ["item", "source", "candidate", "queue"])
def test_tamper_additional_review_evidence_is_rejected(
    tmp_path: Path, location: str
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    item = queue["items"][0]
    targets = {
        "item": item,
        "source": item["source"],
        "candidate": item["impact_candidates"][0],
        "queue": queue,
    }
    targets[location]["unreviewed_property"] = "not in the decision"

    with pytest.raises(MonitorError):
        _validate_payloads(tmp_path, queue, decision)


def test_tamper_with_unchanged_run_id_is_rejected_by_queue_digest(
    tmp_path: Path,
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    original_run_id = queue["run_id"]
    queue["items"][0]["source"]["title"] += " changed after review"
    # Model an attacker who can rewrite the queue and update its self-digest,
    # but cannot rewrite the human's already-recorded decision.
    queue["queue_digest"] = _expected_queue_digest(queue)

    assert queue["run_id"] == original_run_id
    with pytest.raises(MonitorError, match="decision.*queue_digest"):
        _validate_payloads(tmp_path, queue, decision)


@pytest.mark.parametrize("change", ["missing", "different"])
def test_queue_digest_is_required_in_the_decision(
    tmp_path: Path, change: str
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    if change == "missing":
        del decision["queue_digest"]
    else:
        decision["queue_digest"] = "sha256:" + "0" * 64

    with pytest.raises(MonitorError, match="queue_digest"):
        _validate_payloads(tmp_path, queue, decision)


@pytest.mark.parametrize("location", ["decision", "entry"])
def test_exact_queue_decision_schemas_reject_additional_properties(
    tmp_path: Path, location: str
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    target = decision if location == "decision" else decision["decisions"][0]
    target["unexpected"] = "must be rejected"

    with pytest.raises(MonitorError, match="exactly"):
        _validate_payloads(tmp_path, queue, decision)


def test_queue_digest_validation_receipt_records_both_identifiers(
    tmp_path: Path,
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)

    receipt = _validate_payloads(tmp_path, queue, decision)

    assert receipt["status"] == "DECISION_RECORDED"
    assert receipt["run_id"] == queue["run_id"]
    assert receipt["queue_digest"] == queue["queue_digest"]


@pytest.mark.parametrize(
    ("artefact", "location"),
    [
        ("queue", "top_level"),
        ("queue", "source_title"),
        ("decision", "reviewer_ref"),
        ("decision", "entry_item_id"),
    ],
)
def test_duplicate_json_members_are_rejected_before_canonicalisation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    artefact: str,
    location: str,
) -> None:
    queue = _queue()
    decision = _matching_decision(queue)
    queue_text = json.dumps(queue)
    decision_text = json.dumps(decision)

    if artefact == "queue":
        target, key, conflicting_value = {
            "top_level": (queue, "run_status", "BLOCKED"),
            "source_title": (
                queue["items"][0]["source"],
                "title",
                "Conflicting first-consumer title",
            ),
        }[location]
        queue_text = _json_with_conflicting_duplicate(
            queue, target=target, key=key, conflicting_value=conflicting_value
        )
    else:
        target, key, conflicting_value = {
            "reviewer_ref": (
                decision,
                "reviewer_ref",
                "conflicting-first-reviewer",
            ),
            "entry_item_id": (
                decision["decisions"][0],
                "item_id",
                "impact:conflicting-first-item",
            ),
        }[location]
        decision_text = _json_with_conflicting_duplicate(
            decision, target=target, key=key, conflicting_value=conflicting_value
        )

    queue_path = tmp_path / "impact-queue.json"
    decision_path = tmp_path / "technical-review.json"
    queue_path.write_text(queue_text, encoding="utf-8")
    decision_path.write_text(decision_text, encoding="utf-8")

    def digest_must_not_run(value: object) -> str:
        raise AssertionError(
            f"canonicalisation ran before duplicate-member rejection: {value!r}"
        )

    monkeypatch.setattr(monitor_module, "sha256_json", digest_must_not_run)

    with pytest.raises(MonitorError, match="duplicate JSON members"):
        validate_review(queue_path=queue_path, decision_path=decision_path)


@pytest.mark.parametrize("mapping_status", ["MAPPED", "UNMAPPED_SOURCE"])
@pytest.mark.parametrize(
    ("change_kind", "state", "observed_compilation"),
    [
        ("SUPERSEDED", "OPEN", True),
        ("CURRENT_NO_PUBLISHED_COMPILATION", "BLOCKED", False),
        ("NO_LONGER_IN_FORCE", "OPEN", False),
        ("LOOKUP_FAILED", "BLOCKED", False),
        ("BASELINE_NOT_CURRENT", "BLOCKED", True),
        ("BASELINE_NOT_CURRENT", "BLOCKED", False),
    ],
)
def test_supported_queue_matrix_accepts_every_changed_kind_and_mapping_shape(
    tmp_path: Path,
    change_kind: str,
    state: str,
    observed_compilation: bool,
    mapping_status: str,
) -> None:
    original = _queue()
    anchor = original["items"][0]
    independent = _independent_changed_item(
        anchor,
        change_kind=change_kind,
        state=state,
        mapping_status=mapping_status,
        observed_compilation=observed_compilation,
    )
    queue, decision = _bind_independent_queue(
        original, complete=True, items=[anchor, independent]
    )

    receipt = _validate_payloads(tmp_path, queue, decision)

    assert receipt["status"] == "DECISION_RECORDED"


@pytest.mark.parametrize("change_kind", ["INCOMPLETE_SCOPE", "MISSING_OBSERVATION"])
def test_supported_queue_matrix_accepts_scope_kinds_only_in_incomplete_scope(
    tmp_path: Path, change_kind: str
) -> None:
    original = _queue()
    anchor = original["items"][0]
    incomplete = _independent_scope_item(
        anchor, change_kind="INCOMPLETE_SCOPE", suffix="required"
    )
    items = [anchor, incomplete]
    if change_kind == "MISSING_OBSERVATION":
        items.append(
            _independent_scope_item(
                anchor, change_kind="MISSING_OBSERVATION", suffix="missing"
            )
        )
    queue, decision = _bind_independent_queue(
        original, complete=False, items=items
    )

    receipt = _validate_payloads(tmp_path, queue, decision)

    assert receipt["status"] == "DECISION_RECORDED"


@pytest.mark.parametrize(
    "case",
    [
        "incomplete_without_blocker",
        "incomplete_with_two_blockers",
        "complete_with_blocker",
        "complete_with_missing_observation",
    ],
)
def test_observation_scope_matrix_rejects_writer_impossible_scope_items(
    tmp_path: Path, case: str
) -> None:
    original = _queue()
    anchor = original["items"][0]
    incomplete = _independent_scope_item(
        anchor, change_kind="INCOMPLETE_SCOPE", suffix="one"
    )
    missing = _independent_scope_item(
        anchor, change_kind="MISSING_OBSERVATION", suffix="one"
    )
    complete, items = {
        "incomplete_without_blocker": (False, [anchor]),
        "incomplete_with_two_blockers": (
            False,
            [
                anchor,
                incomplete,
                _independent_scope_item(
                    anchor, change_kind="INCOMPLETE_SCOPE", suffix="two"
                ),
            ],
        ),
        "complete_with_blocker": (True, [anchor, incomplete]),
        "complete_with_missing_observation": (True, [anchor, missing]),
    }[case]
    queue, decision = _bind_independent_queue(
        original, complete=complete, items=items
    )

    with pytest.raises(MonitorError, match="observation complete.*scope items"):
        _validate_payloads(tmp_path, queue, decision)


@pytest.mark.parametrize(
    ("change_kind", "state", "observed_compilation"),
    [
        ("SUPERSEDED", "OPEN", True),
        ("CURRENT_NO_PUBLISHED_COMPILATION", "BLOCKED", False),
        ("NO_LONGER_IN_FORCE", "OPEN", False),
        ("LOOKUP_FAILED", "BLOCKED", False),
        ("BASELINE_NOT_CURRENT", "BLOCKED", False),
    ],
)
def test_non_scope_mapping_matrix_rejects_not_evaluated(
    tmp_path: Path,
    change_kind: str,
    state: str,
    observed_compilation: bool,
) -> None:
    original = _queue()
    anchor = original["items"][0]
    independent = _independent_changed_item(
        anchor,
        change_kind=change_kind,
        state=state,
        mapping_status="NOT_EVALUATED",
        observed_compilation=observed_compilation,
    )
    queue, decision = _bind_independent_queue(
        original, complete=True, items=[anchor, independent]
    )

    with pytest.raises(MonitorError, match="mapping_status.*unsupported"):
        _validate_payloads(tmp_path, queue, decision)


def test_superseded_source_creates_an_open_exactly_mapped_review_item() -> None:
    queue = _queue()

    assert queue["mode"] == "synthetic"
    assert queue["run_status"] == "REVIEW_REQUIRED"
    assert len(queue["items"]) == 1
    item = queue["items"][0]
    assert item["change_kind"] == "SUPERSEDED"
    assert item["state"] == "OPEN"
    assert item["mapping_status"] == "MAPPED"
    assert item["impact_candidates"][0]["mapping_basis"] == "exact_register_id_and_collection"


def test_parsing_and_every_provenance_id_use_the_same_source_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sample_sources = {
        "baseline": sample_path("baseline", "sample-sources.json"),
        "observation": sample_path("observations", "sample-register-observation.json"),
        "mapping": sample_path("mappings", "sample-source-skill-map.json"),
    }
    source_paths = {name: tmp_path / f"{name}.json" for name in sample_sources}
    original_bytes = {}
    for name, source in sample_sources.items():
        content = source.read_bytes()
        source_paths[name].write_bytes(content)
        original_bytes[name] = content

    replacement = b"{}\n"
    real_text = SourceSnapshot.text

    def text_then_replace(snapshot: SourceSnapshot, *, label: str) -> str:
        text = real_text(snapshot, label=label)
        snapshot.path.write_bytes(replacement)
        return text

    monkeypatch.setattr(SourceSnapshot, "text", text_then_replace)

    queue = compare(
        baseline_path=source_paths["baseline"],
        observation_path=source_paths["observation"],
        mapping_path=source_paths["mapping"],
    )

    digests = {
        name: hashlib.sha256(content).hexdigest()
        for name, content in original_bytes.items()
    }
    assert queue["run_id"] == "sha256:" + sha256_json(digests)
    assert queue["baseline"]["id"] == "sha256:" + digests["baseline"]
    assert queue["observation"]["id"] == "sha256:" + digests["observation"]
    item = _only(queue, "SUPERSEDED")
    assert item["item_id"] == "impact:" + sha256_json(
        {
            "change_kind": "SUPERSEDED",
            "identity": {
                "register_id": item["source"]["register_id"],
                "collection": item["source"]["collection"],
                "observed_state": "SUPERSEDED",
            },
            "sources": digests,
        }
    )[:24]
    assert item["mapping_status"] == "MAPPED"
    assert all(path.read_bytes() == replacement for path in source_paths.values())


@pytest.mark.parametrize("shape", ["changed", "incomplete", "missing"])
def test_every_item_kind_is_bound_to_all_three_source_snapshots(
    tmp_path: Path, shape: str
) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    if shape in {"incomplete", "missing"}:
        observation["complete"] = False
    if shape == "missing":
        observation["observations"] = observation["observations"][1:]

    mapping = _payload("mappings", "sample-source-skill-map.json")
    first = _compare_fixtures(tmp_path, observation=observation, mapping=mapping)
    # The mapping rules are unchanged; only source metadata bytes differ. Every
    # item ID must still move because it belongs to the exact three-file run.
    mapping["mapping_version"] += "-same-rules-new-source"
    second = _compare_fixtures(tmp_path, observation=observation, mapping=mapping)

    first_ids = {item["change_kind"]: item["item_id"] for item in first["items"]}
    second_ids = {item["change_kind"]: item["item_id"] for item in second["items"]}
    assert first_ids.keys() == second_ids.keys()
    assert all(first_ids[kind] != second_ids[kind] for kind in first_ids)


def test_incomplete_scope_blocks_even_when_every_observed_source_is_unchanged(tmp_path: Path) -> None:
    # The one conclusion this tool must never draw: "no change" from a scope
    # that was never fully observed.
    observation = _payload("observations", "sample-register-observation.json")
    observation["complete"] = False
    for entry in observation["observations"]:
        entry["state"] = "UNCHANGED"
        _clear_compilation(entry)

    queue = _compare_fixtures(tmp_path, observation=observation)

    assert queue["run_status"] == "BLOCKED"
    assert len(queue["items"]) == 1
    item = _only(queue, "INCOMPLETE_SCOPE")
    assert item["state"] == "BLOCKED"
    assert item["mapping_status"] == "NOT_EVALUATED"


def test_a_baseline_title_with_no_observation_is_blocked_not_dropped(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    observation["complete"] = False
    observation["observations"] = [entry for entry in observation["observations"] if entry["register_id"] != "F2099L00001"]

    queue = _compare_fixtures(tmp_path, observation=observation)

    missing = _only(queue, "MISSING_OBSERVATION")
    assert missing["state"] == "BLOCKED"
    assert missing["source"]["register_id"] == "F2099L00001"
    assert missing["mapping_status"] == "NOT_EVALUATED"
    assert queue["run_status"] == "BLOCKED"


@pytest.mark.parametrize(
    ("state", "extra"),
    [
        ("CURRENT_NO_PUBLISHED_COMPILATION", {"current_version_start": "2099-09-01"}),
        ("LOOKUP_FAILED", {"error_category": "register_unavailable"}),
    ],
)
def test_an_unresolved_register_state_is_blocked_not_open(tmp_path: Path, state: str, extra: dict) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    entry = _clear_compilation(observation["observations"][0])
    entry["state"] = state
    entry.update(extra)

    queue = _compare_fixtures(tmp_path, observation=observation)

    item = _only(queue, state)
    assert item["state"] == "BLOCKED"
    assert queue["run_status"] == "BLOCKED"


def test_a_title_no_longer_in_force_is_open_for_human_review(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    entry = _clear_compilation(observation["observations"][0])
    entry["state"] = "NO_LONGER_IN_FORCE"

    queue = _compare_fixtures(tmp_path, observation=observation)

    item = _only(queue, "NO_LONGER_IN_FORCE")
    assert item["state"] == "OPEN"
    assert item["source"]["observed_compilation"] is None
    assert queue["run_status"] == "REVIEW_REQUIRED"


def test_a_stale_baseline_row_blocks_even_an_unchanged_observation(tmp_path: Path) -> None:
    # A baseline entry that is not itself the current version cannot support a
    # currency conclusion, so the UNCHANGED short-circuit must not swallow it.
    baseline = _payload("baseline", "sample-sources.json")
    baseline["titles"][1]["version_is_current"] = False

    queue = _compare_fixtures(tmp_path, baseline=baseline)

    item = _only(queue, "BASELINE_NOT_CURRENT")
    assert item["state"] == "BLOCKED"
    assert item["source"]["register_id"] == "F2099L00001"
    assert queue["run_status"] == "BLOCKED"


def test_a_mapping_for_another_collection_does_not_map_the_source(tmp_path: Path) -> None:
    # Mapping is by exact (register_id, collection). A register ID match alone
    # must leave the changed source visible as UNMAPPED_SOURCE.
    mapping = _payload("mappings", "sample-source-skill-map.json")
    mapping["entries"][0]["collection"] = "LegislativeInstrument"

    queue = _compare_fixtures(tmp_path, mapping=mapping)

    item = _only(queue, "SUPERSEDED")
    assert item["mapping_status"] == "UNMAPPED_SOURCE"
    assert item["impact_candidates"] == []
    assert item["state"] == "OPEN"


def test_an_empty_map_leaves_the_changed_source_visible(tmp_path: Path) -> None:
    mapping = _payload("mappings", "sample-source-skill-map.json")
    mapping["entries"] = []

    queue = _compare_fixtures(tmp_path, mapping=mapping)

    item = _only(queue, "SUPERSEDED")
    assert item["mapping_status"] == "UNMAPPED_SOURCE"
    assert item["impact_candidates"] == []
    assert queue["run_status"] == "REVIEW_REQUIRED"


def test_an_exactly_mapped_source_carries_the_review_question(tmp_path: Path) -> None:
    queue = _compare_fixtures(tmp_path)

    item = _only(queue, "SUPERSEDED")
    assert item["mapping_status"] == "MAPPED"
    assert [candidate["mapping_id"] for candidate in item["impact_candidates"]] == ["map:sample-consumption-tax-to-bas"]
    assert item["impact_candidates"][0]["skill_ref"] == "bas-preparation"


def test_an_observation_collection_that_disagrees_with_the_baseline_is_rejected(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    observation["observations"][0]["collection"] = "LegislativeInstrument"

    with pytest.raises(MonitorError, match="collection does not match baseline"):
        _compare_fixtures(tmp_path, observation=observation)


@pytest.mark.parametrize("field", ["observed_compilation_number", "observed_compilation_date", "observed_register_document_id"])
def test_a_superseded_observation_without_compilation_detail_is_rejected(tmp_path: Path, field: str) -> None:
    # Without this guard the item renders as "Observed compilation: None dated None".
    observation = _payload("observations", "sample-register-observation.json")
    observation["observations"][0][field] = None

    with pytest.raises(MonitorError, match=field):
        _compare_fixtures(tmp_path, observation=observation)


def test_a_current_title_with_no_compilation_cannot_carry_a_document_id(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    entry = observation["observations"][0]
    entry["state"] = "CURRENT_NO_PUBLISHED_COMPILATION"
    entry["current_version_start"] = "2099-09-01"

    with pytest.raises(MonitorError, match="null observed_register_document_id"):
        _compare_fixtures(tmp_path, observation=observation)


@pytest.mark.parametrize(
    ("state", "field", "value"),
    [
        ("UNCHANGED", "observed_compilation_number", "2"),
        ("SUPERSEDED", "current_version_start", "2099-09-01"),
        ("CURRENT_NO_PUBLISHED_COMPILATION", "error_category", "timeout"),
        ("NO_LONGER_IN_FORCE", "observed_register_document_id", "C2099C00002"),
        ("LOOKUP_FAILED", "current_version_start", "2099-09-01"),
    ],
)
def test_observation_states_reject_contradictory_fields(
    tmp_path: Path, state: str, field: str, value: str
) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    entry = _clear_compilation(observation["observations"][0])
    entry.update(
        state=state,
        current_version_start=None,
        error_category=None,
    )
    if state == "SUPERSEDED":
        entry.update(
            observed_compilation_number="2",
            observed_compilation_date="2099-08-01",
            observed_register_document_id="C2099C00002",
        )
    elif state == "CURRENT_NO_PUBLISHED_COMPILATION":
        entry["current_version_start"] = "2099-09-01"
    elif state == "LOOKUP_FAILED":
        entry["error_category"] = "register_unavailable"
    entry[field] = value

    with pytest.raises(MonitorError, match=rf"{state} must have a null {field}"):
        _compare_fixtures(tmp_path, observation=observation)


def test_a_live_observation_cannot_be_compared(tmp_path: Path) -> None:
    """The observation-side twin of the queue-side synthetic-mode gate.

    README rests "Every artefact carries `mode: synthetic`" on both of them,
    and only the queue side was pinned. Without this one a live Register
    observation flows straight through compare() into a queue that then stamps
    itself `mode: synthetic`, which is the misrepresentation the rule exists to
    prevent.
    """
    observation = _payload("observations", "sample-register-observation.json")
    observation["mode"] = "live"

    with pytest.raises(MonitorError, match="synthetic mode"):
        _compare_fixtures(tmp_path, observation=observation)


def test_an_observation_item_must_carry_a_real_checked_at_timestamp(tmp_path: Path) -> None:
    # The twin of observed_at, upgraded from _non_empty in the same hunk and
    # left unpinned. checked_at is the per-title provenance stamp; a free-text
    # value reaches the queue and cannot order anything.
    observation = _payload("observations", "sample-register-observation.json")
    observation["observations"][0]["checked_at"] = "last tuesday"

    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _compare_fixtures(tmp_path, observation=observation)


def test_a_failed_lookup_must_name_its_error_category(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    entry = _clear_compilation(observation["observations"][0])
    entry["state"] = "LOOKUP_FAILED"

    with pytest.raises(MonitorError, match="error_category"):
        _compare_fixtures(tmp_path, observation=observation)


def test_a_map_reusing_a_mapping_id_is_rejected(tmp_path: Path) -> None:
    mapping = _payload("mappings", "sample-source-skill-map.json")
    mapping["entries"].append(dict(mapping["entries"][0]))

    with pytest.raises(MonitorError, match="duplicate mapping IDs"):
        _compare_fixtures(tmp_path, mapping=mapping)


def test_an_input_that_is_not_utf8_is_blocked_not_a_traceback(tmp_path: Path) -> None:
    # A file the loader cannot decode is an ordinary condition, not a crash.
    # Without the UnicodeDecodeError clause the raw exception escapes main()
    # and the CLI prints a traceback carrying the local path layout. The
    # UTF-8 wording belongs to this decode failure alone.
    bad = tmp_path / "baseline.json"
    bad.write_bytes(b'{"source": "\xff\xfe not utf-8"}')

    with pytest.raises(MonitorError, match="could not be read as UTF-8"):
        compare(
            baseline_path=bad,
            observation_path=sample_path("observations", "sample-register-observation.json"),
            mapping_path=sample_path("mappings", "sample-source-skill-map.json"),
        )


def test_an_input_path_that_is_a_directory_is_blocked_not_a_traceback(tmp_path: Path) -> None:
    # The OSError half of the same clause: a directory where a file belongs
    # raises IsADirectoryError on POSIX and PermissionError on Windows, and
    # neither is FileNotFoundError, so only the OSError catch stops it. The
    # message names the read failure and carries the OS detail; it must not
    # borrow the UTF-8 wording, which describes a decode failure.
    bad = tmp_path / "baseline-directory"
    bad.mkdir()

    with pytest.raises(MonitorError, match=r"could not be read: .*\[Errno"):
        compare(
            baseline_path=bad,
            observation_path=sample_path("observations", "sample-register-observation.json"),
            mapping_path=sample_path("mappings", "sample-source-skill-map.json"),
        )


def test_a_baseline_with_no_titles_is_rejected(tmp_path: Path) -> None:
    baseline = _payload("baseline", "sample-sources.json")
    baseline["titles"] = []

    with pytest.raises(MonitorError, match="no titles"):
        _compare_fixtures(tmp_path, baseline=baseline)


def test_an_observation_scope_narrower_than_the_baseline_is_rejected(tmp_path: Path) -> None:
    # Without the equality check a scope mismatch becomes a queue full of
    # MISSING_OBSERVATION noise instead of a hard stop.
    observation = _payload("observations", "sample-register-observation.json")
    observation["expected_register_ids"] = ["C2099A00001"]
    observation["complete"] = False
    observation["observations"] = observation["observations"][:1]

    with pytest.raises(MonitorError, match="exactly match the baseline scope"):
        _compare_fixtures(tmp_path, observation=observation)


def test_complete_observation_rejects_missing_expected_source(tmp_path: Path) -> None:
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["observations"] = payload["observations"][:1]
    bad = tmp_path / "incomplete.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="cover every expected"):
        _load_observation(bad, {"C2099A00001", "F2099L00001"})


def test_partial_observation_rejects_sources_outside_the_baseline(tmp_path: Path) -> None:
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["complete"] = False
    extra = dict(payload["observations"][0])
    extra["register_id"] = "C2099A99999"
    payload["observations"].append(extra)
    bad = tmp_path / "out-of-scope.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="outside the baseline scope"):
        _load_observation(bad, {"C2099A00001", "F2099L00001"})


def test_markdown_keeps_limits_visible_and_escapes_source_text() -> None:
    queue = _queue()
    queue["items"][0]["source"]["title"] = "Demo | [not a link]"
    markdown = render_markdown(queue)

    assert "This is a synthetic metadata-review queue" in markdown
    assert "Demo \\| \\[not a link\\]" in markdown
    assert "does not establish the legal effect" in markdown


def test_baseline_reusing_a_register_id_across_collections_is_rejected(tmp_path: Path) -> None:
    payload = json.loads(sample_path("baseline", "sample-sources.json").read_text(encoding="utf-8"))
    reused = dict(payload["titles"][0])
    reused["collection"] = "LegislativeInstrument"
    payload["titles"].append(reused)
    bad = tmp_path / "baseline.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="duplicate register IDs"):
        compare(
            baseline_path=bad,
            observation_path=sample_path("observations", "sample-register-observation.json"),
            mapping_path=sample_path("mappings", "sample-source-skill-map.json"),
        )


def test_markdown_escapes_observed_compilation_metadata() -> None:
    queue = _queue()
    source = queue["items"][0]["source"]
    source["baseline_compilation"]["number"] = "1 | [not a link]"
    source["observed_compilation"]["number"] = "2`code"
    source["observed_compilation"]["document_id"] = "C2099C00002`injected"
    source["evidence_url"] = "https://example.test/C2099A00001`tick"

    markdown = render_markdown(queue)

    assert "1 \\| \\[not a link\\]" in markdown
    assert "2\\`code" in markdown
    assert "C2099C00002\\`injected" in markdown
    assert "https://example.test/C2099A00001\\`tick" in markdown


def test_control_characters_in_source_metadata_are_rejected(tmp_path: Path) -> None:
    payload = json.loads(sample_path("baseline", "sample-sources.json").read_text(encoding="utf-8"))
    payload["titles"][0]["compilation_number"] = "1\n## Injected heading"
    bad = tmp_path / "baseline.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="control characters"):
        compare(
            baseline_path=bad,
            observation_path=sample_path("observations", "sample-register-observation.json"),
            mapping_path=sample_path("mappings", "sample-source-skill-map.json"),
        )


def test_queue_writes_and_human_decision_is_structurally_valid(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "test-output")
    validation = validate_review(
        queue_path=paths["json"],
        decision_path=sample_path("decisions", "sample-technical-review.json"),
    )

    assert validation["status"] == "DECISION_RECORDED"
    assert validation["decision_count"] == 1
    assert validation["mode"] == "synthetic"


def test_second_queue_commit_failure_restores_the_previous_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    output = tmp_path / "queue"
    write_queue(_queue(), output)
    previous = {
        name: (output / name).read_bytes()
        for name in ("impact-queue.json", "impact-queue.md")
    }
    replacement_queue = _queue()
    replacement_queue["baseline"]["source"] = "Replacement source"

    real_replace = monitor_module.os.replace
    failed = {"value": False}

    def fail_markdown_commit(source: Path, destination: Path) -> None:
        # Let the second swap park the existing Markdown, then fail the actual
        # staged-to-destination commit. This exercises both layers of rollback.
        if (
            not failed["value"]
            and destination == output / "impact-queue.md"
            and source.name.endswith(".partial")
        ):
            failed["value"] = True
            raise OSError(5, "simulated second queue commit failure")
        real_replace(source, destination)

    monkeypatch.setattr(monitor_module.os, "replace", fail_markdown_commit)

    with pytest.raises(OSError, match="second queue commit failure"):
        write_queue(replacement_queue, output)

    assert failed["value"] is True
    assert {
        name: (output / name).read_bytes()
        for name in ("impact-queue.json", "impact-queue.md")
    } == previous
    assert sorted(path.name for path in output.iterdir()) == [
        "impact-queue.json",
        "impact-queue.md",
    ]


def test_unknown_technical_decision_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "decision-test")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"][0]["decision"] = "AUTO_UPDATE_SKILL"
    bad = tmp_path / "bad-decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="allowlisted"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_review_with_blank_reviewer_ref_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["reviewer_ref"] = "   "
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="reviewer_ref"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_review_with_a_non_timestamp_reviewed_at_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["reviewed_at"] = "last tuesday"
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_review_cannot_predate_the_observation(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["reviewed_at"] = "2026-08-07T23:59:59Z"
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="cannot predate"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_reviewed_at_accepts_utc_z_and_explicit_offsets() -> None:
    # The shipped sample uses a trailing Z, which datetime.fromisoformat only
    # accepts natively from Python 3.11; the helper must normalise it on 3.10.
    assert _iso_timestamp("2026-08-08T00:00:00Z", field="reviewed_at") == "2026-08-08T00:00:00Z"
    assert _iso_timestamp("2026-08-08T10:00:00+10:00", field="reviewed_at") == "2026-08-08T10:00:00+10:00"


@pytest.mark.parametrize("value", ["2026-08-08T10:00:00", "2026-08-08 10:00:00", "2026-08-08T10:00:00.123456"])
def test_timestamps_require_an_explicit_timezone(value: str) -> None:
    with pytest.raises(MonitorError, match="explicit UTC offset"):
        _iso_timestamp(value, field="reviewed_at")


@pytest.mark.parametrize("value", ["2026-08-08", "2026-08-08Z", "2026-08-08+10:00", "2026-08-08-05:00"])
def test_a_date_alone_is_never_a_timestamp_however_it_is_qualified(value: str) -> None:
    """The clock is mandatory, and an offset does not substitute for one.

    A date-only value silently means midnight, so accepting one would let a
    review recorded earlier the same day as the observation pass the "cannot
    predate the observation" check. The first pattern written for this grammar
    made the clock and the offset independently optional, which accepted the
    last three values here - none of which any supported interpreter accepted
    before the pattern existed.
    """
    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _iso_timestamp(value, field="observed_at")


@pytest.mark.parametrize(
    "value",
    [
        "20260808T000000Z",        # ISO basic form
        "2026-W32-6T00:00:00Z",    # week date
        "2026-08-08T00:00:00+00",  # bare-hour offset
    ],
)
def test_timestamp_grammar_does_not_depend_on_the_interpreter(value: str) -> None:
    """Each of these parses on Python 3.11+ and raises on the declared 3.10 floor.

    Verified against origin/main's fromisoformat-based helper on 3.10.20 and
    3.12.10. datetime.fromisoformat decides the answer, so without an explicit
    pattern a queue written on one supported interpreter cannot be re-validated
    on another - which is the whole point of a replayable provenance artefact.
    """
    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _iso_timestamp(value, field="observed_at")


def test_a_lowercase_zulu_offset_is_refused_on_every_interpreter() -> None:
    """Not an interpreter divergence: no supported version accepts this.

    datetime.fromisoformat rejects "...00:00:00z" on 3.10, 3.11, 3.12 and 3.13
    alike - only an uppercase "Z" is normalised - so refusing it removes no
    divergence and simply keeps the documented grammar. It is pinned here
    rather than alongside the 3.11-only forms so the distinction stays honest.
    """
    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _iso_timestamp("2026-08-08T00:00:00z", field="observed_at")


@pytest.mark.parametrize("value", ["2026-08-08X00:00:00Z", "2026-08-08/00:00:00Z"])
def test_a_separator_outside_the_documented_grammar_is_refused(value: str) -> None:
    """Unlike the 3.11-only forms above, these parsed on 3.10 and 3.12 alike.

    fromisoformat took any single character as the date/time separator, so
    refusing them narrows the accepted set on every supported interpreter
    rather than removing a divergence. That is a deliberate choice, kept
    because neither ISO 8601 nor RFC 3339 admits an arbitrary separator, and
    README states the resulting grammar. "T", "t" and a space are still
    accepted, so an artefact stored under the old helper stays valid.
    """
    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _iso_timestamp(value, field="observed_at")


@pytest.mark.parametrize("value", ["20990701", "2099-W27-1", "2099-182"])
def test_iso_dates_reject_the_forms_only_newer_interpreters_accept(value: str) -> None:
    with pytest.raises(MonitorError, match="must be an ISO date"):
        _iso_date(value, field="compilation_date")


@pytest.mark.parametrize(
    "value",
    [
        "2026-08-08T00:00:00Z",
        "2026-08-08T00:00:00.5Z",
        "2026-08-08T00:00:00.123456+10:00",
        "2026-08-08 00:00:00+00:00",  # space separator, accepted on 3.10 and 3.12 before the pattern
        "2026-08-08t00:00:00Z",       # lowercase t, likewise
    ],
)
def test_the_accepted_timestamp_forms_are_the_same_on_every_supported_version(value: str) -> None:
    assert _iso_timestamp(value, field="observed_at") == value


def test_an_observation_stamped_with_a_date_and_an_offset_is_rejected_end_to_end(tmp_path: Path) -> None:
    # The unit case above, through the public entry point: with the clock
    # optional this reached the queue as observed_at "2026-08-08+10:00" and
    # compare() returned REVIEW_REQUIRED on it.
    observation = _payload("observations", "sample-register-observation.json")
    observation["observed_at"] = "2026-08-08+10:00"

    with pytest.raises(MonitorError, match="ISO 8601 timestamp"):
        _compare_fixtures(tmp_path, observation=observation)


def test_validate_review_parses_both_timestamps_with_the_pinned_grammar(tmp_path: Path) -> None:
    # A one-digit fractional second is accepted by fromisoformat on 3.11+ and
    # rejected on 3.10. The pinned pattern accepts 1 to 6 digits on every
    # supported version, so this is a case where the pattern is deliberately
    # WIDER than 3.10 rather than narrower: the answer stops moving with the
    # interpreter, which is the property that matters for a stored artefact.
    queue = _queue()
    queue["observation"]["observed_at"] = "2026-08-08T00:00:00.000000Z"
    queue["queue_digest"] = _expected_queue_digest(queue)
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    decision = _matching_decision(queue)
    decision["reviewed_at"] = "2026-08-09T00:00:00.5Z"
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(json.dumps(decision), encoding="utf-8")

    validation = validate_review(queue_path=queue_path, decision_path=decision_path)

    assert validation["status"] == "DECISION_RECORDED"


def test_partial_technical_review_remains_explicit(tmp_path: Path) -> None:
    queue = _queue()
    second = dict(queue["items"][0])
    second["item_id"] = "impact:second-open-item"
    queue["items"].append(second)
    queue["queue_digest"] = _expected_queue_digest(queue)
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps(queue), encoding="utf-8")
    decision = _matching_decision(queue)
    decision_path = tmp_path / "decision.json"
    decision_path.write_text(json.dumps(decision), encoding="utf-8")

    validation = validate_review(
        queue_path=queue_path,
        decision_path=decision_path,
    )

    assert validation["status"] == "PARTIAL_DECISION_RECORDED"
    assert validation["undecided_count"] == 1


def test_observation_rejects_duplicate_expected_ids(tmp_path: Path) -> None:
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["expected_register_ids"].append(payload["expected_register_ids"][0])
    bad = tmp_path / "observation.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="must not contain duplicates"):
        _load_observation(bad, set(payload["expected_register_ids"]))


@pytest.mark.parametrize(
    "value",
    [
        "https://[malformed",
        "https://example.test:not-a-port/path",
        "https://example.test:65536/path",
        "https://exa mple.test/path",
        "https://example.test\\@other.test/path",
    ],
)
def test_https_urls_fail_with_a_domain_error_for_malformed_authorities(value: str) -> None:
    with pytest.raises(MonitorError, match="must be an https URL"):
        _https_url(value, field="evidence_url")


@pytest.mark.parametrize("value", ["http://example.test/x", "ftp://example.test/x"])
def test_a_scheme_other_than_https_is_refused(value: str) -> None:
    """The authority checks above are pinned; the scheme check was not.

    Every evidence_url, source_url and register_page in a queue is a link a
    reviewer is expected to follow. Both of these carry a well-formed
    authority, so with the scheme test dropped nothing else in _https_url
    objects and a cleartext citation ships in the artefact.
    """
    with pytest.raises(MonitorError, match="must be an https URL"):
        _https_url(value, field="evidence_url")


def test_observation_register_ids_with_non_string_entries_fail_cleanly(tmp_path: Path) -> None:
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["expected_register_ids"] = [["C2099A00001"], "F2099L00001"]
    bad = tmp_path / "observation.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="list of strings"):
        _load_observation(bad, {"C2099A00001", "F2099L00001"})


def test_observation_with_a_list_valued_state_is_rejected_cleanly(tmp_path: Path) -> None:
    # An unhashable state would raise TypeError from the set-membership test
    # instead of the clean MonitorError exit.
    payload = json.loads(sample_path("observations", "sample-register-observation.json").read_text(encoding="utf-8"))
    payload["observations"][0]["state"] = ["SUPERSEDED"]
    bad = tmp_path / "observation.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="unsupported state"):
        _load_observation(bad, set(payload["expected_register_ids"]))


def test_queue_item_with_a_list_valued_state_is_rejected_cleanly(tmp_path: Path) -> None:
    # Same trap as the observation loader: an unhashable state raises
    # TypeError from the set-membership test instead of MonitorError.
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(paths["json"].read_text(encoding="utf-8"))
    payload["items"][0]["state"] = ["OPEN"]
    bad = tmp_path / "bad-queue.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="unsupported state"):
        validate_review(
            queue_path=bad,
            decision_path=sample_path("decisions", "sample-technical-review.json"),
        )


def test_non_synthetic_queue_cannot_be_validated(tmp_path: Path) -> None:
    queue = _queue()
    queue["mode"] = "live"
    bad = tmp_path / "live-queue.json"
    bad.write_text(json.dumps(queue), encoding="utf-8")

    with pytest.raises(MonitorError, match="Only synthetic impact queues"):
        validate_review(queue_path=bad, decision_path=sample_path("decisions", "sample-technical-review.json"))


def test_a_decision_for_a_different_run_cannot_be_recorded(tmp_path: Path) -> None:
    """The one line binding a human sign-off to the run it signs off.

    Without it any decision file validates against any later queue, and the
    validation artefact records a sign-off nobody made for that run - which is
    the whole provenance claim this tool exists to make.
    """
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["run_id"] = "sha256:" + "0" * 64
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="exact queue run_id"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_a_blocked_item_cannot_be_signed_off(tmp_path: Path) -> None:
    # A BLOCKED item is one the tool could not assess at all. Recording a
    # technical decision against it would close a question that was never put.
    observation = _payload("observations", "sample-register-observation.json")
    entry = _clear_compilation(observation["observations"][0])
    entry["state"] = "LOOKUP_FAILED"
    entry["error_category"] = "register_unavailable"
    queue = _compare_fixtures(tmp_path, observation=observation)
    decision = {
        "schema_version": "au-tax-technical-review.v2",
        "run_id": queue["run_id"],
        "queue_digest": queue["queue_digest"],
        "reviewer_ref": "demo-tax-reviewer",
        "reviewed_at": "2026-08-09T00:00:00Z",
        "decisions": [
            {
                "item_id": queue["items"][0]["item_id"],
                "decision": "ESCALATE_TECHNICAL_REVIEW",
                "rationale": "The source lookup is blocked.",
                "evidence_note": "Reviewed the synthetic lookup failure.",
            }
        ],
    }
    bad = tmp_path / "blocked-queue.json"
    bad.write_text(json.dumps(queue), encoding="utf-8")
    decision_path = tmp_path / "blocked-decision.json"
    decision_path.write_text(json.dumps(decision), encoding="utf-8")

    with pytest.raises(MonitorError, match="unknown, blocked, or duplicate"):
        validate_review(queue_path=bad, decision_path=decision_path)


def test_a_decision_naming_an_item_outside_the_queue_is_rejected(tmp_path: Path) -> None:
    # The other half of the same condition: an item_id the queue never carried
    # would otherwise count towards the decided set and drive undecided_count to
    # zero, turning PARTIAL_DECISION_RECORDED into DECISION_RECORDED.
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"][0]["item_id"] = "impact:never-in-this-queue"
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="unknown, blocked, or duplicate"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_two_decisions_for_one_item_are_rejected(tmp_path: Path) -> None:
    # The last half of the same condition. Two decisions for one item is either
    # a duplicated file or a reviewer changing their mind in place; neither can
    # be recorded as one decision, and decision_count would count it twice.
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"].append(dict(payload["decisions"][0]))
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="unknown, blocked, or duplicate"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_queue_with_duplicate_item_ids_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    queue["items"].append(dict(queue["items"][0]))
    bad = tmp_path / "duplicate-items.json"
    bad.write_text(json.dumps(queue), encoding="utf-8")

    with pytest.raises(MonitorError, match="duplicate item IDs"):
        validate_review(queue_path=bad, decision_path=sample_path("decisions", "sample-technical-review.json"))


def test_queue_state_and_run_status_must_be_consistent(tmp_path: Path) -> None:
    queue = _queue()
    queue["run_status"] = "NO_CHANGE_DETECTED"
    bad = tmp_path / "inconsistent-queue.json"
    bad.write_text(json.dumps(queue), encoding="utf-8")

    with pytest.raises(MonitorError, match="run_status does not match"):
        validate_review(queue_path=bad, decision_path=sample_path("decisions", "sample-technical-review.json"))


def test_decision_with_a_list_valued_decision_is_rejected_cleanly(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"][0]["decision"] = ["adopt"]
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="not allowlisted"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_queue_with_non_dict_items_is_rejected_without_a_traceback(tmp_path: Path) -> None:
    queue = _queue()
    queue["items"] = ["not-an-item"]
    bad = tmp_path / "queue.json"
    bad.write_text(json.dumps(queue), encoding="utf-8")

    with pytest.raises(MonitorError, match="invalid shape"):
        validate_review(queue_path=bad, decision_path=sample_path("decisions", "sample-technical-review.json"))


def test_decision_with_a_non_string_item_id_is_rejected(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    payload = json.loads(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"))
    payload["decisions"][0]["item_id"] = ["impact:64ea8458e99ade934803959f"]
    bad = tmp_path / "decision.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(MonitorError, match="non-empty string"):
        validate_review(queue_path=paths["json"], decision_path=bad)


def test_samples_resolve_from_the_installed_package() -> None:
    for parts in (
        ("baseline", "sample-sources.json"),
        ("observations", "sample-register-observation.json"),
        ("mappings", "sample-source-skill-map.json"),
        ("decisions", "sample-technical-review.json"),
    ):
        assert sample_path(*parts).is_file()


def test_decision_files_are_accepted_from_any_directory(tmp_path: Path) -> None:
    queue = _queue()
    paths = write_queue(queue, tmp_path / "queue")
    decision = tmp_path / "a-human-technical-review.json"
    decision.write_text(sample_path("decisions", "sample-technical-review.json").read_text(encoding="utf-8"), encoding="utf-8")

    validation = validate_review(queue_path=paths["json"], decision_path=decision)

    assert validation["status"] == "DECISION_RECORDED"


def test_outputs_write_relative_to_the_current_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    queue = _queue()

    paths = write_queue(queue, Path("build") / "relative-out")

    assert (tmp_path / "build" / "relative-out" / "impact-queue.json").is_file()
    assert paths["markdown"].read_text(encoding="utf-8").startswith("# AU Tax Change Impact Queue")


def test_v01_package_has_no_network_client_import() -> None:
    """Walk the AST rather than scanning text.

    A substring scan for "urllib.request" is blind to `from urllib import
    request`, so the package could hold a working network client while the
    assertion passed.
    """
    forbidden_roots = {
        "aiohttp",
        "asyncio",
        "ftplib",
        "http",
        "httpx",
        "mcp",
        "requests",
        "smtplib",
        "socket",
        "ssl",
        "telnetlib",
        "urllib3",
        "webbrowser",
    }
    allowed_urllib = {"urllib.parse"}

    for path in sorted((ROOT / "tax_radar_au").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative import, inside this package
                    continue
                base = node.module or ""
                imported = [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
            else:
                continue
            for name in imported:
                root = name.split(".")[0]
                assert root not in forbidden_roots, f"{path.name} imports {name}"
                if root == "urllib":
                    assert name in allowed_urllib or name.startswith("urllib.parse."), (
                        f"{path.name} imports {name}"
                    )
                if root == "importlib":
                    # importlib.resources reads packaged sample data;
                    # importlib.import_module would load anything by name.
                    assert name.startswith("importlib.resources"), f"{path.name} imports {name}"

    # Second net: dynamic import by name would sidestep the walk above.
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "tax_radar_au").glob("*.py")
    )
    for forbidden in ("__import__", "import_module"):
        assert forbidden not in source


def test_a_padded_register_id_the_loader_accepted_still_maps(tmp_path: Path) -> None:
    """_load_observation strips the identifier for its scope, duplicate and
    coverage checks but never wrote the stripped value back, while compare()
    built its lookup from the raw one. An identifier the loader accepted as an
    exact scope match therefore failed to map, and the observed state was
    discarded in favour of a MISSING_OBSERVATION whose own artefact still
    recorded observation.complete: true."""
    observation = _payload("observations", "sample-register-observation.json")
    observation["observations"][0]["register_id"] += " "

    queue = _compare_fixtures(tmp_path, observation=observation)

    kinds = [item["change_kind"] for item in queue["items"]]
    assert "MISSING_OBSERVATION" not in kinds


def test_a_padded_collection_the_loader_accepted_does_not_raise(tmp_path: Path) -> None:
    """_load_observation called _non_empty on the collection and discarded the
    result, so compare() compared a padded collection against the stripped
    baseline one and raised instead of matching."""
    observation = _payload("observations", "sample-register-observation.json")
    observation["observations"][0]["collection"] += " "

    queue = _compare_fixtures(tmp_path, observation=observation)

    kinds = [item["change_kind"] for item in queue["items"]]
    assert "MISSING_OBSERVATION" not in kinds


def test_nominal_v2_observation_loads_and_compares(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation-v2.json")
    queue = _compare_fixtures(tmp_path, observation=observation)
    assert queue["run_status"] == "REVIEW_REQUIRED"
    assert queue["observation"]["complete"] is True
    assert len(queue["items"]) == 1
    assert queue["items"][0]["change_kind"] == "SUPERSEDED"


def test_v2_observation_requires_scope_id(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation-v2.json")
    del observation["scope_id"]
    with pytest.raises(MonitorError, match="must contain exactly"):
        _compare_fixtures(tmp_path, observation=observation)

    observation = _payload("observations", "sample-register-observation-v2.json")
    observation["scope_id"] = "   "
    with pytest.raises(MonitorError, match="scope_id must be a non-empty string"):
        _compare_fixtures(tmp_path, observation=observation)


def test_v2_observation_rejects_duplicate_evidence_id(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation-v2.json")
    observation["observations"][1]["evidence_id"] = observation["observations"][0]["evidence_id"]
    with pytest.raises(MonitorError, match="duplicate evidence_id"):
        _compare_fixtures(tmp_path, observation=observation)


def test_v2_observation_missing_evidence_id_is_rejected(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation-v2.json")
    del observation["observations"][0]["evidence_id"]
    with pytest.raises(MonitorError, match="invalid shape"):
        _compare_fixtures(tmp_path, observation=observation)


def test_unsupported_observation_schema_version_is_rejected(tmp_path: Path) -> None:
    observation = _payload("observations", "sample-register-observation.json")
    observation["schema_version"] = "au-tax-register-observation.v99"
    with pytest.raises(MonitorError, match="Only au-tax-register-observation.v1 and au-tax-register-observation.v2"):
        _compare_fixtures(tmp_path, observation=observation)

