"""Explicit, bounded archive retrieval. Search results never become personal memory."""

import asyncio
import datetime as dt
import re
import time
import uuid

from .privacy import clean

PAGE_SIZE = 10
MAX_PAGES = 2
MAX_RESULTS = 5
MAX_DAYS = 365
FIELDS = ('channel_id', 'query', 'author', 'after', 'before', 'cursor')
HISTORY_ACTIONS = {'history_search': dict.fromkeys(FIELDS, str)}
READ_ONLY_AFTER_HISTORY = {'history_search', 'read_recent', 'learning_overview', 'web_sources',
                           'channel_capabilities', 'work_list', 'work_read'}


def requested(text):
    visible = re.sub(r'```[\s\S]*?```|`[^`]*`|(?m:^>.*$)', '', text)
    if re.search(r"(?i)\b(?:don't|do not|stop)\s+(?:search|look|read)", visible):
        return False
    # A participle alone ("recommended settings") is not past attribution.
    # Require archive vocabulary, a recall construction, or an explicit past cue.
    return bool(re.search(
        r'(?is)\b(search|find|look\s+(?:up|through|back)|check)\b.*\b(history|archive|chat|messages?|logs?)\b'
        r'|\bwho\s+(said|recommended|mentioned|posted|talked|discussed|linked)\b'
        r'|\b(what|which|who|when|where)\b.*\bdid\b.+\b(say|recommend|mention|post|discuss|talk|link)\b'
        r'|\b(what|which|who|when|where|find|search|look up)\b.*\b(said|recommended|mentioned|posted|talked|discussed|linked)\b.*\b(earlier|before|yesterday|last week|last month|last year|ago|history|chat|messages?|logs?)\b'
        r'|\b(what|which|who|when|where|find|search|look up)\b.*\b(earlier|yesterday|last week|last month|last year|history|chat|messages?|logs?)\b.*\b(said|recommended|mentioned|posted|talked|discussed|linked)\b'
        r'|\b(search|find)\b.*\b(before|last month|last week|last year|ago)\b'
        # "what was that song kev mentioned": a thing someone said, named before the verb. The word
        # before the verb is who said it, so "the most commonly recommended laptop" or "the difference
        # between shared and dedicated" stay general questions.
        r"|\b(?:what(?:'s|’s|s)?|which)\s+(?:(?:was|were|is)\s+)?(?:that|the|those)\s+\w+(?:\s+\w+){0,3}?\s+"
        r'(?!(?:\w+ly|most|more|least|between|and|or|of|for|with|in|on|to|by|than|as|being|been)\b)\w+\s+'
        r'(said|mentioned|recommended|posted|linked|shared|sent|dropped)\b', visible))


def valid_action(action):
    return (isinstance(action, dict) and set(action) == {'kind', *FIELDS}
            and action['kind'] == 'history_search'
            and all(isinstance(action[k], str) for k in FIELDS)
            and bool(re.fullmatch(r'[0-9]{0,25}', action['channel_id']))
            and len(action['query']) <= 80 and len(action['author']) <= 80
            and all(not action[k] or re.fullmatch(r'\d{4}-\d{2}-\d{2}', action[k])
                    for k in ('after', 'before'))
            and bool(re.fullmatch(r'(?:[a-f0-9]{32})?', action['cursor']))
            and all(clean(action[k], 80) == action[k] for k in ('query', 'author')))


def bounds(after, before, now):
    floor, ceiling = now-MAX_DAYS*86400, now
    if after:
        floor = max(floor, dt.datetime.fromisoformat(after).replace(tzinfo=dt.timezone.utc).timestamp())
    if before:
        ceiling = min(ceiling, dt.datetime.fromisoformat(before).replace(tzinfo=dt.timezone.utc).timestamp())
    if floor >= ceiling:
        raise ValueError('Choose a date range within the past year. Before is an exclusive UTC date.')
    return floor, ceiling


def snowflake(at):
    return str(max(0, int(at*1000)-1420070400000) << 22)


