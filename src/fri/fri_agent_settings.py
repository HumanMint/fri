"""Protocol v1 defaults, mirrored from the controller at extraction."""

DEFAULTS = {
    'enabled': False, 'learning_enabled': True, 'proactive': True, 'support_dms': True,
    'attention_model': 'gpt-6-luna', 'attention_effort': 'low',
    'summary_model': 'gpt-6-luna', 'summary_effort': 'medium',
    'heartbeat_seconds': 180, 'learning_first_minutes': 30, 'learning_later_minutes': 360,
    'history_days': 7, 'history_batch': 30, 'history_daily_messages': 360,
    'exploration_enabled': False, 'exploration_days': 365,
    'meme_enabled': False, 'meme_model': 'gpt-6-luna', 'meme_effort': 'medium',
    'meme_interval_minutes': 30, 'meme_daily_checks': 24, 'meme_daily_reactions': 4,
    'status_enabled': False, 'status_model': 'gpt-6-luna', 'status_effort': 'low',
    'status_rotation_minutes': 360,
    'dm_turns': 8, 'public_turns': 20, 'cooldown_min_minutes': 10,
    'cooldown_max_minutes': 20, 'voice_notes': '', 'attention_notes': '', 'learning_notes': '',
    'reply_check_enabled': False, 'reply_check_private': False, 'reply_check_model': 'gpt-6-luna',
    'reply_check_effort': 'low', 'reply_check_minutes': 30, 'reply_check_daily_pairs': 120,
}

RANGES = {'heartbeat_seconds': (60, 1800), 'learning_first_minutes': (15, 360),
          'learning_later_minutes': (60, 1440), 'history_days': (1, 14),
          'history_batch': (5, 40), 'history_daily_messages': (30, 720),
          'exploration_days': (30, 5000),
          'meme_interval_minutes': (15, 360), 'meme_daily_checks': (2, 48),
          'meme_daily_reactions': (0, 8),
          'status_rotation_minutes': (60, 720),
          'dm_turns': (3, 30), 'public_turns': (5, 60),
          'cooldown_min_minutes': (10, 20), 'cooldown_max_minutes': (10, 20),
          'reply_check_minutes': (10, 720), 'reply_check_daily_pairs': (10, 400)}
