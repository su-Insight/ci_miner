import re


def remove_ansi_codes(content):
    """Remove ANSI escape sequences from log content."""
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-9;]*m)")
    return ansi_escape.sub("", content)


def remove_timestamp(content):
    common_time_pattern = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+Z\s*"
    auto_build_pattern = r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\] \[autobuild\]\s*"
    content = re.sub(common_time_pattern, "", content, flags=re.MULTILINE)
    content = re.sub(auto_build_pattern, "", content, flags=re.MULTILINE)
    return content


def classify_maven(content):
    error_pattern = re.compile(
        r"Failed to execute goal(.*?)(?:To see the full stack trace of the errors|Re-run Maven using the -X switch to enable full debug logging)",
        re.DOTALL | re.IGNORECASE,
    )
    pom_pattern = re.compile(
        r"Some problems were encountered while processing the POMs(.*?)Re-run Maven using the -X switch to enable full debug logging",
        re.DOTALL | re.IGNORECASE,
    )
    for mvn_pattern in (error_pattern, pom_pattern):
        match = mvn_pattern.findall(content)
        for maven_message in match:
            if re.search("Compilation failure", maven_message):
                patterns = {
                    r"warnings found and -Werror specified": "compile failed due to warnings found and werror specified",
                    r"Expected \d+ positional arguments, but saw \d+": "compile failed due to incompatible argument numbers",
                    r"Argument .*? should not be passed to this method": "compile failed due to incompatible argument types",
                    r"The method .*?is not applicable for the arguments": "compile failed due to incompatible arguments",
                    r"incompatible types: possible lossy conversion": "compile failed due to incompatible types",
                    r"incompatible types: .*? cannot be converted to": "compile failed due to incompatible types",
                    r"incompatible types: inference variable .*? has incompatible bounds": "compile failed due to incompatible types",
                    r"cannot find symbol": "compile failed due to cannot find symbol",
                    r"class file for .*? not found": "compile failed due to class file not found",
                    r"cannot be dereferenced": "compile failed due to type cannot be dereferenced",
                    r"is not abstract and does not override abstract method": "compile failed due to does not override abstract method",
                    r"return type required": "compile failed due to return type required",
                    r"non-static type variable .*? cannot be referenced from a static context": "compile failed due to non-static type variable be referenced from a static context",
                    r"(T|t)ype mismatch( \(type annotations\))?: required '.*?' but": "compile failed due to type mismatch",
                    r"(T|t)ype mismatch( \(type annotations\))?: cannot convert from .*? to .*?": "compile failed due to type mismatch",
                    r"constraint mismatch: The type .*? is not a valid substitute": "compile failed due to constraint mismatch",
                    r"The import .*? cannot be resolved": "compile failed due to import cannot be resolved",
                    r"Unhandled exception type": "compile failed due to unhandled exception type",
                    r"cannot be resolved (or is not a field|to a (variable|type))": "compile failed due to field cannot be resolved",
                    r"##\[error\].*? cannot be resolved": "compile failed due to field cannot be resolved",
                    r"field .*? may not have been initialized": "compile failed due to field cannot be resolved",
                    r"Illegal redefinition of parameter": "compile failed due to illegal redefinition of parameter",
                    r"The type .*? must implement the inherited abstract method": "compile failed due to inherited abstract method must implement",
                    r"The type .*? is already defined": "compile failed due to type already defined",
                    r"missing return statement": "compile failed due to return statement missing",
                    r"Duplicate method .*? in type": "compile failed due to duplicate method",
                    r"class, interface, or enum expected": "compile failed due to class or interface expected",
                    r"variable parameters is already defined in method": "compile failed due to variable parameters already defined",
                    r"class file has wrong version .*?, should be .*?": "compile failed due to syntax version mismatch",
                    r"The import .*? collides with another import statement": "compile failed due to import collides",
                    r"The Java feature '.*?' is only available with source level .*? and above": "compile failed due to release version does not supported",
                }
                for pattern, reason in patterns.items():
                    if re.search(pattern, maven_message):
                        return reason

            patterns = {
                r"You have \d+ Checkstyle violation": "checkstyle violation detected",
                r"You have \d+ PMD violation": "PMD violation detected",
                r"The following files had format violations": "format violations detected",
                r"Some Enforcer rules have failed": "enforce rule has failed",
                r"Code Analysis Tool has found \d+ error": "code analysis tool has found error",
                r"APILyzer found \d+ problem": "api analyzer found problem",
                r"File .*? has not been previously formatted": "file format is non-compliant",
                r"failed with \d+ bugs and \d+ errors": "spotbugs found bugs",
                r"Imports are not sorted in": "imports are not sorted",
                r"The file .*? is not sorted": "file is not sorted",
                r"Unsupported class file major version": "dependency version too old",
                r"Error while generating Javadoc": "error while generating Javadoc",
                r"the compiler encountered an XPath expression containing '.*?' operators that exceeds the '.*?' limit set by": "compile failed due to operators limit exceed",
                r"An error has occurred in Javadoc report generation": "error while generating Javadoc",
                r"reason: the Java file contained parse errors": "compile failed due to file contained parse errors",
                r"Fatal error compiling: error: invalid flag": "compile failed due to invalid flag",
                r"Fatal error compiling: error: release version .*? not supported": "compile failed due to release version does not supported",
                r"Some files do not have the expected license header": "files lack the expected license",
                r"Detected JDK version .*? is not in the allowed range": "jdk_version_mismatch",
                r"Too many files with unapproved license": "files lack the expected license",
                r"Failed to find Licenses for \d+ artifacts": "artifacts lack the expected license",
                r"Timeout after \d+ ms while waiting on log out": "time_out",
                r"Container stopped with exit code \d+ unexpectedly": "container stopped unexpectedly",
                r"Failed to load .*? for package": "failed to load package",
                r"A required class was missing while executing": "failed to load package",
                r"Unable to find a comment style definition for some files": "could not find comment style definition",
                r"Java heap space": "java heap space",
                r"Premature end of Content-Length delimited message body": "data_transmission_interrupted",
                r"Unable to (check|format) file .*? error: .*? expected": "failed to check file due to character expected",
                r"Unable to (check|format) file .*? error: illegal start of expression": "failed to check file due to illegal start of expression",
                r"An error has occurred in Checkstyle report generation": "checkstyle report generation occurred error",
                r"Connect to .*? failed: Connection refused": "connection_refused",
                r"is not a valid Win32 application": "application is not supported",
                r"EOFException": "EOFException",
                r"Could not download .*?SSL peer shut down incorrectly": "ssl connection closed",
                r"request .*?failed: Attempted read from closed stream": "connection_close",
                r"Command execution failed\.\n": "unknown",
            }
            for pattern, reason in patterns.items():
                if re.search(pattern, maven_message):
                    return reason


