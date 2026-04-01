import csv
import json
import os
import tempfile


REQUIRED_COMMON_COLUMNS = {"repository", "collected"}


def parse_collected(value):
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"Invalid collected value: {value}") from exc


def load_input_table(input_path, id_column="run_id"):
    if not os.path.exists(input_path):
        return ["repository", id_column, "collected"], []

    with open(input_path, "r", encoding="utf-8-sig", newline="") as csvfile:
        reader = csv.DictReader(csvfile)
        if reader.fieldnames is None:
            raise ValueError(f"Input CSV is empty: {input_path}")

        fieldnames = [name for name in reader.fieldnames if name is not None]
        missing_columns = (REQUIRED_COMMON_COLUMNS | {id_column}) - set(fieldnames)
        if missing_columns:
            missing_text = ", ".join(sorted(missing_columns))
            raise ValueError(
                f"Input CSV must contain repository, {id_column}, collected. Missing: {missing_text}"
            )

        rows = []
        for row_number, row in enumerate(reader, start=2):
            repository = str((row.get("repository") or "")).strip()
            record_id = str((row.get(id_column) or "")).strip()
            if not repository or not record_id:
                raise ValueError(
                    f"Invalid row {row_number}: repository and {id_column} are required."
                )

            normalized_row = dict(row)
            normalized_row["repository"] = repository
            normalized_row[id_column] = record_id
            normalized_row["collected"] = str(parse_collected(row.get("collected")))
            rows.append(normalized_row)

        return fieldnames, rows


def ensure_fieldnames(fieldnames, row):
    for key in row.keys():
        if key not in fieldnames:
            fieldnames.append(key)


def build_row_groups(rows, id_column="run_id"):
    # Group duplicate identifiers so the same repository/id pair is resolved once.
    row_groups = {}
    for row in rows:
        key = (row["repository"], str(row[id_column]))
        row_groups.setdefault(key, []).append(row)
    return row_groups


def resolve_duplicate_collected(rows, overwrite):
    # When duplicate rows disagree, overwrite decides whether pending or collected wins.
    collected_values = {parse_collected(row.get("collected")) for row in rows}
    if collected_values == {0}:
        resolved = 0
    elif collected_values == {1}:
        resolved = 1
    else:
        resolved = 0 if overwrite else 1

    for row in rows:
        row["collected"] = str(resolved)
    return resolved


def warn_duplicate_record_id(repository, record_id, rows, id_column):
    if len(rows) > 1:
        print(
            f"Warning: duplicate {id_column} '{record_id}' found for repository '{repository}' "
            f"({len(rows)} rows)."
        )


def get_repository_row_map(rows, id_column="run_id", repositories=None, overwrite=False):
    selected_repositories = set(repositories or [])
    row_groups = build_row_groups(rows, id_column=id_column)
    selected_map = {}

    for key, group_rows in row_groups.items():
        repository, record_id = key
        if repositories and repository not in selected_repositories:
            continue

        warn_duplicate_record_id(repository, record_id, group_rows, id_column)
        resolved_collected = resolve_duplicate_collected(group_rows, overwrite)
        selected_map[key] = {
            "row": group_rows[0],
            "collected": resolved_collected,
        }
    return selected_map


def filter_rows(rows, id_column="run_id", repositories=None, overwrite=False):
    repository_row_map = get_repository_row_map(
        rows,
        id_column=id_column,
        repositories=repositories,
        overwrite=overwrite,
    )

    selected_rows = []
    for entry in repository_row_map.values():
        if not overwrite and entry["collected"] != 0:
            continue
        selected_rows.append(entry["row"])
    return selected_rows


def ensure_explicit_rows(rows, fieldnames, repository, record_ids, id_column="run_id", overwrite=False):
    row_groups = build_row_groups(rows, id_column=id_column)
    explicit_rows = []

    for record_id in record_ids:
        normalized_record_id = str(record_id).strip()
        key = (repository, normalized_record_id)
        group_rows = row_groups.get(key)
        if group_rows is None:
            row = {"repository": repository, id_column: normalized_record_id, "collected": "0"}
            rows.append(row)
            group_rows = [row]
            row_groups[key] = group_rows
            ensure_fieldnames(fieldnames, row)
        warn_duplicate_record_id(repository, normalized_record_id, group_rows, id_column)
        resolved_collected = resolve_duplicate_collected(group_rows, overwrite)
        if overwrite or resolved_collected == 0:
            explicit_rows.append(group_rows[0])
    return explicit_rows


def serialize_value(value):
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def merge_infos_into_rows(
    rows,
    fieldnames,
    infos,
    id_column="run_id",
    info_repository_key="repo",
    info_id_key=None,
):
    # Merge analyzer output back into the task table and expand the schema on demand.
    info_id_key = info_id_key or ("id_build" if id_column == "run_id" else id_column)
    row_groups = build_row_groups(rows, id_column=id_column)
    for info in infos:
        repository = info[info_repository_key]
        record_id = str(info[info_id_key])
        key = (repository, record_id)

        group_rows = row_groups.get(key)
        if group_rows is None:
            row = {"repository": repository, id_column: record_id, "collected": "0"}
            rows.append(row)
            group_rows = [row]
            row_groups[key] = group_rows

        for row in group_rows:
            row["repository"] = repository
            row[id_column] = record_id
            row["collected"] = "1"

            for key_name, value in info.items():
                if key_name in {info_repository_key, info_id_key}:
                    continue
                row[key_name] = serialize_value(value)

            ensure_fieldnames(fieldnames, row)


def merge_build_infos_into_rows(rows, fieldnames, build_infos):
    merge_infos_into_rows(rows, fieldnames, build_infos, id_column="run_id", info_repository_key="repo", info_id_key="id_build")


def filter_pending_entities(rows, repository, items, item_id_getter, id_column="run_id", overwrite=False):
    repository_row_map = get_repository_row_map(
        rows,
        id_column=id_column,
        repositories=[repository],
        overwrite=overwrite,
    )

    selected_items = []
    for item in items:
        key = (repository, str(item_id_getter(item)))
        entry = repository_row_map.get(key)
        if not overwrite and entry and entry["collected"] != 0:
            continue
        selected_items.append(item)
    return selected_items


def write_table(input_path, fieldnames, rows):
    directory = os.path.dirname(os.path.abspath(input_path))
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="",
        delete=False,
        dir=directory,
        suffix=".tmp",
    ) as tmpfile:
        writer = csv.DictWriter(tmpfile, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
        temp_path = tmpfile.name

    os.replace(temp_path, input_path)
