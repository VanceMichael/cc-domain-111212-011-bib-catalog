"""BibTeX 词法与语义解析。

扫描器逐字符前进，同时维护字符索引、行号与 UTF-8 字节偏移；字符串宏、
拼接、嵌套花括号、引号与注释都由扫描器处理，不用正则截取。语义规则见
docs/source-contract.md：@string 在出现处对后续字段生效且声明时求值，
crossref 与 xdata 可前向引用，字段优先级为显式、按 xdata 列表顺序、
crossref，被压制的候选值在字段 shadowed 中留证。重复键、未定义宏与
继承环都记入诊断，不任选一份掩盖。解析结果只是派生数据；原件字节由
storage 保存，清洗后的检索文本不会反写覆盖原件。
"""
import unicodedata

FORMAT = 'bib-catalog/1'

MONTHS = {
    'jan': 'January', 'feb': 'February', 'mar': 'March', 'apr': 'April',
    'may': 'May', 'jun': 'June', 'jul': 'July', 'aug': 'August',
    'sep': 'September', 'oct': 'October', 'nov': 'November', 'dec': 'December',
}

NAME_FIELDS = frozenset({'author', 'editor'})
META_FIELDS = frozenset({'crossref', 'xdata'})
WHITESPACE = ' \t\r\n\f'
IDENT_CHARS = frozenset(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:-')

# LaTeX 重音命令 → 组合用 Unicode 记号，检索表示用 NFC 合成。
ACCENTS = {
    "'": '́', '`': '̀', '"': '̈', '^': '̂', '~': '̃',
    '=': '̄', '.': '̇', 'u': '̆', 'v': '̌', 'H': '̋',
    'c': '̧', 'k': '̨', 'r': '̊', 'd': '̣', 'b': '̱',
}
SPECIALS = {
    'ss': 'ß', 'SS': 'ẞ', 'o': 'ø', 'O': 'Ø', 'ae': 'æ', 'AE': 'Æ',
    'oe': 'œ', 'OE': 'Œ', 'aa': 'å', 'AA': 'Å', 'l': 'ł', 'L': 'Ł',
    'i': 'ı', 'j': 'ȷ',
}
ESCAPED = {'\\': '\\', '{': '{', '}': '}', '%': '%', '&': '&',
           '$': '$', '#': '#', '_': '_'}


class ParseConfig:
    """解析配置；xdata 关闭时 xdata 字段只是普通字段，不参与继承。"""

    __slots__ = ('xdata',)

    def __init__(self, xdata=True):
        self.xdata = bool(xdata)

    def as_json(self):
        return {'xdata': self.xdata}

    @classmethod
    def from_json(cls, data):
        return cls(xdata=bool((data or {}).get('xdata', True)))


class _Error(Exception):
    def __init__(self, message, mark):
        super().__init__(message)
        self.message = message
        self.mark = mark


def _point(mark):
    return {'start': mark[0], 'end': mark[0], 'byte_start': mark[1],
            'byte_end': mark[1], 'line': mark[2]}


class _Scanner:
    """逐字符扫描，同时维护字符索引、UTF-8 字节偏移与行号。"""

    __slots__ = ('text', 'pos', 'byte', 'line')

    def __init__(self, text):
        self.text = text
        self.pos = 0
        self.byte = 0
        self.line = 1

    def mark(self):
        return (self.pos, self.byte, self.line)

    def span(self, mark):
        return {'start': mark[0], 'end': self.pos, 'byte_start': mark[1],
                'byte_end': self.byte, 'line': mark[2]}

    def at_end(self):
        return self.pos >= len(self.text)

    def peek(self):
        return self.text[self.pos] if self.pos < len(self.text) else ''

    def advance(self):
        ch = self.text[self.pos]
        code = ord(ch)
        self.byte += 1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4
        if ch == '\n':
            self.line += 1
        self.pos += 1

    def skip_space(self):
        # 值之外的 % 到行尾都是注释。
        while not self.at_end():
            ch = self.peek()
            if ch in WHITESPACE:
                self.advance()
            elif ch == '%':
                while not self.at_end() and self.peek() != '\n':
                    self.advance()
            else:
                return

    def read_identifier(self):
        start = self.pos
        while not self.at_end() and self.peek() in IDENT_CHARS:
            self.advance()
        return self.text[start:self.pos]


def _read_quoted(scanner):
    # 引号值：花括号需配对，括号内的 " 不结束值。
    scanner.advance()
    start = scanner.mark()
    depth = 0
    while not scanner.at_end():
        ch = scanner.peek()
        if ch == '{':
            depth += 1
        elif ch == '}':
            if depth == 0:
                raise _Error('引号值中出现未配对的花括号', scanner.mark())
            depth -= 1
        elif ch == '"' and depth == 0:
            text = scanner.text[start[0]:scanner.pos]
            scanner.advance()
            return text
        scanner.advance()
    raise _Error('引号值未闭合', start)


def _read_braced(scanner):
    # 花括号值：配对嵌套，内容原样保留。
    scanner.advance()
    start = scanner.mark()
    depth = 1
    while not scanner.at_end():
        ch = scanner.peek()
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                text = scanner.text[start[0]:scanner.pos]
                scanner.advance()
                return text
        scanner.advance()
    raise _Error('花括号值未闭合', start)


def _read_value_part(scanner):
    scanner.skip_space()
    mark = scanner.mark()
    ch = scanner.peek()
    if ch == '"':
        return {'kind': 'literal', 'text': _read_quoted(scanner),
                'span': scanner.span(mark)}
    if ch == '{':
        return {'kind': 'literal', 'text': _read_braced(scanner),
                'span': scanner.span(mark)}
    if ch.isdigit():
        start = scanner.pos
        while not scanner.at_end() and scanner.peek().isdigit():
            scanner.advance()
        return {'kind': 'number', 'text': scanner.text[start:scanner.pos],
                'span': scanner.span(mark)}
    name = scanner.read_identifier()
    if name:
        return {'kind': 'macro', 'name': name, 'span': scanner.span(mark)}
    raise _Error('无法识别的值起始', mark)


def _read_value(scanner):
    parts = [_read_value_part(scanner)]
    while True:
        scanner.skip_space()
        if scanner.peek() == '#':
            scanner.advance()
            parts.append(_read_value_part(scanner))
        else:
            return parts


def _parts_span(parts):
    first, last = parts[0]['span'], parts[-1]['span']
    return {'start': first['start'], 'end': last['end'],
            'byte_start': first['byte_start'], 'byte_end': last['byte_end'],
            'line': first['line']}


def _expect_close(scanner, closer, start):
    scanner.skip_space()
    if scanner.peek() != closer:
        raise _Error('条目未闭合', start)
    scanner.advance()


def _skip_balanced(scanner, closer, start):
    opener = '{' if closer == '}' else '('
    depth = 1
    while not scanner.at_end():
        ch = scanner.peek()
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                scanner.advance()
                return
        scanner.advance()
    raise _Error('注释未闭合', start)


def _read_item(scanner):
    start = scanner.mark()
    scanner.advance()  # '@'
    scanner.skip_space()
    kind_mark = scanner.mark()
    kind = scanner.read_identifier()
    if not kind or not kind[0].isalpha():
        raise _Error('@ 后缺少条目类型', kind_mark)
    kind_lower = kind.casefold()
    scanner.skip_space()
    if scanner.at_end():
        raise _Error('条目缺少开始定界符', scanner.mark())
    ch = scanner.peek()
    if kind_lower == 'comment' and ch not in '{(':
        while not scanner.at_end() and scanner.peek() != '\n':
            scanner.advance()
        return {'kind': 'comment', 'span': scanner.span(start)}
    if ch not in '{(':
        raise _Error('条目缺少开始定界符', scanner.mark())
    closer = '}' if ch == '{' else ')'
    scanner.advance()
    if kind_lower == 'comment':
        _skip_balanced(scanner, closer, start)
        return {'kind': 'comment', 'span': scanner.span(start)}
    if kind_lower == 'preamble':
        value = _read_value(scanner)
        _expect_close(scanner, closer, start)
        return {'kind': 'preamble', 'value': value, 'span': scanner.span(start)}
    if kind_lower == 'string':
        scanner.skip_space()
        name_mark = scanner.mark()
        name = scanner.read_identifier()
        if not name or not name[0].isalpha():
            raise _Error('@string 缺少宏名', name_mark)
        scanner.skip_space()
        if scanner.peek() != '=':
            raise _Error('@string 缺少等号', scanner.mark())
        scanner.advance()
        value = _read_value(scanner)
        _expect_close(scanner, closer, start)
        return {'kind': 'string', 'name': name,
                'name_span': scanner.span(name_mark), 'value': value,
                'value_span': _parts_span(value), 'span': scanner.span(start)}
    # 常规条目：键到第一个逗号，随后是 名 = 值 列表。
    scanner.skip_space()
    key_mark = scanner.mark()
    while not scanner.at_end() and scanner.peek() not in (',', closer):
        scanner.advance()
    key = scanner.text[key_mark[0]:scanner.pos].strip()
    scanner.skip_space()
    ch = scanner.peek()
    if ch == closer:
        scanner.advance()
        if not key:
            raise _Error('条目缺少键', key_mark)
        return {'kind': 'entry', 'type': kind, 'key': key, 'fields': [],
                'span': scanner.span(start)}
    if ch != ',':
        raise _Error('条目键后缺少逗号', scanner.mark())
    scanner.advance()
    fields = []
    while True:
        scanner.skip_space()
        if scanner.peek() == closer:
            scanner.advance()
            break
        field_mark = scanner.mark()
        name = scanner.read_identifier()
        if not name or not name[0].isalpha():
            raise _Error('字段缺少名称', field_mark)
        scanner.skip_space()
        if scanner.peek() != '=':
            raise _Error('字段缺少等号', scanner.mark())
        scanner.advance()
        value = _read_value(scanner)
        fields.append({'name': name, 'value': value,
                       'value_span': _parts_span(value)})
        scanner.skip_space()
        ch = scanner.peek()
        if ch == ',':
            scanner.advance()
            continue
        if ch == closer:
            scanner.advance()
            break
        raise _Error('字段后缺少逗号或结束定界符', scanner.mark())
    return {'kind': 'entry', 'type': kind, 'key': key, 'fields': fields,
            'span': scanner.span(start)}


def _resync(scanner):
    # 出错后只信任下一行行首（允许前导空白）的 @，与查看器口径一致。
    while not scanner.at_end():
        if scanner.peek() == '\n':
            scanner.advance()
            while not scanner.at_end() and scanner.peek() in ' \t':
                scanner.advance()
            if scanner.peek() == '@':
                return
        else:
            scanner.advance()


def _scan_items(text):
    scanner = _Scanner(text)
    items = []
    line_start = True
    while not scanner.at_end():
        ch = scanner.peek()
        if ch == '@' and line_start:
            start = scanner.mark()
            try:
                items.append(_read_item(scanner))
                line_start = False
            except _Error as error:
                _resync(scanner)
                items.append({'kind': 'broken', 'error': error.message,
                              'error_span': _point(error.mark),
                              'span': scanner.span(start)})
                line_start = True  # 重同步停在行首 @ 或末尾
            continue
        if ch == '\n':
            line_start = True
            scanner.advance()
        elif ch in WHITESPACE:
            scanner.advance()
        elif ch == '%':
            while not scanner.at_end() and scanner.peek() != '\n':
                scanner.advance()
        else:
            line_start = False
            scanner.advance()
    return items


# ---------------------------------------------------------------- 阅读文本清洗

def _read_group(text, i):
    # text[i] == '{'；返回 (内部文本, 右括号后索引)。未配对时取到末尾。
    depth = 1
    j = i + 1
    while j < len(text):
        ch = text[j]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return text[i + 1:], len(text)


def _convert_command(text, i):
    # 已知重音与特殊字母转成检索字符；未知命令连同花括号参数原样保留。
    n = len(text)
    j = i + 1
    if j >= n:
        return '\\', j
    ch = text[j]
    if ch.isalpha():
        k = j
        while k < n and text[k].isalpha():
            k += 1
        name = text[j:k]
        raw = text[i:k]
        if name in SPECIALS:
            return SPECIALS[name], k
        if not (len(name) == 1 and name in ACCENTS):
            while k < n and text[k] == '{':
                _, k = _read_group(text, k)
            return text[i:k], k
        mark = ACCENTS[name]
    elif ch in ACCENTS:
        mark = ACCENTS[ch]
        k = j + 1
        raw = text[i:k]
    elif ch in ESCAPED:
        return ESCAPED[ch], j + 1
    else:
        return text[i:j + 1], j + 1
    if k < n and text[k] == '{':
        inner, k = _read_group(text, k)
        base = normalize(inner)
    elif k < n:
        base = text[k]
        k += 1
    else:
        return raw, k
    if not base:
        return raw, k
    return unicodedata.normalize('NFC', base[0] + mark) + base[1:], k


def normalize(text):
    """检索表示：去保护括号、转换已知 LaTeX 命令、折叠空白。原件不受影响。"""
    if text is None:
        return None
    out = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '{':
            inner, i = _read_group(text, i)
            out.append(normalize(inner))
        elif ch == '}':
            i += 1  # 未配对右括号不进入检索文本
        elif ch == '\\':
            rep, i = _convert_command(text, i)
            out.append(rep)
        else:
            out.append(ch)
            i += 1
    return ' '.join(''.join(out).split())


def split_names(value):
    """按顶层 and 拆分姓名；保护括号内的 and 与逗号不算分隔。"""
    parts = []
    depth = 0
    start = 0
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth = max(0, depth - 1)
        elif depth == 0 and ch in WHITESPACE:
            j = i
            while j < n and value[j] in WHITESPACE:
                j += 1
            if value[j:j + 3] == 'and' and j + 3 < n and value[j + 3] in WHITESPACE:
                parts.append(value[start:i])
                k = j + 3
                while k < n and value[k] in WHITESPACE:
                    k += 1
                start = k
                i = k
                continue
            i = j
            continue
        i += 1
    parts.append(value[start:])
    return [part for part in (part.strip() for part in parts) if part]


def _split_top(text, sep):
    parts = []
    depth = 0
    start = 0
    for i, ch in enumerate(text):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth = max(0, depth - 1)
        elif ch == sep and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [part.strip() for part in parts]


def _is_protected(raw):
    if len(raw) < 2 or raw[0] != '{':
        return False
    _, j = _read_group(raw, 0)
    return j == len(raw)


def describe_name(raw):
    """单个姓名的原表示与检索表示；保护括号整体包住时不拆姓名部件。"""
    components = [normalize(part) for part in _split_top(raw, ',')]
    result = {'raw': raw, 'search': normalize(raw), 'components': components}
    if _is_protected(raw):
        result['protected'] = True
    elif len(components) == 2:
        result['family'] = components[0]
        result['given'] = components[1]
    elif len(components) == 3:
        result['family'] = components[0]
        result['suffix'] = components[1]
        result['given'] = components[2]
    return result


# ---------------------------------------------------------------- 语义求值

def _eval_value(parts, macros):
    out = []
    used = {}
    problems = []
    ok = True
    for part in parts:
        kind = part['kind']
        if kind in ('literal', 'number'):
            out.append(part['text'])
            continue
        hit = macros.get(part['name'].casefold())
        used[part['name']] = hit['value'] if hit else None
        if hit and hit['value'] is not None:
            out.append(hit['value'])
        else:
            ok = False
            reason = '宏声明未成功求值：' if hit else '未定义的宏：'
            problems.append({'code': 'undefined-macro',
                             'message': reason + part['name'],
                             'span': part['span']})
    return (''.join(out) if ok else None), problems, used


def _build_entry(item, ordinal, macros, text):
    diagnostics = []
    fields = []
    seen = {}
    for field in item['fields']:
        value, problems, used = _eval_value(field['value'], macros)
        for problem in problems:
            diagnostics.append(dict(problem, field=field['name']))
        raw = text[field['value_span']['start']:field['value_span']['end']]
        name_key = field['name'].casefold()
        if name_key in seen:
            diagnostics.append({'code': 'duplicate-field', 'field': field['name'],
                                'message': '字段重复：' + field['name'],
                                'span': field['value_span'],
                                'kept': seen[name_key]['raw'], 'dropped': raw})
            continue
        record = {
            'name': field['name'], 'raw': raw, 'value': value,
            'text': normalize(value) if value is not None else None,
            'resolved': value is not None,
            'span': field['value_span'], 'macros': used,
            'provenance': {'state': 'explicit'} if value is not None else
                          {'state': 'unresolved', 'reason': 'undefined-macro'},
            'shadowed': [],
        }
        seen[name_key] = record
        fields.append(record)
    if not item['key']:
        diagnostics.append({'code': 'missing-key', 'message': '条目缺少键',
                            'span': item['span']})
    return {'key': item['key'], 'type': item['type'], 'ordinal': ordinal,
            'span': item['span'], 'explicit': fields, 'diagnostics': diagnostics}


def _build_edges(entry, config, by_key):
    edges = []
    fields = {f['name'].casefold(): f for f in entry['explicit']}
    xdata = fields.get('xdata')
    if xdata is not None:
        if not config.xdata:
            edges.append({'via': 'xdata', 'target': None, 'status': 'disabled'})
            entry['diagnostics'].append(
                {'code': 'xdata-disabled', 'message': '配置未启用 xdata 继承'})
        elif not xdata['resolved']:
            edges.append({'via': 'xdata', 'target': None, 'status': 'unresolved'})
            entry['diagnostics'].append(
                {'code': 'unresolved-inheritance', 'via': 'xdata',
                 'message': 'xdata 目标无法求值'})
        else:
            for target in (t.strip() for t in xdata['value'].split(',') if t.strip()):
                edges.append({'via': 'xdata', 'target': target, 'status': None})
    crossref = fields.get('crossref')
    if crossref is not None:
        if not crossref['resolved']:
            edges.append({'via': 'crossref', 'target': None, 'status': 'unresolved'})
            entry['diagnostics'].append(
                {'code': 'unresolved-inheritance', 'via': 'crossref',
                 'message': 'crossref 目标无法求值'})
        else:
            edges.append({'via': 'crossref',
                          'target': crossref['value'].strip(), 'status': None})
    for edge in edges:
        if edge['status'] is not None:
            continue
        target = edge['target']
        group = by_key.get(target) if target else None
        if not target or group is None:
            edge['status'] = 'missing'
            entry['diagnostics'].append(
                {'code': 'missing-target', 'via': edge['via'], 'target': target,
                 'message': '继承目标不存在：' + (target or '')})
        elif len(group) > 1:
            edge['status'] = 'ambiguous'
            entry['diagnostics'].append(
                {'code': 'ambiguous-target', 'via': edge['via'], 'target': target,
                 'message': '继承目标键重复，不任选：' + target})
        else:
            edge['donor'] = group[0]['ordinal']
            edge['status'] = 'ok'
    return edges


def _tarjan(nodes, graph):
    index_of = {}
    lowlink = {}
    on_stack = set()
    stack = []
    counter = 0
    sccs = []
    for root in nodes:
        if root in index_of:
            continue
        index_of[root] = lowlink[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(graph.get(root, ())))]
        while work:
            node, iterator = work[-1]
            descended = False
            for nxt in iterator:
                if nxt not in index_of:
                    index_of[nxt] = lowlink[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(graph.get(nxt, ()))))
                    descended = True
                    break
                if nxt in on_stack:
                    lowlink[node] = min(lowlink[node], index_of[nxt])
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[node])
            if lowlink[node] == index_of[node]:
                scc = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    scc.append(member)
                    if member == node:
                        break
                sccs.append(scc)
    return sccs


