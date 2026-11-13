import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import app
from bibtex import (ParseConfig, describe_name, diff_catalogs, field_of,
                    find_entries, macro_usage, normalize, parse_catalog,
                    split_names)
from sampledata import samples
from storage import Archive


def parse(text, config=None):
    data = text.encode('utf8') if isinstance(text, str) else text
    return parse_catalog(data, config)


class LexerTests(unittest.TestCase):
    def test_concat_quotes_and_nested_braces(self):
        catalog = parse(
            '@string{venue = "Port" # " Conference"}\n'
            '@book{k, title={Annual {Port} Meeting}, note="he said {"hi"} ok", year=2024}\n')
        entry = catalog['entries'][0]
        self.assertEqual(field_of(entry, 'title')['value'], 'Annual {Port} Meeting')
        self.assertEqual(field_of(entry, 'title')['text'], 'Annual Port Meeting')
        self.assertEqual(field_of(entry, 'note')['value'], 'he said {"hi"} ok')
        self.assertEqual(field_of(entry, 'year')['value'], '2024')
        self.assertEqual(catalog['macros'][0]['value'], 'Port Conference')

    def test_comments_and_parens(self):
        catalog = parse(
            '% @string{no = "x"}\n'
            '@comment{@book{no, title={No}}}\n'
            '@comment also ignored\n'
            '@book(k, title={T})\n')
        self.assertEqual([e['key'] for e in catalog['entries']], ['k'])
        self.assertEqual(catalog['stats']['comments'], 2)

    def test_positions_map_to_utf8_bytes(self):
        data = '% éü\n@book{k, title={Héllo {W}orld}}\n'.encode('utf8')
        catalog = parse_catalog(data)
        text = data.decode('utf8')
        entry = catalog['entries'][0]
        span = entry['span']
        self.assertEqual(span['line'], 2)
        self.assertEqual(text[span['start']:span['end']],
                         data[span['byte_start']:span['byte_end']].decode('utf8'))
        title = field_of(entry, 'title')
        tspan = title['span']
        self.assertEqual(text[tspan['start']:tspan['end']],
                         data[tspan['byte_start']:tspan['byte_end']].decode('utf8'))
        self.assertEqual(title['raw'], '{Héllo {W}orld}')
        self.assertEqual(title['text'], 'Héllo World')

    def test_broken_entry_does_not_hide_others(self):
        catalog = parse('@book{good, title={Fine}}\n@book{broken, title=}\n@book{after, title={Also}}\n')
        self.assertEqual([e['key'] for e in catalog['entries']], ['good', 'after'])
        self.assertEqual(catalog['stats']['broken'], 1)
        self.assertEqual(catalog['diagnostics'][0]['code'], 'parse-error')

    def test_no_entries_rejected(self):
        with self.assertRaises(ValueError):
            parse(samples()['invalid.bib'])
        with self.assertRaises(ValueError):
            parse(b'\xff\xfe not utf8')


class MacroTests(unittest.TestCase):
    def test_declaration_time_evaluation_and_redefinition(self):
        catalog = parse(
            '@string{a = "1"}\n'
            '@string{b = a # "2"}\n'
            '@string{a = "9"}\n'
            '@book{k, title = a # b}\n')
        self.assertEqual(catalog['macros'][1]['value'], '12')
        self.assertEqual(field_of(catalog['entries'][0], 'title')['value'], '912')

    def test_predefined_months(self):
        catalog = parse('@book{k, month = jan, year = 2024}\n')
        self.assertEqual(field_of(catalog['entries'][0], 'month')['value'], 'January')

    def test_undefined_macro_marks_field_unresolved(self):
        catalog = parse('@book{k, title={T}, journal=nope}\n')
        entry = catalog['entries'][0]
        journal = field_of(entry, 'journal')
        self.assertFalse(journal['resolved'])
        self.assertIsNone(journal['text'])
        self.assertEqual(journal['raw'], 'nope')
        self.assertEqual(journal['provenance']['state'], 'unresolved')
        self.assertEqual(journal['provenance']['reason'], 'undefined-macro')
        self.assertEqual(field_of(entry, 'title')['text'], 'T')
        self.assertIn('undefined-macro', entry['problems'])
        self.assertIn('unresolved-field', entry['problems'])


