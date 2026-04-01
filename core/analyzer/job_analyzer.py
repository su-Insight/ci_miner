import json
import logging
import re
from collections import OrderedDict
from datetime import datetime, timedelta

import requests

from core.github_service import get_runs_past_period
from utils.github_api import get_request, github_request
from utils.settings import load_config, resolve_github_token

from .run_analyzer import (
    ensure_utc_datetime,
    get_cached_runs,
    get_repository_workflows,
    normalize_cached_runs,
    normalize_run,
    parse_github_datetime,
    restore_cached_runs,
)
from ..parser.error_parser import classify
from ..patterns.step_patterns import classify_step


def download_job_log(repository_name, job_id, token=None):
    if not token:
        return None

    url = f"https://api.github.com/repos/{repository_name}/actions/jobs/{job_id}/logs"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        response = requests.get(url, headers=headers, allow_redirects=True, timeout=30)
        if response.status_code == 200:
            return response.text
    except Exception as exc:
        logging.warning("Failed to download job log for %s: %s", job_id, exc)
    return None


def get_previous_group_job(repository_name, job, token=None):
    run_id = job["run_id"]
    job_name = job["name"]

    url = f"https://api.github.com/repos/{repository_name}/actions/runs/{run_id}/jobs"
    response = get_request(url, token, params={"per_page": 100, "filter": "all"})
    jobs = response.get("jobs", []) if isinstance(response, dict) else []

    # Compare only jobs with the same name inside the current run.
    same_jobs = [
        candidate
        for candidate in jobs
        if candidate.get("name") == job_name and (candidate.get("started_at") or candidate.get("created_at"))
    ]

    if not same_jobs:
        return None

    # Sort newest first so the previous comparable job is the second item.
    sorted_jobs = sorted(
        same_jobs,
        key=lambda item: item.get("started_at") or item.get("created_at"),
        reverse=True,
    )
    return sorted_jobs[1] if len(sorted_jobs) > 1 else None


def get_previous_matrix_job(runs, repository_name, job, token=None):
    group_job = get_previous_group_job(repository_name, job, token)
    if group_job:
        return group_job

    run_id = job["run_id"]
    job_name = job["name"]
    url = f"https://api.github.com/repos/{repository_name}/actions/runs/{run_id}"
    current_run = github_request(url, token, "GET")
    workflow_id = current_run["workflow_id"]

    started = False
    for run in runs.values():
        if run["run_id"] == str(run_id):
            started = True
            continue
        if not started:
            continue
        if workflow_id != run["workflow_id"]:
            continue

        url = f"https://api.github.com/repos/{repository_name}/actions/runs/{run['run_id']}/jobs"
        response = get_request(url, token, params={"per_page": 100, "filter": "all"})
        jobs = response.get("jobs", []) if isinstance(response, dict) else []
        if not jobs:
            return None
        for candidate in jobs:
            if candidate["name"] == job_name:
                current_job = candidate
                current_job["job_id"] = candidate["id"]
                return current_job
        return None
    return None


def remove_timestamp(log_text: str):
    common_time_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*"
    auto_build_pattern = r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[autobuild\]\s*"
    log_text = re.sub(common_time_pattern, "", log_text or "", flags=re.MULTILINE)
    log_text = re.sub(auto_build_pattern, "", log_text, flags=re.MULTILINE)
    return log_text


def get_OS(log_text: str):
    pattern = r"##\[group\]Operating System\s*(.*?)(\n|$)"
    match = re.search(pattern, log_text or "")
    return match.group(1).strip() if match else None


def get_runner_image_version(log_text: str):
    pattern = r"##\[group\]Runner Image\s*.*?(?:\n|$)\s*Version: (.*?)(?:\n|$)"
    match = re.search(pattern, log_text or "")
    return match.group(1) if match else None


def get_actions(log_text: str):
    actions = {}
    pattern = r"Download action repository '(.*?)' \(SHA:(.*?)\)"
    matches = re.findall(pattern, log_text or "")
    for action, action_hash in matches:
        actions[action] = action_hash

    pattern = r"##\[group\]Download immutable action package '(.*?)'\s*Version.*?(?:\n|$)\s*Digest.*?(?:\n|$)\s*Source commit SHA: (.*?)(?:\n|$)"
    matches = re.findall(pattern, log_text or "")
    for action, action_hash in matches:
        actions[action] = action_hash

    return actions


def count_warnings_simple(log_text: str) -> int:
    patterns = [
        r"warning:",
        r"warn:",
        r"\[warn\]",
        r"\[warning\]",
        r"deprecated",
    ]

    count = 0
    for pattern in patterns:
        count += len(re.findall(pattern, log_text or "", re.IGNORECASE))
    return count


def find_different_actions(action1, action2):
    common_keys = set(action1.keys()) & set(action2.keys())
    return not all(action1[key] == action2[key] for key in common_keys)


