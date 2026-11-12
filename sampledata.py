def samples():
    catalog = '@string{venue = "Port" # " Conference"}\n@proceedings{conf, title={Annual {Port} Meeting}, publisher=venue}\n@inproceedings{paper, author={de la Cruz, Ana and {Harbor Group}}, title={A {B} study}, crossref={conf}}\n@string{venue = "New Venue"}\n@article{later, title=venue}\n'
    return {'sample.bib': catalog.encode(), 'cycle.bib': b'@book{a,crossref={b}}\n@book{b,crossref={a}}',
            'invalid.bib': b'plain text without entries'}
