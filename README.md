# CI Miner

CI Miner is a CSV-driven feature extraction tool for GitHub Actions repositories.
It supports two execution modes:

- `--run`: one output row per workflow run
- `--job`: one output row per workflow job

The entrypoint is [CIMiner.py](/C:/Users/17554/PycharmProjects/Paper/ci_miner/CIMiner.py).
It only parses CLI arguments and delegates execution to [core/main.py](/C:/Users/17554/PycharmProjects/Paper/ci_miner/core/main.py).

## Architecture

```text
ci_miner/
|-- CIMiner.py
|-- config.yaml
|-- runs_features.csv
|-- jobs_features.csv
|-- core/
|   |-- main.py
|   |-- csv_store.py
|   |-- github_service.py
|   |-- analyzer/
|   |   |-- run_analyzer.py
|   |   |-- job_analyzer.py
|   |   |-- code_analyzer.py
|   |-- parser/
|   |   |-- test_parser.py
|   |   |-- error_parser.py
|   |-- patterns/
|   |   |-- commit_patterns.py
|   |   |-- step_patterns.py
|   |   |-- test_patterns.py
|-- utils/
|   |-- github_api.py
|   |-- settings.py
|   |-- subprocess_stream.py
|   |-- scc/
```

## Execution Model

The active execution path is sequential. The tool does not use multithreading or multiprocessing.

Run mode:

1. Parse CLI arguments in `CIMiner.py`
2. Load config and token in `core/main.py`
3. Load `runs_features.csv` or a custom input CSV
4. Select run IDs for the requested repository or repositories
5. Fetch run payloads from GitHub
6. Build repository-level run context and cache windows
7. Extract run, workflow, PR, job-summary, test, and code-level features
8. Merge results back into the same CSV file

Job mode:

1. Parse CLI arguments in `CIMiner.py`
2. Load config and token in `core/main.py`
3. Load `jobs_features.csv` or a custom input CSV
4. Select job IDs for the requested repository or repositories
5. Fetch job payloads from GitHub
6. Build a recent run cache window for previous-build comparison
7. Extract job, runner, action, log, and error features
8. Merge results back into the same CSV file

## CLI Usage

Exactly one mode must be selected:

- `--run`
- `--job`

Common rules:

- `--repo` is required
- `--repo` can be repeated
- `--input` is optional
- `--overwrite` ignores the `collected` status
- `--token` overrides `GITHUB_TOKEN` in `config.yaml`
- `--run_id` is only valid with `--run`
- `--job_id` is only valid with `--job`
- When `--run_id` or `--job_id` is used, exactly one `--repo` must be provided

### Run Mode

Process all pending run IDs for one repository:

```bash
python CIMiner.py --run --repo apache/accumulo
```

Process all pending run IDs for multiple repositories:

```bash
python CIMiner.py --run --repo apache/accumulo --repo apache/kafka
```

Process a specific run:

```bash
python CIMiner.py --run --repo apache/accumulo --run_id 123456
```

Process multiple specific runs:

```bash
python CIMiner.py --run --repo apache/accumulo --run_id 123456 --run_id 123457
```

Default run-level CSV:

```text
runs_features.csv
```

### Job Mode

Process all pending job IDs for one repository:

```bash
python CIMiner.py --job --repo apache/accumulo
```

Process all pending job IDs for multiple repositories:

```bash
python CIMiner.py --job --repo apache/accumulo --repo apache/kafka
```

Process a specific job:

```bash
python CIMiner.py --job --repo apache/accumulo --job_id 987654
```

Process multiple specific jobs:

```bash
python CIMiner.py --job --repo apache/accumulo --job_id 987654 --job_id 987655
```

Default job-level CSV:

```text
jobs_features.csv
```

### Optional Arguments

