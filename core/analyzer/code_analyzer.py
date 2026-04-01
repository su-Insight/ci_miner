import json
import math
import os
import re

import javalang
from git import Repo
from collections import Counter
from pathlib import Path


def is_test_file(file_name):
    test_indicators = ["test", "tests", "spec", "__tests__", "unittest", "/tests/", "/spec/"]
    return any(indicator in file_name.lower() for indicator in test_indicators)


def is_production_file(file_path):
    production_extensions = [
        ".py", ".java", ".cpp", ".js", ".ts", ".c", ".h", ".cs", ".swift", ".go",
        ".rb", ".php", ".kt", ".scala", ".groovy", ".rs", ".m", ".lua", ".pl",
        ".sh", ".bash", ".sql", ".ps1", ".cls", ".trigger", ".f", ".f90", ".asm",
        ".s", ".vhd", ".vhdl", ".verilog", ".sv", ".tml", ".json", ".xml", ".html",
        ".css", ".sass", ".less", ".jsp", ".asp", ".aspx", ".erb", ".twig", ".hbs",
    ]
    test_indicators = ["test", "tests", "spec", "__tests__"]
    return (
        not any(indicator in file_path for indicator in test_indicators)
        and file_path.endswith(tuple(production_extensions))
    )


def count_dependencies(content, file_type):
    if file_type in ["pom.xml", "build.gradle", "build.gradle.kts"]:
        dependency_pattern = re.compile(r"<dependency>|implementation|compile|api|testImplementation")
        return len(dependency_pattern.findall(content))
    if file_type == "requirements.txt":
        return sum(1 for line in content.splitlines() if line.strip() and not line.startswith("#"))
    if file_type == "package.json":
        dependencies = re.findall(r"\"dependencies\": {([^}]+)}|\"devDependencies\": {([^}]+)}", content)
        return sum(len(dep.split(",")) for dep in dependencies if dep)
    if file_type == "Gemfile":
        return len(re.findall(r"^gem ", content, re.MULTILINE))
    if file_type == "composer.json":
        dependencies = re.findall(r"\"require\": {([^}]+)}|\"require-dev\": {([^}]+)}", content)
        return sum(len(dep.split(",")) for dep in dependencies if dep)
    return 0


def is_documentation_file(file_path):
    doc_extensions = (".md", ".rst", ".txt", ".pdf")
    doc_directories = ["doc", "docs", "documentation", "guide", "help", "manual", "manuals", "guides"]

    lower_path = file_path.lower()
    if lower_path.endswith(doc_extensions):
        return True

    if lower_path.endswith(".html"):
        path_segments = lower_path.split("/")
        if any(doc_dir in path_segments for doc_dir in doc_directories):
            return True
        if any(doc_dir in lower_path for doc_dir in doc_directories):
            return True
        return False

    path_segments = lower_path.split("/")
    if any(doc_dir in path_segments for doc_dir in doc_directories):
        return True

    return False


