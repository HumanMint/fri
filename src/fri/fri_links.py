"""Discord post links in Fri's replies: one goes out only if the model was shown that exact link."""

import re

URL = (r'https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/channels/(?:[0-9]{1,25}|@me)/[0-9]{1,25}/[0-9]{1,25}'
       r'(?![\w/])')
POST = re.compile(URL)
LINK = re.compile(rf'\[(?P<label>[^\]\n]{{1,100}})\]\(\s*<?(?P<masked>{URL})>?\s*\)|<?(?P<bare>{URL})>?')
# Where the model reads a post's own link: a read's or search's rows, a pin list, nearby posts, the images it
# looked at, the current message. Never someone's words, saved turns or summaries.
SHOWN = ('source', 'reply_target', 'message_burst', 'invited_context', 'inspected_images', 'action_result',
         'action_results')
EMPTY = 'can’t link that one.'


def shown(context):
    """Every post link the model was shown as a post's source or url this call."""
    found = set()

    def walk(value, key=None):
        if isinstance(value, dict):
            for name, item in value.items():
                walk(item, name)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, key)
        elif key in ('source', 'url') and isinstance(value, str) and POST.fullmatch(value):
            found.add(value)
    walk({key: context.get(key) for key in SHOWN})
    return found


def link_posts(reply, context):
    """A post link the model was shown stays, as <link> so it shows no preview. Any other one (made up, mangled or
    copied from someone's words) goes, and a masked one keeps its label. Other links are left as they are."""
    if not POST.search(reply):
        return reply
    allowed = shown(context)

    def kept(match):
        url = match['masked'] or match['bare']
        if match['masked']:
            return f"[{match['label']}](<{url}>)" if url in allowed else match['label']
        return f'<{url}>' if url in allowed else ''
    result = LINK.sub(kept, reply)
    return result if result.strip() else EMPTY  # Only a dropped link: nothing left to send.