```bash
python CIMiner.py --run --repo apache/accumulo --input custom_runs.csv --overwrite
python CIMiner.py --job --repo apache/accumulo --input custom_jobs.csv --token <TOKEN>
python CIMiner.py --run --repo apache/accumulo --config custom_config.yaml
```

## Input CSV Rules

Run mode requires:

- `repository`
- `run_id`
- `collected`

Job mode requires:

- `repository`
- `job_id`
- `collected`

Behavior:

- If the CSV does not exist, the tool creates an in-memory table and writes results back to the requested path
- `collected = 0` means pending
- `collected = 1` means already extracted
- `--overwrite` ignores the `collected` filter
- Duplicate IDs inside the same repository are warned about and resolved before extraction

## Config

The active config file is [config.yaml](/C:/Users/17554/PycharmProjects/Paper/ci_miner/config.yaml).

Token resolution order:

1. `--token`
2. `GITHUB_TOKEN` in `config.yaml`

Important config sections:

- `paths.clone_path`: local repository clone root for code-level analysis
- `paths.cache_path`: cache root for repository run history
- `paths.scc_path`: path to the `scc` binary folder
- `fetch_all`: enables every feature group
- `fetch_sloc`: enables code size features
- `fetch_commit_details`: enables code churn and AST-related features
- `fetch_pull_request_details`: enables PR-related features
- `fetch_job_details`: enables run-level job summary features
- `fetch_test_parsing_results`: enables test result parsing from logs
- `job.previous_same_branch_build.search_range_days`: how far back the job analyzer looks when searching for the previous comparable build
- `committer.committer_fail_rate.*`: history windows for committer trust and repository failure metrics

## Feature Model

The project operates at three analysis levels:

- Code level: source changes, AST diffs, churn, entropy, hotspot files, and repository code size
- Run level: workflow runs, PR context, run metadata, repository context, and job summaries
- Job level: individual jobs, steps, logs, runner changes, action changes, and error classification

Code-level features are stored inside the run-level CSV because they are attached to a specific run.

## Run-Level Field Reference

The final run CSV contains three control columns and the extracted feature columns below.

### Control Columns

| Field | Meaning |
|---|---|
| `repository` | Repository in `owner/repo` format. |
| `run_id` | GitHub Actions workflow run ID. |
| `collected` | Extraction status. `0` means pending, `1` means extracted. |

### Core Run Metadata

| Field | Meaning |
|---|---|
| `workflow_id` | GitHub workflow identifier associated with the run. |
| `actor_name` | Login name of the user or bot that triggered the run. |
| `branch` | Head branch used by the run. |
| `commit_sha` | Head commit SHA of the run. |
| `languages` | Dominant repository language from the GitHub languages API. |
| `status` | Run execution status, such as `queued`, `in_progress`, or `completed`. |
| `workflow_event_trigger` | Event that triggered the run, such as `push` or `pull_request`. |
| `conclusion` | Final run conclusion, such as `success`, `failure`, or `cancelled`. |
| `created_at` | Run creation timestamp. |
| `updated_at` | Last run update timestamp. |
| `build_duration` | Run duration in seconds. |
| `total_builds` | Extraction-order counter within the current repository batch. |
| `gh_first_commit_created_at` | Timestamp of the head commit attached to the run. |
| `workflow_name` | Human-readable workflow name. |
| `trigger_event` | Legacy research-oriented trigger classification kept by the original analyzer. |

### Repository and Workflow Context

| Field | Meaning |
|---|---|
| `build_language` | Build ecosystem inferred from repository files, such as Maven or Gradle. |
| `dependencies_count` | Estimated number of dependencies found in dependency manifests. |
| `workflow_size` | Approximate workflow file size, measured as line count. |
| `test_frameworks` | Detected test frameworks for the repository. |

### Test Outcome and Job Summary

