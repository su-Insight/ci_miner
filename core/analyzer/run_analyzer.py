import base64
import json
import logging
import os
import platform
import re
import shutil
import stat
import subprocess
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from threading import Lock

from core.github_service import get_all_collaborators, get_large_action_repos, get_runs_past_period, get_user_commits
from core.parser.test_parser import parse_test_results, identify_build_language, \
    identify_test_framework_and_count_dependencies
from core.patterns.commit_patterns import classify_commit
from utils.github_api import get_request
from utils.settings import get_path_config, feature_enabled, resolve_github_token, load_config




USER_EXPERIENCE_LOCK = Lock()


def ensure_utc_datetime(value):
    if not value:
        return value
    if isinstance(value, str):
        normalized = value.strip()
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"Unsupported datetime value: {value!r}")

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_github_datetime(value):
    if not value:
        return value
    return ensure_utc_datetime(value)


def normalize_run(run):
    if not isinstance(run, dict):
        return run

    normalized = dict(run)
    normalized["run_id"] = normalized.get("run_id") or normalized.get("id")
    normalized["created_at"] = parse_github_datetime(normalized.get("created_at"))
    normalized["updated_at"] = parse_github_datetime(normalized.get("updated_at"))
    normalized["actor_login"] = (normalized.get("actor") or {}).get("login")
    normalized["actor_id"] = (normalized.get("actor") or {}).get("id")
    return normalized


def serialize_for_cache(obj):
    if isinstance(obj, list):
        return [serialize_for_cache(item) for item in obj]
    if isinstance(obj, dict):
        return {key: serialize_for_cache(value) for key, value in obj.items()}
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def normalize_cached_runs(cache_runs):
    normalized = {}
    for run_id, run in (cache_runs or {}).items():
        if not isinstance(run, dict):
            continue
        normalized_run = normalize_run(run)
        normalized[str(normalized_run["run_id"])] = normalized_run
    return normalized


def get_cache_file_path(config, owner, repo):
    cache_root = get_path_config(config, "cache_path", os.path.join("data", ".cache"))
    os.makedirs(cache_root, exist_ok=True)
    return os.path.join(cache_root, f"{owner}_{repo}.json")


