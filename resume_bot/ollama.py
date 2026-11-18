"""Ollama's documented /api/generate endpoint, JSON-only responses."""
import json
import os
import time
from urllib.request import Request, urlopen
from urllib.error import URLError


class OllamaError(RuntimeError):
    pass


class OllamaLimitError(OllamaError):
    """The model used its whole token or time budget. An identical request cannot do better."""


class OllamaBudgetError(OllamaLimitError):
    """The output was cut by the token cap or the context window. When the context fills, Ollama
    silently discards the oldest tokens (the instructions and the source) and keeps generating,
    so the answer is unreliable even though the request "succeeded". A larger budget can help."""


# A runaway JSON generation streams forever: every chunk resets the socket timeout, and once the
# context fills, Ollama shifts it and keeps generating without the instructions. Each request is
# therefore capped in tokens and, while streaming, in wall-clock time, so one request cannot hold
# the single Ollama slot that every other job is queued behind.
MAX_TOKENS = 4096
STREAM_DEADLINE = 600
# Ollama's default window (4096) cannot hold a resume plus its quoted extraction. Every request
# asks for an explicit window; one budget error retries once in a window twice as large.
NUM_CTX = 8192
MAX_NUM_CTX = 16384
CHARS_PER_TOKEN = 3  # conservative for English and JSON; real prompts tokenize to more chars per token


class _TopLevelJsonFields:
    """Count complete top-level JSON members in streamed output.

    Only top-level member boundaries count: commas inside quoted strings, arrays and
    nested experience objects do not increment progress. This measures emitted JSON,
    not validation or remaining generation time.
    """
    def __init__(self):
        self.depth = 0
        self.quoted = False
        self.escape = False
        self.started = False
        self.saw_member = False
        self.count = 0

    def feed(self, text):
        for char in text:
            if self.quoted:
                if self.escape:
                    self.escape = False
                elif char == '\\':
                    self.escape = True
                elif char == '"':
                    self.quoted = False
                continue
            if not self.started:
                if char == '{':
                    self.started = True
                    self.depth = 1
                continue
            if char == '"':
                self.quoted = True
                if self.depth == 1:
                    self.saw_member = True
            elif char in '{[':
                self.depth += 1
            elif char in '}]':
                self.depth -= 1
                if self.depth == 0:
                    if self.saw_member:
                        self.count += 1
                    self.started = False
            elif char == ',' and self.depth == 1:
                self.count += 1
            elif self.depth == 1 and not char.isspace():
                self.saw_member = True
        return self.count


