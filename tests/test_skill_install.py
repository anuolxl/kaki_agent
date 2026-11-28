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
