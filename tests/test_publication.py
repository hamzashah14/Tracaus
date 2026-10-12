import subprocess

import pytest

from scripts.dev.check_public_repository import check, forbidden_path


def repository(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "--template=", "-b", "main"], cwd=root, check=True, capture_output=True)
    for key, value in (("user.name", "Synthetic Author"), ("user.email", "author@example.invalid")):
        subprocess.run(["git", "config", key, value], cwd=root, check=True)
    (root / ".gitignore").write_text(".local/\n.env\n")
    (root / "README.md").write_text("# Example\n")
    commit(root)
    return root


def commit(root):
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "commit", "-m", "Synthetic checkpoint"],
        cwd=root,
        check=True,
        capture_output=True,
    )


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.production",
        ".aws/credentials",
        "docs/implementation/STATE.md",
        "AGENTS.md",
        "../outside",
        "/absolute",
        "private.key",
        "canary.ticket",
        ".local/a.json",
        ".git/config",
    ],
)
def test_private_and_unsafe_paths_are_rejected(path):
    assert forbidden_path(path)


def test_public_example_and_license_paths_are_allowed():
    for path in (".env.example", "examples/deployment.example.json", "docs/DEPLOY.md", "LICENSE"):
        assert not forbidden_path(path)


def test_doc_links_cannot_point_to_unpublished_files_or_missing_anchors(tmp_path):
    root = repository(tmp_path)
    (root / "README.md").write_text("# Example\n[private](.env)\n[missing](README.md#absent)\n")
    commit(root)
    errors = check(root)
    assert any("Unpublished" in error for error in errors)
    assert any("Missing documentation anchor" in error for error in errors)
