"""Accept only inert Teams metadata accompanying the fixed setup command.

Learn's message-activity contract includes a clientInfo entity and a text/html
mirror even for plain text. This is not general HTML/file support: only the
already approved setup text and its attribute-free mirror may pass.
"""
from html.parser import HTMLParser

from .errors import Problem


class _Mirror(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag not in {"div", "p", "span"} or attrs or len(self.stack) >= 8:
            raise ValueError("unsupported setup markup")
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise ValueError("unbalanced setup markup")

    def handle_data(self, data):
        self.parts.append(data)

    def handle_comment(self, data):
        raise ValueError("setup comments are unsupported")

    def handle_decl(self, decl):
        raise ValueError("setup declarations are unsupported")

    def unknown_decl(self, data):
        raise ValueError("setup declarations are unsupported")

    def handle_pi(self, data):
        raise ValueError("setup processing instructions are unsupported")


def normalize_handshake(activity, expected):
    try:
        if activity.get("type") != "message" or activity.get("text") != expected:
            raise ValueError("not the fixed setup message")
        entities = activity.get("entities", [])
        if not isinstance(entities, list) or len(entities) > 1:
            raise ValueError("unsupported setup entities")
        for entity in entities:
            if (not isinstance(entity, dict) or entity.get("type") != "clientInfo"
                    or set(entity) - {"type", "locale", "country", "platform", "timezone"}
                    or any(not isinstance(value, str) or len(value) > 128 for value in entity.values())):
                raise ValueError("unsupported setup client metadata")
        attachments = activity.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments) > 1:
            raise ValueError("unsupported setup attachments")
        for attachment in attachments:
            if (not isinstance(attachment, dict) or set(attachment) != {"contentType", "content"}
                    or attachment["contentType"] != "text/html" or not isinstance(attachment["content"], str)
                    or len(attachment["content"]) > 2048):
                raise ValueError("only a bounded inline setup mirror is accepted")
            mirror = _Mirror()
            mirror.feed(attachment["content"])
            mirror.close()
            if mirror.stack or "".join(mirror.parts).strip() != expected:
                raise ValueError("setup mirror content differs")
        return {key: value for key, value in activity.items() if key not in {"attachments", "entities"}}
    except (ValueError, TypeError, RecursionError):
        raise Problem(422, "TEAMS_CONTENT_UNSUPPORTED", "setup metadata must contain only the exact command mirror") from None