| Field | Meaning |
|---|---|
| `tests_passed` | Parsed count of passed tests. |
| `tests_failed` | Parsed count of failed tests. |
| `tests_skipped` | Parsed count of skipped tests. |
| `tests_total` | Parsed total number of tests. |
| `gh_job_id` | List of job IDs under the run. |
| `total_jobs` | Number of jobs under the run. |
| `job_details` | Serialized job summary payload for the run. |
| `tests_ran` | Boolean-style indicator showing whether test-related jobs or steps were detected. |

### Pull Request Context

| Field | Meaning |
|---|---|
| `gh_is_pr` | Whether the run is associated with a pull request. |
| `gh_num_pr_comments` | Number of comments on the associated pull request. |
| `git_merged_with` | Merge SHA associated with the linked pull request. |
| `pr_number` | Pull request number identified by the original analyzer. |
| `gh_pr_description_complexity` | Pull request description complexity metric from the original analyzer. |

### Code Size and Churn

| Field | Meaning |
|---|---|
| `gh_sloc` | Source lines of code at the analyzed commit. |
| `gh_test_lines_per_kloc` | Test lines per thousand source lines of code. |
| `sloc_initial` | Initial source lines of code measured by the original analyzer. |
| `test_lines_initial` | Initial test line count measured by the original analyzer. |
| `gh_files_added` | Number of added files. |
| `gh_files_deleted` | Number of deleted files. |
| `gh_files_modified` | Number of modified files. |
| `gh_lines_added` | Number of added lines. |
| `gh_lines_deleted` | Number of deleted lines. |
| `gh_src_churn` | Source-code churn derived from line additions and deletions. |
| `gh_tests_added` | Number of added test lines. |
| `gh_tests_deleted` | Number of deleted test lines. |
| `gh_test_churn` | Test-code churn derived from test line additions and deletions. |
| `gh_src_files` | Number of source files touched. |
| `gh_doc_files` | Number of documentation files touched. |
| `gh_other_files` | Number of non-source and non-doc files touched. |
| `gh_config_files` | Number of configuration files touched. |
| `dockerfile_changed` | Whether a Dockerfile changed in the analyzed diff. |
| `docker_compose_changed` | Whether a Docker Compose file changed in the analyzed diff. |

### Repository Activity and Risk Signals

| Field | Meaning |
|---|---|
| `gh_commits_on_files_touched` | Historical number of commits touching the files changed by this run. |
| `git_num_committers` | Number of unique committers in the configured history window. |
| `git_commits` | Number of commits in the configured history window or comparison window. |
| `gh_team_size_last_3_month` | Team size estimate in the recent activity window. |
| `repo_fail_rate_history` | Repository failure rate over the configured long history window. |
| `gh_committer_bayesian_trust_score_history` | Bayesian trust score for the triggering committer over the long history window. |
| `repo_fail_rate_recent` | Repository failure rate over the configured recent history window. |
| `gh_committer_bayesian_trust_score_recent` | Bayesian trust score for the triggering committer over the recent history window. |
| `git_committer_repo_exp` | Repository-specific experience score of the triggering committer. |
| `is_core_member` | Whether the actor is considered a core contributor by the original analyzer. |
| `concurrent_jobs` | Number of concurrent jobs considered by the original analyzer. |
| `committer_cross_project_exp` | Cross-project experience score of the triggering committer. |
| `gh_committer_first_build` | Whether this is the committer's first observed build in the repository. |
| `prev_build_same_files_touched` | Whether the previous build touched overlapping files. |
| `repo_ci_config_churn_nums` | Number of CI configuration changes observed in the configured comparison window. |
| `git_same_committer` | Whether the previous relevant build was triggered by the same committer. |
| `gh_previous_build_result` | Result of the previous comparable build. |
| `commit_message_issue_ref` | Whether the commit message references an issue. |
| `git_commit_attention` | Commit-intent signal derived from commit message patterns. |
| `external_github_resource` | Whether the run references external GitHub resources. |
| `base_sha` | Base SHA used for PR or comparison-based analysis. |

### Structural and AST Features

