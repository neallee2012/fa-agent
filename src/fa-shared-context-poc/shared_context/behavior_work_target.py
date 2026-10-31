"""Standing C1 Hosted target: the configured targets, kept current on the native executor."""
from .behavior_operations import control_key
from .errors import Problem


def target_key(domain, agent_id=None):
    return control_key("standing-target", domain, *((agent_id,) if agent_id is not None else ()))


def require(condition, message):
    if not condition:
        raise Problem(409, "STANDING_TARGET_DENIED", message)


def selected_targets(store, config, *, agent_id=None):
    if agent_id is not None and agent_id != config.targets[0].agent_id:
        from .behavior_agent_registry import read_agent_registry
        return (read_agent_registry(store, config, agent_id).scopes[1].target,)
    return config.active_targets


def instruction_hosted_version(control, conversation, *, existing):
    """A new conversation uses the endpoint's compatible served version; an existing one retains its capture.

    Version selection is not read from environment variables or the order of active versions.
    """
    from .behavior_agent_versions import current_hosted_version
    if control.policy.agent_scope is not None:
        from .behavior_agent_registry import registered_agent_scope
        target = registered_agent_scope(control.store, control.policy).target
    else:
        native = control.runtime.dep.native_launches
        target = None if native is None else native.targets[0]
    if existing:
        return conversation.hosted_agent_version
    return current_hosted_version(control.runtime, target)


def refuse_retired_selection(store, config, policy):
    """No deployed producer wrote a standing-target row (#1051) and a fresh startup reads no whole partition, so a
    leftover row is refused here instead of being ignored: at startup (activate_target) and whenever a peer is built
    (build_peer, which for_agent reports as 503). Point reads of both key forms; never a table scan."""
    keys = tuple(dict.fromkeys((target_key(config.table.scope.authority_domain),
                                target_key(policy.authority_domain, policy.agent_id))))
    if any(row is not None for row in store.read_snapshot(keys).values()):
        raise Problem(502, "DELIVERY_RECORD_INVALID", "a retired standing target selection row is stored")


def activate_target(composition):
    runtime = composition.runtime
    if runtime.work_policy is None:
        return
    refuse_retired_selection(runtime.store, composition.config, runtime.work_policy.policy)
    targets = selected_targets(runtime.store, composition.config, agent_id=runtime.work_policy.policy.agent_id)
    native = runtime.dep.native_launches
    if native.targets == targets:
        return
    from .behavior_foundry import FoundryExecutor
    executor = getattr(runtime.dep.workers.adapter.start, "__self__", None)
    require(isinstance(executor, FoundryExecutor) and executor.authority is native
            and set(executor.clients) == {target.executor_id for target in targets},
            "existing exact Foundry executor clients are required")
    # Identity/origin/executor are unchanged; reuse the original SDK HTTP client and credentials.
    for target in targets:
        executor.clients[target.executor_id].target = target
    native.targets = targets
