"""Use the existing source authority for selected and historically attached text."""

from .behavior_attachments import AttachedText
from .errors import Problem
from .source_materials import MAX_GRAPH_NODES, SourceReference


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

