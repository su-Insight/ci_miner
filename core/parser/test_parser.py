import json
import logging
import re
import base64

from core.patterns.test_patterns import framework_regex
from utils.github_api import get_request


def count_dependencies(content, file_type):
    if not content:
        return 0

    if file_type in ["pom.xml", "build.gradle", "build.gradle.kts"]:
        dependency_pattern = re.compile(r"<dependency>|implementation|compile|api|testImplementation")
        return len(dependency_pattern.findall(content))
    if file_type == "requirements.txt":
        return sum(1 for line in content.splitlines() if line.strip() and not line.startswith("#"))
    if file_type == "package.json":
        try:
            package_data = json.loads(content)
        except json.JSONDecodeError:
            logging.error("Failed to parse package.json while counting dependencies.")
            return 0
        return len(package_data.get("dependencies", {})) + len(package_data.get("devDependencies", {}))
    if file_type == "Gemfile":
        return len(re.findall(r"^gem ", content, re.MULTILINE))
    if file_type == "composer.json":
        try:
            composer_data = json.loads(content)
        except json.JSONDecodeError:
            logging.error("Failed to parse composer.json while counting dependencies.")
            return 0
        return len(composer_data.get("require", {})) + len(composer_data.get("require-dev", {}))
    return 0


def get_file_content(owner, repo, path, token=None, ref=None):
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    params = {"ref": ref} if ref else None
    response = get_request(url, token, params=params)
    if response and "content" in response:
        return base64.b64decode(response["content"]).decode("utf-8")
    return None


def identify_test_framework(files, owner, repo, token=None):
    test_framework_mapping = {
        "junit": ["pom.xml", "build.gradle"],
        "rspec": ["Gemfile", "Rakefile"],
        "testunit": ["Gemfile"],
        "cucumber-ruby": ["Gemfile", "Rakefile"],
        "cucumber-java": ["pom.xml", "build.gradle"],
        "phpunit": ["composer.json"],
        "pytest": ["requirements.txt", "setup.py", "pyproject.toml"],
        "unittest": ["requirements.txt", "setup.py", "pyproject.toml"],
        "jest": ["package.json"],
        "mocha": ["package.json"],
    }
    framework_dependencies = {
        "junit": re.compile(r"junit"),
        "rspec": re.compile(r"rspec"),
        "testunit": re.compile(r"gem\s*['\"]test-unit['\"]"),
        "cucumber-ruby": re.compile(r"gem\s*['\"]cucumber['\"]|cucumber"),
        "cucumber-java": re.compile(r"cucumber-java|cucumber-junit|io.cucumber:cucumber"),
        "phpunit": re.compile(r'"phpunit/phpunit"'),
        "pytest": re.compile(r"pytest"),
        "unittest": re.compile(r"unittest"),
        "jest": re.compile(r'"jest"'),
        "mocha": re.compile(r'"mocha"'),
    }

    frameworks_found = []
    for framework, paths in test_framework_mapping.items():
        for path in paths:
            if path not in files:
                continue
            content = get_file_content(owner, repo, path, token)
            if content and framework_dependencies[framework].search(content):
                frameworks_found.append(framework)
                break
    return frameworks_found


def identify_test_framework_and_count_dependencies(files, owner, repo, token=None):
    detected_frameworks = identify_test_framework(files, owner, repo, token)
    dependency_count = 0

    for file_name in files:
        if file_name not in ["pom.xml", "build.gradle", "requirements.txt", "Gemfile", "package.json", "composer.json"]:
            continue
        content = get_file_content(owner, repo, file_name, token)
        dependency_count += count_dependencies(content, file_name)

    return detected_frameworks, dependency_count


def identify_build_language(files):
    build_file_mapping = {
        "ruby": ["Gemfile", "Rakefile"],
        "java-ant": ["build.xml"],
        "java-maven": ["pom.xml"],
        "java-gradle": ["build.gradle", "settings.gradle", "build.gradle.kts"],
    }
    for language, build_files in build_file_mapping.items():
        if any(file_name in files for file_name in build_files):
            return language
    return None


def remove_ansi_escape_sequences(text):
    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")
    return ansi_escape.sub("", text or "")


def parse_test_results(framework, log_content, build_language):
    log_content = remove_ansi_escape_sequences(log_content)
    if framework == "junit" and build_language == "java-maven":
        framework = "junit-maven"
    elif framework == "junit" and build_language == "java-gradle":
        framework = "junit-gradle"

    regex = framework_regex.get(framework)
    if not regex:
        return {"passed": 0, "failed": 0, "skipped": 0, "total": 0}

    matches = regex.findall(log_content)
    if not matches:
        return {"passed": 0, "failed": 0, "skipped": 0, "total": 0}

    passed_tests = 0
    failed_tests = 0
    skipped_tests = 0
    for match in matches:
        if framework == "pytest":
            passed_tests += int(match[0] or 0)
            failed_tests += int(match[1] or 0)
            skipped_tests += int(match[2] or 0)
        elif framework == "junit-gradle":
            passed_tests += int(match[0])
            failed_tests += int(match[1]) + int(match[2])
            skipped_tests += int(match[3])
        elif framework == "junit-maven":
            total = int(match[0])
            failed = int(match[1]) + int(match[2])
            skipped = int(match[3])
            passed_tests += max(total - failed - skipped, 0)
            failed_tests += failed
            skipped_tests += skipped
        else:
            numbers = [int(item) for item in match if str(item).strip().isdigit()]
            if numbers:
                total_guess = numbers[0]
                failed_guess = numbers[2] if len(numbers) > 2 else 0
                skipped_guess = numbers[-1] if len(numbers) > 3 else 0
                passed_tests += max(total_guess - failed_guess - skipped_guess, 0)
                failed_tests += failed_guess
                skipped_tests += skipped_guess

    return {
        "passed": passed_tests,
        "failed": failed_tests,
        "skipped": skipped_tests,
        "total": passed_tests + failed_tests + skipped_tests,
    }
