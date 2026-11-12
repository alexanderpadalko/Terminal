from pathlib import Path
import re
import json

def mask_comments(source):
    out = list(source)
    string = False
    escape = False
    i = 0
    while i < len(source):
        ch = source[i]
        if string:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                string = False
        elif ch == '"':
            string = True
        elif source[i:i + 2] == '//':
            while i < len(source) and source[i] != '\n':
                out[i] = ' '
                i += 1
            continue
        i += 1
    return ''.join(out)

def split_args(text):
    parts, start, depth = ([], 0, 0)
    quote = escape = False
    for i, ch in enumerate(text):
        if quote:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                quote = False
        elif ch == '"':
            quote = True
        elif ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        elif ch == ',' and depth == 0:
            parts.append(text[start:i].strip())
            start = i + 1
    parts.append(text[start:].strip())
    return parts

def extract(path):
    original = path.read_text(encoding='utf-8-sig')
    source = mask_comments(original)
    constants = dict(re.findall('^\\s*(?:const\\s+)?(?:string|int)\\s+(\\w+)\\s*=\\s*("[^"\\n]*"|\\d+)\\s*$', source, re.M))
    items = []
    for match in re.finditer('\\binput\\.(\\w+)\\s*\\(', source):
        start = match.end()
        depth, quote, escape, end = (1, False, False, start)
        while depth:
            ch = source[end]
            if quote:
                if escape:
                    escape = False
                elif ch == '\\':
                    escape = True
                elif ch == '"':
                    quote = False
            elif ch == '"':
                quote = True
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
            end += 1
        declaration = re.search('(\\w+)\\s*=\\s*$', source[source.rfind('\n', 0, match.start()) + 1:match.start()])
        assert declaration, match.group()
        args = split_args(source[start:end - 1])
        positional, keywords = ([], {})
        for arg in args:
            named = re.match('^(\\w+)\\s*=\\s*(.*)$', arg, re.S)
            if named:
                keywords[named[1]] = named[2]
            else:
                positional.append(arg)
        title = keywords.get('title', positional[1] if len(positional) > 1 else '""')
        group = keywords.get('group', '"Без группы"')
        items.append({'variable': declaration[1], 'type': match[1], 'title': json.loads(title), 'default': keywords.get('defval', positional[0] if positional else ''), 'group': json.loads(constants.get(group, group)), 'line': source.count('\n', 0, match.start()) + 1, 'arguments': keywords, 'limits': {k: constants.get(keywords[k], keywords[k]) for k in ('minval', 'maxval', 'step', 'options', 'active') if k in keywords}})
    assert len(items) == len(re.findall('\\binput\\.\\w+\\s*\\(', source))
    assert len({x['variable'] for x in items}) == len(items)
    return items