class NameTests(unittest.TestCase):
    def test_comma_names_and_protected_groups(self):
        catalog = parse('@book{k, author={de la Cruz, Ana and {Harbor Group}}}\n')
        names = field_of(catalog['entries'][0], 'author')['names']
        self.assertEqual(len(names), 2)
        self.assertEqual(names[0]['family'], 'de la Cruz')
        self.assertEqual(names[0]['given'], 'Ana')
        self.assertEqual(names[0]['raw'], 'de la Cruz, Ana')
        self.assertEqual(names[1]['search'], 'Harbor Group')
        self.assertTrue(names[1]['protected'])
        self.assertNotIn('family', names[1])

    def test_latex_commands_keep_raw_and_search_forms(self):
        catalog = parse('@book{k, author={G{\\"o}del, Kurt}}\n')
        name = field_of(catalog['entries'][0], 'author')['names'][0]
        self.assertEqual(name['raw'], 'G{\\"o}del, Kurt')
        self.assertEqual(name['search'], 'Gödel, Kurt')
        self.assertEqual(name['family'], 'Gödel')

    def test_and_inside_braces_does_not_split(self):
        self.assertEqual(split_names('{Harbor and Port} and Smith, Jo'),
                         ['{Harbor and Port}', 'Smith, Jo'])

    def test_normalize_unknown_commands_stay_verbatim(self):
        self.assertEqual(normalize('{A} \\foo{bar} \\ss'), 'A \\foo{bar} ß')

    def test_describe_name_suffix_form(self):
        name = describe_name('Cruz, Jr., Ana')
        self.assertEqual(name['family'], 'Cruz')
        self.assertEqual(name['suffix'], 'Jr.')
        self.assertEqual(name['given'], 'Ana')


class InheritanceTests(unittest.TestCase):
    def test_crossref_provenance_and_shadow_evidence(self):
        catalog = parse(samples()['sample.bib'])
        paper = find_entries(catalog, 'paper')[0]
        publisher = field_of(paper, 'publisher')
        self.assertEqual(publisher['provenance'],
                         {'state': 'inherited', 'via': 'crossref',
                          'from': 'conf', 'root': 'conf'})
        self.assertEqual(publisher['text'], 'Port Conference')
        title = field_of(paper, 'title')
        self.assertEqual(title['provenance']['state'], 'explicit')
        self.assertEqual(title['shadowed'][0]['from'], 'conf')
        self.assertEqual(title['shadowed'][0]['via'], 'crossref')
        self.assertTrue(title['shadowed'][0]['conflict'])
        later = find_entries(catalog, 'later')[0]
        self.assertEqual(field_of(later, 'title')['macros'], {'venue': 'New Venue'})
        conf = find_entries(catalog, 'conf')[0]
        self.assertEqual(field_of(conf, 'publisher')['macros'],
                         {'venue': 'Port Conference'})

    def test_forward_reference(self):
        catalog = parse('@inproceedings{p, crossref={c}}\n@proceedings{c, title={T}}\n')
        paper = find_entries(catalog, 'p')[0]
        self.assertEqual(field_of(paper, 'title')['provenance']['from'], 'c')

    def test_xdata_order_and_priority(self):
        catalog = parse(
            '@book{a, publisher={A}, year=2001}\n'
            '@book{b, publisher={B}}\n'
            '@book{c, xdata={a, b}, crossref={b}}\n')
        entry = find_entries(catalog, 'c')[0]
        publisher = field_of(entry, 'publisher')
        self.assertEqual(publisher['text'], 'A')
        self.assertEqual(publisher['provenance']['via'], 'xdata')
        shadow_froms = [(s['from'], s['via']) for s in publisher['shadowed']]
        self.assertIn(('b', 'xdata'), shadow_froms)
        self.assertIn(('b', 'crossref'), shadow_froms)
        self.assertEqual(field_of(entry, 'year')['provenance']['from'], 'a')

    def test_explicit_beats_inherited(self):
        catalog = parse('@book{a, title={A}, publisher={P}}\n'
                        '@book{c, title={C}, crossref={a}}\n')
        entry = find_entries(catalog, 'c')[0]
        self.assertEqual(field_of(entry, 'title')['text'], 'C')
        self.assertEqual(field_of(entry, 'title')['shadowed'][0]['value'], 'A')
        self.assertEqual(field_of(entry, 'publisher')['provenance']['from'], 'a')

    def test_xdata_disabled_by_config(self):
        text = '@book{a, publisher={A}}\n@book{c, xdata={a}}\n'
        catalog = parse(text, ParseConfig(xdata=False))
        entry = find_entries(catalog, 'c')[0]
        self.assertIsNone(field_of(entry, 'publisher'))
        self.assertEqual(entry['inheritance'][0]['status'], 'disabled')
        self.assertIn('xdata-disabled', entry['problems'])

    def test_cycle_reported_on_both_entries(self):
        catalog = parse(samples()['cycle.bib'])
        self.assertEqual(len(catalog['entries']), 2)
        for entry in catalog['entries']:
            self.assertIn('inheritance-cycle', entry['problems'])
            self.assertEqual([f['name'] for f in entry['fields']], ['crossref'])
        statuses = {e['key']: e['inheritance'][0]['status']
                    for e in catalog['entries']}
        self.assertEqual(statuses, {'a': 'cycle', 'b': 'cycle'})

    def test_duplicate_keys_and_ambiguous_target(self):
        catalog = parse('@book{dup, title={One}}\n@book{dup, title={Two}}\n'
                        '@book{ref, crossref={dup}}\n')
        dups = find_entries(catalog, 'dup')
        self.assertEqual(len(dups), 2)
        self.assertEqual({field_of(d, 'title')['text'] for d in dups},
                         {'One', 'Two'})
        for d in dups:
            self.assertIn('duplicate-key', d['problems'])
        ref = find_entries(catalog, 'ref')[0]
        self.assertEqual(ref['inheritance'][0]['status'], 'ambiguous')
        self.assertIsNone(field_of(ref, 'title'))
        self.assertIn('ambiguous-target', ref['problems'])
        self.assertTrue(any(d['code'] == 'duplicate-key'
                            for d in catalog['diagnostics']))

    def test_missing_target(self):
        catalog = parse('@book{ref, crossref={ghost}, title={T}}\n')
        ref = catalog['entries'][0]
        self.assertEqual(ref['inheritance'][0]['status'], 'missing')
        self.assertIn('missing-target', ref['problems'])
        self.assertEqual(field_of(ref, 'title')['text'], 'T')

    def test_transitive_inheritance_keeps_root(self):
        catalog = parse('@book{a, publisher={P}}\n@book{b, crossref={a}}\n'
                        '@book{c, crossref={b}}\n')
        entry = find_entries(catalog, 'c')[0]
        publisher = field_of(entry, 'publisher')
        self.assertEqual(publisher['provenance']['from'], 'b')
        self.assertEqual(publisher['provenance']['root'], 'a')


