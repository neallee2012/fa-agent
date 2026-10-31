"""M2-M4 API modules kept only because current tests build their data through api.create_app; no
production code or image imports them. Imported as a top-level package because pytest puts
tests/shared_context on sys.path, as it does for ``from conftest import``."""
