import argparse

from core.main import main


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Use the input CSV as both the task list and the output file."
    )
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument(
        "--run",
        action="store_true",
        help="Run-level mode. Uses runs_features.csv by default.",
    )
    mode_group.add_argument(
        "--job",
        action="store_true",
        help="Job-level mode. Uses jobs_features.csv by default.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        required=True,
        help="Repository in owner/repo format. Can be provided multiple times.",
    )
    parser.add_argument(
        "--run_id",
        action="append",
        help="Specific run id to extract. Valid only with --run and requires exactly one --repo.",
    )
    parser.add_argument(
        "--job_id",
        action="append",
        help="Specific job id to extract. Valid only with --job and requires exactly one --repo.",
    )
    parser.add_argument(
        "--input",
        help="Task CSV path. Defaults to runs_features.csv in --run mode or jobs_features.csv in --job mode.",
    )
    parser.add_argument(
        "--token",
        help="GitHub token. If omitted, reads GITHUB_TOKEN from config.yaml.",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config YAML file.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-extract selected runs even if collected is not 0.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    main(parse_args())