def detect_unverified_actions(action_list, token):
    third_actions = []
    for action in action_list:
        base_action = action.split("@")[0].strip()
        url = f"https://api.github.com/repos/{base_action}"
        action_repo = github_request(url, token, "GET")
        if not action_repo or "organization" not in action_repo:
            third_actions.append(base_action)
    return len(third_actions) != 0


def extract_job_time(date):
    return date.weekday(), date.hour


def enrich_job_with_run_context(repository_name, job, token):
    enriched = dict(job)
    enriched["job_id"] = enriched.get("job_id") or enriched.get("id")
    run_id = enriched.get("run_id")

    if not run_id:
        return enriched
    if all(
        key in enriched
        for key in ["head_sha", "head_branch", "workflow_id", "run_event", "run_status", "run_conclusion"]
    ):
        return enriched

    url = f"https://api.github.com/repos/{repository_name}/actions/runs/{run_id}"
    run = github_request(url, token, "GET")
    if not isinstance(run, dict):
        return enriched

    enriched["head_sha"] = run.get("head_sha")
    enriched["head_branch"] = run.get("head_branch")
    enriched["workflow_id"] = run.get("workflow_id")
    enriched["run_event"] = run.get("event")
    enriched["run_status"] = run.get("status")
    enriched["run_conclusion"] = run.get("conclusion")
    enriched["run_created_at"] = run.get("created_at")
    enriched["run_updated_at"] = run.get("updated_at")
    enriched["run_attempt"] = run.get("run_attempt")
    enriched["actor"] = run.get("actor")
    enriched["actor_login"] = (run.get("actor") or {}).get("login")
    enriched["actor_id"] = (run.get("actor") or {}).get("id")
    return enriched


def build_job_run_context(repository_name, job, token, config):
    owner, repo = repository_name.split("/")
    lookback_days = (
        config.get("job", {})
        .get("previous_same_branch_build", {})
        .get("search_range_days", 30)
    )

    run_date = job.get("run_created_at") or job.get("created_at")
    if not run_date:
        return OrderedDict()

    end_run_date = ensure_utc_datetime(run_date)
    start_run_date = end_run_date - timedelta(days=lookback_days)

    # Reuse the run cache so previous-build comparison stays in the same time window
    # as the run analyzer.
    cache_runs = normalize_cached_runs(get_cached_runs(config, owner, repo))
    cached_dates = [run.get("created_at") for run in cache_runs.values() if run.get("created_at")]

    if cached_dates:
        cached_min = ensure_utc_datetime(min(cached_dates))
        cached_max = ensure_utc_datetime(max(cached_dates))

        if cached_min > start_run_date:
            earlier_runs = get_runs_past_period(owner, repo, token, start_run_date, cached_min)
            cache_runs.update(normalize_cached_runs(earlier_runs))

        if cached_max < end_run_date:
            later_runs = get_runs_past_period(owner, repo, token, cached_max, end_run_date)
            cache_runs.update(normalize_cached_runs(later_runs))
    else:
        fetched_runs = get_runs_past_period(owner, repo, token, start_run_date, end_run_date)
        cache_runs.update(normalize_cached_runs(fetched_runs))

    current_run_id = str(job.get("run_id") or "")
    if current_run_id and current_run_id not in cache_runs:
        current_run = {
            "run_id": job.get("run_id"),
            "id": job.get("run_id"),
            "head_sha": job.get("head_sha"),
            "head_branch": job.get("head_branch"),
            "workflow_id": job.get("workflow_id"),
            "created_at": job.get("run_created_at"),
            "updated_at": job.get("run_updated_at"),
            "conclusion": job.get("run_conclusion"),
            "status": job.get("run_status"),
            "event": job.get("run_event"),
            "run_attempt": job.get("run_attempt"),
            "actor": job.get("actor"),
            "actor_id": job.get("actor_id"),
            "actor_login": job.get("actor_login"),
        }
        cache_runs[current_run_id] = normalize_run(current_run)

    ordered_runs = OrderedDict(
        sorted(
            cache_runs.items(),
            key=lambda item: item[1].get("created_at") or datetime.min,
            reverse=True,
        )
    )
    restore_cached_runs(config, owner, repo, ordered_runs)
    return ordered_runs


