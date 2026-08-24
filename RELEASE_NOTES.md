# v0.1.3

Changes since `v0.1.0`:

- bind observations to the exact validated Register identifier and enforce the supported source-state contracts;
- bind queue and review provenance to source bytes and publish related artefacts as one staged set;
- bind review decisions to the exact queue evidence they were made on;
- define and validate the `au-tax-register-observation.v2` contract;
- preserve the intended CLI exit status when standard output has already closed;
- give accurate read-failure messages, deduplicate status and change-kind logic, and contain `--out` writes to their resolved destination with ruff and mypy added to CI; and
- adopt the shared release-policy workflow, publish the attested distribution to PyPI via trusted publishing, add editorconfig/CODEOWNERS/mailmap/job timeouts/Dependabot pacing, and refresh documentation so every claim matches the repository under the Tax Radar AU name.

The package remains a synthetic, local change-review demonstration. It does not supply tax answers, update skills automatically or process client data.
