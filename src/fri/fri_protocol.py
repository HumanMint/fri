"""Versioned wire compatibility, independent of settings revisions or Gene cog SHAs."""

PROTOCOL = 'gene-fri/1'
CAPABILITIES = ('scoped-context', 'gene-controls', 'owner-reviews', 'privacy-revisions',
                'workspace-outbox', 'social-outbox', 'research-receipts', 'agent-learning')


def contract():
    return {'protocol': PROTOCOL, 'capabilities': list(CAPABILITIES),
            'deployment_authority': 'gene-controller'}


def compatible(value):
    return (isinstance(value, dict) and value.get('protocol') == PROTOCOL
            and value.get('deployment_authority') == 'gene-controller'
            and isinstance(value.get('capabilities'), list)
            and all(item in value['capabilities'] for item in CAPABILITIES))
