"""Versioned wire compatibility, independent of settings revisions or Gene cog SHAs."""

PROTOCOL = 'gene-fri/1'
CAPABILITIES = ('scoped-context', 'gene-controls', 'owner-reviews', 'privacy-revisions',
                'workspace-outbox', 'social-outbox', 'research-receipts', 'agent-learning', 'event-mention-chat',
                'turn-provenance')
OPTIONAL = ('reply-checks', 'member-profiles', 'requester-notices', 'usage-window', 'memory-moves',
            'requester-notice-authorize',
            'member-summaries', 'author-fence', 'turn-withdrawal',
            'summary-generations', 'summary-generation-writes',
            'requester-notice-recheck')  # Used when offered; an older controller still runs Fri.


def contract():
    return {'protocol': PROTOCOL, 'capabilities': list(CAPABILITIES+OPTIONAL),
            'deployment_authority': 'gene-controller'}


def compatible(value):
    """Whether Fri runs with this contract. Every capability is a name: a list holding anything else (an object, a
    list) isn't one, so Fri keeps what it read before rather than fail building its capabilities (fri #350)."""
    return (isinstance(value, dict) and value.get('protocol') == PROTOCOL
            and value.get('deployment_authority') == 'gene-controller'
            and isinstance(value.get('capabilities'), list)
            and all(isinstance(item, str) for item in value['capabilities'])
            and all(item in value['capabilities'] for item in CAPABILITIES))