| Field | Meaning |
|---|---|
| `gh_files_entropy` | Entropy score of touched files, used as a change dispersion signal. |
| `gh_files_type_modified` | Diversity of modified file types. |
| `gh_cross_module_changes` | Whether the change spans multiple modules or subsystems. |
| `gh_hotspot_files_touched` | Whether historically hot files were touched. |
| `ast_class_added` | Number of Java classes added. |
| `ast_class_deleted` | Number of Java classes deleted. |
| `ast_class_modified` | Number of Java classes modified. |
| `ast_class_changed` | Total number of Java class-level changes. |
| `ast_met_added` | Number of methods added. |
| `ast_met_deleted` | Number of methods deleted. |
| `ast_met_changed` | Number of methods changed. |
| `ast_met_sig_modified` | Number of method signature changes. |
| `ast_met_body_modified` | Number of method body changes. |
| `ast_field_added` | Number of fields added. |
| `ast_field_deleted` | Number of fields deleted. |
| `ast_field_changed` | Number of field-level changes. |
| `ast_import_added` | Number of imports added. |
| `ast_import_deleted` | Number of imports deleted. |
| `ast_import_changed` | Number of import-level changes. |
| `src_ast_diff` | Aggregate AST change score for source files. |
| `test_ast_diff` | Aggregate AST change score for test files. |
| `gh_dependencies_churn` | Dependency-level churn measured from AST and build file changes. |

## Job-Level Field Reference

The final job CSV contains three control columns and the extracted feature columns below.

### Control Columns

| Field | Meaning |
|---|---|
| `repository` | Repository in `owner/repo` format. |
| `job_id` | GitHub Actions job ID. |
| `collected` | Extraction status. `0` means pending, `1` means extracted. |

### Job Metadata

| Field | Meaning |
|---|---|
| `run_id` | Parent workflow run ID. |
| `workflow_id` | Workflow ID of the parent run. |
| `workflow_name` | Workflow name of the parent run. |
| `job_name` | Human-readable job name. |
| `job_status` | Job execution status. |
| `job_conclusion` | Final job conclusion. |
| `job_started_at` | Job start timestamp. |
| `job_completed_at` | Job completion timestamp. |
| `job_duration` | Job duration in seconds. |
| `branch` | Branch of the parent run. |
| `commit_sha` | Head commit SHA of the parent run. |
| `workflow_event_trigger` | Event type of the parent run. |
| `run_status` | Parent run status. |
| `run_conclusion` | Parent run conclusion. |
| `actor_name` | Login name of the actor who triggered the parent run. |
| `steps` | Raw job step payload. |
| `step_count` | Number of steps in the job. |
| `labels` | Runner labels assigned to the job. |
| `runner_name` | Name of the runner that executed the job. |
| `runner_group_name` | Runner group name, when available. |

### Job Diagnostics

| Field | Meaning |
|---|---|
| `error_reason` | Classified failure reason extracted from the job log. |
| `is_runner_changed` | Whether the runner image changed compared with the previous comparable job. |
| `is_action_changed` | Whether referenced actions changed compared with the previous comparable job. |
| `log_warn_nums` | Number of warning-like patterns found in the job log. |
| `operation_system` | Operating system parsed from the job log. |
| `gh_first_error_step` | First failed step mapped to a normalized step category. |
| `runner_type` | Whether the runner is GitHub-hosted or self-hosted. |
| `use_unverified_action` | Whether the job appears to use third-party or unverified actions. |
| `is_artifact_share` | Whether the job uses artifact upload or download actions. |
| `is_action_cache` | Whether the job uses the GitHub Actions cache action. |
| `day_of_week` | Day of week derived from the job start time. |
| `time_of_day` | Hour-of-day derived from the job start time. |
| `is_master` | Whether the job ran on the repository default branch. |

## Notes

- Cached run history is used in both run and job mode to support historical comparisons and previous-build features.