def _mark_cycles(entries):
    graph = {}
    for entry in entries:
        donors = [edge['donor'] for edge in entry['edges'] if edge['status'] == 'ok']
        if donors:
            graph[entry['ordinal']] = donors
    by_ordinal = {entry['ordinal']: entry for entry in entries}
    for scc in _tarjan([entry['ordinal'] for entry in entries], graph):
        cyclic = len(scc) > 1 or scc[0] in graph.get(scc[0], ())
        if not cyclic:
            continue
        members = sorted(scc)
        keys = [by_ordinal[member]['key'] for member in members]
        for member in members:
            entry = by_ordinal[member]
            entry['diagnostics'].append(
                {'code': 'inheritance-cycle', 'members': keys,
                 'message': '继承环：' + '、'.join(keys)})
            for edge in entry['edges']:
                if edge['status'] == 'ok' and edge['donor'] in scc:
                    edge['status'] = 'cycle'


def _merge_fields(entries):
    # 环边已移除，继承图是 DAG；按 donor 优先的顺序合并。
    by_ordinal = {entry['ordinal']: entry for entry in entries}
    visited = set()
    order = []
    for entry in entries:
        if entry['ordinal'] in visited:
            continue
        stack = [(entry, False)]
        while stack:
            node, expanded = stack.pop()
            if not expanded:
                if node['ordinal'] in visited:
                    continue
                visited.add(node['ordinal'])
                stack.append((node, True))
                for edge in node['edges']:
                    if edge['status'] == 'ok':
                        donor = by_ordinal[edge['donor']]
                        if donor['ordinal'] not in visited:
                            stack.append((donor, False))
            else:
                order.append(node)
    for entry in order:
        merged = {}
        merged_order = []
        for field in entry['explicit']:
            key = field['name'].casefold()
            merged[key] = field
            merged_order.append(key)
        for edge in entry['edges']:
            if edge['status'] != 'ok':
                continue
            donor = by_ordinal[edge['donor']]
            for name, record in donor['merged'].items():
                if name in META_FIELDS:
                    continue
                if name in merged:
                    winner = merged[name]
                    shadow = {'from': donor['key'], 'via': edge['via'],
                              'root': record['provenance'].get('root', donor['key']),
                              'value': record['value'],
                              'conflict': record['value'] != winner['value']}
                    if shadow not in winner['shadowed']:
                        winner['shadowed'].append(shadow)
                    continue
                copy = dict(record)
                root = record['provenance'].get('root', donor['key'])
                if record['resolved']:
                    provenance = {'state': 'inherited', 'via': edge['via'],
                                  'from': donor['key'], 'root': root}
                else:
                    provenance = {'state': 'unresolved',
                                  'reason': record['provenance'].get(
                                      'reason', 'undefined-macro'),
                                  'via': edge['via'], 'from': donor['key'],
                                  'root': root}
                copy['provenance'] = provenance
                copy['shadowed'] = []
                copy['macros'] = {}  # 宏在来源条目求值，继承副本不重复记
                merged[name] = copy
                merged_order.append(name)
        entry['merged'] = merged
        entry['merged_order'] = merged_order