class Ollama:
    def __init__(self, model=None, url=None, timeout=240, max_tokens=MAX_TOKENS, deadline=STREAM_DEADLINE,
                 num_ctx=None):
        self.model = model or os.environ.get('OLLAMA_MODEL','qwen2.5:7b')
        self.url = (url or os.environ.get('OLLAMA_URL','http://127.0.0.1:11434')).rstrip('/')
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.deadline = deadline
        self.num_ctx = int(num_ctx or os.environ.get('OLLAMA_NUM_CTX') or NUM_CTX)

    def budget(self, prompt):
        """(num_ctx, num_predict) that leave room for the prompt plus the whole answer."""
        prompt_tokens = len(prompt) // CHARS_PER_TOKEN + 64
        num_ctx = self.num_ctx
        while prompt_tokens + self.max_tokens > num_ctx and num_ctx < MAX_NUM_CTX:
            num_ctx = min(MAX_NUM_CTX, num_ctx * 2)
        return num_ctx, min(self.max_tokens, max(256, num_ctx - prompt_tokens))

    def generate(self, prompt, schema=None, on_progress=None):
        try:
            return self._generate_once(prompt, schema, on_progress)
        except OllamaBudgetError:
            if self.num_ctx >= MAX_NUM_CTX:
                raise
            # Recover the incomplete answer once, in a larger window. The cut answer is discarded,
            # never merged, because text generated after a context shift is not grounded.
            larger = Ollama(self.model, self.url, self.timeout, self.max_tokens * 2, self.deadline,
                            min(MAX_NUM_CTX, self.num_ctx * 2))
            return larger._generate_once(prompt, schema, on_progress)

    def _generate_once(self, prompt, schema=None, on_progress=None):
        if on_progress is None:
            return self._generate(prompt, schema, None)
        try:
            return self._generate(prompt, schema, on_progress)
        except OllamaLimitError:
            raise  # temperature 0: repeating the same request would exhaust the same budget
        except OllamaError as exc:
            if 'unreachable' in str(exc) and 'refused' in str(exc).lower():
                raise  # the service is down; a second request cannot help
            # The streamed request failed part-way. Run it once without streaming (the request
            # format used before live progress) and say so instead of inventing progress.
            on_progress({'phase': 'fallback', 'chars': 0, 'chunks': 0, 'elapsed': 0.0,
                         'reason': str(exc)[:200]})
            return self._generate(prompt, schema, None)

    def _generate(self, prompt, schema=None, on_progress=None):
        """Return the model's JSON object.

        With ``on_progress`` the request streams, and the callback receives measured metrics as
        output arrives: ``{'phase': 'waiting'|'generating'|'done', 'chars': int, 'chunks': int,
        'elapsed': float}`` plus Ollama's final counters (eval_count, eval_duration,
        prompt_eval_count, load_duration) when the stream ends. Nothing is estimated here.
        """
        streaming = on_progress is not None
        num_ctx, num_predict = self.budget(prompt)
        payload = {
            'model':self.model,
            'prompt':prompt,
            'format':schema if schema is not None else 'json',
            'stream':streaming,
            'options':{'temperature':0, 'num_ctx':num_ctx, 'num_predict':num_predict},
        }

        def check_budget(item):
            if item.get('done_reason') == 'length':
                raise OllamaBudgetError(f'Ollama output reached the {num_predict}-token limit')
            # prompt_eval_count omits cached prompt tokens, so never count fewer than a lower bound.
            used = max(int(item.get('prompt_eval_count') or 0), len(prompt) // 4) + int(item.get('eval_count') or 0)
            if used >= num_ctx:
                raise OllamaBudgetError(f'Ollama context window of {num_ctx} tokens overflowed; output discarded')
        req = Request(self.url + '/api/generate',
                      data=json.dumps(payload).encode('utf8'),
                      headers={'Content-Type':'application/json'},method='POST')
        started = time.monotonic()
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                if not streaming:
                    body = json.load(resp)
                    text = body['response']
                    check_budget(body)
                else:
                    on_progress({'phase': 'waiting', 'chars': 0, 'chunks': 0, 'elapsed': 0.0})
                    parts, chunks, final = [], 0, {}
                    emitted, fields = 0, _TopLevelJsonFields()
                    for line in resp:
                        if not line.strip():
                            continue
                        item = json.loads(line)
                        if item.get('error'):
                            raise OllamaError('Ollama reported an error during generation')
                        piece = item.get('response', '')
                        if piece:
                            parts.append(piece)
                            chunks += 1
                            emitted += len(piece)
                            fields.feed(piece)
                        if item.get('done'):
                            check_budget(item)
                            final = {key: item[key] for key in (
                                'eval_count', 'eval_duration', 'prompt_eval_count', 'load_duration',
                                'total_duration') if key in item}
                            break
                        if time.monotonic() - started > self.deadline:
                            # Leaving the with-block closes the connection, which makes Ollama
                            # abort the generation and free its slot.
                            raise OllamaLimitError(f'Ollama generation exceeded {self.deadline} seconds')
                        on_progress({'phase': 'generating', 'chars': emitted,
                                     'chunks': chunks, 'fields_emitted': fields.count,
                                     'elapsed': time.monotonic() - started})
                    else:
                        raise OllamaError('Ollama stream ended before completion')
                    text = ''.join(parts)
                    on_progress({'phase': 'done', 'chars': len(text), 'chunks': chunks,
                                 'fields_emitted': fields.count,
                                 'elapsed': time.monotonic() - started, **final})
        except OllamaError:
            raise
        except (URLError,TimeoutError,OSError,ValueError,KeyError,TypeError) as exc:
            raise OllamaError(f'Ollama unreachable or invalid response: {exc}') from exc
        try:
            result = json.loads(text)
        except (TypeError,ValueError) as exc:
            raise OllamaError('Model did not return valid JSON') from exc
        if not isinstance(result, dict):
            raise OllamaError('Model JSON must be an object')
        return result

    def runtime(self):
        """What Ollama reports for this model right now (/api/ps): bytes in VRAM vs total.
        Returns None when the model is not loaded or Ollama cannot be reached."""
        try:
            with urlopen(self.url + '/api/ps', timeout=3) as resp:
                models = json.load(resp).get('models', [])
        except (URLError, TimeoutError, OSError, ValueError):
            return None
        for item in models:
            if item.get('name') in (self.model, self.model + ':latest') or item.get('model') == self.model:
                size, vram = int(item.get('size') or 0), int(item.get('size_vram') or 0)
                return {'model': item.get('name'), 'size': size, 'size_vram': vram,
                        'gpu_percent': round(100 * vram / size) if size else 0,
                        'context_length': item.get('context_length')}
        return None


def generate(ai, prompt, schema=None, on_progress=None):
    """Call ``ai.generate`` with streaming metrics when the model client supports them."""
    import inspect
    try:
        accepts = 'on_progress' in inspect.signature(ai.generate).parameters
    except (TypeError, ValueError):
        accepts = False
    if on_progress is not None and accepts:
        return ai.generate(prompt, schema=schema, on_progress=on_progress) if schema is not None else \
            ai.generate(prompt, on_progress=on_progress)
    return ai.generate(prompt, schema=schema) if schema is not None else ai.generate(prompt)