def get_cached_runs(config, owner, repo):
    cache_file_path = get_cache_file_path(config, owner, repo)
    if not os.path.exists(cache_file_path):
        return {}
    try:
        with open(cache_file_path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        logging.warning(f"Failed to read cache {cache_file_path}: {exc}")
        return {}


def restore_cached_runs(config, owner, repo, cache_runs):
    cache_file_path = get_cache_file_path(config, owner, repo)
    serializable_cache = {
        str(run_id): serialize_for_cache(run)
        for run_id, run in (cache_runs or {}).items()
    }
    with open(cache_file_path, "w", encoding="utf-8") as handle:
        json.dump(serializable_cache, handle, ensure_ascii=False, indent=2)


def build_run_context(repository_name, builds, token, config):
    if not builds:
        return OrderedDict()

    owner, repo = repository_name.split("/")
    history_weeks = (
        config.get("committer", {})
        .get("committer_fail_rate", {})
        .get("history_time_range_weeks", 0)
    )

    selected_runs = [normalize_run(run) for run in builds]
    selected_runs.sort(key=lambda item: item.get("created_at") or datetime.min, reverse=True)

    latest_selected = max(selected_runs, key=lambda item: item.get("created_at") or datetime.min)
    earliest_selected = min(selected_runs, key=lambda item: item.get("created_at") or datetime.max)

    start_run_date = earliest_selected["created_at"]
    if start_run_date:
        start_run_date = ensure_utc_datetime(start_run_date) - timedelta(weeks=history_weeks)

    end_run_date = latest_selected["created_at"]
    if end_run_date:
        end_run_date = ensure_utc_datetime(end_run_date)

    # Extend the cached run window so history-based metrics can be computed
    # around the selected runs without refetching the full repository history.
    cache_runs = normalize_cached_runs(get_cached_runs(config, owner, repo))
    cached_dates = [run.get("created_at") for run in cache_runs.values() if run.get("created_at")]

    if start_run_date and end_run_date:
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

    for run in selected_runs:
        cache_runs[str(run["run_id"])] = run

    ordered_runs = OrderedDict(
        sorted(
            cache_runs.items(),
            key=lambda item: item[1].get("created_at") or datetime.min,
            reverse=True,
        )
    )
    restore_cached_runs(config, owner, repo, ordered_runs)
    return ordered_runs


def get_repository_workflows(repo_full_name, token):
    url = f"https://api.github.com/repos/{repo_full_name}/actions/workflows?per_page=100"
    response = get_request(url, token)
    if not response or "workflows" not in response:
        return []
    return response["workflows"]


def get_repository_languages(repo_full_name, token):
    url = f"https://api.github.com/repos/{repo_full_name}/languages"
    languages_data = get_request(url, token)
    if languages_data:
        total_bytes = sum(languages_data.values()) or 1
        return max(languages_data, key=lambda lang: languages_data[lang] / total_bytes)
    return "No language found"


def get_github_repo_files(owner, repo, token=None):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/"
    response = get_request(url, token)
    if not isinstance(response, list):
        return []
    return [file["name"] for file in response if file.get("type") == "file"]


def get_jobs_payload_for_run(config, repo_full_name, run_id, token):
    url = f"https://api.github.com/repos/{repo_full_name}/actions/runs/{run_id}/jobs?per_page=100"
    response = get_request(url, token)
    return response.get("jobs", []) if response else []


def get_jobs_for_run(config, repo_full_name, run_id, token):
    jobs = get_jobs_payload_for_run(config, repo_full_name, run_id, token)

    jobs_ids = []
    job_details = []
    for job in jobs or []:
        jobs_ids.append(job.get("id") or job.get("job_id"))
        job_details.append(
            {
                "job_name": job.get("name"),
                "job_start": job.get("started_at"),
                "job_end": job.get("completed_at"),
                "job_duration": None,
                "job_result": job.get("conclusion"),
                "steps": [
                    {
                        "step_name": step.get("name"),
                        "step_conclusion": step.get("conclusion"),
                        "step_start": step.get("started_at"),
                        "step_end": step.get("completed_at"),
                        "step_duration": None,
                    }
                    for step in (job.get("steps") or [])
                ],
            }
        )
    return jobs_ids, job_details, len(job_details)


def get_github_actions_job_log(repo_full_name, run_id, token):
    url = f"https://api.github.com/repos/{repo_full_name}/actions/runs/{run_id}/logs"
    return get_request(url, token)


def count_lines_in_workflow(repo_full_name, workflow_path, commit_sha, token):
    if not workflow_path:
        return None

    url = f"https://api.github.com/repos/{repo_full_name}/contents/{workflow_path}"
    response = get_request(url, token, params={"ref": commit_sha})
    if response and "content" in response:
        try:
            content = base64.b64decode(response["content"]).decode("utf-8")
            return len(content.splitlines())
        except Exception:
            return None
    return None


def fetch_pull_request_details(repo_full_name, commit_sha, token):
    pr_search_url = f"https://api.github.com/repos/{repo_full_name}/commits/{commit_sha}/pulls"
    pr_response = get_request(pr_search_url, token)
    if pr_response and isinstance(pr_response, list):
        pr_info = pr_response[0] if pr_response else None
        if pr_info:
            pr_number = pr_info.get("number", 0)
            pr_details = get_request(
                f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}",
                token,
            )
            return {
                "gh_is_pr": True,
                "gh_num_pr_comments": pr_details.get("comments", 0) if pr_details else 0,
                "git_merged_with": pr_info.get("merge_commit_sha"),
                "gh_pr_description_complexity": len((pr_info.get("body") or "").split()),
            }

    return {
        "gh_is_pr": False,
        "gh_num_pr_comments": 0,
        "git_merged_with": None,
        "gh_pr_description_complexity": 0,
    }


def get_default_code_features():
    return {
        "gh_sloc": None,
        "gh_test_lines_per_kloc": None,
        "sloc_initial": None,
        "test_lines_initial": None,
        "gh_files_added": 0,
        "gh_files_deleted": 0,
        "gh_files_modified": 0,
        "gh_lines_added": 0,
        "gh_lines_deleted": 0,
        "gh_src_churn": 0,
        "gh_tests_added": 0,
        "gh_tests_deleted": 0,
        "gh_test_churn": 0,
        "gh_src_files": 0,
        "gh_doc_files": 0,
        "gh_other_files": 0,
        "gh_commits_on_files_touched": 0,
        "dockerfile_changed": 0,
        "docker_compose_changed": 0,
        "git_num_committers": 0,
        "git_commits": 0,
        "gh_team_size_last_3_month": 0,
        "gh_config_files": 0,
        "gh_files_entropy": 0,
        "gh_files_type_modified": 0,
        "gh_cross_module_changes": 0,
        "gh_hotspot_files_touched": 0,
        "ast_class_added": 0,
        "ast_class_deleted": 0,
        "ast_class_modified": 0,
        "ast_class_changed": 0,
        "ast_met_added": 0,
        "ast_met_deleted": 0,
        "ast_met_changed": 0,
        "ast_met_sig_modified": 0,
        "ast_met_body_modified": 0,
        "ast_field_added": 0,
        "ast_field_deleted": 0,
        "ast_field_changed": 0,
        "ast_import_added": 0,
        "ast_import_deleted": 0,
        "ast_import_changed": 0,
        "src_ast_diff": 0,
        "test_ast_diff": 0,
        "gh_dependencies_churn": 0,
        "commit_message_issue_ref": False,
        "git_commit_attention": [],
        "external_github_resource": False,
        "git_same_committer": None,
        "gh_previous_build_result": None,
        "repo_fail_rate_history": 0,
        "gh_committer_bayesian_trust_score_history": 0,
        "repo_fail_rate_recent": 0,
        "gh_committer_bayesian_trust_score_recent": 0,
        "git_committer_repo_exp": 0,
        "is_core_member": None,
        "concurrent_jobs": 0,
        "committer_cross_project_exp": None,
        "gh_committer_first_build": None,
        "prev_build_same_files_touched": None,
        "repo_ci_config_churn_nums": 0,
        "base_sha": None,
        "pr_number": None,
        "gh_pr_description_complexity": 0,
        "trigger_event": None,
    }