def classify_gradle(content):
    error_pattern = re.compile(r"\* What went wrong:(.*?)\* Try:", re.DOTALL | re.IGNORECASE)
    match = error_pattern.findall(content)
    for gradle_message in match:
        pass


def classify_ant(content):
    error_pattern = re.compile(r"BUILD FAILED(.*?)Total time: \d+ (?:seconds?|minutes)", re.DOTALL | re.IGNORECASE)
    match = error_pattern.findall(content)
    for ant_message in match:
        if re.search("Compile failed; see the compiler error output for details", ant_message):
            patterns = {
                r"error: incompatible types: incompatible parameter types in lambda expression": "compile failed due to incompatible types",
            }
            for pattern, reason in patterns.items():
                if re.search(pattern, content):
                    return reason


def classify_test(content):
    test_result_patterns = [
        r"Tests run: \d+, Failures: (\d+), Errors: (\d+), Skipped: \d+",
        r"=== (\d+) failed, \d+ passed, \d+ warnings",
        r"tests: (\d+) errors found",
        r"\|   (\d+) failing",
        r"features \(\d+ passed, (\d+) failed\)",
        r"Failed!  - Failed:\s+(\d+), Passed:\s+\d+, Skipped:\s+\d+, Total:\s+\d+, Duration",
        r"scenarios \((\d+) failed, \d+ skipped, \d+ passed\)",
    ]
    for pattern in test_result_patterns:
        matches = re.findall(pattern, content)
        for match in matches:
            if isinstance(match, tuple):
                for num in match:
                    if int(num) > 0:
                        return "test_failure"
            if isinstance(match, str) and int(match) > 0:
                return "test_failure"

    test_patterns = [
        r"Test failure\(s\)!",
        r"Expected \d+, but found \d+ classes using the wrong JUnit APIs",
        r"There are test failures",
        r"Coverage checks have not been met",
        r"An unexpected error occurred while launching the test runtime",
        r"Please refer to .*? for the individual test results",
    ]
    for pattern in test_patterns:
        if re.search(pattern, content):
            return "test_failure"


def classify(content):
    if not content:
        return None

    lines = content.splitlines()
    last_line = lines[-1] or lines[-2]
    if "Job is completed before starting" in last_line:
        return "job is completed before starting"

    content = remove_timestamp(content)
    content = remove_ansi_codes(content)

    pattern_msg = (
        classify_maven(content)
        or classify_gradle(content)
        or classify_ant(content)
        or classify_test(content)
        or None
    )
    if pattern_msg:
        return pattern_msg
