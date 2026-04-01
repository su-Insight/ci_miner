import re


def classify_commit(message):
    text = (message or "").strip().lower()
    if not text:
        return None

    rules = [
        ("fix", r"\bfix(e[ds])?\b|\bbug\b|\bhotfix\b|\bpatch\b"),
        ("test", r"\btest(s|ing)?\b|\bspec\b"),
        ("docs", r"\bdoc(s|umentation)?\b|\breadme\b"),
        ("refactor", r"\brefactor(ing)?\b|\bcleanup\b|\brestructure\b"),
        ("build", r"\bbuild\b|\bci\b|\bworkflow\b|\bgithub actions\b|\bpipeline\b"),
        ("perf", r"\bperf(ormance)?\b|\boptimi[sz]e\b"),
        ("feat", r"\bfeat(ure)?\b|\badd(ed|s|ing)?\b|\bimplement(ed|s|ing)?\b"),
        ("revert", r"\brevert(ed|s|ing)?\b"),
        ("chore", r"\bchore\b|\bbump\b|\bupgrade\b|\bdeps?\b"),
    ]

    for label, pattern in rules:
        if re.search(pattern, text):
            return label
    return "other"
