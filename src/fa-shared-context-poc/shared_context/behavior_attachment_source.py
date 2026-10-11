"""Use the existing source authority for selected and historically attached text."""

from .behavior_attachments import AttachedText, AttachedTexts
from .errors import Problem
from .source_materials import MAX_GRAPH_NODES, SourceReference


def read_sources(runtime, binding, policy, principal, attachments):
    if not attachments:
        return [], {}
    return read_scoped_sources(
        runtime.dep.sources, runtime.store, runtime._source_context(binding, policy, principal), attachments,
        source_visibility=policy.source_visibility, source_ids=binding.source_ids,
        validate_output=lambda exact: runtime._validate_source_output(exact, policy),
    )


def read_scoped_sources(sources, store, context, attachments, *, source_visibility, source_ids, validate_output):
    """Read the original graph using server-selected source scope, without an execution binding."""
    if not attachments:
        return [], {}
    if source_visibility != "team":
        raise Problem(403, "ATTACHMENT_SOURCE_DENIED", "this profile supports original Team source access only")
    entries, originals = [], {}
    for attachment in attachments:
        if attachment.source.source_id not in source_ids:
            raise Problem(403, "BEHAVIOR_SOURCE_DENIED", "source is not in the original server-selected scope")
        reference = SourceReference(scope=context.scope(source_visibility), **attachment.source.model_dump())
        graph = sources.read_graph(context, (reference,))
        exact = graph[reference.key]
        validate_output(exact)
        if exact.event.media_type not in {"text/plain", "text/markdown", "application/json"}:
            raise Problem(422, "ATTACHMENT_FORMAT", "only exact supported text sources can be attached")
        try:
            entries.append(AttachedText(attachment=attachment, media_type=exact.event.media_type,
                                        text=exact.original.decode("utf-8")))
        except (UnicodeError, ValueError):
            raise Problem(422, "ATTACHMENT_FORMAT", "the complete attachment must be valid UTF-8 and match its reference") from None
        originals.update(graph)
        if len(originals) > MAX_GRAPH_NODES:
            raise Problem(413, "ATTACHMENT_SOURCE_LIMIT", "complete attachment dependencies exceed the existing source graph bound")
    keys = sources.source_read_keys(originals)
    rows = store.read_snapshot(keys)
    sources.guard_sources(rows, originals)
    return entries, rows


def retained_attachments(control, conversation_id, turn_number):
    from .behavior_work_policy import work_policy_key, WorkRegistration
    from .behavior_delivery import _parse

    records = sorted((record for record in control.records.values()
                      if record.profile.instruction is not None
                      and record.profile.instruction.conversation_id == conversation_id
                      and record.profile.instruction.turn_number < turn_number),
                     key=lambda record: record.profile.instruction.turn_number)
    if [record.profile.instruction.turn_number for record in records] != list(range(1, turn_number)):
        raise Problem(409, "ATTACHMENT_HISTORY_INVALID", "the original conversation's attachment history is incomplete")
    found, rows = {}, {}
    for record in records:
        attachments = tuple(item for item in record.profile.instruction.attachments
                            if (item.source.source_id, item.source.version, item.source.sha256) not in found)
        if not attachments:
            continue
        key = work_policy_key("registration", record.admission_id)
        row = control.store.read_snapshot((key,))[key]
        if row is None or _parse(row.payload["data"], WorkRegistration) != record:
            raise Problem(409, "ATTACHMENT_HISTORY_INVALID", "the original attachment registration changed")
        rows[key] = row
        for item in attachments:
            found[(item.source.source_id, item.source.version, item.source.sha256)] = item
    if len(found) > MAX_GRAPH_NODES:
        raise Problem(413, "ATTACHMENT_SOURCE_LIMIT", "retained attachments exceed the existing source graph bound")
    return tuple(found.values()), rows


def validate_admission(control, policy, principal, command, conversation):
    if not command.attachments and policy.profile.text_attachment_profile is None:
        return
    if policy.profile.text_attachment_profile is None:
        raise Problem(403, "ATTACHMENTS_DISABLED", "the original server policy does not enable source attachments")
    def read():
        control._human(principal, "submit")
        prior, _ = retained_attachments(control, conversation.conversation_id, len(conversation.jobs) + 1)
        all_sources = (*prior, *command.attachments)
        if len({(item.source.source_id, item.source.version, item.source.sha256) for item in all_sources}) > MAX_GRAPH_NODES:
            raise Problem(413, "ATTACHMENT_SOURCE_LIMIT", "retained attachments exceed the existing source graph bound")
        read_sources(control.runtime, policy.template, policy.legacy_template_policy, principal, all_sources)
        control._human(principal, "submit")
    control.store.consistent_read(read)


def current_sources(authority, session, principal):
    task = session.assignment.task.instruction
    if task is None or task.text_attachment_profile is None:
        return AttachedTexts(), {}
    def read():
        binding, policy, _ = authority.runtime.authorize(session.binding_id, principal, "read")
        profile = authority.runtime.profiles[session.binding_id]
        control = authority.runtime.work_policy
        if (control is None or profile.text_attachment_profile != task.text_attachment_profile
                or profile.instruction != task
                or control.records.get(session.binding_id) is None
                or control.records[session.binding_id].profile != profile):
            raise Problem(403, "ATTACHMENTS_DISABLED", "the exact original registered attachment profile is required")
        prior, snapshot = retained_attachments(control, task.conversation_id, task.turn_number)
        _, historical = read_sources(authority.runtime, binding, policy, principal, prior)
        current, selected = read_sources(authority.runtime, binding, policy, principal, task.attachments)
        snapshot.update(historical)
        snapshot.update(selected)
        if len(set((*authority._keys(session), *snapshot))) > 96:
            raise Problem(413, "ATTACHMENT_SOURCE_LIMIT", "complete attachment authorization exceeds the atomic read-set bound")
        authority.runtime.authorize(session.binding_id, principal, "read")
        return AttachedTexts(entries=tuple(current)), snapshot
    return authority.store.consistent_read(read)