class DeterminismTests(unittest.TestCase):
    def test_replay_is_deterministic(self):
        for name in ('sample.bib', 'xdata.bib', 'cycle.bib'):
            data = samples()[name]
            first = json.dumps(parse_catalog(data), ensure_ascii=False, sort_keys=True)
            second = json.dumps(parse_catalog(data), ensure_ascii=False, sort_keys=True)
            self.assertEqual(first, second, name)

    def test_macro_usage_explains_midfile_change(self):
        catalog = parse(samples()['sample.bib'])
        usage = macro_usage(catalog, 'venue')
        self.assertEqual([d['value'] for d in usage['definitions']],
                         ['Port Conference', 'New Venue'])
        pairs = {(u['entry'], u['field']): u['value'] for u in usage['uses']}
        self.assertEqual(pairs[('conf', 'publisher')], 'Port Conference')
        self.assertEqual(pairs[('later', 'title')], 'New Venue')


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'archive.sqlite3'

    def test_parse_roundtrip_reparse_and_raw_preserved(self):
        data = samples()['xdata.bib']
        with Archive(self.path) as archive:
            cid = archive.save('src', data, [])
            self.assertEqual(archive.save_parse(cid, parse_catalog(data)), 1)
            reparsed = parse_catalog(data, ParseConfig(xdata=False))
            self.assertEqual(archive.save_parse(cid, reparsed), 2)
            self.assertEqual([p['seq'] for p in archive.parses(cid)], [1, 2])
            self.assertEqual(archive.parses(cid)[1]['config'], {'xdata': False})
            self.assertEqual(archive.catalog(cid)['config'], {'xdata': False})
            first = archive.catalog(cid, 1)
            changes = diff_catalogs(first, reparsed)
            messages = [c['message'] for c in changes]
            self.assertTrue(any('vol' in m and 'publisher' in m for m in messages))
            self.assertTrue(any('vol' in m and 'editor' in m for m in messages))
            self.assertEqual(archive.raw(cid), data)

    def test_reparse_same_config_is_identical(self):
        data = samples()['sample.bib']
        with Archive(self.path) as archive:
            cid = archive.save('src', data, [])
            archive.save_parse(cid, parse_catalog(data))
            archive.save_parse(cid, parse_catalog(data))
            self.assertEqual(archive.catalog(cid, 1), archive.catalog(cid, 2))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = Path(self.directory.name)

    def run_cli(self, argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = app.main(argv)
        text = out.getvalue()
        return code, json.loads(text) if text else None

    def test_parse_query_reparse_flow(self):
        path = self.dir/'xdata.bib'
        path.write_bytes(samples()['xdata.bib'])
        code, catalog = self.run_cli(['parse', str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(catalog['stats']['broken'], 1)
        self.assertEqual([e['key'] for e in catalog['entries']],
                         ['series', 'vol', 'loose'])
        database = str(self.dir/'store.sqlite3')
        code, captured = self.run_cli(
            ['capture', str(path), '--database', database, '--source', 'cli'])
        self.assertEqual(code, 0)
        self.assertEqual(captured['parse'], 1)
        self.assertEqual(captured['entries'], 3)
        code, found = self.run_cli(
            ['query', '--database', database, '--source', 'cli', '--key', 'vol'])
        self.assertEqual(code, 0)
        self.assertEqual(len(found['matches']), 1)
        prov = {f['name']: f['provenance'] for f in found['matches'][0]['fields']}
        self.assertEqual(prov['editor']['state'], 'inherited')
        self.assertEqual(prov['editor']['from'], 'series')
        self.assertEqual(prov['editor']['via'], 'xdata')
        self.assertEqual(prov['title']['state'], 'explicit')
        code, reparsed = self.run_cli(
            ['reparse', '--database', database, '--capture',
             str(captured['capture_id']), '--no-xdata'])
        self.assertEqual(code, 0)
        self.assertEqual(reparsed['parse'], 2)
        self.assertTrue(any(c['entry'] == 'vol' and c['field'] == 'editor'
                            and c['after'] is None
                            for c in reparsed['changes']))
        code, runs = self.run_cli(
            ['parses', '--database', database, '--capture',
             str(captured['capture_id'])])
        self.assertEqual(code, 0)
        self.assertEqual([r['seq'] for r in runs], [1, 2])
        code, latest = self.run_cli(
            ['query', '--database', database, '--source', 'cli', '--key', 'vol'])
        names = [f['name'] for f in latest['matches'][0]['fields']]
        self.assertNotIn('editor', names)
        code, first = self.run_cli(
            ['query', '--database', database, '--source', 'cli', '--key', 'vol',
             '--parse', '1'])
        names = [f['name'] for f in first['matches'][0]['fields']]
        self.assertIn('editor', names)

    def test_query_missing_source_and_capture(self):
        database = str(self.dir/'empty.sqlite3')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = app.main(['query', '--database', database,
                             '--source', 'nope', '--key', 'k'])
        self.assertEqual(code, 2)
        self.assertIn('来源没有收件', err.getvalue())

    def test_parse_invalid_fails(self):
        path = self.dir/'invalid.bib'
        path.write_bytes(samples()['invalid.bib'])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = app.main(['parse', str(path)])
        self.assertEqual(code, 2)
        self.assertIn('ValueError', err.getvalue())

    def test_catalog_demo(self):
        code, report = self.run_cli(['catalog-demo'])
        self.assertEqual(code, 0)
        self.assertEqual(report['captures'], 3)
        self.assertEqual(report['requests'], 3)
        names = report['comma_names']
        self.assertEqual(names[0]['family'], 'de la Cruz')
        self.assertEqual(names[1]['search'], 'Harbor Group')
        self.assertEqual(report['inheritance']['paper.publisher']['from'], 'conf')
        macro = report['macro_shift']
        self.assertEqual(macro['conf.publisher'], {'venue': 'Port Conference'})
        self.assertEqual(macro['later.title'], {'venue': 'New Venue'})
        self.assertEqual(set(report['cycle']), {'a', 'b'})
        self.assertEqual(report['isolation']['loose.title'], 'Studies')
        self.assertEqual(report['isolation']['loose.journal']['state'], 'unresolved')
        self.assertTrue(report['reparse']['changes'])


if __name__ == '__main__':
    unittest.main()
