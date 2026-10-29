"""Discord post links in Fri's replies: one goes out only if the model was shown that exact link."""

import re

# A post's link in any form Discord opens it: any case, http, a sign-in or port, the www, ptb or canary host.
URL = (r'https?://(?:[^\s/@<>]*@)?(?:(?:www|ptb|canary)\.)?discord(?:app)?\.com\.?(?::[0-9]*)?'
       r'/+channels/+(?:[0-9]{1,25}|@me)/+[0-9]{1,25}/+[0-9]{1,25}')
POST = re.compile(URL, re.I)
# Then the rest of it: a trailing slash, query or fragment is part of it. Sentence punctuation or markdown at its
# end isn't, nor an emoji or other non-URL text right after it. Inside <…> or a masked link's (…), it runs to the bracket.
TAIL = r'(?:[A-Za-z0-9\-._~:/?#@!$&*+,;=%|]*[A-Za-z0-9\-/#@$&+=%])?'
LINK = re.compile(rf'\[(?P<label>[^\]\n]{{1,100}})\]\(\s*<?(?P<masked>{URL}[^\s<>()]*)>?\s*\)|'
                  rf'<(?P<angled>{URL}[^\s<>]*)>|<?(?P<bare>{URL}{TAIL})>?', re.I)
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


def link_posts(reply, context, empty=EMPTY):
    """A post link the model was shown stays, as <link> so it shows no preview. Any other one (made up, mangled,
    another form of a shown one, or copied from someone's words) goes, and a masked one keeps its label. Other
    links are left as they are. `empty`: what to send when only a dropped link was there."""
    if not POST.search(reply):
        return reply
    allowed = shown(context)

    def kept(match):
        url = match['masked'] or match['angled'] or match['bare']
        if match['masked']:
            return f"[{match['label']}](<{url}>)" if url in allowed else match['label']
        return f'<{url}>' if url in allowed else ''
    result = LINK.sub(kept, reply)
    return result if result.strip() else empty  # Only a dropped link: nothing left to send.
