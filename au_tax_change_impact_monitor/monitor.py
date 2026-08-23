from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import MonitorError
from .util import SourceSnapshot, load_json, load_json_exact, safe_markdown, sha256_json


OBSERVATION_STATES = {
    "UNCHANGED",
    "SUPERSEDED",
    "CURRENT_NO_PUBLISHED_COMPILATION",
    "NO_LONGER_IN_FORCE",
    "LOOKUP_FAILED",
}
ALLOWED_DECISIONS = {
    "AWAIT_PRIMARY_TEXT",
    "NO_WORKFLOW_CHANGE",
    "UPDATE_CANDIDATE",
    "ESCALATE_TECHNICAL_REVIEW",
}
# The one calendar-date and timestamp grammar this package accepts. ISO 8601
# extended forms only: no basic form, no week or ordinal dates, no bare-hour
# offset. See _parse_timestamp for why the grammar is pinned here.
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
# The clock is mandatory: a timestamp orders an audit trail, and a date alone
# would silently mean midnight. An offset carried by a date alone does not make
# it one, so "2026-08-08+10:00" is rejected here rather than reaching the
# tzinfo check below and passing it.
TIMESTAMP_PATTERN = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})"
    # "T", "t" and a space are the separators datetime.fromisoformat accepted
    # on every version this package supports, so an artefact already stored
    # with one stays valid. Any other separator is refused.
    r"[Tt ]"
    r"(?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?P<fraction>\.\d{1,6})?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})?"
)
SHA256_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

QUEUE_FIELDS = {
    "schema_version",
    "run_id",
    "queue_digest",
    "mode",
    "run_status",
    "source_digests",
    "baseline",
    "observation",
    "items",
}
SOURCE_DIGEST_FIELDS = {"baseline", "observation", "mapping"}
BASELINE_QUEUE_FIELDS = {"id", "retrieved", "source"}
OBSERVATION_QUEUE_FIELDS = {"id", "observed_at", "complete"}
ITEM_FIELDS = {
    "item_id",
    "state",
    "change_kind",
    "source",
    "impact_candidates",
    "mapping_status",
    "limitations",
}
SOURCE_FIELDS = {
    "register_id",
    "collection",
    "title",
    "baseline_compilation",
    "observed_compilation",
    "evidence_url",
}
BASELINE_COMPILATION_FIELDS = {"number", "date"}
OBSERVED_COMPILATION_FIELDS = {"number", "date", "document_id"}
CANDIDATE_FIELDS = {
    "mapping_id",
    "skill_ref",
    "owner_role",
    "review_question",
    "mapping_basis",
}
DECISION_FIELDS = {
    "schema_version",
    "run_id",
    "queue_digest",
    "reviewer_ref",
    "reviewed_at",
    "decisions",
}
DECISION_ENTRY_FIELDS = {"item_id", "decision", "rationale", "evidence_note"}
CHANGE_KIND_MATRIX = {
    "INCOMPLETE_SCOPE": ("BLOCKED", frozenset({"NOT_EVALUATED"})),
    "MISSING_OBSERVATION": ("BLOCKED", frozenset({"NOT_EVALUATED"})),
    "SUPERSEDED": ("OPEN", frozenset({"MAPPED", "UNMAPPED_SOURCE"})),
    "CURRENT_NO_PUBLISHED_COMPILATION": (
        "BLOCKED",
        frozenset({"MAPPED", "UNMAPPED_SOURCE"}),
    ),
    "NO_LONGER_IN_FORCE": (
        "OPEN",
        frozenset({"MAPPED", "UNMAPPED_SOURCE"}),
    ),
    "LOOKUP_FAILED": (
        "BLOCKED",
        frozenset({"MAPPED", "UNMAPPED_SOURCE"}),
    ),
    "BASELINE_NOT_CURRENT": (
        "BLOCKED",
        frozenset({"MAPPED", "UNMAPPED_SOURCE"}),
    ),
}
CHANGE_KINDS = set(CHANGE_KIND_MATRIX)
MAPPING_STATUSES = {"MAPPED", "UNMAPPED_SOURCE", "NOT_EVALUATED"}


@dataclass(frozen=True)
class BaselineTitle:
    register_id: str
    collection: str
    name: str
    compilation_number: str
    compilation_date: str
    version_is_current: bool
    current_version_start: str | None
    source_url: str
    register_page: str


