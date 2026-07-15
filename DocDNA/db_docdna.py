#!/usr/bin/env python3
"""
db_docdna.py
-------------
SQLite-based DocDNA database for fast AI queries.
Enhanced schema with FTS5 full-text search, rich function metadata,
call graphs, FAQs, and code content tables.
"""

import sqlite3
import json
import ast
import re
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime


class DocDNADatabase:
    """SQLite database for DocDNA storage and queries"""

    def __init__(self, db_path: Path):
        """Rebuild full-text search index in the database.
        Returns: None
        """
        self.db_path = db_path
        self.conn = None

    def connect(self):
        """Connect to database with optimized settings"""
        self.conn = sqlite3.connect(str(self.db_path))
        # Performance optimizations
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA synchronous=NORMAL')
        self.conn.execute('PRAGMA cache_size=-64000')  # 64MB cache
        self._create_tables()
        self._create_fts_tables()

    def rebuild_fts_index(self):
        """Rebuild FTS index for all tables. Call once after all inserts complete."""
        try:
            self.conn.execute('INSERT INTO functions_fts(functions_fts) VALUES(\'rebuild\')')
        except:
            pass

    def close(self):
        """Close database connection"""
        if self.conn:
            self.conn.close()

    def _create_tables(self):
        """Create all database tables with enhanced schema"""
        with self.conn:
            # Enhanced functions table with rich metadata
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS functions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    args TEXT,           -- JSON list of argument signatures
                    docstring TEXT,      -- Extracted docstring
                    decorators TEXT,     -- JSON list of decorators
                    is_method INTEGER,   -- 1 if method, 0 if function
                    return_type TEXT,    -- Return annotation
                    params TEXT,         -- Rich params description (generated later)
                    impact TEXT,         -- Impact description (generated later)
                    inferred_purpose TEXT, -- Purpose inferred from name/code
                    confidence TEXT,      -- 'high', 'medium', 'low'
                    source TEXT,          -- 'docstring', 'name_inference', 'code_analysis'
                    symbol_id TEXT,
                    UNIQUE(name, file, line)
                )
            ''')

            # Enhanced classes table
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS classes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    bases TEXT,        -- JSON list of base classes
                    docstring TEXT,
                    methods TEXT,      -- JSON list of method names
                    inferred_purpose TEXT,
                    params TEXT,         -- JSON with source_hash for change detection
                    UNIQUE(name, file, line)
                )
            ''')

            # Keywords table
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    word TEXT NOT NULL,
                    file TEXT NOT NULL,
                    line INTEGER NOT NULL,
                    context TEXT,
                    UNIQUE(word, file, line)
                )
            ''')

            # Patterns table
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS patterns (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    type TEXT NOT NULL,
                    file TEXT NOT NULL,
                    line INTEGER,
                    description TEXT,
                    code_snippet TEXT,
                    UNIQUE(type, file, line)
                )
            ''')

            # Capability tags — one row per tag per function
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS function_tags (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    UNIQUE(name, file, tag)
                )
            ''')

            # Call graph table
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS call_graph (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    caller TEXT NOT NULL,
                    callee TEXT NOT NULL,
                    caller_file TEXT NOT NULL,
                    callee_file TEXT,
                    line_number INTEGER,
                    caller_symbol_id TEXT NOT NULL,
                    callee_symbol_id TEXT,
                    UNIQUE(caller_symbol_id, callee, callee_symbol_id)
                )
            ''')

            # Data-flow table — what each function does to shared context/text
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS data_flow (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    file TEXT NOT NULL,
                    action TEXT NOT NULL,  -- reads, modifies, passes-through, returns-new
                    note TEXT,             -- short description of what it does to the text
                    UNIQUE(name, file)
                )
            ''')

            # FAQs table
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS faqs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    refs TEXT,          -- JSON list of file:line references
                    source TEXT,         -- 'docstring', 'file_header', 'generated'
                    UNIQUE(question)
                )
            ''')

            # Architecture insights table
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS architecture_insights (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    description TEXT NOT NULL,
                    details TEXT,        -- JSON with specifics
                    file TEXT,
                    line INTEGER,
                    UNIQUE(category, file)
                )
            ''')

            # Code content table (for line-by-line lookups)
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS code_content (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file TEXT NOT NULL,
                    line_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    indentation INTEGER,
                    UNIQUE(file, line_number)
                )
            ''')

            # Import statements table
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS imports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file TEXT NOT NULL,
                    line_number INTEGER,
                    module TEXT NOT NULL,
                    alias TEXT,
                    from_module TEXT,
                    UNIQUE(file, line_number, module)
                )
            ''')

            # Files table (for joins and metadata)
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    path TEXT UNIQUE NOT NULL,
                    line_count INTEGER,
                    docstring TEXT,
                    module_name TEXT,
                    purpose TEXT,
                    content_hash TEXT,
                    language TEXT
                )
            ''')

            # Migrate older DBs that lack newer columns
            try:
                self.conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
            except Exception:
                pass  # Column already exists
            try:
                self.conn.execute("ALTER TABLE files ADD COLUMN language TEXT")
            except Exception:
                pass  # Column already exists

            # Older databases used UNIQUE(name, file) for classes, which
            # silently discarded same-named definitions in one source file.
            # Rebuild that table once so line-qualified identities are kept.
            class_unique = []
            for index in self.conn.execute("PRAGMA index_list(classes)"):
                if index[2]:
                    class_unique = [row[2] for row in self.conn.execute(
                        f"PRAGMA index_info([{index[1]}])"
                    )]
                    if class_unique == ["name", "file"]:
                        break
            if class_unique == ["name", "file"]:
                self.conn.execute("DROP INDEX IF EXISTS idx_classes_name")
                self.conn.execute("DROP INDEX IF EXISTS idx_classes_file")
                self.conn.execute("ALTER TABLE classes RENAME TO classes_legacy")
                self.conn.execute('''
                    CREATE TABLE classes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        file TEXT NOT NULL,
                        line INTEGER NOT NULL,
                        bases TEXT,
                        docstring TEXT,
                        methods TEXT,
                        inferred_purpose TEXT,
                        params TEXT,
                        UNIQUE(name, file, line)
                    )
                ''')
                self.conn.execute('''
                    INSERT INTO classes
                    (name, file, line, bases, docstring, methods,
                     inferred_purpose, params)
                    SELECT name, file, line, bases, docstring, methods,
                           inferred_purpose, params
                    FROM classes_legacy
                ''')
                self.conn.execute("DROP TABLE classes_legacy")

            # Migrate older databases without changing their human-readable
            # names. Existing call edges cannot recover an exact callee, so
            # they remain unresolved until the next full or incremental sync.
            try:
                self.conn.execute("ALTER TABLE functions ADD COLUMN symbol_id TEXT")
            except Exception:
                pass
            self.conn.execute("""
                UPDATE functions
                SET symbol_id = file || ':' || line || ':' || name
                WHERE symbol_id IS NULL OR symbol_id = ''
            """)
            call_columns = {
                row[1] for row in self.conn.execute("PRAGMA table_info(call_graph)")
            }
            if "caller_symbol_id" not in call_columns:
                self.conn.execute("DROP INDEX IF EXISTS idx_call_caller")
                self.conn.execute("DROP INDEX IF EXISTS idx_call_callee")
                self.conn.execute("ALTER TABLE call_graph RENAME TO call_graph_legacy")
                self.conn.execute('''
                    CREATE TABLE call_graph (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        caller TEXT NOT NULL,
                        callee TEXT NOT NULL,
                        caller_file TEXT NOT NULL,
                        callee_file TEXT,
                        line_number INTEGER,
                        caller_symbol_id TEXT NOT NULL,
                        callee_symbol_id TEXT,
                        UNIQUE(caller_symbol_id, callee, callee_symbol_id)
                    )
                ''')
                self.conn.execute('''
                    INSERT INTO call_graph
                    (caller, callee, caller_file, callee_file, line_number,
                     caller_symbol_id, callee_symbol_id)
                    SELECT caller, callee, caller_file, callee_file, line_number,
                           caller_file || ':0:' || caller, NULL
                    FROM call_graph_legacy
                ''')
                self.conn.execute("DROP TABLE call_graph_legacy")

            # Create indexes for fast lookups
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_funcs_name ON functions(name)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_funcs_file ON functions(file)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_funcs_return ON functions(return_type)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_classes_name ON classes(name)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_classes_file ON classes(file)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_call_caller ON call_graph(caller)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_call_callee ON call_graph(callee)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_faqs_question ON faqs(question)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_code_file_line ON code_content(file, line_number)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_keywords_word ON keywords(word)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module)')
            self.conn.execute('CREATE INDEX IF NOT EXISTS idx_imports_file ON imports(file)')

    def _create_fts_tables(self):
        """Create FTS5 virtual tables for full-text search"""
        with self.conn:
            # FTS for functions (search by name, docstring, args)
            self.conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS functions_fts USING fts5(
                    name,
                    file,
                    docstring,
                    args,
                    inferred_purpose,
                    content='functions',
                    content_rowid='id'
                )
            ''')

            # FTS for code content
            self.conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS code_fts USING fts5(
                    file,
                    content,
                    content='code_content',
                    content_rowid='id'
                )
            ''')

            # FTS for FAQs
            self.conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS faqs_fts USING fts5(
                    question,
                    answer,
                    content='faqs',
                    content_rowid='id'
                )
            ''')

    # ========== Insert Functions ==========

    def insert_functions(self, functions: Dict[str, Dict]):
        """Insert function data with enhanced metadata"""
        rows = []
        for name, data in functions.items():
            display_name = data.get('name') or name
            # Parse arguments from args_json if present
            args_json = data.get('args_json', '[]')
            if isinstance(args_json, str):
                try:
                    args_list = json.loads(args_json)
                except:
                    args_list = []
            else:
                args_list = args_json

            # Parse decorators
            dec_json = data.get('decorators', '[]')
            if isinstance(dec_json, str):
                try:
                    decorators = json.loads(dec_json)
                except:
                    decorators = []
            else:
                decorators = dec_json

            # Determine is_method
            is_method = data.get('is_method', 0) or 0

            # Infer purpose if not provided
            inferred_purpose = data.get('inferred_purpose') or self._infer_purpose_from_name(display_name)

            # Determine source
            source = 'docstring' if data.get('docstring') else 'name_inference'

            rows.append((
                display_name,
                data.get('file', ''),
                data.get('line', 0),
                json.dumps(args_list),
                data.get('docstring'),
                json.dumps(decorators),
                is_method,
                data.get('return_type'),
                inferred_purpose,
                'high' if data.get('docstring') else 'medium',
                source,
                data.get('params'),
                data.get('impact'),
                f"{data.get('file', '')}:{data.get('line', 0)}:{display_name}"
            ))

        with self.conn:
            self.conn.executemany('''
                INSERT OR REPLACE INTO functions
                (name, file, line, args, docstring, decorators, is_method, return_type,
                 inferred_purpose, confidence, source, params, impact, symbol_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', rows)

    def _infer_purpose_from_name(self, name: str) -> str:
        """Infer purpose from function name"""
        # Remove leading underscore
        clean_name = name.lstrip('_')
        # Split on underscores or camelCase
        parts = re.sub(r'([a-z])([A-Z])', r'\1 \2', clean_name).split()
        if parts:
            verb = parts[0].lower()
            if verb in ['get', 'fetch', 'load', 'retrieve']:
                return f"Retrieve {clean_name}"
            elif verb in ['set', 'update', 'assign']:
                return f"Update {clean_name}"
            elif verb in ['create', 'make', 'build', 'generate']:
                return f"Create {clean_name}"
            elif verb in ['delete', 'remove', 'clear']:
                return f"Delete {clean_name}"
            elif verb in ['check', 'validate', 'verify', 'is']:
                return f"Check {clean_name}"
            elif verb in ['process', 'handle']:
                return f"Process {clean_name}"
            elif verb in ['parse', 'extract']:
                return f"Parse {clean_name}"
            elif verb in ['render', 'draw', 'display']:
                return f"Display {clean_name}"
            else:
                return clean_name.title().replace('_', ' ')
        return name

    def _rebuild_functions_fts(self):
        """Rebuild functions FTS index"""
        try:
            self.conn.execute('''
                INSERT INTO functions_fts(functions_fts) VALUES('rebuild')
            ''')
        except:
            pass  # Index might not exist yet

    # ========== Insert Classes ==========

    def insert_classes(self, classes: Dict[str, Dict]):
        """Insert class data.
        Args:
        classes (Dict[str, Dict]): A dictionary where keys are class names and values are dictionaries containing class details.
        Returns:
        None
        """
        """Insert class data"""
        rows = []
        for name, data in classes.items():
            display_name = data.get('name') or name
            bases_json = data.get('bases', '[]')
            if isinstance(bases_json, str):
                try:
                    bases = json.loads(bases_json)
                except:
                    bases = []
            else:
                bases = bases_json

            methods_json = data.get('methods', '[]')
            if isinstance(methods_json, str):
                try:
                    methods = json.loads(methods_json)
                except:
                    methods = []
            else:
                methods = methods_json

            rows.append((
                display_name,
                data.get('file', ''),
                data.get('line', 0),
                json.dumps(bases),
                data.get('docstring'),
                json.dumps(methods),
                display_name  # Class name is usually the purpose
            ))

        with self.conn:
            self.conn.executemany('''
                INSERT OR REPLACE INTO classes
                (name, file, line, bases, docstring, methods, inferred_purpose)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', rows)

    # ========== Insert Keywords ==========

    def insert_keywords(self, keywords: Dict[str, List]):
        """Insert keyword data into the database.
        Args:
        keywords (Dict[str, List]): A dictionary where keys are words and values are lists of locations containing file, line, and context information.
        Returns: None
        """
        """Insert keyword data"""
        rows = []
        for word, locations in keywords.items():
            for loc in locations:
                rows.append((word, loc.get('file', ''), loc.get('line', 0), loc.get('context')))

        with self.conn:
            self.conn.executemany('''
                INSERT OR REPLACE INTO keywords (word, file, line, context)
                VALUES (?, ?, ?, ?)
            ''', rows)

    # ========== Insert Patterns ==========

    def insert_patterns(self, patterns: Dict[str, List]):
        """Insert code pattern data (threading, GUI, error handling, etc.) into the patterns table."""
        for pattern_type, entries in patterns.items():
            for entry in entries:
                file_path = entry.get('file', '')
                line = entry.get('line', 0)
                # Build a human-readable description from the extra keys
                extra = {k: v for k, v in entry.items() if k not in ('file', 'line')}
                description = '; '.join(f"{k}={v}" for k, v in extra.items() if v)
                self.conn.execute('''
                    INSERT OR REPLACE INTO patterns (type, file, line, description, code_snippet)
                    VALUES (?, ?, ?, ?, ?)
                ''', (pattern_type, file_path, line, description, None))

    # ========== Insert Code Content ==========

    def insert_code_content(self, file_path: str, lines: List[str]):
        """Insert line-by-line code content"""
        for i, line in enumerate(lines, 1):
            indentation = len(line) - len(line.lstrip()) if line.strip() else 0
            self.conn.execute('''
                INSERT OR REPLACE INTO code_content (file, line_number, content, indentation)
                VALUES (?, ?, ?, ?)
            ''', (file_path, i, line, indentation))

    # ========== Insert Imports ==========

    def insert_imports(self, file_path: str, imports: List[Dict]):
        """Insert import statements"""
        for imp in imports:
            self.conn.execute('''
                INSERT OR REPLACE INTO imports (file, line_number, module, alias, from_module)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                file_path,
                imp.get('line', 0),
                imp.get('module', ''),
                imp.get('alias'),
                imp.get('from_module', '')
            ))

    # ========== Insert Call Graph ==========

    def insert_call_relationships(self, call_graph: Dict):
        """Insert call relationships"""
        for caller, info in call_graph.items():
            for callee in info.get('calls', []):
                caller_name = info.get('name') or caller
                caller_file = info.get('file', '')
                caller_symbol_id = info.get('symbol_id') or (
                    f"{caller_file}:{info.get('line', 0)}:{caller_name}"
                )
                candidates = self.conn.execute(
                    "SELECT symbol_id, file FROM functions WHERE name = ?", (callee,)
                ).fetchall()
                callee_symbol_id = candidates[0][0] if len(candidates) == 1 else None
                callee_file = candidates[0][1] if len(candidates) == 1 else info.get('callee_file', '')
                self.conn.execute('''
                    INSERT OR REPLACE INTO call_graph
                    (caller, callee, caller_file, callee_file, line_number,
                     caller_symbol_id, callee_symbol_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    caller_name,
                    callee,
                    caller_file,
                    callee_file,
                    info.get('line', 0),
                    caller_symbol_id,
                    callee_symbol_id,
                ))

    # ========== Insert FAQs ==========

    def insert_faqs(self, faqs: List[Dict]):
        """Perform full-text search on functions using SQLite FTS."""
        """Insert FAQs"""
        for faq in faqs:
            self.conn.execute('''
                INSERT OR REPLACE INTO faqs (question, answer, refs, source)
                VALUES (?, ?, ?, ?)
            ''', (
                faq.get('question', ''),
                faq.get('answer', ''),
                json.dumps(faq.get('refs', [])),
                faq.get('source', 'generated')
            ))
        # Rebuild FTS
        self._rebuild_faqs_fts()

    def _rebuild_faqs_fts(self):
        """Rebuild FAQs FTS index"""
        try:
            self.conn.execute("INSERT INTO faqs_fts(faqs_fts) VALUES('rebuild')")
        except:
            pass

    def rebuild_all_fts(self):
        """Rebuild all FTS5 indexes after bulk data insertion."""
        for table in ('functions_fts', 'code_fts', 'faqs_fts'):
            try:
                self.conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
            except Exception:
                pass

    # ========== Insert Architecture Insights ==========

    def insert_architecture_insights(self, insights: List[Dict]):
        """Insert architecture insights"""
        for insight in insights:
            self.conn.execute('''
                INSERT OR REPLACE INTO architecture_insights (category, description, details, file, line)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                insight.get('category', ''),
                insight.get('description', ''),
                json.dumps(insight.get('details', {})),
                insight.get('file', ''),
                insight.get('line', 0)
            ))

    # ========== Insert Files Metadata ==========

    def insert_file_metadata(self, files: List[Dict]):
        """Insert file metadata including content hash for staleness detection."""
        for f in files:
            self.conn.execute('''
                INSERT OR REPLACE INTO files (path, line_count, docstring, module_name, purpose, content_hash, language)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                f.get('path', ''),
                f.get('line_count', 0),
                f.get('docstring'),
                f.get('module_name'),
                f.get('purpose'),
                f.get('content_hash'),
                f.get('language', 'unknown'),
            ))

    # ========== Query Methods ==========

    def query_function(self, name: str) -> Dict:
        """Query function by name"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT name, file, line, args, docstring, return_type, inferred_purpose, confidence, source, params, impact
            FROM functions WHERE name = ?
        ''', (name,))
        row = cursor.fetchone()
        if row:
            return {
                "name": row[0],
                "file": row[1],
                "line": row[2],
                "args": row[3],
                "docstring": row[4],
                "return_type": row[5],
                "inferred_purpose": row[6],
                "confidence": row[7],
                "source": row[8],
                "params": row[9],
                "impact": row[10]
            }
        return {}

    def search_keywords(self, word: str) -> List[Dict]:
        """Search for keyword matches in database."""
        """Search for keyword"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT file, line, context FROM keywords WHERE word = ?', (word,))
        return [{"file": r[0], "line": r[1], "context": r[2]} for r in cursor.fetchall()]

    def search_functions_fts(self, query: str, limit: int = 20) -> List[Dict]:
        """Full-text search on functions"""
        cursor = self.conn.cursor()
        try:
            cursor.execute('''
                SELECT name, file, line, docstring, inferred_purpose
                FROM functions_fts 
                WHERE functions_fts MATCH ?
                LIMIT ?
            ''', (query, limit))
            return [{"name": r[0], "file": r[1], "line": r[2], "docstring": r[3], "purpose": r[4]} for r in cursor.fetchall()]
        except:
            return []

    def search_code_fts(self, query: str, limit: int = 20) -> List[Dict]:
        """Full-text search on code"""
        cursor = self.conn.cursor()
        try:
            cursor.execute('''
                SELECT file, line_number, content
                FROM code_fts
                WHERE code_fts MATCH ?
                LIMIT ?
            ''', (query, limit))
            return [{"file": r[0], "line": r[1], "content": r[2]} for r in cursor.fetchall()]
        except:
            return []

    def search_faqs_fts(self, query: str, limit: int = 10) -> List[Dict]:
        """Full-text search on FAQs"""
        cursor = self.conn.cursor()
        try:
            cursor.execute('''
                SELECT question, answer, refs
                FROM faqs_fts
                WHERE faqs_fts MATCH ?
                LIMIT ?
            ''', (query, limit))
            return [{"question": r[0], "answer": r[1], "refs": r[2]} for r in cursor.fetchall()]
        except:
            return []

    def get_callers(self, function_name: str) -> List[Dict]:
        """Find what calls a function"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT caller, caller_file FROM call_graph WHERE callee = ?
        ''', (function_name,))
        return [{"caller": r[0], "file": r[1]} for r in cursor.fetchall()]

    def get_callees(self, function_name: str) -> List[Dict]:
        """Find what a function calls"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT callee, callee_file FROM call_graph WHERE caller = ?
        ''', (function_name,))
        return [{"callee": r[0], "file": r[1]} for r in cursor.fetchall()]

    def get_code_line(self, file_path: str, line_number: int) -> str:
        """Get a specific line of code"""
        cursor = self.conn.cursor()
        cursor.execute('''
            SELECT content FROM code_content WHERE file = ? AND line_number = ?
        ''', (file_path, line_number))
        row = cursor.fetchone()
        return row[0] if row else ""

    def get_completion_summary(self) -> Dict[str, Any]:
        """Get database summary"""
        cursor = self.conn.cursor()
        counts = {}
        tables = ['functions', 'classes', 'keywords', 'patterns',
                  'call_graph', 'faqs', 'architecture_insights', 'code_content', 'imports', 'files']
        for table in tables:
            try:
                cursor.execute(f'SELECT COUNT(*) FROM {table}')
                counts[table] = cursor.fetchone()[0]
            except:
                counts[table] = 0
        return counts
