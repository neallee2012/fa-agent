import importlib
import uuid

import pytest

from shared_context.image_import_check import import_all


def _package(tmp_path, monkeypatch, **modules):
    name = "fake_" + uuid.uuid4().hex
    (tmp_path / name).mkdir()
    (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")
    for module, body in modules.items():
        (tmp_path / name / f"{module}.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    return importlib.import_module(name)


def test_a_package_missing_from_the_image_is_skipped(tmp_path, monkeypatch):
    package = _package(tmp_path, monkeypatch, ok="", api_only="import fa_package_not_in_this_image\n")
    assert import_all(package) == (1, 1)


@pytest.mark.parametrize("body, error", [
    ("raise TypeError('Python 3.11 rejects this class')\n", TypeError),
    ("from .gone import thing\n", ModuleNotFoundError),
])
def test_any_other_import_failure_fails_the_build(tmp_path, monkeypatch, body, error):
    package = _package(tmp_path, monkeypatch, ok="", broken=body)
    with pytest.raises(error):
        import_all(package)