def bayesian_trust_score(failures, total_commits, C=5, m=0.1):
    if total_commits == 0:
        return 0.0

    success_rate = (total_commits - failures) / total_commits
    return (total_commits * success_rate + C * (1 - m)) / (total_commits + C)


def get_unique_committers(local_repo_path, run_date, days=None):
    unique_committers = set()
    committers = []

    if isinstance(run_date, str):
        run_date = datetime.fromisoformat(run_date.replace("Z", "+00:00"))

    since_date = run_date - timedelta(days=days) if days is not None else None

    try:
        cmd = [
            "git", "-C", local_repo_path, "log",
            "--until", (run_date - timedelta(seconds=1)).isoformat(),
            "--format=%an <%ae>",
        ]
        if since_date:
            cmd.extend(["--since", since_date.isoformat()])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15 * 60,
            encoding="utf-8",
            errors="ignore",
        )
        if result.stdout:
            committers = result.stdout.strip().splitlines()
            unique_committers.update(committers)
    except Exception as exc:
        logging.error("Unexpected error fetching committers: %s", exc)

    return unique_committers, committers


def is_same_committer_as_last(run, recent_runs):
    run_id = str(run["run_id"])
    ids = list(recent_runs.keys())
    for index, cur_run_id in enumerate(ids):
        recent_run = recent_runs[cur_run_id]
        if str(recent_run["run_id"]) == run_id:
            if index + 1 >= len(ids):
                return None, None
            next_id = ids[index + 1]
            return recent_runs[next_id].get("actor_id") == run.get("actor_id"), recent_runs[next_id].get("conclusion")
    return None, None


def get_committer_fail_rate(runs, date, user_id, weeks):
    date = ensure_utc_datetime(date)
    start_date = date - timedelta(weeks=weeks)

    filtered_runs = []
    user_runs = []
    for run in runs.values():
        created_at = run.get("created_at")
        run_created = ensure_utc_datetime(created_at) if created_at else None
        if run_created and start_date <= run_created <= date:
            filtered_runs.append(run)
            if user_id == run.get("actor_id"):
                user_runs.append(run)

    failure_nums = sum(1 for run in filtered_runs if run.get("conclusion") == "failure")
    user_failure_nums = sum(1 for run in user_runs if run.get("conclusion") == "failure")
    repo_fail_rate = failure_nums / len(runs) if runs else 0
    return repo_fail_rate, bayesian_trust_score(user_failure_nums, len(user_runs), C=5, m=repo_fail_rate)


def get_runs_in_range(runs, start_date, end_date):
    start_date = ensure_utc_datetime(start_date)
    end_date = ensure_utc_datetime(end_date)
    filtered_runs = []
    for run in runs.values():
        created_at = run.get("created_at")
        run_date = ensure_utc_datetime(created_at) if created_at else None
        if run_date and start_date <= run_date <= end_date:
            filtered_runs.append(run)
    return filtered_runs


def calculate_hotspot_files(runs, run, days, min_total=5, failure_rate_threshold=20):
    files_count = {}
    if runs:
        end_date = run["created_at"]
        end_date = ensure_utc_datetime(end_date)
        start_date = end_date - timedelta(days=days)
        runs = get_runs_in_range(runs, start_date, end_date)
    else:
        return []

    for run_item in runs:
        conclusion = run_item.get("conclusion")
        if not conclusion:
            continue
        for commit in run_item.get("commits", []):
            if not commit:
                continue
            for file in commit.get("files", []):
                filename = file.get("filename")
                if not filename:
                    continue
                if filename not in files_count:
                    files_count[filename] = {
                        "total": 1,
                        "failures": 1 if conclusion == "failure" else 0,
                    }
                else:
                    files_count[filename]["total"] += 1
                    if conclusion == "failure":
                        files_count[filename]["failures"] += 1

    hotspot_files = []
    for filename, stats in files_count.items():
        total = stats["total"]
        failures = stats["failures"]
        failure_rate = (failures / total) * 100 if total > 0 else 0
        if total >= min_total and failure_rate >= failure_rate_threshold:
            hotspot_files.append({
                "filename": filename,
                "total": total,
                "failures": failures,
                "failure_rate": round(failure_rate, 2),
                "success_rate": round(100 - failure_rate, 2),
            })
    hotspot_files.sort(key=lambda item: item["failure_rate"], reverse=True)
    return [item["filename"] for item in hotspot_files]


