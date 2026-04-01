from .analyzer.job_analyzer import extract_repository_jobs, fetch_jobs_by_ids
from .analyzer.run_analyzer import extract_repository_runs, fetch_runs_by_ids
from .csv_store import (
    ensure_explicit_rows,
    filter_rows,
    load_input_table,
    merge_infos_into_rows,
    write_table,
)
from utils.settings import load_config, resolve_github_token


def get_mode(args):
    if args.run:
        return "run"
    if args.job:
        return "job"
    raise ValueError("Either --run or --job must be provided.")


def get_mode_defaults(mode):
    # Each mode defines its identifier column, default CSV, fetcher, and analyzer.
    if mode == "run":
        return {
            "id_column": "run_id",
            "explicit_arg": "run_id",
            "default_input": "runs_features.csv",
            "fetch_by_ids": fetch_runs_by_ids,
            "extract": extract_repository_runs,
            "info_id_key": "id_build",
        }
    return {
        "id_column": "job_id",
        "explicit_arg": "job_id",
        "default_input": "jobs_features.csv",
        "fetch_by_ids": fetch_jobs_by_ids,
        "extract": extract_repository_jobs,
        "info_id_key": "job_id",
    }


def validate_mode_args(args, mode):
    if mode == "run" and args.job_id:
        raise ValueError("--job_id is only valid with --job.")
    if mode == "job" and args.run_id:
        raise ValueError("--run_id is only valid with --run.")


def process_repository(repository, explicit_ids, rows, fieldnames, token, config_path, mode_settings, overwrite):
    id_column = mode_settings["id_column"]
    fetch_by_ids = mode_settings["fetch_by_ids"]
    extract = mode_settings["extract"]
    mode_name = "run" if id_column == "run_id" else "job"

    if explicit_ids:
        # Explicit IDs force the task rows to exist before extraction.
        selected_rows = ensure_explicit_rows(
            rows,
            fieldnames,
            repository,
            explicit_ids,
            id_column=id_column,
            overwrite=overwrite,
        )
    else:
        # Repository-only mode reads pending items from the input CSV.
        selected_rows = filter_rows(
            rows,
            id_column=id_column,
            repositories=[repository],
            overwrite=overwrite,
        )

    selected_ids = [str(row[id_column]) for row in selected_rows]
    if not selected_ids:
        print(f"[{mode_name}] {repository}: no pending {mode_name}s selected.")
        return []

    print(f"[{mode_name}] {repository}: selected {len(selected_ids)} {mode_name}(s).")

    entities = fetch_by_ids(repository, selected_ids, token=token, config_file=config_path)
    if not entities:
        print(f"[{mode_name}] {repository}: fetched 0 {mode_name}(s).")
        return []

    print(f"[{mode_name}] {repository}: fetched {len(entities)} {mode_name}(s), extracting features...")

    if mode_settings["id_column"] == "run_id":
        results = extract(repository=repository, builds=entities, token=token, config_file=config_path)
    else:
        results = extract(repository=repository, jobs=entities, token=token, config_file=config_path)

    print(f"[{mode_name}] {repository}: extracted {len(results)} record(s).")
    return results


def main(args):
    if not args.repo:
        raise ValueError("--repo is required.")

    mode = get_mode(args)
    validate_mode_args(args, mode)
    mode_settings = get_mode_defaults(mode)
    explicit_ids = getattr(args, mode_settings["explicit_arg"])

    if explicit_ids and len(args.repo) != 1:
        raise ValueError(f"--{mode_settings['explicit_arg']} requires exactly one --repo.")

    input_path = args.input or mode_settings["default_input"]
    config = load_config(args.config)
    token = resolve_github_token(args.token, config)
    fieldnames, rows = load_input_table(input_path, id_column=mode_settings["id_column"])
    mode_name = "run" if mode == "run" else "job"

    print(
        f"Starting {mode_name}-level extraction: "
        f"{len(args.repo)} repos, input={input_path}, overwrite={args.overwrite}"
    )

    extracted_infos = []
    target_repositories = [args.repo[0]] if explicit_ids else args.repo
    for repository in target_repositories:
        repository_explicit_ids = explicit_ids if explicit_ids else None
        extracted_infos.extend(
            process_repository(
                repository=repository,
                explicit_ids=repository_explicit_ids,
                rows=rows,
                fieldnames=fieldnames,
                token=token,
                config_path=args.config,
                mode_settings=mode_settings,
                overwrite=args.overwrite,
            )
        )

    if not extracted_infos:
        print("No runs matched the current selection." if mode == "run" else "No jobs matched the current selection.")
        return

    merge_infos_into_rows(
        rows,
        fieldnames,
        extracted_infos,
        id_column=mode_settings["id_column"],
        info_repository_key="repo",
        info_id_key=mode_settings["info_id_key"],
    )
    write_table(input_path, fieldnames, rows)
    print(f"Finished {mode_name}-level extraction: wrote {len(extracted_infos)} record(s) to {input_path}")
