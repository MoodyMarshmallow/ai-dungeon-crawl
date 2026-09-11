"""Action prompt text, loaded once per episode; capabilities remain code-owned."""
from dataclasses import dataclass, field
import json
import re
from importlib.resources import files
import os
from pathlib import Path
import stat

MAX_PROMPT_BYTES = 65536
PROMPT_NAMES = ('system_prompt', 'initial_prompt', 'execute_shell')


@dataclass(frozen=True)
class ActionPrompts:
    system_prompt: str
    initial_prompt: str
    execute_shell: str
    _documents: tuple[str, ...] | None = field(default=None, repr=False, compare=False)

    def effective_text(self):
        return {name: getattr(self, name) for name in PROMPT_NAMES}

    def write(self, directory):
        directory.mkdir(parents=True)
        documents = self._documents or tuple(self.effective_text().values())
        for name, text in zip(PROMPT_NAMES, documents, strict=True):
            (directory / f'{name}.md').write_text(text, encoding='utf-8')


def base_prompts():
    directory = files('ai_dungeon_crawl.agent').joinpath('prompt_templates/action_agent')
    return _from_documents(tuple(directory.joinpath(f'{name}.md').read_text(encoding='utf-8')
                                 for name in PROMPT_NAMES))


def read_only_prompt(path, *, single_line=False):
    text = files('ai_dungeon_crawl.agent').joinpath('prompt_templates', path + '.md').read_text(encoding='utf-8')
    return text.removesuffix('\n') if single_line else text


def write_read_only_prompts(directory):
    """Copy packaged prose into a parent-owned, sandbox-read-only directory."""
    source = files('ai_dungeon_crawl.agent').joinpath('prompt_templates/review_agent')
    target = directory / 'review'
    target.mkdir(parents=True)
    for prompt in source.iterdir():
        (target / prompt.name).write_text(prompt.read_text(encoding='utf-8'), encoding='utf-8')


def load_prompts(directory):
    directory = Path(directory)
    for parent in (directory.parent.parent, directory.parent, directory):
        if parent.is_symlink() or not parent.is_dir():
            raise ValueError('Prompt directories must be real directories')
    expected = {f'{name}.md' for name in PROMPT_NAMES}
    if {entry.name for entry in directory.iterdir()} != expected:
        raise ValueError('Action prompts must contain exactly system_prompt.md, initial_prompt.md, execute_shell.md')
    documents = []
    for name in PROMPT_NAMES:
        with os.fdopen(os.open(directory / f'{name}.md', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as reader:
            if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                raise ValueError('Action prompts must be regular files')
            raw = reader.read(MAX_PROMPT_BYTES + 1)
        if len(raw) > MAX_PROMPT_BYTES:
            raise ValueError('Action prompt exceeds 64 KiB')
        text = raw.decode('utf-8')
        if not text.strip() or '\0' in text:
            raise ValueError('Action prompts must be nonempty text without NUL')
        documents.append(text)
    return _from_documents(tuple(documents))


def _from_documents(documents):
    return ActionPrompts(*(_prompt_body(text) for text in documents), _documents=documents)


def _prompt_body(text):
    """Read a small YAML front-matter subset; metadata never configures policy."""
    if len(text.encode('utf-8')) > MAX_PROMPT_BYTES or '\0' in text:
        raise ValueError('Action prompt exceeds its text bounds')
    lines = text.splitlines(keepends=True)
    first = lines[0].rstrip('\r\n') if lines else ''
    body = text
    if text.lstrip('\ufeff \t\r\n').startswith('---'):
        if first != '---':
            raise ValueError('Malformed action prompt front matter')
        end = next((index for index, line in enumerate(lines[1:], 1)
                    if line.rstrip('\r\n') == '---'), None)
        if end is None or len(''.join(lines[:end + 1]).encode('utf-8')) > 4096:
            raise ValueError('Missing or oversized action prompt front matter')
        fields = set()
        for line in lines[1:end]:
            if not line.strip():
                continue
            key, separator, value = line.rstrip('\r\n').partition(':')
            if not separator or key not in ('purpose', 'usage') or key in fields:
                raise ValueError('Front matter allows exactly purpose and usage')
            value = value.strip()
            if value.startswith('"'):
                try:
                    parsed = json.loads(value)
                except ValueError as exc:
                    raise ValueError('Malformed quoted front-matter string') from exc
            else:
                # Plain single-line YAML strings only; no nested YAML, tags,
                # aliases, block scalars, implicit non-string types or mappings.
                value = value.split(' #', 1)[0].rstrip()
                if (not value or value[0] in "[]{}&*!|>'#@%\x60" or
                        re.search(r':(?:\s|$)', value) or
                        value.lower() in ('true', 'false', 'null', '~') or
                        re.fullmatch(r'[-+]?\d+(?:\.\d+)?', value)):
                    raise ValueError('Front matter requires single-line string values')
                parsed = value
            if not isinstance(parsed, str) or not parsed.strip() or '\0' in parsed:
                raise ValueError('Front matter requires nonempty string values')
            fields.add(key)
        if fields != {'purpose', 'usage'}:
            raise ValueError('Front matter requires purpose and usage')
        body = ''.join(lines[end + 1:])
    if not body.strip():
        raise ValueError('Action prompt body must be nonempty')
    return body