class JavaCodeAnalyzer:
    def __init__(self, repo_path, hotspot_files=None):
        self.repo = Repo(repo_path)
        self.repo_path = repo_path
        self.hotspot_files = set(hotspot_files or [])

    def get_file_content(self, commit, file_path):
        try:
            return commit.tree[file_path].data_stream.read().decode("utf-8")
        except Exception:
            return None

    def analyze_diff_stats(self, base_sha, head_sha):
        diff_index = self.repo.commit(base_sha).diff(head_sha, create_patch=True)
        stats = Counter()
        file_changes = Counter()

        for diff in diff_index:
            file_path = diff.b_path or diff.a_path
            if not file_path:
                continue

            if diff.new_file:
                stats["gh_files_added"] += 1
            elif diff.deleted_file:
                stats["gh_files_deleted"] += 1
            else:
                stats["gh_files_modified"] += 1

            suffix = Path(file_path).suffix.lower()
            file_changes[suffix] += 1

            added = deleted = 0
            try:
                for line in diff.diff.decode("utf-8", errors="ignore").split("\n"):
                    if line.startswith("+") and not line.startswith("+++"):
                        added += 1
                    elif line.startswith("-") and not line.startswith("---"):
                        deleted += 1
            except Exception:
                pass

            stats["gh_lines_added"] += added
            stats["gh_lines_deleted"] += deleted

            if is_test_file(file_path):
                stats["gh_tests_added"] += added
                stats["gh_tests_deleted"] += deleted
            elif is_production_file(file_path):
                stats["gh_src_files"] += 1
                stats["gh_src_churn"] += added + deleted
            elif is_documentation_file(file_path):
                stats["gh_doc_files"] += 1
            elif any(x in file_path for x in ["pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle", ".yml", ".yaml"]):
                stats["gh_config_files"] += 1
            else:
                stats["gh_other_files"] += 1

            if file_path in self.hotspot_files:
                stats["gh_hotspot_files_touched"] = 1

            if "dockerfile" in file_path:
                stats["dockerfile_changed"] = True
            elif "docker-compose" in file_path:
                stats["docker_compose_changed"] = True

        stats["gh_files_type_modified"] = len(file_changes)
        stats["gh_test_churn"] = stats["gh_tests_added"] + stats["gh_tests_deleted"]

        total_mod = sum(file_changes.values())
        if total_mod > 0:
            entropy = -sum((c / total_mod) * math.log(c / total_mod, 2) for c in file_changes.values())
            stats["gh_files_entropy"] = round(entropy, 3)
        else:
            stats["gh_files_entropy"] = 0

        modules = {(diff.b_path or diff.a_path).split("/")[0] for diff in diff_index if (diff.b_path or diff.a_path)}
        stats["gh_cross_module_changes"] = int(len(modules) > 1)

        return dict(stats)

    def parse_ast(self, code):
        try:
            return javalang.parse.parse(code)
        except Exception:
            return None

    def compare_ast(self, tree_base, tree_head):
        stats = Counter()
        if not tree_base and not tree_head:
            return stats

        base_classes = {c.name: c for c in getattr(tree_base, "types", []) if hasattr(c, "name")}
        head_classes = {c.name: c for c in getattr(tree_head, "types", []) if hasattr(c, "name")}

        base_class_names = set(base_classes.keys())
        head_class_names = set(head_classes.keys())

        stats["ast_class_added"] = len(head_class_names - base_class_names)
        stats["ast_class_deleted"] = len(base_class_names - head_class_names)
        stats["ast_class_modified"] = len(base_class_names & head_class_names)
        stats["ast_class_changed"] = stats["ast_class_added"] + stats["ast_class_deleted"] + stats["ast_class_modified"]

        for cls_name in base_class_names & head_class_names:
            base_methods = {m.name: m for m in base_classes[cls_name].methods}
            head_methods = {m.name: m for m in head_classes[cls_name].methods}
            base_met_names = set(base_methods.keys())
            head_met_names = set(head_methods.keys())

            stats["ast_met_added"] += len(head_met_names - base_met_names)
            stats["ast_met_deleted"] += len(base_met_names - head_met_names)
            stats["ast_met_changed"] += stats["ast_met_added"] + stats["ast_met_deleted"]

            for met in base_met_names & head_met_names:
                if base_methods[met].parameters != head_methods[met].parameters:
                    stats["ast_met_sig_modified"] += 1
                elif str(base_methods[met].body) != str(head_methods[met].body):
                    stats["ast_met_body_modified"] += 1

        for cls_name in base_class_names & head_class_names:
            base_fields = {f.declarators[0].name for f in base_classes[cls_name].fields if f.declarators}
            head_fields = {f.declarators[0].name for f in head_classes[cls_name].fields if f.declarators}
            stats["ast_field_added"] += len(head_fields - base_fields)
            stats["ast_field_deleted"] += len(base_fields - head_fields)
            stats["ast_field_changed"] += stats["ast_field_added"] + stats["ast_field_deleted"]

        base_imports = {imp.path for imp in getattr(tree_base, "imports", [])}
        head_imports = {imp.path for imp in getattr(tree_head, "imports", [])}
        stats["ast_import_added"] = len(head_imports - base_imports)
        stats["ast_import_deleted"] = len(base_imports - head_imports)
        stats["ast_import_changed"] = stats["ast_import_added"] + stats["ast_import_deleted"]

        return stats

    def analyze_ast_diff(self, base_sha, head_sha):
        diff_index = self.repo.commit(base_sha).diff(head_sha)
        stats = Counter()
        prod_diff = test_diff = False

        for diff in diff_index:
            file_path = diff.b_path or diff.a_path
            if not file_path or not file_path.endswith(".java"):
                continue

            base_content = self.get_file_content(self.repo.commit(base_sha), file_path)
            head_content = self.get_file_content(self.repo.commit(head_sha), file_path)
            if not base_content and not head_content:
                continue

            diff_stats = self.compare_ast(self.parse_ast(base_content), self.parse_ast(head_content))
            stats.update(diff_stats)

            if diff_stats["ast_class_changed"] or diff_stats["ast_met_changed"]:
                if is_test_file(file_path):
                    test_diff = True
                elif is_production_file(file_path):
                    prod_diff = True

        stats["src_ast_diff"] = int(prod_diff)
        stats["test_ast_diff"] = int(test_diff)
        return dict(stats)

    def analyze_dependencies_churn(self, commit_sha, parent_commit):
        dependency_files = ["pom.xml", "build.gradle", "build.gradle.kts", "requirements.txt", "package.json", "Gemfile", "composer.json"]
        dependencies_changed = 0
        dependencies_nums = 0
        for file_path in dependency_files:
            current = self.get_file_content(self.repo.commit(commit_sha), file_path)
            parent = self.get_file_content(parent_commit, file_path)
            if current:
                dependencies_nums += count_dependencies(current, file_path)
            if current and parent:
                if count_dependencies(current, file_path) != count_dependencies(parent, file_path):
                    dependencies_changed += 1
        return {"gh_dependencies_churn": dependencies_changed, "dependencies_count": dependencies_nums}

    def analyze_all(self, base_sha, head_sha):
        results = Counter()
        results.update(self.analyze_diff_stats(base_sha, head_sha))
        results.update(self.analyze_ast_diff(base_sha, head_sha))
        results.update(self.analyze_dependencies_churn(head_sha, base_sha))
        all_keys = [
            "gh_files_added", "gh_files_deleted", "gh_files_modified",
            "gh_lines_added", "gh_lines_deleted", "gh_src_files",
            "gh_src_churn", "gh_tests_added", "gh_tests_deleted",
            "gh_test_churn", "gh_doc_files", "gh_config_files",
            "gh_other_files", "gh_files_entropy", "gh_files_type_modified",
            "gh_cross_module_changes", "gh_hotspot_files_touched",
            "ast_class_added", "ast_class_deleted", "ast_class_modified",
            "ast_class_changed", "ast_met_added", "ast_met_deleted",
            "ast_met_changed", "ast_met_sig_modified", "ast_met_body_modified",
            "ast_field_added", "ast_field_deleted", "ast_field_changed",
            "ast_import_added", "ast_import_deleted", "ast_import_changed",
            "src_ast_diff", "test_ast_diff", "gh_dependencies_churn", "dockerfile_changed", "docker_compose_changed",
        ]
        for key in all_keys:
            results.setdefault(key, 0)
        return dict(results)


def collect_code_features(repo_path, base_sha, head_sha, hotspot_files=None):
    analyzer = JavaCodeAnalyzer(repo_path, hotspot_files=hotspot_files)
    return analyzer.analyze_all(base_sha, head_sha)
