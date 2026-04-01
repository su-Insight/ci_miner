import re


def classify_step(step_name):
    text = (step_name or "").strip().lower()
    if not text:
        return None

    patterns = [
        ("checkout", r"\bcheckout\b"),
        ("setup", r"\bsetup\b|\binstall\b|\bprepare\b"),
        ("build", r"\bbuild\b|\bcompile\b|\bpackage\b"),
        ("test", r"\btest\b|\bpytest\b|\bjunit\b|\bunit\b|\bintegration\b"),
        ("lint", r"\blint\b|\bformat\b|\bstyle\b"),
        ("deploy", r"\bdeploy\b|\brelease\b|\bpublish\b"),
        ("cache", r"\bcache\b"),
        ("artifact", r"\bartifact\b|\bupload\b|\bdownload\b"),
        ("security", r"\bsecurity\b|\bscan\b|\bsast\b|\bcodeql\b"),
    ]

    for label, pattern in patterns:
        if re.search(pattern, text):
            return label
    return "other"
