import hashlib
import json
import sqlite3

class Archive:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript(
            'CREATE TABLE IF NOT EXISTS captures('
            'id INTEGER PRIMARY KEY, source TEXT NOT NULL, digest TEXT NOT NULL,'
            'raw BLOB NOT NULL, rows_json TEXT NOT NULL, UNIQUE(source,digest));'
            'CREATE TABLE IF NOT EXISTS parses('
            'id INTEGER PRIMARY KEY,'
            'capture INTEGER NOT NULL REFERENCES captures(id),'
            'seq INTEGER NOT NULL, config_json TEXT NOT NULL,'
            'catalog_json TEXT NOT NULL, UNIQUE(capture,seq));')

    def save(self, source, raw, rows):
        if not source.strip():
            raise ValueError('来源标识不能为空')
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.dumps(rows, ensure_ascii=False, sort_keys=True)
        with self.connection:
            self.connection.execute(
                'INSERT OR IGNORE INTO captures(source,digest,raw,rows_json) VALUES(?,?,?,?)',
                (source, digest, raw, payload))
            record = self.connection.execute(
                'SELECT id FROM captures WHERE source=? AND digest=?', (source, digest)).fetchone()
        return record[0]

    def list(self):
        return [dict(id=row[0], source=row[1], digest=row[2], rows=json.loads(row[3]))
                for row in self.connection.execute(
                    'SELECT id,source,digest,rows_json FROM captures ORDER BY id')]

    def raw(self, capture_id):
        row = self.connection.execute('SELECT raw FROM captures WHERE id=?', (capture_id,)).fetchone()
        if row is None:
            raise KeyError(capture_id)
        return bytes(row[0])

    def captures_by_source(self, source):
        return [row[0] for row in self.connection.execute(
            'SELECT id FROM captures WHERE source=? ORDER BY id', (source,))]

    def save_parse(self, capture_id, catalog):
        # 每次解析追加新的序号，重放同一原件与配置得到同一目录内容。
        row = self.connection.execute(
            'SELECT COALESCE(MAX(seq),0) FROM parses WHERE capture=?',
            (capture_id,)).fetchone()
        seq = row[0] + 1
        config = json.dumps(catalog.get('config', {}), ensure_ascii=False, sort_keys=True)
        payload = json.dumps(catalog, ensure_ascii=False, sort_keys=True)
        with self.connection:
            self.connection.execute(
                'INSERT INTO parses(capture,seq,config_json,catalog_json) VALUES(?,?,?,?)',
                (capture_id, seq, config, payload))
        return seq

    def parses(self, capture_id):
        return [dict(seq=row[0], config=json.loads(row[1]))
                for row in self.connection.execute(
                    'SELECT seq,config_json FROM parses WHERE capture=? ORDER BY seq',
                    (capture_id,))]

    def catalog(self, capture_id, seq=None):
        if seq is None:
            row = self.connection.execute(
                'SELECT catalog_json FROM parses WHERE capture=?'
                ' ORDER BY seq DESC LIMIT 1', (capture_id,)).fetchone()
        else:
            row = self.connection.execute(
                'SELECT catalog_json FROM parses WHERE capture=? AND seq=?',
                (capture_id, seq)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
