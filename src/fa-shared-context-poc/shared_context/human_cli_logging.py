"""Scoped authentication diagnostics for standalone human CLI invocations."""

from contextlib import contextmanager
import logging


@contextmanager
def private_auth_logging():
    roots = tuple(logging.getLogger(name) for name in ("azure", "msal"))
    root_state = [(logger, logger.handlers, logger.propagate) for logger in roots]
    active = [
        logger for name, logger in list(logging.root.manager.loggerDict.items())
        if isinstance(logger, logging.Logger) and
        (name in {"azure", "msal"} or name.startswith(("azure.", "msal.")))
    ]
    disabled = [(logger, logger.disabled) for logger in active]
    try:
        for logger in roots:
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
        for logger in active:
            logger.disabled = True
        yield
    finally:
        for logger, handlers, propagate in root_state:
            logger.handlers = handlers
            logger.propagate = propagate
        for logger, was_disabled in disabled:
            logger.disabled = was_disabled
