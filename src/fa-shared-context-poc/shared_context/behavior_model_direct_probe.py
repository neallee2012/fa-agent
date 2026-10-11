"""One-shot, default-off check: may this Hosted agent identity call model inference on the project endpoint?

ADR-0006 §5 A1. Foundry documents that a Hosted agent's own identity has implicit access to model
inferencing through the project endpoint, so the VM could call the model directly instead of through
the API. This module sends exactly one deliberately invalid Responses request (an out-of-range
temperature on the configured deployment): the service rejects it before generating any token, and
only the HTTP status is logged.

- 400: the identity was authorized and the request reached the deployment.
- 401 or 403: a role assignment is missing (report it; never add one here).
- 404: the configured deployment name differs.

Nothing is stored; no token, request or response body is logged. Off unless both variables are set.
"""

import logging
import os
import re
import threading

import httpx

from .behavior_http_diagnostics import _exception_type

URL_ENV = "FA_MODEL_DIRECT_PROBE_URL"
MODEL_ENV = "FA_MODEL_DIRECT_PROBE_MODEL"
SCOPE = "https://ai.azure.com/.default"
# The identity token may only ever be sent to a Foundry project's own Responses endpoint.
_URL = re.compile(
    r"https://[a-z0-9][a-z0-9-]{1,62}\.services\.ai\.azure\.com"
    r"/api/projects/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/openai/v1/responses")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def probe(credential, *, environ=None, transport=None):
    """Send the single invalid request when configured; return the logged outcome (None when off)."""
    environ = os.environ if environ is None else environ
    url, model = environ.get(URL_ENV), environ.get(MODEL_ENV)
    log = logging.getLogger(__name__)
    if not url and not model:
        return None
    if not url or not _URL.fullmatch(url) or not model or not _MODEL.fullmatch(model):
        log.warning("FA_MODEL_DIRECT_PROBE status=config_invalid")
        return "config_invalid"
    try:
        token = credential.get_token(SCOPE).token
    except Exception as error:
        log.warning("FA_MODEL_DIRECT_PROBE status=token_unavailable exception_type=%s", _exception_type(error))
        return "token_unavailable"
    try:
        with httpx.Client(transport=transport or httpx.HTTPTransport(retries=0), trust_env=False,
                          follow_redirects=False, timeout=httpx.Timeout(30, connect=10)) as client:
            response = client.post(url, json={
                # temperature 5 is outside the accepted 0-2 range (and unsupported by reasoning models), so the
                # deployment refuses the request before generation; the output bound only caps a surprise.
                "model": model, "input": "probe", "temperature": 5, "max_output_tokens": 16, "store": False,
            }, headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"})
    except httpx.HTTPError as error:
        log.warning("FA_MODEL_DIRECT_PROBE status=transport_error exception_type=%s", _exception_type(error))
        return "transport_error"
    log.warning("FA_MODEL_DIRECT_PROBE status=%s", response.status_code)
    return response.status_code


def start(credential, *, environ=None):
    """Run the probe once in the background so it never delays or fails Hosted startup."""
    environ = os.environ if environ is None else environ
    if not environ.get(URL_ENV) and not environ.get(MODEL_ENV):
        return None
    thread = threading.Thread(target=probe, args=(credential,), kwargs={"environ": environ},
                              name="fa-model-direct-probe", daemon=True)
    thread.start()
    return thread
