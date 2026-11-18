"""Real screenshot delivery route behind the Hosted factory, with no SDK process or live service."""

from types import SimpleNamespace


from assistant_images_helpers import png
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


def test_imagemagick_gets_no_hosted_secret_from_the_environment(monkeypatch, tmp_path):
    """A resized screenshot's convert is dumpable after exec; a same-UID shell command could read its environ."""
    from shared_context import assistant_image_capability as capability
    monkeypatch.setenv("IDENTITY_HEADER", "fa-fit-canary")
    seen = []

    def run(args, **options):
        seen.append(options["env"])
        return SimpleNamespace(stdout=png(1))
    monkeypatch.setattr(capability, "os", SimpleNamespace(name="posix", environ=capability.os.environ))
    monkeypatch.setattr(capability.shutil, "which", lambda name: "/usr/bin/convert")
    monkeypatch.setattr(capability.subprocess, "run", run)
    capability._fit(png(3) + b"\0" * capability.MAX_IMAGE_BYTES, tmp_path / "image-tmp")
    assert seen and all("fa-fit-canary" not in env.values() and "IDENTITY_HEADER" not in env for env in seen)
    assert set(seen[0]) == {"PATH", "LANG", "HOME", "TMPDIR", "MAGICK_TEMPORARY_PATH"}
