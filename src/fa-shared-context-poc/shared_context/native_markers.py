"""ADR-0009 lifecycle table, "Hosted 標記 store" (M7): the item shapes of the separate, expiring marker store.

Fixed in the H2d v2 snapshot so phase 4 needs no new profile. Keys: ``deferred:<run_token>`` (written before
``exit_for_recovery()``), ``crashes:<SID>`` (recovered without a ``deferred:``, at most 8 entries), ``refused:<RID>``
(refused by ``execution_version``; the receipt is unchanged). Times are the Hosted clock, epoch seconds.

The store is created on its first write, as conversation stores are; H0 never opens it.
"""

from typing import Annotated, Literal

from pydantic import Field

from .behavior_native_turn_wire import NativeCrash
from .behavior_wire import Identifier, WireModel

MARKERS_STORE = "fa/v1/markers"
# Fixed when the store is created: ``deferred:`` and ``refused:`` live 30 days, as long as the platform keeps a session.
MARKER_TTL_SECONDS = 30 * 24 * 60 * 60
# ``crashes:<SID>`` ends a day after its last entry (the crash-loop window is 30 minutes): past its ``expires_at`` a read
# and the next append treat it as empty. The store has one item TTL fixed at creation (ADR-0007 §1), so the bytes
# themselves stay up to MARKER_TTL_SECONDS after the last write; nothing reads them as crashes then.
CRASHES_TTL_SECONDS = 24 * 60 * 60
MAX_CRASHES = 8
# The failure code a read of a ``refused:<RID>`` receipt projects: the API's §7a (e) code
# (``behavior_native_turn_api.EARLIER_VERSION_UNFINISHED``), which every poller already treats as final.
EARLIER_VERSION_UNFINISHED = "NATIVE_EARLIER_VERSION_UNFINISHED"

_Seconds = Annotated[float, Field(ge=0)]


class _Deferred(WireModel):
    schema_version: Literal["fa-native-deferred-v1"] = "fa-native-deferred-v1"
    run_token: Identifier
    request_id: Identifier
    at: _Seconds
    expires_at: _Seconds


class _Crashes(WireModel):
    schema_version: Literal["fa-native-crashes-v1"] = "fa-native-crashes-v1"
    hosted_session_id: Identifier
    crashes: Annotated[tuple[NativeCrash, ...], Field(max_length=MAX_CRASHES)] = ()
    expires_at: _Seconds


class _Refused(WireModel):
    schema_version: Literal["fa-native-refused-v1"] = "fa-native-refused-v1"
    request_id: Identifier
    execution_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    hosted_agent_version: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
    at: _Seconds
    expires_at: _Seconds
