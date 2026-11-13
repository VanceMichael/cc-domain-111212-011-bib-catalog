def samples():
    catalog = '@string{venue = "Port" # " Conference"}\n@proceedings{conf, title={Annual {Port} Meeting}, publisher=venue}\n@inproceedings{paper, author={de la Cruz, Ana and {Harbor Group}}, title={A {B} study}, crossref={conf}}\n@string{venue = "New Venue"}\n@article{later, title=venue}\n'
    xdata = '@string{venue = "Port" # " Conference"}\n@book{series, editor={G{\\"o}del, Kurt}, publisher=venue, title={Lecture Notes}}\n@collection{vol, title={Selected {P}apers}, xdata={series}}\n@string{venue = "New Venue"}\n@book{broken, title=}\n@article{loose, title={Studies}, journal=unknownvenue}\n'
    return {'sample.bib': catalog.encode(), 'xdata.bib': xdata.encode(),
            'cycle.bib': b'@book{a,crossref={b}}\n@book{b,crossref={a}}',
            'invalid.bib': b'plain text without entries'}
