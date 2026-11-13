import argparse
import base64
import json
from pathlib import Path
import sys
import tempfile

from bibtex import (ParseConfig, diff_catalogs, field_of, find_entries,
                    macro_usage, parse_catalog)
from source import inspect_bytes
from sampledata import samples
from storage import Archive
from transport import ReplaySource, fetch

MAX_INPUT = 16*1024*1024

def read_input(path):
    with Path(path).open('rb') as handle:
        data = handle.read(MAX_INPUT+1)
    if len(data) > MAX_INPUT:
        raise ValueError('输入超过接收限制')
    return data

def demo():
    fixtures = samples()
    name = next(key for key in fixtures if key.startswith('sample.'))
    data = fixtures[name]
    routes = {'GET /sample':[{'base64':base64.b64encode(data).decode()}]}
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        status, headers, raw = fetch(server.url + '/sample')
        if status != 200:
            raise ValueError('来源请求失败')
        rows = inspect_bytes(raw)
        with Archive(Path(directory)/'archive.sqlite3') as archive:
            first = archive.save('local-demo', raw, rows)
            repeated = archive.save('local-demo', raw, rows)
            return dict(input=name, bytes=len(raw), captures=len(archive.list()),
                        same_capture=first==repeated, rows=rows, requests=len(server.requests))

def capture_archive(archive, source, raw):
    rows = inspect_bytes(raw)
    catalog = parse_catalog(raw)
    capture_id = archive.save(source, raw, rows)
    result = {'capture_id': capture_id, 'rows': len(rows)}
    if not archive.parses(capture_id):
        result['parse'] = archive.save_parse(capture_id, catalog)
        result['entries'] = len(catalog['entries'])
        result['problems'] = catalog['stats']['diagnostics']
    return result

def run_query(archive, args):
    if args.capture is not None:
        capture_id = args.capture
        archive.raw(capture_id)
        note = None
        ids = None
    else:
        if not args.source:
            raise ValueError('query 需要 --source 或 --capture')
        ids = archive.captures_by_source(args.source)
        if not ids:
            raise ValueError('来源没有收件：' + args.source)
        capture_id = ids[-1]
        note = '来源有多个收件，使用最新；可用 --capture 指定' if len(ids) > 1 else None
    catalog = archive.catalog(capture_id, args.parse)
    if catalog is None:
        raise ValueError('该收件没有对应解析记录，请先 reparse')
    seq = args.parse if args.parse is not None else archive.parses(capture_id)[-1]['seq']
    result = {'capture': capture_id, 'parse': seq, 'key': args.key,
              'matches': find_entries(catalog, args.key),
              'diagnostics': [diag for diag in catalog['diagnostics']
                              if diag.get('key') == args.key or diag.get('entry') == args.key]}
    if note:
        result['note'] = note
        result['captures'] = ids
    return result

def catalog_demo():
    """用含逗号人名、跨条目继承和中途改宏的目录演示解析、查询与重新解析。"""
    fixtures = samples()
    paths = (('/bibliography', 'sample.bib', 'demo-main'),
             ('/xdata', 'xdata.bib', 'demo-xdata'),
             ('/cycle', 'cycle.bib', 'demo-cycle'))
    routes = {'GET ' + path: [dict(base64=base64.b64encode(fixtures[name]).decode())]
              for path, name, _ in paths}
    with tempfile.TemporaryDirectory() as directory, ReplaySource(routes) as server:
        with Archive(Path(directory)/'archive.sqlite3') as archive:
            catalogs = {}
            capture_ids = {}
            for path, name, source in paths:
                status, headers, raw = fetch(server.url + path)
                if status != 200:
                    raise ValueError('来源请求失败')
                capture_ids[source] = capture_archive(archive, source, raw)['capture_id']
                catalogs[source] = archive.catalog(capture_ids[source])
            main = catalogs['demo-main']
            xdata_cat = catalogs['demo-xdata']
            cycle_cat = catalogs['demo-cycle']
            paper = find_entries(main, 'paper')[0]
            conf = find_entries(main, 'conf')[0]
            later = find_entries(main, 'later')[0]
            vol = find_entries(xdata_cat, 'vol')[0]
            loose = find_entries(xdata_cat, 'loose')[0]
            xdata_id = capture_ids['demo-xdata']
            reparsed = parse_catalog(archive.raw(xdata_id), ParseConfig(xdata=False))
            seq = archive.save_parse(xdata_id, reparsed)
            return {
                'captures': len(archive.list()),
                'requests': len(server.requests),
                'comma_names': field_of(paper, 'author')['names'],
                'inheritance': {
                    'paper.publisher': field_of(paper, 'publisher')['provenance'],
                    'paper.title_shadowed': field_of(paper, 'title')['shadowed'],
                    'vol.fields': {f['name']: f['provenance'] for f in vol['fields']},
                },
                'macro_shift': {
                    'conf.publisher': field_of(conf, 'publisher')['macros'],
                    'later.title': field_of(later, 'title')['macros'],
                    'venue': macro_usage(main, 'venue'),
                },
                'cycle': {e['key']: e['problems'] for e in cycle_cat['entries']},
                'isolation': {
                    'broken_diags': [d for d in xdata_cat['diagnostics']
                                     if d['code'] == 'parse-error'],
                    'loose.title': field_of(loose, 'title')['text'],
                    'loose.journal': field_of(loose, 'journal')['provenance'],
                },
                'reparse': {'capture': xdata_id, 'parse': seq,
                            'changes': diff_catalogs(xdata_cat, reparsed)},
            }