def parse_catalog(data, config=None):
    """解析整个目录，返回条目、宏、诊断与统计；坏记录只记入诊断。"""
    config = config or ParseConfig()
    if isinstance(data, (bytes, bytearray)):
        try:
            text = bytes(data).decode('utf8')
        except UnicodeDecodeError:
            raise ValueError('来源不是有效 UTF-8')
    else:
        text = data
    items = _scan_items(text)
    if not items:
        raise ValueError('没有可解析的 BibTeX 条目')
    macros = {name: {'name': name, 'value': value, 'span': None}
              for name, value in MONTHS.items()}
    macro_records = []
    entries = []
    catalog_diags = []
    broken = comments = preambles = 0
    for ordinal, item in enumerate(items):
        kind = item['kind']
        if kind == 'broken':
            broken += 1
            catalog_diags.append({'code': 'parse-error', 'message': item['error'],
                                  'span': item['error_span'],
                                  'item_span': item['span']})
        elif kind == 'comment':
            comments += 1
        elif kind == 'preamble':
            preambles += 1
            _, problems, _ = _eval_value(item['value'], macros)
            for problem in problems:
                catalog_diags.append(dict(problem, preamble=True))
        elif kind == 'string':
            value, problems, used = _eval_value(item['value'], macros)
            span = item['value_span']
            macro_records.append({
                'name': item['name'], 'value': value,
                'resolved': value is not None,
                'raw': text[span['start']:span['end']], 'used': used,
                'span': item['span'], 'ordinal': ordinal})
            macros[item['name'].casefold()] = {
                'name': item['name'], 'value': value, 'span': item['span']}
            for problem in problems:
                catalog_diags.append(dict(problem, macro=item['name']))
        elif kind == 'entry':
            entries.append(_build_entry(item, ordinal, macros, text))
    by_key = {}
    for entry in entries:
        if entry['key']:
            by_key.setdefault(entry['key'], []).append(entry)
    dup_diags = []
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        ordinals = [member['ordinal'] for member in group]
        for member in group:
            member['diagnostics'].append(
                {'code': 'duplicate-key', 'key': key, 'ordinals': ordinals,
                 'message': '条目键重复：' + key})
        dup_diags.append({'code': 'duplicate-key', 'key': key,
                          'message': '条目键重复：' + key,
                          'spans': [member['span'] for member in group]})
    dup_diags.sort(key=lambda diag: diag['spans'][0]['start'])
    catalog_diags.extend(dup_diags)
    for entry in entries:
        entry['edges'] = _build_edges(entry, config, by_key)
    _mark_cycles(entries)
    _merge_fields(entries)
    out_entries = []
    for entry in entries:
        fields = [entry['merged'][name] for name in entry['merged_order']]
        for field in fields:
            if field['name'].casefold() in NAME_FIELDS and field['resolved']:
                field['names'] = [describe_name(part)
                                  for part in split_names(field['value'])]
        problems = {diag['code'] for diag in entry['diagnostics']}
        if any(not field['resolved'] for field in fields):
            problems.add('unresolved-field')
        out_entries.append({
            'key': entry['key'], 'type': entry['type'],
            'ordinal': entry['ordinal'], 'span': entry['span'],
            'fields': fields,
            'inheritance': [{'via': edge['via'], 'target': edge['target'],
                             'status': edge['status']}
                            for edge in entry['edges']],
            'diagnostics': entry['diagnostics'],
            'problems': sorted(problems)})
    diag_total = len(catalog_diags) + sum(len(entry['diagnostics'])
                                          for entry in entries)
    return {'format': FORMAT, 'config': config.as_json(),
            'entries': out_entries, 'macros': macro_records,
            'diagnostics': catalog_diags,
            'stats': {'items': len(items), 'entries': len(entries),
                      'macros': len(macro_records), 'broken': broken,
                      'comments': comments, 'preambles': preambles,
                      'diagnostics': diag_total}}


