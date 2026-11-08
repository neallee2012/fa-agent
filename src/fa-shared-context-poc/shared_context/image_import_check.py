"""Image build check: import every shared_context module the image's packages allow, lazy-only ones included.

Tests and CI run Python 3.12 while the images run 3.11; a module imported only inside a function (native_hitl_runtime)
then fails first on dev. ``python -m shared_context.image_import_check`` in the Dockerfile moves that to the build.
"""
import importlib
import pkgutil

import shared_context


def import_all(package=shared_context):
    imported = skipped = 0
    for module in pkgutil.iter_modules(package.__path__):
        try:
            importlib.import_module(f"{package.__name__}.{module.name}")
            imported += 1
        except ModuleNotFoundError as error:
            # shortcut: a third-party package missing from this image means "the other image's module" and is
            # skipped, so a dependency dropped from this image's requirements is not caught here; list modules then.
            if not error.name or (error.name + ".").startswith(package.__name__ + "."):
                raise
            skipped += 1
    return imported, skipped


if __name__ == "__main__":
    imported, skipped = import_all()
    print(f"FA_IMPORT_CHECK imported={imported} skipped_missing_package={skipped}", flush=True)