def main(argv=None):
    parser = argparse.ArgumentParser(description='来源检查、原件留存与目录解析')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('inspect', 'capture', 'parse'):
        command = commands.add_parser(name)
        command.add_argument('path')
        if name == 'capture':
            command.add_argument('--database', required=True)
            command.add_argument('--source', required=True)
        if name == 'parse':
            command.add_argument('--no-xdata', action='store_true')
    listing = commands.add_parser('list')
    listing.add_argument('--database', required=True)
    fixtures = commands.add_parser('fixtures')
    fixtures.add_argument('directory')
    commands.add_parser('demo')
    commands.add_parser('catalog-demo')
    query = commands.add_parser('query')
    query.add_argument('--database', required=True)
    query.add_argument('--source')
    query.add_argument('--capture', type=int)
    query.add_argument('--key', required=True)
    query.add_argument('--parse', type=int)
    reparse = commands.add_parser('reparse')
    reparse.add_argument('--database', required=True)
    reparse.add_argument('--capture', type=int, required=True)
    reparse.add_argument('--no-xdata', action='store_true')
    parses = commands.add_parser('parses')
    parses.add_argument('--database', required=True)
    parses.add_argument('--capture', type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'demo':
            result = demo()
        elif args.command == 'catalog-demo':
            result = catalog_demo()
        elif args.command == 'fixtures':
            destination = Path(args.directory)
            destination.mkdir(parents=True, exist_ok=True)
            for name, raw in samples().items():
                path = destination/name
                if path.exists():
                    raise ValueError('不覆盖已有夹具文件：'+name)
                path.write_bytes(raw)
            result = dict(files=sorted(samples()))
        elif args.command == 'list':
            with Archive(args.database) as archive:
                result = archive.list()
        elif args.command == 'query':
            with Archive(args.database) as archive:
                result = run_query(archive, args)
        elif args.command == 'reparse':
            with Archive(args.database) as archive:
                raw = archive.raw(args.capture)
                config = ParseConfig(xdata=not args.no_xdata)
                catalog = parse_catalog(raw, config)
                previous = archive.catalog(args.capture)
                seq = archive.save_parse(args.capture, catalog)
                result = {'capture': args.capture, 'parse': seq,
                          'config': config.as_json(), 'entries': len(catalog['entries']),
                          'changes': diff_catalogs(previous, catalog) if previous else []}
        elif args.command == 'parses':
            with Archive(args.database) as archive:
                archive.raw(args.capture)
                result = archive.parses(args.capture)
        elif args.command == 'parse':
            result = parse_catalog(read_input(args.path),
                                   ParseConfig(xdata=not args.no_xdata))
        elif args.command == 'inspect':
            result = inspect_bytes(read_input(args.path))
        else:
            raw = read_input(args.path)
            with Archive(args.database) as archive:
                result = capture_archive(archive, args.source, raw)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as error:
        print(json.dumps(dict(error=type(error).__name__, reason=str(error)), ensure_ascii=False), file=sys.stderr)
        return 2

if __name__ == '__main__':
    raise SystemExit(main())
