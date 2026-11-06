"""Docs/help text must only point at options that exist."""

from click.testing import CliRunner

from spooling.cli import cli


def _help(*args):
    return CliRunner().invoke(cli, [*args, "--help"]).output


def test_push_help_points_at_a_real_command_for_project_names():
    out = _help("push")
    assert "stats --by" not in out
    assert "cost breakdown --by project" in out
    # and that command really has the option the help names
    assert "--by" in _help("cost", "breakdown")


def test_push_documents_scoping_and_team_options():
    out = _help("push")
    for opt in ("--project", "--cwd", "--title", "--copy", "--with-spans", "--dry-run", "--no-redact"):
        assert opt in out


def test_readme_documents_every_push_scoping_option():
    from pathlib import Path
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    for opt in ("--project", "--cwd", "--title", "--copy", "--with-spans", "--dry-run", "--no-redact", "cloud login --key sk_<team-key>"):
        assert opt in readme, f"README does not mention {opt}"
    assert "stats --by" not in readme


def test_readme_documents_the_team_commands():
    from pathlib import Path
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    for cmd in ("spooling cloud teams", "spooling cloud whoami"):
        assert cmd in readme
    out = CliRunner().invoke(cli, ["cloud", "--help"]).output
    assert "teams" in out and "whoami" in out