def get_committer_recent_commits(run, repository_name, weeks):
    owner, repo = repository_name.split("/")
    user_name = run.get("actor_login")
    date = str(run.get("created_at"))
    commits = get_user_commits(owner, repo, user_name, token=run.get("_resolved_token"), date=date, weeks=weeks)
    return len(commits)


def has_cross_project_experience(run, config):
    user_name = run.get("actor_login")
    if not user_name:
        return None

    experience_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "user_experience.json")
    min_runs = config.get("committer", {}).get("cross_project_experience", {}).get("small_repository_run_nums", 0)
    repo_nums = config.get("committer", {}).get("cross_project_experience", {}).get("large_repository_nums", 0)

    with USER_EXPERIENCE_LOCK:
        if os.path.exists(experience_file):
            try:
                with open(experience_file, "r", encoding="utf-8") as handle:
                    users_experience = json.load(handle)
            except Exception:
                users_experience = {}
        else:
            users_experience = {}

        if user_name in users_experience:
            return users_experience[user_name]

    user_large_repos = get_large_action_repos(user_name, run.get("_resolved_token"), min_runs)
    user_experience = len(user_large_repos) >= repo_nums

    with USER_EXPERIENCE_LOCK:
        users_experience[user_name] = user_experience
        with open(experience_file, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(users_experience, indent=4))

    return user_experience


def get_committers(run, repository_name, local_repo_path, days=None):
    unique_committers, committers = get_unique_committers(local_repo_path, run["created_at"], days)
    committers_count = {}
    for committer in committers:
        committers_count[committer] = committers_count.get(committer, 0) + 1
    core_committers = [user for user, count in committers_count.items() if count > 5]
    return unique_committers, core_committers


def is_modified_files_between_commits(run, repository_name, previous_run, token):
    owner, repo = repository_name.split("/")
    current_data = get_request(f"https://api.github.com/repos/{owner}/{repo}/commits/{run['head_sha']}", token)
    previous_data = get_request(f"https://api.github.com/repos/{owner}/{repo}/commits/{previous_run['head_sha']}", token)
    if not current_data or not previous_data:
        return None
    current_files = [file["filename"] for file in current_data.get("files", [])]
    previous_files = [file["filename"] for file in previous_data.get("files", [])]
    return len(list(set(current_files) & set(previous_files))) == 0


def parse_commits_with_branches(output, separator="%x1f"):
    if not output:
        return []

    commits = []
    current_commit = None
    for line in output.strip().split("\n"):
        if not line:
            if current_commit:
                commits.append(current_commit)
            current_commit = None
            continue

        if separator in line:
            parts = line.split(separator)
            if len(parts) >= 6:
                sha, author, email, date, message, refs = parts
                current_commit = {
                    "sha": sha,
                    "author": author,
                    "email": email,
                    "date": date,
                    "message": message,
                    "files": [],
                }
        elif current_commit and "\t" in line:
            file_info = line.split("\t")
            if len(file_info) >= 2:
                current_commit["files"].append({
                    "status": file_info[0].strip(),
                    "filename": file_info[1].strip(),
                })

    if current_commit:
        commits.append(current_commit)
    return commits