def analyze_all(repository_name, job, token=None):
    # Keep a stable schema even when some remote data is missing.
    results = {"error_reason": None}
    config = load_config()

    owner, repo = repository_name.split("/")
    runs = build_job_run_context(repository_name, job, token, config)
    if not runs:
        return results

    job_id = job["job_id"]
    url = f"https://api.github.com/repos/{owner}/{repo}"
    repository_api = github_request(url, token, "GET")
    if not repository_api:
        return results
    default_branch = repository_api["default_branch"]

    log_content = download_job_log(repository_name, job_id, token)
    if not log_content:
        return results

    results["error_reason"] = classify(log_content)

    cleaned_log = remove_timestamp(log_content)
    previous_job = get_previous_matrix_job(runs, repository_name, job, token=token)
    cleaned_previous_log = None
    if previous_job:
        previous_job_id = str(previous_job["job_id"])
        previous_log_content = download_job_log(repository_name, previous_job_id, token)
        if previous_log_content:
            cleaned_previous_log = remove_timestamp(previous_log_content)

    results["is_runner_changed"] = (
        get_runner_image_version(cleaned_log) != get_runner_image_version(cleaned_previous_log)
    ) if cleaned_previous_log else None

    actions = get_actions(cleaned_log)
    results["is_action_changed"] = (
        find_different_actions(actions, get_actions(cleaned_previous_log))
        if cleaned_previous_log else None
    )
    results["log_warn_nums"] = count_warnings_simple(cleaned_log)

    operation_system = get_OS(cleaned_log)
    results["operation_system"] = operation_system

    if job.get("steps"):
        job_steps = job["steps"]
        if isinstance(job_steps, str):
            job_steps = re.sub(r"'", "\"", job_steps)
            try:
                job_steps = json.loads(job_steps)
            except json.decoder.JSONDecodeError:
                job_steps = None
        failure_steps = [step["name"] for step in (job_steps or []) if step.get("conclusion") == "failure"]
        results["gh_first_error_step"] = classify_step(failure_steps[0]) if failure_steps else None
    else:
        results["gh_first_error_step"] = None

    results["runner_type"] = (
        "gitHub-hosted runner"
        if operation_system in ["Ubuntu", "Windows", "macOS"]
        else "self-hosted runner"
    )
    results["use_unverified_action"] = detect_unverified_actions(actions, token)

    action_names = [action.split("@")[0].strip() for action in actions]
    results["is_artifact_share"] = (
        "actions/upload-artifact" in action_names or "actions/download-artifact" in action_names
    )
    results["is_action_cache"] = "actions/cache" in action_names

    started_at = job.get("started_at")
    if isinstance(started_at, str):
        started_at = parse_github_datetime(started_at)
    if started_at:
        results["day_of_week"], results["time_of_day"] = extract_job_time(started_at)
    else:
        results["day_of_week"], results["time_of_day"] = (None, None)

    results["is_master"] = job.get("head_branch") == default_branch
    return results


def fetch_job_by_id(repository_name, job_id, token=None, config_file="config.yaml"):
    config = load_config(config_file)
    resolved_token = resolve_github_token(token, config)
    job_id_text = str(job_id)
    url = f"https://api.github.com/repos/{repository_name}/actions/jobs/{job_id_text}"
    job = get_request(url, resolved_token)
    if not isinstance(job, dict) or str(job.get("id")) != job_id_text:
        raise ValueError(f"job_id must exist in repository. Missing in {repository_name}: {job_id_text}")
    return enrich_job_with_run_context(repository_name, job, resolved_token)


def fetch_jobs_by_ids(repository_name, job_ids, token=None, config_file="config.yaml"):
    return [fetch_job_by_id(repository_name, job_id, token=token, config_file=config_file) for job_id in job_ids]


def compile_job_info(repo_full_name, job, workflow=None, token=None):
    started_at = parse_github_datetime(job.get("started_at"))
    completed_at = parse_github_datetime(job.get("completed_at"))
    duration = None
    if started_at and completed_at:
        duration = (completed_at - started_at).total_seconds()

    results = {
        "repo": repo_full_name,
        "job_id": job.get("job_id") or job.get("id"),
        "run_id": job.get("run_id"),
        "workflow_id": job.get("workflow_id"),
        "workflow_name": job.get("workflow_name") or (workflow.get("name") if workflow else None),
        "job_name": job.get("name"),
        "job_status": job.get("status"),
        "job_conclusion": job.get("conclusion"),
        "job_started_at": started_at,
        "job_completed_at": completed_at,
        "job_duration": duration,
        "branch": job.get("head_branch"),
        "commit_sha": job.get("head_sha"),
        "workflow_event_trigger": job.get("run_event"),
        "run_status": job.get("run_status"),
        "run_conclusion": job.get("run_conclusion"),
        "actor_name": (job.get("actor") or {}).get("login", "unknown"),
        "steps": job.get("steps", []),
        "step_count": len(job.get("steps", []) or []),
        "labels": job.get("labels", []),
        "runner_name": job.get("runner_name"),
        "runner_group_name": job.get("runner_group_name"),
        "error_reason": None,
    }
    results.update(analyze_all(repo_full_name, job, token))
    return results


def extract_repository_jobs(repository, jobs, token=None, config_file="config.yaml"):
    config = load_config(config_file)
    resolved_token = resolve_github_token(token, config)

    print(f"[job] {repository}: preparing workflow metadata for {len(jobs)} job(s)...")
    workflows = get_repository_workflows(repository, resolved_token)
    job_infos = []

    for job in jobs:
        workflow = next(
            (item for item in workflows if str(item.get("id")) == str(job.get("workflow_id"))),
            None,
        )
        job_infos.append(compile_job_info(repository, job, workflow=workflow, token=resolved_token))

    print(f"[job] {repository}: completed job feature extraction.")
    return job_infos
