import re

def inspect_bytes(data):
    text = data.decode('utf8')
    result = []
    for match in re.finditer(r'(?m)^\s*@([A-Za-z]+)\s*[{(]', text):
        kind = match.group(1).casefold()
        result.append({'kind': kind, 'character_offset': match.start(),
                       'line': text.count('\n', 0, match.start()) + 1})
    if not result:
        raise ValueError('没有可查看的 BibTeX 条目起始')
    return result