# ---------------------------------------------------------------- 查询与对比

def find_entries(catalog, key):
    return [entry for entry in catalog['entries'] if entry['key'] == key]


def field_of(entry, name):
    key = name.casefold()
    for field in entry['fields']:
        if field['name'].casefold() == key:
            return field
    return None


def macro_usage(catalog, name):
    """宏的定义与使用位置，用来解释中途改宏对不同条目的影响。"""
    folded = name.casefold()
    definitions = [{'value': record['value'], 'resolved': record['resolved'],
                    'line': record['span']['line']}
                   for record in catalog['macros']
                   if record['name'].casefold() == folded]
    uses = []
    for entry in catalog['entries']:
        for field in entry['fields']:
            for written, value in field.get('macros', {}).items():
                if written.casefold() == folded:
                    uses.append({'entry': entry['key'], 'field': field['name'],
                                 'value': value, 'line': field['span']['line']})
    return {'name': name, 'definitions': definitions, 'uses': uses}


def _field_summary(field):
    if field is None:
        return None
    provenance = field['provenance']
    summary = {'state': provenance['state'], 'text': field['text']}
    if provenance['state'] == 'inherited':
        summary.update({'from': provenance['from'], 'via': provenance['via'],
                        'root': provenance['root']})
    if provenance['state'] == 'unresolved':
        summary['reason'] = provenance.get('reason')
    return summary


