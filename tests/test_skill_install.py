import importlib.util
from pathlib import Path


def test_installed_skill_bundles_cross_package_markdown_without_extra_skills(tmp_path):
    path = Path(__file__).resolve().parents[1] / "scripts/setup_development.py"
    spec = importlib.util.spec_from_file_location("kaki_setup_development", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sources = tmp_path / "source-skills"
    original = sources / "vc-casegen-task"
    original.mkdir(parents=True)
    (original / "SKILL.md").write_text("Read [contract](../other/references/input.md#fields)")
    other = sources / "other/references"
    other.mkdir(parents=True)
    (other / "input.md").write_text("Fields and [details](details.md)")
    (other / "details.md").write_text("More [fields](input.md#fields)")
    target = tmp_path / "installed/vc-casegen-task"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text((original / "SKILL.md").read_text())
    module.bundle_references(original, target)
    assert "../other/" not in (target / "SKILL.md").read_text()
    bundled = target / "references/upstream/other__references__input.md"
    assert bundled.exists() and "other__references__details.md" in bundled.read_text()
    assert len(list((tmp_path / "installed").glob("*/SKILL.md"))) == 1
    assert len(list((target / "references/upstream").glob("*.md"))) == 2


def test_development_setup_preserves_custom_protection_and_installs_devbox_profiles(tmp_path):
    import json
    import sys
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "kaki_setup", Path(__file__).resolve().parents[1] / "scripts/setup_development.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text(
        json.dumps(
            {
                "providers": {"openai": {"apiKey": "preserved-fixture"}},
                "tools": {"development": {"protected_branches": ["feat/user"], "allow_push": False}},
            }
        )
    )
    harness = tmp_path / "harness"
    prechecks = harness / "omh/prechecks"
    prechecks.mkdir(parents=True)
    for name in ["omh-doctor.sh", "omh-common.sh"]:
        (prechecks / name).write_text("# fixture")
    interpreter = tmp_path / "venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    module.install(
        SimpleNamespace(
            home=home,
            harness=harness,
            private_skill_source=None,
            python=interpreter,
            devbox_target="user@devbox",
        )
    )
    config = json.loads((home / "config.json").read_text())
    dev = config["tools"]["development"]
    assert dev["allow_push"] is False and "allowPush" not in dev
    assert "feat/user" in dev["protected_branches"] and "master" in dev["protected_branches"]
    assert config["providers"]["openai"]["apiKey"] == "preserved-fixture"
    for name in [
        "devbox-inspect",
        "devbox-prepare",
        "devbox-dependencies",
        "devbox-go-cache",
        "devbox-build",
        "devbox-status",
        "devbox-cancel",
    ]:
        assert config["tools"]["commands"][name]["argv"][0] == str(interpreter)
    assert (home / "skills/kaki-devbox-build/scripts/remote_worker.py").is_file()
