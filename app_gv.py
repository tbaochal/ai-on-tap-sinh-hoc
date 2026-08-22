from pathlib import Path

exec(
    compile(
        Path(__file__).with_name("app.py").read_text(encoding="utf-8"),
        "app.py",
        "exec"
    )
)