class HistorySearch:
    def __init__(self, actions):
        self.actions = actions
        self.pages, self.cursors, self.evidence = 0, {}, {}
        self.seen = set()

    @property
    def remaining(self):
        return MAX_PAGES-self.pages

    def descriptor(self, channels):
        return {'channels': [{'channel_id': key, 'name': clean(value.name, 60)}
                             for key, value in channels.items()],
                'default_channel_id': str(self.actions.channel) if self.actions.public else '',
                'remaining_searches': self.remaining, 'max_age_days': MAX_DAYS,
                'utc_date': dt.datetime.now(dt.timezone.utc).date().isoformat(),
                'description': 'Search old server messages for the current question. Choose a short natural '
                'keyword/phrase (watched, played, recommend, or a specific title), or empty query for an '
                'author/date search. author is a name/mention from the question, me, or empty. '
                'after/before are YYYY-MM-DD UTC dates or empty; before is exclusive. At most two '
                '10-hit pages this turn, five excerpts each. If needed, repeat with the exact returned '
                'cursor and unchanged fields for older matches, or refine the query with cursor="". '
                'Only listed channels; public queries stay in the current channel. Ask which channel '
                'in a DM if unclear. Old bot replies are excluded; never save archive discoveries.'}

    async def author(self, name, channel):
        from discord.http import Route

        a = self.actions
        if not name:
            return ''
        if name.casefold() in ('me', 'myself'):
            return str(a.user)
        mention = re.fullmatch(r'<@!?(\d{1,25})>', name)
        identifier = mention[1] if mention else name if name.isdigit() else ''
        # Names only select evidence. They never identify the requesting actor or grant authority.
        candidates = {}
        recent = a.agent.messages(a.channel) if hasattr(a.agent, 'messages') else []
        known = [*recent, *({'user_id': str(m.id), 'name': m.display_name}
                          for m in getattr(a.event, 'mentions', []))]
        if identifier:
            if identifier not in a.message and not any(str(r.get('user_id')) == identifier and
                    r.get('name', '').casefold() in a.message.casefold() for r in known):
                raise ValueError('Mention the member to search so their identity is unambiguous.')
            candidates[identifier] = None
        else:
            if name.casefold() not in a.message.casefold():
                raise ValueError('Use the member name from the current question, or mention them.')
            for row in known:
                if name.casefold() in row.get('name', '').casefold():
                    candidates[str(row['user_id'])] = None
            route = Route('GET', '/guilds/{guild_id}/members/search', guild_id=a.guild)
            members = await a.fri.http.request(route, params={'query': name, 'limit': 5})
            if not isinstance(members, list) or len(members) >= 5:
                raise ValueError('Several members may match. Mention the intended member to narrow it down.')
            for member in members:
                user = member['user']
                labels = (member.get('nick') or '', user.get('global_name') or '', user.get('username') or '')
                if not user.get('bot') and any(name.casefold() in label.casefold() for label in labels):
                    candidates[str(user['id'])] = None
        if len(candidates) != 1:
            raise ValueError('I could not identify one member from that name. Mention them to narrow the search.')
        identifier = next(iter(candidates))
        member = await a.agent.member(int(identifier), fresh=True, guild_id=a.guild)
        if not member or getattr(member, 'bot', False):
            raise ValueError('That member is unavailable for this search.')
        profile = await a.agent.profile(getattr(channel, 'parent_id', None) or channel.id, int(identifier))
        if not profile['enabled']:
            raise ValueError('That member is unavailable for this search.')
        return identifier

    async def execute(self, action, channels):
        import discord

        from . import feedback as commands

        a = self.actions
        if not valid_action(action) or not requested(a.message) or not isinstance(a.event, discord.Message):
            return {'blocked': 'Ask a current question about server history to search it.'}
        if self.remaining <= 0:
            return {'blocked': 'The two-search allowance for this question is used up.'}
        channel_id = action['channel_id'] or (str(a.channel) if a.public else '')
        if channel_id not in channels:
            return {'blocked': 'Choose an approved readable channel; public lookups stay in this channel.'}
        self.pages += 1  # Reserve attempts, including failed and malformed provider responses.
        a.private_evidence = True
        channel = channels[channel_id]
        try:
            async with asyncio.timeout(45):
                current = await a.event.channel.fetch_message(a.event.id)
                if (current.author.id != a.user or current.webhook_id or current.content != a.event.content
                        or not -30 <= time.time()-current.created_at.timestamp() <= 900):
                    return {'blocked': 'The history question changed or expired. Send a fresh question.'}
                fingerprint = tuple(action[key] for key in FIELDS if key != 'cursor')
                if action['cursor']:
                    prior = self.cursors.pop(action['cursor'], None)
                    if prior is None or prior['fingerprint'] != fingerprint:
                        return {'blocked': 'That search page expired or belongs to different filters.'}
                    plan = dict(prior['plan'])
                else:
                    floor, ceiling = bounds(action['after'], action['before'], time.time())
                    author = await self.author(action['author'], channel)
                    plan = {'min_id': snowflake(floor), 'max_id': snowflake(ceiling), 'author': author,
                            'floor': floor, 'ceiling': ceiling, 'query': action['query']}
                return await self.page(channel, channel_id, plan, fingerprint)
        except (ValueError, KeyError, TypeError, OverflowError):
            return {'error': 'The history search could not validate its filters or results. Try a member mention and a date range.'}
        except (discord.HTTPException, commands.UserFeedbackCheckFailure, TimeoutError):
            return {'error': 'The archive could not be checked right now. No complete search result was obtained.'}

    async def page(self, channel, channel_id, plan, fingerprint):
        import discord
        from discord.http import Route

        a = self.actions
        permissions = channel.permissions_for(channel.guild.me)
        if not permissions.view_channel or not permissions.read_message_history:
            return {'blocked': 'Fri cannot currently read that channel’s history.'}
        params = {'channel_id': channel_id, 'author_type': 'user', 'limit': PAGE_SIZE,
                  'min_id': plan['min_id'], 'max_id': plan['max_id'], 'sort_by': 'timestamp',
                  'sort_order': 'desc', 'include_nsfw': 'false'}
        if plan['author']:
            params['author_id'] = plan['author']
        if plan['query']:
            params['content'] = plan['query']
        route = Route('GET', '/guilds/{guild_id}/messages/search', guild_id=a.guild)
        result = await a.fri.http.request(route, params=params)
        if result.get('code') or result.get('doing_deep_historical_index'):
            return {'error': 'Discord’s history index is not ready. No complete search result was obtained.'}
        groups = result['messages']
        if not isinstance(groups, list):
            raise ValueError('Invalid search result')
        rows = sorted((row for group in groups for row in group if row.get('hit', True)),
                      key=lambda row: int(row['id']), reverse=True)
        if len(rows) > PAGE_SIZE or len({row['id'] for row in rows}) != len(rows):
            raise ValueError('Invalid search page')
        messages = []
        scope = getattr(channel, 'parent_id', None) or channel.id
        for row in rows:
            if (str(row['channel_id']) != channel_id or not int(plan['min_id']) <= int(row['id']) < int(plan['max_id'])
                    or plan['author'] and str(row['author']['id']) != plan['author']):
                raise ValueError('Out-of-scope search result')
        a.sources.add(channel_id)
        examined = []
        for row in rows:
            if len(messages) >= MAX_RESULTS:
                break
            examined.append(row['id'])
            if row['id'] in self.seen or row['author'].get('bot') or row.get('webhook_id'):
                continue
            self.seen.add(row['id'])
            profile = await a.agent.profile(scope, int(row['author']['id']))
            if not profile['enabled']:
                continue
            try:
                message = await channel.fetch_message(int(row['id']))
            except discord.NotFound:
                continue
            if (message.author.bot or message.webhook_id or str(message.author.id) != str(row['author']['id'])
                    or message.content != row['content'] or not message.content.strip()):
                continue
            a.observe(scope, message.author.id, profile['revision'])
            self.evidence[(channel_id, message.id)] = (message.author.id, message.content)
            messages.append({'id': str(message.id), 'user_id': str(message.author.id),
                'author': clean(message.author.display_name, 80), 'at': message.created_at.isoformat(),
                'text': clean(message.content, 600), 'source': message.jump_url})
        cursor = ''
        # Discord warns a short page does not establish archive exhaustion.
        if examined and self.remaining:
            cursor = uuid.uuid4().hex
            self.cursors[cursor] = {'fingerprint': fingerprint,
                'plan': {**plan, 'max_id': str(min(map(int, examined)))}}
        return {'kind': 'history_search', 'channel_id': channel_id, 'channel_name': clean(channel.name, 60),
                'messages': messages, 'matched': len(rows), 'shown': len(messages), 'next_cursor': cursor,
                'remaining_searches': self.remaining, 'after_utc': dt.datetime.fromtimestamp(plan['floor'], dt.timezone.utc).isoformat(),
                'before_utc': dt.datetime.fromtimestamp(plan['ceiling'], dt.timezone.utc).isoformat(),
                'query': plan['query'], 'author_id': plan['author'], 'complete_archive': False,
                'personal_notes_saved': 0}

    async def valid(self, channels):
        async with asyncio.timeout(30):
            return await self._valid(channels)

    async def _valid(self, channels):
        for (channel_id, message_id), (user, content) in self.evidence.items():
            if channel_id not in channels:
                return False
            channel = channels[channel_id]
            permissions = channel.permissions_for(channel.guild.me)
            if not permissions.view_channel or not permissions.read_message_history:
                return False
            message = await channel.fetch_message(message_id)
            if (message.author.id != user or message.content != content or message.author.bot or message.webhook_id):
                return False
        return True
