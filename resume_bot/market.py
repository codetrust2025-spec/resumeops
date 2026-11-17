"""Public job research sourced from configured employer boards and vetted local excerpts."""
import json
import re
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from html import unescape
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from .workflow import ROLES, matched_terms

_MAX_BYTES = 2_000_000
_STOP = {'a', 'an', 'the', 'for', 'and', 'of', 'to', 'junior', 'senior', 'sr', 'jr', 'lead'}


def title_words(value):
    return set(re.findall(r'[^\W_]+', value.casefold())) - _STOP


def safe_link(value):
    try:
        url = urlsplit(value)
        return (url.scheme == 'https' and url.hostname and not url.username and not url.password
                and not url.port and not any(char.isspace() for char in value))
    except ValueError:
        return False


def _configured(root):
    if root is None:
        return {}
    try:
        with (root / 'market-sources.json').open('rb') as stream:
            data = json.loads(stream.read(_MAX_BYTES + 1))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _public_json(path):
    with build_opener(_NoRedirect()).open('https://boards-api.greenhouse.io/v1/boards/' + path,
                                        timeout=4) as response:
        data = response.read(_MAX_BYTES + 1)
    return json.loads(data) if len(data) <= _MAX_BYTES else None


@lru_cache(maxsize=16)
def _board(token, interval):
    now = datetime.now(UTC).isoformat(timespec='seconds')
    try:
        data = _public_json(token + '/jobs')
        jobs = data.get('jobs') if isinstance(data, dict) else None
        return [
            {'title': j.get('title'), 'url': j.get('absolute_url'),
             'board': token, 'job_id': j['id'], 'provider': 'Greenhouse public employer board',
             'retrieved_at': now}
            for j in jobs[:500] if isinstance(j, dict) and type(j.get('id')) is int
        ] if isinstance(jobs, list) else []
    except (OSError, ValueError):
        return []


def _description(post):
    if 'board' not in post or post['provider'] != 'Greenhouse public employer board':
        return post.get('description') if isinstance(post.get('description'), str) else ''
    try:
        data = _public_json(f"{post['board']}/jobs/{post['job_id']}")
        if isinstance(data, dict) and isinstance(data.get('content'), str):
            return unescape(data['content'])
    except (OSError, ValueError):
        pass
    return ''


def _dated_source(post, now):
    try:
        date = datetime.fromisoformat(post.get('retrieved_at', ''))
        return date.tzinfo is not None and timedelta(0) <= now - date <= timedelta(days=30)
    except (ValueError, TypeError):
        return False


def relevant_sources(title, root=None):
    config = _configured(root)
    now = datetime.now(UTC)
    saved = config.get('authorized_postings', [])
    boards = config.get('greenhouse_boards', [])
    posts = [
        {**row, 'provider': 'Operator-supplied public posting'}
        for row in (saved[:100] if isinstance(saved, list) else []) if isinstance(row, dict)
    ]
    for token in (boards[:3] if isinstance(boards, list) else []):
        if isinstance(token, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', token):
            posts.extend(_board(token, int(now.timestamp()) // 900))

    wanted = title_words(title)
    scored = []
    for post in posts:
        name, url = (post.get(key) for key in ('title', 'url'))
        if not all(isinstance(v, str) for v in (name, url)) or not safe_link(url) or not _dated_source(post, now):
            continue
        words = title_words(name)
        score = len(wanted & words) / max(len(wanted | words), 1)
        if score >= .5:
            scored.append((score, post))
    seen = set()
    output = []
    vocabulary = list(dict.fromkeys(term for role in ROLES.values() for term in role['keywords']))
    for score, post in sorted(scored, key=lambda row: row[0], reverse=True)[:8]:
        if post['url'] in seen:
            continue
        seen.add(post['url'])
        description = _description(post)
        if not description:
            continue
        lines = re.sub(r'<(?:br\b[^>]*|/(?:p|li|div|h[1-6]))\s*>', '\n', description[:12000], flags=re.I)
        lines = re.sub(r'<[^>]*>', ' ', lines)
        requirements = [' '.join(line.split()) for line in lines.splitlines()]
        excerpts = [line[:180] for line in requirements if re.search(
            r'\b(requirements?|proficien|experience with|responsibilit|qualifications?|maintain|design|develop)', line, re.I)][:4]
        keywords = matched_terms(vocabulary, lines)
        for line in requirements:
            phrase = re.search(r'\b(?:experience with|proficien\w+ in|knowledge of)\s+([^.;:]+)', line, re.I)
            if phrase:
                for item in re.split(r',|\band\b', phrase[1], flags=re.I):
                    item = item.strip()
                    if 2 <= len(item) <= 36 and len(item.split()) <= 5 and re.fullmatch(r'[\w .+#/-]+', item):
                        keywords.append(item)
        output.append({'label': post['title'][:140], 'provider': post['provider'], 'url': post['url'],
                       'retrieved_at': post['retrieved_at'], 'relevance': round(score * 100),
                       'responsibilities': excerpts, 'terms': list(dict.fromkeys(keywords))[:18]})
        if len(output) == 4:
            break
    return output
