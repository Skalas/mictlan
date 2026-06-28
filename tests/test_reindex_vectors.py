"""The post-apply vector reindex hook (brain-mcp embedding refresh)."""

import mictlan.orchestrate as orch


class _Result:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_skips_when_brain_mcp_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(orch, "BRAIN_MCP_DIR", tmp_path / "no-brain-mcp")
    calls = []
    monkeypatch.setattr(orch.subprocess, "run", lambda *a, **k: calls.append((a, k)))

    orch.reindex_vectors(tmp_path / "Vault")

    assert calls == []  # never shells out when brain-mcp is missing
    assert "skipped" in capsys.readouterr().out


def test_invokes_brain_reindex_with_vault_env(tmp_path, monkeypatch):
    brain = tmp_path / "brain-mcp"
    brain.mkdir()
    (brain / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(orch, "BRAIN_MCP_DIR", brain)

    captured = {}

    def fake_run(cmd, capture_output, text, env):
        captured["cmd"] = cmd
        captured["env"] = env
        return _Result(returncode=0, stdout="reindexed 3 notes")

    monkeypatch.setattr(orch.subprocess, "run", fake_run)

    vault = tmp_path / "Obsidian Vault"
    orch.reindex_vectors(vault)

    assert captured["cmd"] == ["uv", "run", "--project", str(brain), "brain-reindex"]
    assert captured["env"]["VAULT_PATH"] == str(vault)


def test_failure_is_swallowed(tmp_path, monkeypatch, capsys):
    brain = tmp_path / "brain-mcp"
    brain.mkdir()
    (brain / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    monkeypatch.setattr(orch, "BRAIN_MCP_DIR", brain)
    monkeypatch.setattr(
        orch.subprocess, "run", lambda *a, **k: _Result(returncode=1, stderr="boom")
    )

    # Must not raise — the dream apply already mutated the vault successfully.
    orch.reindex_vectors(tmp_path / "Vault")
    assert "reindex failed" in capsys.readouterr().out
