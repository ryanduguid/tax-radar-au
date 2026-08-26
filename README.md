# Tax Radar AU

> **Archived and superseded:** the maintained `tax_radar_au` change-review queue now lives in [au-tax-legislation-corpus](https://github.com/ryanduguid/au-tax-legislation-corpus/blob/main/RADAR.md); this repository remains only for its historical v0.1.x releases and tags.

[![tests](https://github.com/ryanduguid/tax-radar-au/actions/workflows/ci.yml/badge.svg)](https://github.com/ryanduguid/tax-radar-au/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-4F485E.svg?labelColor=04001F)](LICENSE) [![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-5C2D91.svg?logo=python&logoColor=white&labelColor=04001F)](https://www.python.org/downloads/)

A **provenance-first change-review queue**, not a tax-answering system or an automatic skill updater.

> Compatibility: the Python distribution, import package, CLI command, wheel names and existing releases remain `au-tax-change-impact-monitor` / `au_tax_change_impact_monitor`.

The first version compares fabricated source-index metadata with a fabricated Register-observation contract. It keeps important states distinct (`SUPERSEDED`, `CURRENT_NO_PUBLISHED_COMPILATION`, `NO_LONGER_IN_FORCE`, and `LOOKUP_FAILED`), then maps only exact register ID + collection pairs to a potential workflow-review question.

```text
Synthetic source index + synthetic Register observation + exact source-to-skill map
                                      |
                                      v
                          Change classification and scope gate
                                      |
                                      v
                         Potential-impact technical-review queue
                                      |
                                      v
                         Human technical-tax decision, outside this tool
```

## Demo

```bash
python -m pip install -e ".[dev]"

au-tax-change-impact-monitor compare \
  --baseline au_tax_change_impact_monitor/samples/baseline/sample-sources.json \
  --observation au_tax_change_impact_monitor/samples/observations/sample-register-observation.json \
  --map au_tax_change_impact_monitor/samples/mappings/sample-source-skill-map.json \
  --out build/demo
```

The sample fixtures ship inside the package, so a plain `pip install` can run the same demo from any directory; `python -c "from au_tax_change_impact_monitor.util import sample_path; print(sample_path())"` prints their installed location. Every input option accepts any readable path, and `--out` is created relative to the current directory.

The example creates one `SUPERSEDED` source item mapped to a BAS-review question. It deliberately does not infer the legal effect of the change, update a skill, or send a notification.

The output directory contains deterministic `impact-queue.json` and `impact-queue.md` files. Both are staged before publication; if committing the second file fails, the writer restores the complete previous pair rather than leaving two runs mixed together. Input digests and identifiers come from the same immutable byte snapshots that are parsed. The v2 JSON queue records the baseline, observation and mapping snapshot digests in `source_digests`. Its source-derived `run_id` identifies that three-file run, while `queue_digest` identifies the complete canonical queue evidence: schema version, run summary, source digests, baseline, observation and every item field. The digest is calculated before the `queue_digest` field itself is added.

An item is `OPEN` when it needs human technical review, carrying `change_kind` `SUPERSEDED` or `NO_LONGER_IN_FORCE`. An item is `BLOCKED` for any of five reasons, each named by its own `change_kind`:

| `change_kind` | Cause |
| --- | --- |
| `INCOMPLETE_SCOPE` | The observation is marked incomplete, so no “unchanged” result can be relied on. |
| `MISSING_OBSERVATION` | A baseline title was not observed at all. |
| `LOOKUP_FAILED` | The observation records a failed lookup for that title. |
| `CURRENT_NO_PUBLISHED_COMPILATION` | The title is current but has no published compilation to compare. |
| `BASELINE_NOT_CURRENT` | The baseline row is itself marked `version_is_current: false`, so it is a stale index entry and cannot support a currency conclusion. |

`compare` exits 0 for `REVIEW_REQUIRED` and `NO_CHANGE_DETECTED`, and 2 when the run status is `BLOCKED`. Rejected input also exits 2, in one of two shapes: a malformed command line is rejected by argparse, which prints its own usage block, and everything the monitor itself rejects prints a single `au-tax-change-impact-monitor: blocked: ...` line on stderr.

If stdout cannot encode a character in the `--out` path (a redirected stream on Windows uses the ANSI code page, not UTF-8), that one line is printed backslash-escaped and a `au-tax-change-impact-monitor: note: ...` line on stderr says so. The exit status still reflects the run, and the files on disk carry the real path.

If stdout cannot be written at all (a reader that closed the pipe, or a redirect to a handle this process cannot write to), the summary is dropped, a `au-tax-change-impact-monitor: note: ...` line on stderr says so, and the exit status is still the one the run decided. A redirected stdout is buffered, so that failure otherwise surfaces only when the interpreter flushes on the way out, where it is reported as `Exception ignored ...` and exits 120 on a run whose queue files are complete. The CLI closes stdout instead, which is what keeps the exit status the run's own.

```bash
au-tax-change-impact-monitor validate-review \
  --queue build/demo/impact-queue.json \
  --decision path/to/a-human-technical-review.json
```

Only `AWAIT_PRIMARY_TEXT`, `NO_WORKFLOW_CHANGE`, `UPDATE_CANDIDATE`, and `ESCALATE_TECHNICAL_REVIEW` are accepted. A v2 technical-review decision must copy both `run_id` and `queue_digest` from the exact queue reviewed; the packaged `sample-technical-review.json` demonstrates the complete decision shape. Validation enforces exact queue, nested evidence and decision schemas before recalculating the digest. It rejects a changed, missing or additional evidence field even when `run_id` is left unchanged. The validation receipt records both identifiers.

Validation reports `PARTIAL_DECISION_RECORDED` while any open item remains undecided; it checks structure and matching queue only and does not certify the review, edit a skill, or establish a legal conclusion. Observation and review timestamps require an explicit UTC offset (or `Z`) so audit ordering is unambiguous. Decisions written for the v1 queue schema do not carry a complete-evidence digest and are intentionally not accepted as v2 decisions; review the new queue and record a new decision instead of carrying an earlier decision forward.

The accepted timestamp grammar is exactly `YYYY-MM-DDThh:mm:ss[.ffffff][Z|+hh:mm|-hh:mm]`, with `t` or a single space allowed in place of `T`. It is pinned by a pattern rather than handed to `datetime.fromisoformat`, whose grammar differs between Python 3.10 and 3.11+, so the same stored artefact validates the same way on every supported interpreter. The ISO basic form (`20260808T000000Z`), week and ordinal dates, a bare-hour offset (`+00`), a lowercase `z`, a date with no clock, and any other date/time separator are all refused. Dates in the baseline and observation are `YYYY-MM-DD` on the same basis.

## Strict scope

- Inputs are metadata only. No legislation EPUB, HTML, PDF, section JSONL, rate, or source text is read or stored.
- The synthetic demo never performs network I/O or Register scraping.
- A source is mapped by exact `(register_id, collection)` only. An unmapped change remains visible as `UNMAPPED_SOURCE`; it is never silently dismissed.
- `UNCHANGED` is valid only inside a complete observation scope. A partial/failed observation cannot produce a “no change” conclusion.
- Every artefact carries `mode: synthetic` to prevent the demo being misrepresented as a live legislative monitor.

## Relationship to existing work

The intended future source is a reviewed, deliberately versioned observation output from [au-tax-legislation-corpus](https://github.com/ryanduguid/au-tax-legislation-corpus), the Commonwealth tax legislation corpus builder. That corpus’s distinction between a superseded compilation, a current version with no published compilation, a title no longer in force, and a failed lookup is preserved here. This project is not a replacement corpus builder and must not treat derived corpus material as authorised legal text.

## Development

```bash
pytest
python -m build
```


MIT licensed.
