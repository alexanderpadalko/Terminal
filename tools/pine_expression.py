import re

def expression(text):
    """Translate ternaries, including parentheses, in reviewed numeric blocks."""
    text = text.strip()
    depth, quoted, escaped, start, parts = (0, False, False, 0, [])
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in '([':
            depth += 1
        elif char in ')]':
            depth -= 1
        elif char == ',' and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    if parts:
        parts.append(text[start:])
        return ', '.join((expression(part) for part in parts))
    result, index = ('', 0)
    while index < len(text):
        char = text[index]
        if char == '(':
            end, depth = (index + 1, 1)
            while depth:
                if text[end] == '(':
                    depth += 1
                elif text[end] == ')':
                    depth -= 1
                end += 1
            result += '(' + expression(text[index + 1:end - 1]) + ')'
            index = end
        else:
            result += char
            index += 1
    depth, question, nested = (0, None, 0)
    for index, char in enumerate(result):
        if char in '([':
            depth += 1
        elif char in ')]':
            depth -= 1
        elif depth == 0 and char == '?':
            if question is None:
                question = index
            else:
                nested += 1
        elif depth == 0 and char == ':' and (question is not None):
            if nested:
                nested -= 1
            else:
                condition, yes, no = (result[:question], result[question + 1:index], result[index + 1:])
                return '(' + expression(yes) + ' if ' + expression(condition) + ' else ' + expression(no) + ')'
    result = re.sub('\\bna\\b(?!\\s*\\()', 'None', result)
    result = re.sub('\\btrue\\b', 'True', result)
    return re.sub('\\bfalse\\b', 'False', result)