def get_commits_in_range_cli(repo_path, base_sha, head_sha):
    result = subprocess.run(
        [
            "git", "-C", repo_path, "log",
            "--pretty=format:%H|%an|%ae|%ad|%s|%D",
            "--date=short",
            "--name-status",
            f"{base_sha}..{head_sha}",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode == 0:
        return parse_commits_with_branches(result.stdout)
    return []


def count_concurrent_jobs(run, repository_name, token):
    jobs = get_jobs_payload_for_run({}, repository_name, run["run_id"], token)
    return len(jobs) if jobs else 1


def calculate_description_complexity(pr_details):
    if not pr_details:
        return 0

    title = pr_details.get("title", "")
    body = pr_details.get("body", "")
    title_words = len(title.split())
    body_words = len(body.split()) if body else 0
    has_code_blocks = "```" in body if body else False
    has_issue_references = ("#" in body or "fixes" in body.lower() or "closes" in body.lower()) if body else False
    has_uris = ("http://" in body or "https://" in body) if body else False
    return title_words * 2 + body_words + (50 if has_code_blocks else 0) + (30 if has_issue_references else 0) + (20 if has_uris else 0)


def calculate_commit_metrics(run):
    metrics = {"commit_message_issue_ref": False}
    if "commits" not in run:
        return None

    file_changed = 0
    commit_types = set()
    for commit in run["commits"]:
        if not commit:
            continue
        if re.search(r"#\d+", commit.get("message", "")):
            metrics["commit_message_issue_ref"] = True
        if commit.get("files"):
            file_changed += 1
        commit_types.add(classify_commit(commit.get("message", "")))

    commit_types.discard(None)
    metrics["gh_commits_on_files_touched"] = file_changed
    metrics["git_commit_attention"] = list(commit_types)
    return metrics


def contains_external_resources(workflow):
    if not workflow:
        return False
    pattern = r"^\s*uses:\s*([^/\s]+/[^/\s]+)/\.github/workflows/[^\s@]+@([^\s]+)$"
    url_pattern = r"https?://(api\.)?github\.com"
    git_pattern = r"git@github\.com:[^\n+]\.git"
    return bool(re.search(git_pattern, workflow) or re.search(url_pattern, workflow) or re.search(pattern, workflow))


def get_ci_config_churn_nums(run, repository_name, weeks, repo_path):
    end_date = run["created_at"] if isinstance(run["created_at"], datetime) else datetime.fromisoformat(str(run["created_at"]).replace("Z", "+00:00"))
    start_date = end_date - timedelta(days=7 * weeks)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")
    separator = "%x1f"
    result = subprocess.run(
        [
            "git", "-C", repo_path, "log",
            "--all",
            f"--since={start_str}",
            f"--until={end_str}",
            f"--pretty=format:%H{separator}%an{separator}%ae{separator}%ad{separator}%s{separator}%D",
            "--date=short",
            "--name-status",
        ],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    commits = parse_commits_with_branches(result.stdout, separator)
    if not commits:
        return 0
    return sum(
        1
        for commit in commits if commit
        for file in commit["files"]
        if re.match(r"\.github/workflows/.*?\.(yaml|yml)", file["filename"])
    )


def ensure_executable(path):
    if platform.system() != "Windows":
        st = os.stat(path)
        os.chmod(path, st.st_mode | stat.S_IEXEC)


def get_scc_path(config):
    script_dir = get_path_config(config, "scc_path", os.path.join("utils", "scc"))
    system = platform.system()
    if system == "Windows":
        return os.path.join(script_dir, "scc.exe")
    if system == "Darwin":
        return os.path.join(script_dir, "scc_mac")
    if system == "Linux":
        return os.path.join(script_dir, "scc_linux")
    raise RuntimeError(f"Unsupported platform: {system}")


def calculate_sloc_and_test_lines(config, local_repo_path, commit_sha, timestamp):
    scc_path = get_scc_path(config)
    ensure_executable(scc_path)

    if not os.path.exists(local_repo_path):
        return None, None

    checkout_successful = False
    if commit_sha:
        try:
            subprocess.run(
                ["git", "-C", local_repo_path, "checkout", commit_sha],
                check=True,
                capture_output=True,
                text=True,
                timeout=15 * 60,
                encoding="utf-8",
                errors="replace",
            )
            checkout_successful = True
        except subprocess.CalledProcessError:
            checkout_successful = False

    if not checkout_successful and timestamp:
        try:
            git_timestamp = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d %H:%M:%S")
            result = subprocess.run(
                ["git", "-C", local_repo_path, "rev-list", "-1", "--before", git_timestamp, "HEAD"],
                capture_output=True,
                text=True,
                timeout=15 * 60,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode == 0 and result.stdout.strip():
                commit_sha = result.stdout.strip()
                subprocess.run(
                    ["git", "-C", local_repo_path, "checkout", commit_sha],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=15 * 60,
                    encoding="utf-8",
                    errors="replace",
                )
                checkout_successful = True
        except Exception:
            checkout_successful = False

    try:
        result = subprocess.run(
            [scc_path, "--no-cocomo", "--format", "json", local_repo_path],
            capture_output=True,
            text=True,
            timeout=15 * 60,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return None, None

        loc_data = json.loads(result.stdout)
        sloc = sum(entry["Code"] for entry in loc_data)

        result = subprocess.run(
            ["git", "-C", local_repo_path, "ls-files"],
            capture_output=True,
            text=True,
            timeout=15 * 60,
            encoding="utf-8",
            errors="replace",
        )
        test_lines = 0
        if result.returncode == 0:
            files_in_repo = result.stdout.strip().split("\n")
            for file_path in files_in_repo:
                if "test" in file_path.lower() or "spec" in file_path.lower():
                    full_path = os.path.join(local_repo_path, file_path)
                    if os.path.exists(full_path):
                        try:
                            with open(full_path, "r", encoding="utf-8", errors="ignore") as handle:
                                test_lines += len(handle.readlines())
                        except Exception:
                            continue

        return sloc, test_lines
    except Exception:
        return None, None
    finally:
        try:
            subprocess.run(
                ["git", "-C", local_repo_path, "checkout", "main"],
                check=True,
                capture_output=True,
                text=True,
                timeout=15 * 60,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.CalledProcessError:
            pass


def ensure_local_repository(repository_name, config):
    clone_root = get_path_config(config, "clone_path", os.path.join("data", "clone"))
    if not clone_root:
        return None

    owner, repo = repository_name.split("/")
    owner_dir = os.path.join(clone_root, owner)
    repo_path = os.path.join(owner_dir, repo)
    git_dir = os.path.join(repo_path, ".git")

    try:
        os.makedirs(owner_dir, exist_ok=True)
        if not os.path.isdir(git_dir):
            clone_url = f"https://github.com/{repository_name}.git"
            subprocess.run(
                ["git", "clone", clone_url, repo],
                cwd=owner_dir,
                check=True,
                capture_output=True,
                text=True,
                timeout=1800,
            )
        else:
            subprocess.run(
                ["git", "fetch", "--all", "--tags"],
                cwd=repo_path,
                check=True,
                capture_output=True,
                text=True,
                timeout=1800,
            )
    except Exception as exc:
        logging.warning(f"Failed to prepare local repository for {repository_name}: {exc}")
        return None

    return repo_path


def get_parent_commit_sha(repo_full_name, head_sha, token):
    if not head_sha:
        return None

    commit_url = f"https://api.github.com/repos/{repo_full_name}/commits/{head_sha}"
    commit_response = get_request(commit_url, token)
    parents = commit_response.get("parents", []) if isinstance(commit_response, dict) else []
    if not parents:
        return None
    return parents[0].get("sha")


def get_code_features(config, repo_full_name, run, token, repo_path=None, recent_runs=None):
    # Code-level features are stored on the run row because they describe the
    # change set associated with this workflow run.
    code_features = get_default_code_features()
    if not feature_enabled(config, "fetch_sloc") and not feature_enabled(config, "fetch_commit_details"):
        return code_features

    try:
        from .code_analyzer import collect_code_features
    except ImportError as exc:
        logging.warning(f"Code analyzer dependencies are unavailable: {exc}")
        return code_features

    repo_path = repo_path or ensure_local_repository(repo_full_name, config)
    if not repo_path:
        return code_features

    head_sha = run.get("head_sha")
    parent_sha = get_parent_commit_sha(repo_full_name, head_sha, token)
    if not head_sha or not parent_sha:
        return code_features

    if feature_enabled(config, "fetch_sloc"):
        timestamp = run.get("created_at")
        if isinstance(timestamp, datetime):
            timestamp = timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")
        sloc, test_lines = calculate_sloc_and_test_lines(config, repo_path, head_sha, timestamp)
        code_features["gh_sloc"] = sloc
        code_features["sloc_initial"] = sloc
        code_features["test_lines_initial"] = test_lines
        if sloc:
            code_features["gh_test_lines_per_kloc"] = (test_lines / sloc) * 1000

    hotspot_files = calculate_hotspot_files(recent_runs or {}, run, 30) if recent_runs else []
    try:
        analyzed = collect_code_features(repo_path, parent_sha, head_sha, hotspot_files=hotspot_files)
    except Exception as exc:
        logging.warning(f"Code-level feature extraction failed for {repo_full_name}#{run.get('run_id')}: {exc}")
        return code_features

    code_features.update(analyzed)
    if analyzed.get("dependencies_count") is not None:
        code_features["dependencies_count"] = analyzed.get("dependencies_count")
    code_features["git_commits"] = 1
    return code_features


def collect_original_run_metrics(config, repo_full_name, run, token, repo_path, recent_runs):
    metrics = {}
    if not repo_path:
        return metrics

    run["_resolved_token"] = token
    committers, core_committers = get_committers(run, repo_full_name, repo_path)
    metrics["git_num_committers"] = len(committers)
    unique_committers, _ = get_committers(run, repo_full_name, repo_path, 30 * 3)
    metrics["gh_team_size_last_3_month"] = len(unique_committers)

    date = str(run["created_at"].strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(run["created_at"], datetime) else run["created_at"])
    metrics["trigger_event"] = run.get("event")

    parent_sha = get_parent_commit_sha(repo_full_name, run.get("head_sha"), token)
    metrics["base_sha"] = parent_sha
    if parent_sha and "commits" not in run:
        run["commits"] = get_commits_in_range_cli(repo_path, parent_sha, run.get("head_sha"))

    commit_metrics = calculate_commit_metrics(run)
    if commit_metrics:
        metrics.update(commit_metrics)
        metrics["git_commits"] = len(run.get("commits", []))

    pr_response = get_request(f"https://api.github.com/repos/{repo_full_name}/commits/{run.get('head_sha')}/pulls", token)
    if pr_response and isinstance(pr_response, list):
        pr_info = pr_response[0]
        metrics["gh_pr_description_complexity"] = calculate_description_complexity(pr_info)
        metrics["pr_number"] = pr_info.get("number")
    else:
        metrics["gh_pr_description_complexity"] = 0
        metrics["pr_number"] = None

    metrics["git_same_committer"], metrics["gh_previous_build_result"] = is_same_committer_as_last(run, recent_runs)
    metrics["repo_fail_rate_history"], metrics["gh_committer_bayesian_trust_score_history"] = get_committer_fail_rate(
        recent_runs,
        date,
        run.get("actor_id"),
        weeks=config.get("committer", {}).get("committer_fail_rate", {}).get("history_time_range_weeks", 0),
    )
    metrics["repo_fail_rate_recent"], metrics["gh_committer_bayesian_trust_score_recent"] = get_committer_fail_rate(
        recent_runs,
        date,
        run.get("actor_id"),
        weeks=config.get("committer", {}).get("committer_fail_rate", {}).get("recent_time_range_weeks", 0),
    )
    metrics["git_committer_repo_exp"] = get_committer_recent_commits(
        run,
        repo_full_name,
        weeks=config.get("committer", {}).get("committer_experience_weeks", 0),
    )
    metrics["is_core_member"] = run.get("actor_login") in [committer.split(" <")[0] for committer in core_committers]
    metrics["concurrent_jobs"] = count_concurrent_jobs(run, repo_full_name, token)
    metrics["committer_cross_project_exp"] = has_cross_project_experience(run, config)
    metrics["gh_committer_first_build"] = run.get("actor_login") not in [committer.split(" <")[0] for committer in committers]

    last_event_run = [item for item in recent_runs.values() if item.get("head_branch") == run.get("head_branch")]
    if not last_event_run:
        metrics["prev_build_same_files_touched"] = None
    else:
        metrics["prev_build_same_files_touched"] = is_modified_files_between_commits(run, repo_full_name, last_event_run[0], token)

    try:
        metrics["repo_ci_config_churn_nums"] = get_ci_config_churn_nums(run, repo_full_name, 4, repo_path)
    except Exception:
        metrics["repo_ci_config_churn_nums"] = 0

    return metrics


def compile_build_info(
    config,
    run,
    repo_full_name,
    workflow,
    languages,
    build_language,
    detected_frameworks,
    dependency_count,
    total_builds,
    token,
    repo_path=None,
    recent_runs=None,
):
    start_time = run["created_at"]
    end_time = run["updated_at"]
    duration = (end_time - start_time).total_seconds() if start_time and end_time else None

    jobs_ids, job_details, job_count = get_jobs_for_run(config, repo_full_name, run["run_id"], token)
    tests_ran = any(
        "test" in (job.get("job_name") or "").lower()
        or any("test" in (step.get("step_name") or "").lower() for step in job.get("steps", []))
        for job in job_details
    )

    workflow_name = workflow.get("name") if workflow else None
    workflow_path = workflow.get("path") if workflow else None
    workflow_size = count_lines_in_workflow(
        repo_full_name,
        workflow_path,
        run["head_sha"],
        token,
    )

    determined_framework = detected_frameworks[0] if detected_frameworks else "unknown"
    cumulative_test_results = {"passed": 0, "failed": 0, "skipped": 0, "total": 0}
    if feature_enabled(config, "fetch_test_parsing_results"):
        log_content = None
        try:
            log_content = get_github_actions_job_log(repo_full_name, run["run_id"], token)
        except Exception:
            log_content = None
        if isinstance(log_content, str):
            cumulative_test_results = parse_test_results(determined_framework, log_content, build_language)

    # This dictionary is the final run-level record before CSV merge.
    build_info = {
        "repo": repo_full_name,
        "id_build": run["run_id"],
        "workflow_id": run.get("workflow_id"),
        "actor_name": (run.get("actor") or {}).get("login", "unknown"),
        "branch": run.get("head_branch"),
        "commit_sha": run.get("head_sha"),
        "languages": languages,
        "status": run.get("status"),
        "workflow_event_trigger": run.get("event"),
        "conclusion": run.get("conclusion"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "build_duration": duration,
        "total_builds": total_builds,
        "gh_first_commit_created_at": (run.get("head_commit") or {}).get("timestamp", "N/A"),
        "build_language": build_language,
        "dependencies_count": dependency_count,
        "workflow_size": workflow_size,
        "test_frameworks": detected_frameworks,
        "workflow_name": workflow_name,
    }

    if feature_enabled(config, "fetch_sloc") or feature_enabled(config, "fetch_commit_details"):
        build_info.update(get_code_features(config, repo_full_name, run, token, repo_path=repo_path, recent_runs=recent_runs))
        build_info.update(collect_original_run_metrics(config, repo_full_name, run, token, repo_path, recent_runs or {}))
    if feature_enabled(config, "fetch_test_parsing_results"):
        build_info.update({
            "tests_passed": cumulative_test_results["passed"],
            "tests_failed": cumulative_test_results["failed"],
            "tests_skipped": cumulative_test_results["skipped"],
            "tests_total": cumulative_test_results["total"],
        })
    if feature_enabled(config, "fetch_job_details"):
        build_info.update({
            "gh_job_id": jobs_ids,
            "total_jobs": job_count,
            "job_details": job_details,
            "tests_ran": tests_ran,
        })
    if feature_enabled(config, "fetch_pull_request_details"):
        build_info.update(fetch_pull_request_details(repo_full_name, run.get("head_sha"), token))

    return build_info


def get_builds_info(repo_full_name, token, config, builds, recent_runs=None):
    owner, repo = repo_full_name.split("/")
    repo_workflows = get_repository_workflows(repo_full_name, token)
    languages = get_repository_languages(repo_full_name, token)
    repo_files = get_github_repo_files(owner, repo, token)
    build_language = identify_build_language(repo_files)
    detected_frameworks, dependency_count = identify_test_framework_and_count_dependencies(
        repo_files,
        owner,
        repo,
        token,
    )

    repo_path = None
    if feature_enabled(config, "fetch_sloc") or feature_enabled(config, "fetch_commit_details"):
        repo_path = ensure_local_repository(repo_full_name, config)

    recent_runs = recent_runs or OrderedDict((str(run.get("run_id")), run) for run in builds)
    builds_info = []
    for idx, run in enumerate(builds, start=1):
        workflow = next(
            (item for item in repo_workflows if str(item.get("id")) == str(run.get("workflow_id"))),
            None,
        )
        builds_info.append(
            compile_build_info(
                config=config,
                run=run,
                repo_full_name=repo_full_name,
                workflow=workflow,
                languages=languages,
                build_language=build_language,
                detected_frameworks=detected_frameworks,
                dependency_count=dependency_count,
                total_builds=idx,
                token=token,
                repo_path=repo_path,
                recent_runs=recent_runs,
            )
        )
    return builds_info


def fetch_repository_runs(repository_name, token=None, config_file="config.yaml"):
    config = load_config(config_file)
    resolved_token = resolve_github_token(token, config)
    runs = []
    page = 1

    while True:
        url = f"https://api.github.com/repos/{repository_name}/actions/runs"
        response = get_request(url, resolved_token, params={"per_page": 100, "page": page})
        batch = response.get("workflow_runs", []) if response else []
        if not batch:
            break

        runs.extend(normalize_run(run) for run in batch)
        if len(batch) < 100:
            break
        page += 1

    return runs


def fetch_runs_by_ids(repository_name, run_ids, token=None, config_file="config.yaml"):
    config = load_config(config_file)
    resolved_token = resolve_github_token(token, config)
    runs = []
    missing_run_ids = []

    for run_id in run_ids:
        run_id_text = str(run_id)
        url = f"https://api.github.com/repos/{repository_name}/actions/runs/{run_id_text}"
        run = get_request(url, resolved_token)
        if not isinstance(run, dict) or str(run.get("id")) != run_id_text:
            missing_run_ids.append(run_id_text)
            continue
        runs.append(normalize_run(run))

    if missing_run_ids:
        missing_text = ", ".join(sorted(missing_run_ids))
        raise ValueError(f"run_id must exist in repository. Missing in {repository_name}: {missing_text}")

    return runs


def extract_repository_runs(repository, builds, token=None, config_file="config.yaml"):
    config = load_config(config_file)
    resolved_token = resolve_github_token(token, config)
    if not resolved_token:
        logging.warning("No GitHub token provided. API requests may fail or be rate-limited.")

    print(f"[run] {repository}: building run context for {len(builds)} run(s)...")
    run_context = build_run_context(repository, builds, resolved_token, config)
    selected_run_ids = {str(run.get("run_id") or run.get("id")) for run in builds}
    ordered_selected_runs = [
        run for run in run_context.values() if str(run.get("run_id") or run.get("id")) in selected_run_ids
    ]
    print(f"[run] {repository}: context ready with {len(run_context)} cached/related run(s).")

    return get_builds_info(
        repo_full_name=repository,
        token=resolved_token,
        config=config,
        builds=ordered_selected_runs,
        recent_runs=run_context,
    )