def _non_empty(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MonitorError(f"{field} must be a non-empty string.")
    text = value.strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise MonitorError(f"{field} must not contain control characters.")
    return text


def _sha256_id(value: Any, *, field: str) -> str:
    text = _non_empty(value, field=field)
    if SHA256_ID_PATTERN.fullmatch(text) is None:
        raise MonitorError(f"{field} must be a lowercase sha256 identifier.")
    return text


def _iso_date(value: Any, *, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    text = _non_empty(value, field=field)
    # Match before parsing: date.fromisoformat accepts the ISO basic form
    # ("20990701") and week dates ("2099-W27-1") from Python 3.11 and rejects
    # both on the declared 3.10 floor, so delegating to it alone would let the
    # interpreter decide whether a stored artefact is valid.
    if DATE_PATTERN.fullmatch(text) is None:
        raise MonitorError(f"{field} must be an ISO date.")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise MonitorError(f"{field} must be an ISO date.") from exc
    return text


def _parse_timestamp(text: str, *, field: str) -> datetime:
    """Parse the one timestamp grammar this package accepts.

    datetime.fromisoformat widened its grammar in Python 3.11: the basic form
    ("20260808T000000Z"), week dates, a bare-hour offset ("+00") and a
    lowercase "z" all parse there and raise on 3.10, which is inside this
    package's declared requires-python range and inside its own CI matrix.
    Delegating to it would make an artefact valid or invalid according to
    whichever interpreter the next reviewer happens to run. Matching
    TIMESTAMP_PATTERN first, then parsing with strptime, keeps the accepted set
    identical on every supported version.

    The point of the pattern is that the accepted set is FIXED, not that it
    matches any one interpreter. It is not a subset of 3.10: a fractional
    second of 1 to 6 digits is accepted here, where 3.10 took only 3 or 6. It
    is narrower than 3.11+ in three respects - the basic and week forms and a
    bare-hour offset are refused, a fraction longer than 6 digits is refused,
    and only "T", "t" and a space are taken as the date/time separator where
    fromisoformat took any single character. What matters for an artefact is
    that none of those answers change with the interpreter running the check.
    README documents the resulting grammar.
    """
    match = TIMESTAMP_PATTERN.fullmatch(text)
    if match is None:
        raise MonitorError(f"{field} must be an ISO 8601 timestamp.")
    stamp = f"{match['date']}T{match['clock']}"
    fmt = "%Y-%m-%dT%H:%M:%S"
    if match["fraction"] is not None:
        stamp += match["fraction"]
        fmt += ".%f"
    offset = match["offset"]
    if offset is not None:
        # strptime's %z takes an extended offset; Z is not one of its forms.
        stamp += "+00:00" if offset == "Z" else offset
        fmt += "%z"
    try:
        return datetime.strptime(stamp, fmt)
    except ValueError as exc:
        raise MonitorError(f"{field} must be an ISO 8601 timestamp.") from exc


def _iso_timestamp(value: Any, *, field: str) -> str:
    text = _non_empty(value, field=field)
    parsed = _parse_timestamp(text, field=field)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MonitorError(f"{field} must include an explicit UTC offset or Z.")
    return text


def _https_url(value: Any, *, field: str) -> str:
    text = _non_empty(value, field=field)
    try:
        parsed = urlsplit(text)
        hostname = parsed.hostname
        _ = parsed.port  # Force validation of a supplied port.
    except ValueError as exc:
        raise MonitorError(f"{field} must be an https URL.") from exc
    invalid_authority = (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or "\\" in parsed.netloc
        or any(character.isspace() for character in parsed.netloc)
    )
    if parsed.scheme.lower() != "https" or invalid_authority:
        raise MonitorError(f"{field} must be an https URL.")
    return text


def _load_baseline(
    path: Path | SourceSnapshot,
) -> tuple[list[BaselineTitle], dict[str, Any]]:
    raw = load_json_exact(path, {"corpus", "retrieved", "source", "source_api", "titles"}, label="baseline source index")
    if raw["source"] != "Federal Register of Legislation" or not isinstance(raw["titles"], list):
        raise MonitorError("Baseline must be a Federal Register title index with a titles list.")
    _iso_date(raw["retrieved"], field="baseline retrieved")
    _https_url(raw["source_api"], field="baseline source_api")
    titles: list[BaselineTitle] = []
    seen: set[str] = set()
    expected = {"register_id", "name", "collection", "compilation_number", "compilation_date", "version_is_current", "current_version_start", "retrieved", "source_url", "register_page"}
    for index, raw_title in enumerate(raw["titles"], start=1):
        if not isinstance(raw_title, dict) or set(raw_title) != expected:
            raise MonitorError(f"Baseline title {index} has an invalid shape.")
        title = BaselineTitle(
            register_id=_non_empty(raw_title["register_id"], field=f"title {index} register_id"),
            collection=_non_empty(raw_title["collection"], field=f"title {index} collection"),
            name=_non_empty(raw_title["name"], field=f"title {index} name"),
            compilation_number=_non_empty(raw_title["compilation_number"], field=f"title {index} compilation_number"),
            compilation_date=_iso_date(raw_title["compilation_date"], field=f"title {index} compilation_date") or "",
            version_is_current=raw_title["version_is_current"],
            current_version_start=_iso_date(raw_title["current_version_start"], field=f"title {index} current_version_start", nullable=True),
            source_url=_https_url(raw_title["source_url"], field=f"title {index} source_url"),
            register_page=_https_url(raw_title["register_page"], field=f"title {index} register_page"),
        )
        if not isinstance(title.version_is_current, bool):
            raise MonitorError(f"title {index} version_is_current must be a boolean.")
        if title.register_id in seen:
            raise MonitorError("Baseline source index contains duplicate register IDs.")
        seen.add(title.register_id)
        titles.append(title)
    if not titles:
        raise MonitorError("Baseline source index has no titles.")
    return titles, raw


def _load_observation(
    path: Path | SourceSnapshot, expected_ids: set[str]
) -> dict[str, Any]:
    # We inspect schema_version before loading exact keys since v1 and v2 have distinct required top-level sets.
    pre_raw = load_json(path, label="Register observation")
    if not isinstance(pre_raw, dict) or "schema_version" not in pre_raw:
        raise MonitorError("Register observation must be a JSON object with schema_version.")
    version = pre_raw.get("schema_version")
    if version == "au-tax-register-observation.v1":
        raw = load_json_exact(
            path,
            {"schema_version", "mode", "observed_at", "expected_register_ids", "complete", "observations"},
            label="Register observation",
        )
    elif version == "au-tax-register-observation.v2":
        raw = load_json_exact(
            path,
            {"schema_version", "mode", "observed_at", "scope_id", "expected_register_ids", "complete", "observations"},
            label="Register observation",
        )
        _non_empty(raw["scope_id"], field="scope_id")
    else:
        raise MonitorError("Only au-tax-register-observation.v1 and au-tax-register-observation.v2 in synthetic mode are supported.")

    if raw["mode"] != "synthetic":
        raise MonitorError("Only synthetic mode is supported.")
    _iso_timestamp(raw["observed_at"], field="observed_at")
    if not isinstance(raw["expected_register_ids"], list) or not all(isinstance(item, str) for item in raw["expected_register_ids"]):
        raise MonitorError("Observation expected_register_ids must be a list of strings.")
    cleaned_expected = [_non_empty(item, field="expected_register_ids item") for item in raw["expected_register_ids"]]
    if len(cleaned_expected) != len(set(cleaned_expected)):
        raise MonitorError("Observation expected_register_ids must not contain duplicates.")
    if set(cleaned_expected) != expected_ids:
        raise MonitorError("Observation expected_register_ids must exactly match the baseline scope.")
    if not isinstance(raw["complete"], bool) or not isinstance(raw["observations"], list):
        raise MonitorError("Observation complete/observations fields are invalid.")

    if version == "au-tax-register-observation.v2":
        required = {
            "register_id",
            "collection",
            "state",
            "evidence_id",
            "evidence_url",
            "checked_at",
            "observed_compilation_number",
            "observed_compilation_date",
            "observed_register_document_id",
            "current_version_start",
            "error_category",
        }
    else:
        required = {
            "register_id",
            "collection",
            "state",
            "observed_compilation_number",
            "observed_compilation_date",
            "observed_register_document_id",
            "current_version_start",
            "evidence_url",
            "checked_at",
            "error_category",
        }

    seen: set[str] = set()
    seen_evidence_ids: set[str] = set()
    for index, item in enumerate(raw["observations"], start=1):
        if not isinstance(item, dict) or set(item) != required:
            raise MonitorError(f"Observation item {index} has an invalid shape.")
        register_id = _non_empty(item["register_id"], field=f"observation {index} register_id")
        if register_id not in expected_ids:
            raise MonitorError("Observation contains a register ID outside the baseline scope.")
        if register_id in seen:
            raise MonitorError("Observation contains duplicate register IDs.")
        seen.add(register_id)
        collection = _non_empty(item["collection"], field=f"observation {index} collection")
        item["register_id"] = register_id
        item["collection"] = collection

        if version == "au-tax-register-observation.v2":
            evidence_id = _non_empty(item["evidence_id"], field=f"observation {index} evidence_id")
            if evidence_id in seen_evidence_ids:
                raise MonitorError(f"Observation item {index} has duplicate evidence_id.")
            seen_evidence_ids.add(evidence_id)
            item["evidence_id"] = evidence_id

        if not isinstance(item["state"], str) or item["state"] not in OBSERVATION_STATES:
            raise MonitorError(f"Observation {index} has an unsupported state.")
        state = item["state"]
        state_fields = {
            "UNCHANGED": set(),
            "SUPERSEDED": {
                "observed_compilation_number",
                "observed_compilation_date",
                "observed_register_document_id",
            },
            "CURRENT_NO_PUBLISHED_COMPILATION": {"current_version_start"},
            "NO_LONGER_IN_FORCE": set(),
            "LOOKUP_FAILED": {"error_category"},
        }
        conditional_fields = (
            "observed_register_document_id",
            "observed_compilation_number",
            "observed_compilation_date",
            "current_version_start",
            "error_category",
        )
        for field in conditional_fields:
            if field not in state_fields[state] and item[field] is not None:
                raise MonitorError(f"{state} must have a null {field}.")
        _https_url(item["evidence_url"], field=f"observation {index} evidence_url")
        _iso_timestamp(item["checked_at"], field=f"observation {index} checked_at")
        if state == "SUPERSEDED":
            for field in ("observed_compilation_number", "observed_compilation_date", "observed_register_document_id"):
                _non_empty(item[field], field=f"observation {index} {field}")
            _iso_date(item["observed_compilation_date"], field=f"observation {index} observed_compilation_date")
        elif state == "CURRENT_NO_PUBLISHED_COMPILATION":
            _iso_date(item["current_version_start"], field=f"observation {index} current_version_start")
        elif state == "LOOKUP_FAILED":
            _non_empty(item["error_category"], field=f"observation {index} error_category")
    if raw["complete"] and seen != expected_ids:
        raise MonitorError("A complete observation must cover every expected register ID exactly once.")
    return raw


def _load_mapping(path: Path | SourceSnapshot) -> list[dict[str, str]]:
    raw = load_json_exact(path, {"schema_version", "mapping_version", "entries"}, label="source-to-skill map")
    if raw["schema_version"] != "au-tax-source-skill-map.v1" or not isinstance(raw["entries"], list):
        raise MonitorError("Source-to-skill map has an unsupported schema.")
    required = {"mapping_id", "register_id", "collection", "source_kind", "skill_ref", "skill_path", "owner_role", "review_question"}
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw["entries"], start=1):
        if not isinstance(item, dict) or set(item) != required:
            raise MonitorError(f"Mapping entry {index} has an invalid shape.")
        cleaned = {key: _non_empty(value, field=f"mapping {index} {key}") for key, value in item.items()}
        if cleaned["mapping_id"] in seen:
            raise MonitorError("Source-to-skill map contains duplicate mapping IDs.")
        seen.add(cleaned["mapping_id"])
        result.append(cleaned)
    return result


def _candidate(mapping: dict[str, str]) -> dict[str, str]:
    return {
        "mapping_id": mapping["mapping_id"],
        "skill_ref": mapping["skill_ref"],
        "owner_role": mapping["owner_role"],
        "review_question": mapping["review_question"],
        "mapping_basis": "exact_register_id_and_collection",
    }


def _source_bound_item_id(
    source_digests: dict[str, str], *, change_kind: str, identity: dict[str, str]
) -> str:
    return "impact:" + sha256_json(
        {
            "change_kind": change_kind,
            "identity": identity,
            "sources": source_digests,
        }
    )[:24]


def _run_status_for(items: list[dict[str, Any]]) -> str:
    if any(item["state"] == "BLOCKED" for item in items):
        return "BLOCKED"
    if items:
        return "REVIEW_REQUIRED"
    return "NO_CHANGE_DETECTED"


def _incomplete_scope_item(source_digests: dict[str, str]) -> dict[str, Any]:
    return {
        "item_id": _source_bound_item_id(
            source_digests,
            change_kind="INCOMPLETE_SCOPE",
            identity={"scope": "observation"},
        ),
        "state": "BLOCKED",
        "change_kind": "INCOMPLETE_SCOPE",
        "source": None,
        "impact_candidates": [],
        "mapping_status": "NOT_EVALUATED",
        "limitations": ["The observation scope is incomplete; no unchanged result can be relied on."],
    }


def _missing_observation_item(
    source_digests: dict[str, str], *, title: BaselineTitle
) -> dict[str, Any]:
    return {
        "item_id": _source_bound_item_id(
            source_digests,
            change_kind="MISSING_OBSERVATION",
            identity={
                "register_id": title.register_id,
                "collection": title.collection,
            },
        ),
        "state": "BLOCKED",
        "change_kind": "MISSING_OBSERVATION",
        "source": {"register_id": title.register_id, "collection": title.collection, "title": title.name, "baseline_compilation": {"number": title.compilation_number, "date": title.compilation_date}, "observed_compilation": None, "evidence_url": title.register_page},
        "impact_candidates": [],
        "mapping_status": "NOT_EVALUATED",
        "limitations": ["The expected source was not observed; currency cannot be assessed."],
    }


def _changed_item(
    source_digests: dict[str, str],
    *,
    title: BaselineTitle,
    observed: dict[str, Any],
    state: str,
    candidates: list[dict[str, str]],
) -> dict[str, Any]:
    change_kind = (
        observed["state"] if title.version_is_current else "BASELINE_NOT_CURRENT"
    )
    observed_compilation = None
    if observed["state"] == "SUPERSEDED":
        observed_compilation = {"number": observed["observed_compilation_number"], "date": observed["observed_compilation_date"], "document_id": observed["observed_register_document_id"]}
    return {
        "item_id": _source_bound_item_id(
            source_digests,
            change_kind=change_kind,
            identity={
                "register_id": title.register_id,
                "collection": title.collection,
                "observed_state": observed["state"],
            },
        ),
        "state": state,
        "change_kind": change_kind,
        "source": {
            "register_id": title.register_id,
            "collection": title.collection,
            "title": title.name,
            "baseline_compilation": {"number": title.compilation_number, "date": title.compilation_date},
            "observed_compilation": observed_compilation,
            "evidence_url": observed["evidence_url"],
        },
        "impact_candidates": candidates,
        "mapping_status": "MAPPED" if candidates else "UNMAPPED_SOURCE",
        "limitations": [
            "A source-version state does not establish the legal effect of a change.",
            "This item is not tax advice and does not update any workflow.",
        ],
    }


def compare(*, baseline_path: Path, observation_path: Path, mapping_path: Path) -> dict[str, Any]:
    # Each source is read once. Parsing and every provenance identifier use the
    # same immutable bytes, so an in-flight replacement cannot make a queue
    # describe one version while naming another version's digest.
    baseline_source = SourceSnapshot.capture(
        baseline_path, label="baseline source index"
    )
    titles, baseline_raw = _load_baseline(baseline_source)
    expected_ids = {title.register_id for title in titles}
    observation_source = SourceSnapshot.capture(
        observation_path, label="Register observation"
    )
    observation = _load_observation(observation_source, expected_ids)
    mapping_source = SourceSnapshot.capture(
        mapping_path, label="source-to-skill map"
    )
    mappings = _load_mapping(mapping_source)
    source_digests = {
        "baseline": baseline_source.sha256,
        "observation": observation_source.sha256,
        "mapping": mapping_source.sha256,
    }
    observations = {item["register_id"]: item for item in observation["observations"]}
    items: list[dict[str, Any]] = []
    if not observation["complete"]:
        items.append(_incomplete_scope_item(source_digests))
    for title in sorted(titles, key=lambda value: (value.register_id, value.collection)):
        observed = observations.get(title.register_id)
        if observed is None:
            items.append(_missing_observation_item(source_digests, title=title))
            continue
        if observed["collection"] != title.collection:
            raise MonitorError(f"Observation collection does not match baseline for {title.register_id}.")
        if observed["state"] == "UNCHANGED" and title.version_is_current:
            continue
        state = "BLOCKED" if observed["state"] in {"CURRENT_NO_PUBLISHED_COMPILATION", "LOOKUP_FAILED"} or not title.version_is_current else "OPEN"
        applicable = [_candidate(item) for item in mappings if item["register_id"] == title.register_id and item["collection"] == title.collection]
        items.append(
            _changed_item(
                source_digests,
                title=title,
                observed=observed,
                state=state,
                candidates=applicable,
            )
        )
    items.sort(key=lambda item: (item["state"] != "BLOCKED", item["change_kind"], item["item_id"]))
    run_status = _run_status_for(items)
    run_id = "sha256:" + sha256_json(source_digests)
    queue = {
        "schema_version": "au-tax-impact-queue.v2",
        "run_id": run_id,
        "mode": "synthetic",
        "run_status": run_status,
        "source_digests": {
            name: "sha256:" + digest for name, digest in source_digests.items()
        },
        "baseline": {"id": "sha256:" + baseline_source.sha256, "retrieved": baseline_raw["retrieved"], "source": baseline_raw["source"]},
        "observation": {"id": "sha256:" + observation_source.sha256, "observed_at": observation["observed_at"], "complete": observation["complete"]},
        "items": items,
    }
    queue["queue_digest"] = "sha256:" + sha256_json(queue)
    return queue


def render_markdown(queue: dict[str, Any]) -> str:
    lines = [
        "# AU Tax Change Impact Queue",
        "",
        f"**Run status: {queue['run_status']}**",
        "",
        "This is a synthetic metadata-review queue. It does not establish current law, legal effect, tax advice, a workflow update, or a client action.",
        "",
        f"- Baseline source: {safe_markdown(queue['baseline']['source'])}",
        f"- Baseline retrieved: {queue['baseline']['retrieved']}",
        f"- Observation complete: {queue['observation']['complete']}",
        "",
        "## Open items",
        "",
    ]
    if not queue["items"]:
        lines.append("No changed items were identified within the complete synthetic observation scope. This is not a statement about live law.")
    for item in queue["items"]:
        lines += [f"### {item['state']}: {item['change_kind']}", ""]
        source = item["source"]
        if source is not None:
            lines += [
                f"- Source: {safe_markdown(source['title'])} (`{safe_markdown(source['register_id'])}`, {safe_markdown(source['collection'])})",
                f"- Baseline compilation: {safe_markdown(source['baseline_compilation']['number'])} dated {source['baseline_compilation']['date']}",
                f"- Evidence: {safe_markdown(source['evidence_url'])}",
            ]
            if source["observed_compilation"]:
                observed = source["observed_compilation"]
                lines.append(f"- Observed compilation: {safe_markdown(observed['number'])} dated {observed['date']} (`{safe_markdown(observed['document_id'])}`)")
        lines.append(f"- Mapping status: {item['mapping_status']}")
        for candidate in item["impact_candidates"]:
            lines.append(f"- Review candidate (`{safe_markdown(candidate['skill_ref'])}`): {safe_markdown(candidate['review_question'])}")
        for limitation in item["limitations"]:
            lines.append(f"- Limitation: {safe_markdown(limitation)}")
        lines.append("")
    return "\n".join(lines)


from .persist import write_queue_files


def write_queue(queue: dict[str, Any], output_dir: Path) -> dict[str, Path]:
    """Stage and commit the JSON/Markdown pair, restoring the old pair on failure."""
    return write_queue_files(
        json.dumps(queue, indent=2, sort_keys=True) + "\n",
        render_markdown(queue),
        output_dir,
    )


def _validate_compilation(
    value: Any, *, field: str, observed: bool
) -> None:
    expected = (
        OBSERVED_COMPILATION_FIELDS if observed else BASELINE_COMPILATION_FIELDS
    )
    if not isinstance(value, dict) or set(value) != expected:
        raise MonitorError(f"{field} has an invalid shape.")
    _non_empty(value["number"], field=f"{field} number")
    _iso_date(value["date"], field=f"{field} date")
    if observed:
        _non_empty(value["document_id"], field=f"{field} document_id")


def _validate_source(value: Any, *, item_index: int, change_kind: str) -> None:
    field = f"Impact queue item {item_index} source"
    if change_kind == "INCOMPLETE_SCOPE":
        if value is not None:
            raise MonitorError(f"{field} must be null for INCOMPLETE_SCOPE.")
        return
    if not isinstance(value, dict) or set(value) != SOURCE_FIELDS:
        raise MonitorError(f"{field} has an invalid shape.")
    for name in ("register_id", "collection", "title"):
        _non_empty(value[name], field=f"{field} {name}")
    _https_url(value["evidence_url"], field=f"{field} evidence_url")
    _validate_compilation(
        value["baseline_compilation"],
        field=f"{field} baseline_compilation",
        observed=False,
    )
    observed_compilation = value["observed_compilation"]
    if observed_compilation is not None:
        _validate_compilation(
            observed_compilation,
            field=f"{field} observed_compilation",
            observed=True,
        )
    if change_kind == "SUPERSEDED" and observed_compilation is None:
        raise MonitorError(f"{field} must include observed_compilation for SUPERSEDED.")
    if change_kind not in {"SUPERSEDED", "BASELINE_NOT_CURRENT"} and observed_compilation is not None:
        raise MonitorError(
            f"{field} observed_compilation is unsupported for {change_kind}."
        )


def _validate_candidates(value: Any, *, item_index: int) -> int:
    if not isinstance(value, list):
        raise MonitorError(
            f"Impact queue item {item_index} impact_candidates must be a list."
        )
    mapping_ids: set[str] = set()
    for candidate_index, candidate in enumerate(value, start=1):
        field = f"Impact queue item {item_index} candidate {candidate_index}"
        if not isinstance(candidate, dict) or set(candidate) != CANDIDATE_FIELDS:
            raise MonitorError(f"{field} has an invalid shape.")
        for name in (
            "mapping_id",
            "skill_ref",
            "owner_role",
            "review_question",
        ):
            _non_empty(candidate[name], field=f"{field} {name}")
        if candidate["mapping_basis"] != "exact_register_id_and_collection":
            raise MonitorError(f"{field} has an unsupported mapping_basis.")
        mapping_id = candidate["mapping_id"]
        if mapping_id in mapping_ids:
            raise MonitorError(
                f"Impact queue item {item_index} contains duplicate candidate mapping IDs."
            )
        mapping_ids.add(mapping_id)
    return len(value)


def _validate_queue_evidence(queue: dict[str, Any]) -> tuple[set[str], str]:
    if queue["schema_version"] != "au-tax-impact-queue.v2":
        raise MonitorError("Queue or decision schema version is unsupported.")
    if queue["mode"] != "synthetic":
        raise MonitorError("Only synthetic impact queues can be validated.")
    _sha256_id(queue["run_id"], field="Impact queue run_id")
    _sha256_id(queue["queue_digest"], field="Impact queue queue_digest")
    if not isinstance(queue["run_status"], str) or queue["run_status"] not in {
        "BLOCKED",
        "REVIEW_REQUIRED",
        "NO_CHANGE_DETECTED",
    }:
        raise MonitorError("Impact queue has an unsupported run_status.")

    source_digests = queue["source_digests"]
    if not isinstance(source_digests, dict) or set(source_digests) != SOURCE_DIGEST_FIELDS:
        raise MonitorError("Impact queue source_digests has an invalid shape.")
    for name in sorted(SOURCE_DIGEST_FIELDS):
        _sha256_id(
            source_digests[name], field=f"Impact queue source_digests {name}"
        )

    baseline = queue["baseline"]
    if not isinstance(baseline, dict) or set(baseline) != BASELINE_QUEUE_FIELDS:
        raise MonitorError("Impact queue baseline has an invalid shape.")
    _sha256_id(baseline["id"], field="Impact queue baseline id")
    _iso_date(baseline["retrieved"], field="Impact queue baseline retrieved")
    _non_empty(baseline["source"], field="Impact queue baseline source")
    if baseline["id"] != source_digests["baseline"]:
        raise MonitorError(
            "Impact queue baseline id must equal the baseline source digest."
        )

    observation = queue["observation"]
    if (
        not isinstance(observation, dict)
        or set(observation) != OBSERVATION_QUEUE_FIELDS
    ):
        raise MonitorError("Impact queue observation has an invalid shape.")
    _sha256_id(observation["id"], field="Impact queue observation id")
    observed_at = _iso_timestamp(
        observation["observed_at"], field="impact queue observed_at"
    )
    if not isinstance(observation["complete"], bool):
        raise MonitorError("Impact queue observation complete must be a boolean.")
    if observation["id"] != source_digests["observation"]:
        raise MonitorError(
            "Impact queue observation id must equal the observation source digest."
        )

    if not isinstance(queue["items"], list):
        raise MonitorError("Impact queue items must be a list.")
    open_items: set[str] = set()
    queue_item_ids: set[str] = set()
    incomplete_scope_count = 0
    missing_observation_count = 0
    for index, item in enumerate(queue["items"], start=1):
        if not isinstance(item, dict) or set(item) != ITEM_FIELDS:
            raise MonitorError(f"Impact queue item {index} has an invalid shape.")
        item_id = _non_empty(
            item["item_id"], field=f"impact queue item {index} item_id"
        )
        if item_id in queue_item_ids:
            raise MonitorError("Impact queue contains duplicate item IDs.")
        queue_item_ids.add(item_id)
        if not isinstance(item["state"], str) or item["state"] not in {
            "OPEN",
            "BLOCKED",
        }:
            raise MonitorError(f"Impact queue item {index} has an unsupported state.")
        if (
            not isinstance(item["change_kind"], str)
            or item["change_kind"] not in CHANGE_KINDS
        ):
            raise MonitorError(
                f"Impact queue item {index} has an unsupported change_kind."
            )
        change_kind = item["change_kind"]
        expected_state, supported_mapping_statuses = CHANGE_KIND_MATRIX[change_kind]
        if change_kind == "INCOMPLETE_SCOPE":
            incomplete_scope_count += 1
        elif change_kind == "MISSING_OBSERVATION":
            missing_observation_count += 1
        _validate_source(item["source"], item_index=index, change_kind=change_kind)
        candidate_count = _validate_candidates(
            item["impact_candidates"], item_index=index
        )
        if (
            not isinstance(item["mapping_status"], str)
            or item["mapping_status"] not in MAPPING_STATUSES
        ):
            raise MonitorError(
                f"Impact queue item {index} has an unsupported mapping_status."
            )
        if item["mapping_status"] == "MAPPED" and candidate_count == 0:
            raise MonitorError(
                f"Impact queue item {index} cannot be MAPPED without a candidate."
            )
        if item["mapping_status"] != "MAPPED" and candidate_count:
            raise MonitorError(
                f"Impact queue item {index} with candidates must be MAPPED."
            )
        if item["state"] != expected_state:
            raise MonitorError(
                f"Impact queue item {index} change_kind must remain {expected_state}."
            )
        if item["mapping_status"] not in supported_mapping_statuses:
            raise MonitorError(
                f"Impact queue item {index} mapping_status is unsupported for its change_kind."
            )
        limitations = item["limitations"]
        if not isinstance(limitations, list):
            raise MonitorError(
                f"Impact queue item {index} limitations must be a list."
            )
        for limitation_index, limitation in enumerate(limitations, start=1):
            _non_empty(
                limitation,
                field=(
                    f"impact queue item {index} limitation {limitation_index}"
                ),
            )
        if item["state"] == "OPEN":
            open_items.add(item_id)

    expected_incomplete_scope_count = 0 if observation["complete"] else 1
    if (
        incomplete_scope_count != expected_incomplete_scope_count
        or (observation["complete"] and missing_observation_count)
    ):
        raise MonitorError(
            "Impact queue observation complete is inconsistent with its scope items."
        )

    # Recomputed on purpose: validation derives the writer's rule and never
    # trusts the stored summary state.
    if queue["run_status"] != _run_status_for(queue["items"]):
        raise MonitorError("Impact queue run_status does not match its items.")
    return open_items, observed_at


def validate_review(*, queue_path: Path, decision_path: Path) -> dict[str, Any]:
    queue = load_json_exact(queue_path, QUEUE_FIELDS, label="impact queue")
    decision = load_json_exact(
        decision_path, DECISION_FIELDS, label="technical review decision"
    )
    if decision["schema_version"] != "au-tax-technical-review.v2":
        raise MonitorError("Queue or decision schema version is unsupported.")

    # Fully validate every queue field before hashing it. Exact schemas keep
    # arbitrary nested data out of the canonicalisation step and ensure the
    # digest has one supported interpretation.
    open_items, observed_at = _validate_queue_evidence(queue)

    raw_source_digests = {
        name: queue["source_digests"][name].removeprefix("sha256:")
        for name in sorted(SOURCE_DIGEST_FIELDS)
    }
    expected_run_id = "sha256:" + sha256_json(raw_source_digests)
    if queue["run_id"] != expected_run_id:
        raise MonitorError(
            "Impact queue run_id does not match its exact source digests."
        )
    queue_payload = {
        key: value for key, value in queue.items() if key != "queue_digest"
    }
    expected_queue_digest = "sha256:" + sha256_json(queue_payload)
    if queue["queue_digest"] != expected_queue_digest:
        raise MonitorError(
            "Impact queue queue_digest does not match its complete review evidence."
        )

    _sha256_id(decision["run_id"], field="Technical review run_id")
    _sha256_id(
        decision["queue_digest"], field="Technical review queue_digest"
    )
    if queue["run_id"] != decision["run_id"]:
        raise MonitorError(
            "Technical review decision must refer to the exact queue run_id."
        )
    if queue["queue_digest"] != decision["queue_digest"]:
        raise MonitorError(
            "Technical review decision must refer to the exact queue_digest."
        )
    _non_empty(decision["reviewer_ref"], field="technical review reviewer_ref")
    reviewed_at = _iso_timestamp(
        decision["reviewed_at"], field="technical review reviewed_at"
    )
    # Same parser as the validation above: a second fromisoformat call here
    # would reintroduce the interpreter-dependent grammar it just removed.
    reviewed_value = _parse_timestamp(
        reviewed_at, field="technical review reviewed_at"
    )
    observed_value = _parse_timestamp(observed_at, field="impact queue observed_at")
    try:
        review_predates_observation = reviewed_value < observed_value
    except TypeError as exc:
        raise MonitorError(
            "Review and observation timestamps must use compatible timezone qualifiers."
        ) from exc
    if review_predates_observation:
        raise MonitorError(
            "Technical review reviewed_at cannot predate the queue observation."
        )

    if not isinstance(decision["decisions"], list) or not decision["decisions"]:
        raise MonitorError(
            "Technical review decision must include at least one decision."
        )
    seen: set[str] = set()
    for item in decision["decisions"]:
        if not isinstance(item, dict) or set(item) != DECISION_ENTRY_FIELDS:
            raise MonitorError(
                "Each technical decision must contain exactly item_id, decision, rationale, and evidence_note."
            )
        item_id = _non_empty(item["item_id"], field="technical decision item_id")
        if item_id not in open_items or item_id in seen:
            raise MonitorError(
                "Technical decision references an unknown, blocked, or duplicate item."
            )
        if (
            not isinstance(item["decision"], str)
            or item["decision"] not in ALLOWED_DECISIONS
        ):
            raise MonitorError("Technical decision is not allowlisted.")
        _non_empty(item["rationale"], field="technical decision rationale")
        _non_empty(
            item["evidence_note"], field="technical decision evidence_note"
        )
        seen.add(item_id)
    undecided_count = len(open_items - seen)
    status = (
        "DECISION_RECORDED"
        if undecided_count == 0
        else "PARTIAL_DECISION_RECORDED"
    )
    return {
        "schema_version": "au-tax-review-decision-validation.v2",
        "run_id": queue["run_id"],
        "queue_digest": queue["queue_digest"],
        "mode": "synthetic",
        "status": status,
        "decision_count": len(seen),
        "undecided_count": undecided_count,
        "limitation": "Validation records structurally valid human decisions only; it does not establish legal effect, change a skill, notify anyone, or produce tax advice.",
    }