def _short(text, limit=40):
    if text is None:
        return '无值'
    return text if len(text) <= limit else text[:limit] + '…'


def _describe(summary):
    if summary is None:
        return '缺失'
    state = summary['state']
    if state == 'explicit':
        return '显式（%s）' % _short(summary['text'])
    if state == 'inherited':
        return '继承自 %s（%s）（%s）' % (
            summary['from'], summary['via'], _short(summary['text']))
    return '未解引用（%s）' % (summary.get('reason') or '')


def diff_catalogs(old, new):
    """对比两次解析，逐条目逐字段解释结果变化。"""
    changes = []
    old_entries = {entry['ordinal']: entry for entry in old['entries']}
    new_entries = {entry['ordinal']: entry for entry in new['entries']}
    for ordinal in sorted(set(old_entries) | set(new_entries)):
        old = old_entries.get(ordinal)
        new = new_entries.get(ordinal)
        label = (old or new)['key'] or ('#%d' % ordinal)
        if old is None:
            changes.append({'entry': label, 'field': None, 'before': None,
                            'after': '条目出现',
                            'message': '条目 %s 出现' % label})
            continue
        if new is None:
            changes.append({'entry': label, 'field': None, 'before': '条目消失',
                            'after': None,
                            'message': '条目 %s 消失' % label})
            continue
        old_fields = {f['name'].casefold(): f for f in old['fields']}
        new_fields = {f['name'].casefold(): f for f in new['fields']}
        names = [f['name'].casefold() for f in old['fields']]
        names += [f['name'].casefold() for f in new['fields']
                  if f['name'].casefold() not in old_fields]
        for name in names:
            before = _field_summary(old_fields.get(name))
            after = _field_summary(new_fields.get(name))
            if before == after:
                continue
            field_name = (old_fields.get(name) or new_fields.get(name))['name']
            changes.append({
                'entry': label, 'field': field_name,
                'before': before, 'after': after,
                'message': '条目 %s 字段 %s：%s → %s' % (
                    label, field_name, _describe(before), _describe(after))})
        for code in sorted(set(old['problems']) - set(new['problems'])):
            changes.append({'entry': label, 'field': None, 'before': code,
                            'after': None,
                            'message': '条目 %s 问题解除：%s' % (label, code)})
        for code in sorted(set(new['problems']) - set(old['problems'])):
            changes.append({'entry': label, 'field': None, 'before': None,
                            'after': code,
                            'message': '条目 %s 新增问题：%s' % (label, code)})
    return changes
