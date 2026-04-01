import time
from datetime import datetime, timedelta

import requests
from dateutil.relativedelta import relativedelta

from utils.github_api import github_request


def get_all_collaborators(owner, repo, token):
    url = f"https://api.github.com/repos/{owner}/{repo}/collaborators"

    collaborators = []
    page = 1
    while True:
        params = {"per_page": 100, "page": page}
        response = github_request(url, token, "GET", params=params)
        if not response:
            break
        for user in response:
            collaborators.append({"login": user["login"], "id": user["id"]})
        page += 1
        time.sleep(2)

    return collaborators


def get_large_action_repos(username, token, min_runs=1000):
    """Return repositories for a user whose workflow-run volume exceeds a threshold."""
    repos = []
    page = 1
    while True:
        url = f"https://api.github.com/users/{username}/repos"
        params = {"per_page": 100, "page": page}
        response = github_request(url, token, "GET", params=params)
        if not response:
            break
        repos.extend(response)
        page += 1

    large_repos = []
    print(f"Found {len(repos)} repositories for user {username}.")
    for repo in repos:
        owner = repo["owner"]["login"]
        repo_name = repo["name"]

        url_runs = f"https://api.github.com/repos/{owner}/{repo_name}/actions/runs"
        response = github_request(url_runs, token, "GET", params={"per_page": 1})
        total_count = response["total_count"] if response else 0

        if total_count > min_runs:
            large_repos.append({"repo": f"{owner}/{repo_name}", "workflow_runs": total_count})

    return large_repos


def get_runs_past_period(owner, repo, token, start_date, end_date, max_pages=100):
    """Fetch successful or failed workflow runs within a time range."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    first_page = github_request(url, token, "GET", params={"per_page": 100, "page": 1}) or {}
    total_count = first_page.get("total_count", max_pages)
    max_pages = total_count // 100 + 1

    low, high = 1, max_pages
    found_page = None

    while low <= high:
        mid = (low + high) // 2
        print(f"Fetching runs, page {mid}...")
        response = github_request(url, token, "GET", params={"per_page": 100, "page": mid})
        runs = response.get("workflow_runs", []) if response else []
        if not runs:
            high = mid - 1
            continue

        first_time = datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))
        last_time = datetime.fromisoformat(runs[-1]["created_at"].replace("Z", "+00:00"))

        if last_time > end_date:
            low = mid + 1
        elif first_time <= end_date:
            found_page = mid
            high = mid - 1
        else:
            found_page = mid
            break

    results = {}
    if found_page:
        page = found_page
        while True:
            response = github_request(url, token, "GET", params={"per_page": 100, "page": page})
            runs = response.get("workflow_runs", []) if response else []
            if not runs:
                break

            stop = False
            for run in runs:
                created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
                if start_date <= created <= end_date and run["conclusion"] in ["success", "failure"]:
                    results[run["id"]] = {
                        "id": run["id"],
                        "name": run["name"],
                        "head_sha": run["head_sha"],
                        "head_branch": run["head_branch"],
                        "created_at": run["created_at"],
                        "conclusion": run["conclusion"],
                        "event": run["event"],
                        "actor": run.get("actor"),
                    }
                elif created < start_date:
                    stop = True
                    break

            if stop:
                break

            page += 1
            if page > max_pages:
                break
    else:
        print("No cached page entry point was found for the requested run window.")
    return results


def get_runs_past_n_weeks(owner, repo, token, end_date, weeks=3, max_results=500, max_pages=100):
    """Fetch workflow runs in the N-week window before an end date."""
    end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    start = end - relativedelta(weeks=weeks)
    return get_runs_past_period(owner, repo, token, start, end, max_pages)


def get_runs_before_date_fast(owner, repo, token, target_date, max_pages=100):
    """Fetch up to 50 successful or failed runs before a target date."""
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    target_time = datetime.fromisoformat(target_date.replace("Z", "+00:00"))

    low, high = 1, max_pages
    found_page = None

    while low <= high:
        mid = (low + high) // 2
        print(f"Fetching runs, page {mid}...")
        response = github_request(url, token, "GET", params={"per_page": 100, "page": mid})
        runs = response.get("workflow_runs", []) if response else []

        if not runs:
            high = mid - 1
            continue

        first_time = datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))
        last_time = datetime.fromisoformat(runs[-1]["created_at"].replace("Z", "+00:00"))

        if last_time > target_time:
            low = mid + 1
        elif first_time <= target_time:
            found_page = mid
            high = mid - 1
        else:
            found_page = mid
            break

    results = []
    if found_page:
        for page in range(found_page, found_page + 3):
            params = {"per_page": 100, "page": page}
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            runs = response.json().get("workflow_runs", [])
            for run in runs:
                created = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
                if created <= target_time and run["conclusion"] in ["success", "failure"]:
                    results.append(
                        {
                            "id": run["id"],
                            "name": run["name"],
                            "created_at": run["created_at"],
                            "conclusion": run["conclusion"],
                            "actor": run["actor"],
                            "event": run["event"],
                        }
                    )
                    if len(results) >= 50:
                        return results
    return results


def get_user_commits(owner, repo, username, token, date, weeks=4):
    end = datetime.fromisoformat(date.replace("Z", "+00:00"))
    start = end - timedelta(days=7 * weeks)

    all_commits = []
    page = 1

    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/commits"
        params = {
            "author": username,
            "since": start.isoformat() + "Z",
            "until": end.isoformat() + "Z",
            "per_page": 100,
            "page": page,
        }
        commits = github_request(url, token, "GET", params=params)
        if not commits:
            break
        all_commits.extend(commits)
        page += 1

    return all_commits
