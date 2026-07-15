#!/usr/bin/env python3
"""
docdna_sync.py — Mr. Smith: Incremental DocDNA sync for changed files.

Re-parses only changed .py files and updates the database in-place,
avoiding a full regeneration. ~300ms per changed file.

    Usage:
        docdna-sync <project> --files f1.py f2.py   # Database-only sync
        docdna-sync <project> --files f1.py --write-docstrings  # Explicit source edits
    docdna-sync <project> --git                   # Legacy Git-only incremental sync
    docdna-sync <project> --all                   # Re-scan everything (light regen)
    docdna-sync <project> --install-hook          # Set up git post-commit hook
    docdna-sync <project> --remove-hook           # Remove git hook
    docdna-sync <project> --init                  # Create AGENTS.md, sub-agent, git hook
"""

import sys
import os
import json
import sqlite3
import re
import ast
import hashlib
import subprocess
from pathlib import Path
from typing import Optional
from collections import defaultdict

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(errors="replace")

# Reuse AST parsing helpers from post_docdna_enhance.py
sys.path.insert(0, str(Path(__file__).resolve().parent))
from post_docdna_enhance import (
    EXCLUDE_DIRS,
    get_annotation,
    get_decorator_name,
    _infer_purpose_from_name,
    setup_mr_smith,
)
from multilang_parser import is_supported_source, parse_multilang_file

EXCLUDE_DIRS = EXCLUDE_DIRS | {'DocDNA', 'DocDNA_Tool'}


# ---------------------------------------------------------------------------
# AST parsing for a single file (mirrors post_docdna_enhance.parse_project_files)
# ---------------------------------------------------------------------------

def parse_file(target_dir: Path, rel_path: str) -> dict:
    """Parse a supported source file and return extracted metadata.

    Returns dict with keys:
        functions, classes, imports, call_graph, code_lines, file_meta
    """
    full_path = target_dir / rel_path
    if full_path.suffix.lower() != ".py":
        return parse_multilang_file(full_path, rel_path)
    content = full_path.read_text(encoding='utf-8', errors='ignore')
    tree = ast.parse(content, filename=str(full_path))
    lines = content.splitlines()

    result = {
        "functions": {},
        "classes": {},
        "imports": [],
        "call_graph": {},
        "code_lines": lines,
        "file_meta": {
            "path": rel_path,
            "line_count": len(lines),
            "docstring": ast.get_docstring(tree),
            "module_name": full_path.stem,
            "purpose": _infer_file_purpose(ast.get_docstring(tree), full_path.stem),
            "content_hash": hashlib.md5(full_path.read_bytes()).hexdigest(),
            "language": "python",
        },
    }

    parent_map = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }

    for node in ast.walk(tree):
        # Classes
        if isinstance(node, ast.ClassDef):
            bases = [get_annotation(b) for b in node.bases if b]
            methods = [n.name for n in node.body if isinstance(n, ast.FunctionDef)]
            docstring = ast.get_docstring(node)

            class_key = f"{rel_path}:{node.lineno}:{node.name}"
            result["classes"][class_key] = {
                "name": node.name,
                "file": rel_path,
                "line": node.lineno,
                "bases": bases,
                "docstring": docstring,
                "methods": methods,
            }

        # Functions
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_name = node.name

            args_list = []
            for arg in node.args.args:
                if arg.arg not in ('self', 'cls'):
                    arg_str = arg.arg
                    if arg.annotation:
                        arg_str += f": {get_annotation(arg.annotation)}"
                    args_list.append(arg_str)

            return_type = get_annotation(node.returns) if node.returns else None
            docstring = ast.get_docstring(node)
            inferred = docstring or _infer_purpose_from_name(func_name)

            is_method = int(isinstance(parent_map.get(node), ast.ClassDef))

            function_key = f"{rel_path}:{node.lineno}:{func_name}"
            result["functions"][function_key] = {
                "name": func_name,
                "file": rel_path,
                "line": node.lineno,
                "args_json": json.dumps(args_list),
                "docstring": docstring,
                "is_method": is_method,
                "return_type": return_type,
                "inferred_purpose": inferred,
                "decorators": json.dumps([get_decorator_name(d) for d in node.decorator_list]),
            }

            # Call graph
            calls = []
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name):
                        calls.append(child.func.id)
                    elif isinstance(child.func, ast.Attribute):
                        calls.append(child.func.attr)

            if calls:
                result["call_graph"][function_key] = {
                    "name": func_name,
                    "line": node.lineno,
                    "calls": list(set(calls)),
                    "file": rel_path,
                }

        # Imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                result["imports"].append({
                    "module": alias.name,
                    "alias": alias.asname,
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ''
            for alias in node.names:
                result["imports"].append({
                    "module": f"{module}.{alias.name}" if module else alias.name,
                    "alias": alias.asname,
                    "from_module": module,
                    "line": node.lineno,
                })

    return result


def _infer_file_purpose(docstring: str, module_name: str) -> str:
    """Infer file purpose from docstring or module name."""
    if docstring:
        first_line = docstring.split('\n')[0].strip()
        if first_line:
            return first_line
    return f"Python module: {module_name}"


# ---------------------------------------------------------------------------
# Docstring sync (Mr. Smith keeps docs current)
# ---------------------------------------------------------------------------

def _get_stored_hash(conn: sqlite3.Connection, name: str, file: str, line: int) -> Optional[str]:
    """Get stored source_hash for a function/class from DB params column."""
    try:
        c = conn.cursor()
        c.execute("SELECT params FROM functions WHERE name = ? AND file = ? AND line = ?", (name, file, line))
        row = c.fetchone()
        if row and row[0]:
            params = json.loads(row[0])
            return params.get("source_hash")
        c.execute("SELECT params FROM classes WHERE name = ? AND file = ? AND line = ?", (name, file, line))
        row = c.fetchone()
        if row and row[0]:
            params = json.loads(row[0])
            return params.get("source_hash")
    except Exception:
        pass
    return None


def _sync_docstrings(conn: sqlite3.Connection, target_dir: Path, rel_path: str,
                     allow_source_edits: bool = False):
    """Compare current source hashes with stored DB hashes.

    Regenerates docstrings for any function/class whose body changed.
    Updates the source file in-place with new docstrings.
    """
    if Path(rel_path).suffix.lower() != ".py" or not allow_source_edits:
        return

    from doc_writer import documented_items, load_ai_config

    full_path = target_dir / rel_path
    if not full_path.exists():
        return
    if full_path.stat().st_size > 500_000:
        return  # skip huge files
    if _is_excluded_path(rel_path):
        return

    try:
        source = full_path.read_text(encoding="utf-8")
    except Exception:
        return

    # Compute current hashes
    current_hashes = documented_items(source)
    if not current_hashes:
        return

    # Compare with stored hashes (query before deletion)
    needs_update = {}
    for (name, line), current_hash in current_hashes.items():
        stored_hash = _get_stored_hash(conn, name, rel_path, line)
        if stored_hash != current_hash:
            needs_update[(name, line)] = current_hash

    if not needs_update:
        return

    # Load AI config and generate docstrings (batched to cut API calls)
    ai_config = load_ai_config()
    from doc_writer import find_undocumented, _generate_batch_docstrings, insert_docstring
    import ast

    print(f"    ✍️ {len(needs_update)} function(s) changed, updating docstrings")

    undocumented = find_undocumented(source)
    # Filter to only the ones that need updating
    to_fix = [u for u in undocumented if (u["name"], u["line"]) in needs_update]

    if not to_fix:
        # All changed functions already have good docstrings — just update hashes
        _store_hashes(conn, rel_path, needs_update)
        return

    # Generate docstrings in batches (1 request per batch, not per item)
    items_sorted = sorted(to_fix, key=lambda x: x["line"], reverse=True)
    docstring_map = {}
    batch_size = int(ai_config.get("batch_size", 15))
    batches = [items_sorted[i:i + batch_size] for i in range(0, len(items_sorted), batch_size)]
    for batch in batches:
        docstrings = _generate_batch_docstrings(batch, ai_config)
        for item, docstring in zip(batch, docstrings):
            docstring_map[id(item)] = docstring

    # Apply in reverse line order so insertions don't shift remaining line numbers
    for item in items_sorted:
        docstring = docstring_map.get(id(item))
        if not docstring:
            print(f"      {item['kind']} {item['name']} ... SKIP")
            continue
        preview = docstring.splitlines()[0][:60]
        print(f"      {item['kind']} {item['name']} ... \"{preview}\"")
        new_source = insert_docstring(source, item, docstring)

        # Verify the result is still valid Python before committing
        try:
            ast.parse(new_source)
        except SyntaxError as e:
            print(f"      SKIP (would break syntax: {e})")
            continue

        source = new_source

    # Backup original before overwriting
    bak_path = full_path.with_name(full_path.name + ".bak")
    if not bak_path.exists():
        bak_path.write_text(full_path.read_text(encoding="utf-8"), encoding="utf-8")

    # Write updated source
    full_path.write_text(source, encoding="utf-8")

    # Update hashes in DB (before main sync deletes/re-inserts)
    # We need the final hashes from the now-updated source
    final_hashes = documented_items(full_path.read_text(encoding="utf-8"))
    _store_hashes(conn, rel_path, {
        symbol: final_hashes.get(symbol, h)
        for symbol, h in needs_update.items()
    })


def _store_hashes(conn: sqlite3.Connection, file: str, hashes: dict[tuple[str, int], str]):
    """Update params column with new source hashes for functions/classes."""
    c = conn.cursor()
    for (name, line), h in hashes.items():
        params = json.dumps({"source_hash": h})
        c.execute("UPDATE functions SET params = ? WHERE name = ? AND file = ? AND line = ?",
                  (params, name, file, line))
        try:
            c.execute("UPDATE classes SET params = ? WHERE name = ? AND file = ? AND line = ?",
                      (params, name, file, line))
        except sqlite3.OperationalError:
            pass  # classes table may not have params column
    conn.commit()


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _is_excluded_path(rel_path: str) -> bool:
    """Check path components, not the absolute project path."""
    return any(part in EXCLUDE_DIRS for part in Path(rel_path).parts)


def _remove_file_records(conn: sqlite3.Connection, rel_path: str):
    """Remove every record derived from a file, including cross-file edges."""
    c = conn.cursor()
    # Remove unresolved edges whose callee name belongs to this file before
    # deleting the function rows that make that relationship discoverable.
    c.execute("""
        DELETE FROM call_graph
        WHERE caller_file = ? OR callee_file = ?
           OR (callee_file = '' AND callee IN (
               SELECT name FROM functions WHERE file = ?
           ))
    """, (rel_path, rel_path, rel_path))
    for table in ("functions", "classes", "keywords", "code_content", "imports",
                  "patterns", "function_tags", "data_flow", "files"):
        column = "path" if table == "files" else "file"
        c.execute(f"DELETE FROM {table} WHERE {column} = ?", (rel_path,))
    c.execute("DELETE FROM faqs WHERE refs LIKE ?", (f"%{rel_path}%",))
    c.execute("DELETE FROM architecture_insights WHERE file = ?", (rel_path,))


def sync_file(conn: sqlite3.Connection, target_dir: Path, rel_path: str,
              allow_source_edits: bool = False):
    """Sync a single file into the database: remove stale, insert fresh."""
    # Parse before deleting anything. A syntax error must never leave the
    # previous valid index partially erased.
    try:
        _sync_docstrings(conn, target_dir, rel_path, allow_source_edits)
        data = parse_file(target_dir, rel_path)
    except SyntaxError as e:
        print(f"  ⚠️ Syntax error in {rel_path}: {e}")
        return False
    except Exception as e:
        print(f"  ⚠️ Failed to parse {rel_path}: {e}")
        return False

    c = conn.cursor()
    _remove_file_records(conn, rel_path)

    # Insert functions with source hash in params
    source_text = "\n".join(data["code_lines"])
    for _, func_data in data["functions"].items():
        if Path(rel_path).suffix.lower() == ".py":
            from doc_writer import get_body_hash
            current_hash = get_body_hash(
                source_text,
                func_data.get("name", ""),
                rel_path,
                func_data.get("line", 0),
            ) or ""
        else:
            current_hash = hashlib.md5(source_text.encode("utf-8")).hexdigest()
        params_json = json.dumps({"source_hash": current_hash})
        c.execute('''
            INSERT OR REPLACE INTO functions
            (name, file, line, args, docstring, is_method, return_type,
             inferred_purpose, confidence, source, decorators, params)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            func_data.get("name", ""),
            func_data.get("file", ""),
            func_data.get("line", 0),
            func_data.get("args_json", "[]"),
            func_data.get("docstring"),
            func_data.get("is_method", 0),
            func_data.get("return_type"),
            func_data.get("inferred_purpose"),
            "high" if func_data.get("docstring") else "medium",
            "docstring" if func_data.get("docstring") else "name_inference",
            func_data.get("decorators", "[]"),
            params_json,
        ))
        c.execute("UPDATE functions SET symbol_id = ? WHERE name = ? AND file = ? AND line = ?",
                  (f"{rel_path}:{func_data.get('line', 0)}:{func_data.get('name', '')}",
                   func_data.get("name", ""), rel_path, func_data.get("line", 0)))

    # Insert classes
    for _, class_data in data["classes"].items():
        c.execute('''
            INSERT OR REPLACE INTO classes
            (name, file, line, bases, docstring, methods)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            class_data.get("name", ""),
            class_data.get("file", ""),
            class_data.get("line", 0),
            json.dumps(class_data.get("bases", [])),
            class_data.get("docstring"),
            json.dumps(class_data.get("methods", [])),
        ))

    # Insert code content
    for i, line in enumerate(data["code_lines"], 1):
        c.execute('''
            INSERT OR REPLACE INTO code_content (file, line_number, content, indentation)
            VALUES (?, ?, ?, ?)
        ''', (rel_path, i, line, len(line) - len(line.lstrip()) if line.strip() else 0))

    # Insert imports
    for imp in data["imports"]:
        c.execute('''
            INSERT OR REPLACE INTO imports (file, line_number, module, alias, from_module)
            VALUES (?, ?, ?, ?, ?)
        ''', (rel_path, imp.get("line", 0), imp.get("module", ""),
              imp.get("alias"), imp.get("from_module", "")))

    # Insert call graph
    for _, info in data["call_graph"].items():
        for callee in info.get("calls", []):
            candidates = c.execute(
                "SELECT symbol_id, file FROM functions WHERE name = ?", (callee,)
            ).fetchall()
            callee_symbol_id = candidates[0][0] if len(candidates) == 1 else None
            callee_file = candidates[0][1] if len(candidates) == 1 else ""
            caller_symbol_id = f"{rel_path}:{info.get('line', 0)}:{info.get('name', '')}"
            c.execute('''
                INSERT OR REPLACE INTO call_graph
                (caller, callee, caller_file, callee_file, line_number,
                 caller_symbol_id, callee_symbol_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (info.get("name", ""), callee, info.get("file", ""), callee_file, 0,
                  caller_symbol_id, callee_symbol_id))

    # 8. Upsert file metadata
    fm = data["file_meta"]
    c.execute('''
        INSERT OR REPLACE INTO files (path, line_count, docstring, module_name, purpose, content_hash, language)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (fm["path"], fm["line_count"], fm["docstring"], fm["module_name"], fm["purpose"], fm.get("content_hash"), fm.get("language", "unknown")))

    conn.commit()
    return True


def rebuild_fts(conn: sqlite3.Connection):
    """Rebuild all FTS5 indexes."""
    for table in ('functions_fts', 'code_fts', 'faqs_fts'):
        try:
            conn.execute(f"INSERT INTO {table}({table}) VALUES('rebuild')")
        except Exception:
            pass
    conn.commit()


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def find_source_files(target_dir: Path, rel_paths: list[str]) -> list[str]:
    """Resolve relative paths to supported source files within the project."""
    resolved = []
    for p in rel_paths:
        # If not already absolute, resolve relative to target_dir, not CWD
        candidate = Path(p)
        if not candidate.is_absolute():
            candidate = target_dir / p
        try:
            rel = candidate.resolve().relative_to(target_dir.resolve())
            if is_supported_source(rel):
                resolved.append(str(rel))
        except ValueError:
            print(f"  Skipping {p} (outside project)")
    return resolved


def files_changed_in_git(target_dir: Path) -> list[str]:
    """Get supported source files changed in the last commit."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True, text=True, cwd=target_dir, timeout=10,
        )
        if result.returncode != 0:
            result = subprocess.run(
                ["git", "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", "HEAD"],
                capture_output=True, text=True, cwd=target_dir, timeout=10,
            )
        files = [f.strip() for f in result.stdout.splitlines()
                 if is_supported_source(Path(f.strip()))]
        return files
    except (subprocess.TimeoutExpired, FileNotFoundError):
        print("  ⚠️ Git not available or not a git repo")
        return []


def all_source_files(target_dir: Path) -> list[str]:
    """All supported source files in the project (excluding DocDNA, etc.)."""
    files = []
    for f in target_dir.rglob("*"):
        if not f.is_file() or not is_supported_source(f):
            continue
        rel = f.relative_to(target_dir)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        files.append(str(rel))
    return sorted(files)


def all_py_files(target_dir: Path) -> list[str]:
    """Backward-compatible alias for callers using the old function name."""
    return all_source_files(target_dir)


# ---------------------------------------------------------------------------
# Git hook management
# ---------------------------------------------------------------------------

def _hook_content(target_dir: Path) -> str:
    """Build a hook that selects an incremental or full refresh safely."""
    refresh_script = Path(__file__).with_name("docdna_refresh.py")
    return f"""#!/bin/sh
# Post-commit hook for DocDNA refresh (installed by docdna-sync --install-hook)
"{Path(sys.executable).resolve().as_posix()}" "{refresh_script.resolve().as_posix()}" "{target_dir.resolve().as_posix()}" --git
"""


def install_hook(target_dir: Path):
    """Install the safe DocDNA refresh hook after each Git commit."""
    git_dir = target_dir / ".git" / "hooks"
    if not git_dir.exists():
        print("  ⚠️ No .git/hooks directory found")
        return False

    hook_path = git_dir / "post-commit"
    content = _hook_content(target_dir)

    hook_path.write_text(content)
    hook_path.chmod(0o755)
    print(f"  ✅ Installed post-commit hook at {hook_path}")
    return True


def remove_hook(target_dir: Path):
    """Remove git post-commit hook if installed by us."""
    hook_path = target_dir / ".git" / "hooks" / "post-commit"
    if hook_path.exists() and "DocDNA refresh" in hook_path.read_text():
        hook_path.unlink()
        print(f"  ✅ Removed post-commit hook at {hook_path}")
        return True
    print("  No docdna-sync hook found")
    return False


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def count_table(cur, table: str) -> int:
    """Function to count rows in a database table using SQL query."""
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        return cur.fetchone()[0]
    except Exception:
        return 0


def rebuild_function_locator(conn: sqlite3.Connection, docdna_dir: Path):
    """Rebuild function_locator.json from the database."""
    c = conn.cursor()
    locator = {}
    try:
        c.execute("SELECT name, file, line, inferred_purpose FROM functions")
        for r in c.fetchall():
            name, file, line, purpose = r
            locator[name] = {
                "file": file,
                "line": line,
                "purpose": purpose or "",
                "category": "public_function" if not name.startswith("_") else "private_function",
            }
        with open(docdna_dir / "ai_instant" / "function_locator.json", "w") as f:
            json.dump(locator, f, indent=2)
    except Exception as e:
        print(f"  ⚠️ Failed to update function_locator.json: {e}")


def rebuild_call_graph_json(docdna_dir: Path):
    """Clear call_graph.json (now in DB — kept for human readers)."""
    try:
        with open(docdna_dir / "code_details" / "call_graph.json", "w") as f:
            json.dump({}, f)
    except Exception:
        pass


def print_summary(conn: sqlite3.Connection, files_synced: list[str]):
    """Print a short summary of the sync."""
    c = conn.cursor()
    print(f"  Synced {len(files_synced)} files")
    print(f"  DB now has: {count_table(c, 'functions')} functions, "
          f"{count_table(c, 'classes')} classes, "
          f"{count_table(c, 'call_graph')} call edges, "
          f"{count_table(c, 'code_content')} code lines, "
          f"{count_table(c, 'imports')} imports, "
          f"{count_table(c, 'files')} files")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    """Main function to handle AI profile resolution and execute main logic."""
    if len(sys.argv) < 3:
        print(__doc__)
        return 1

    # Normal syncs are parser-only. Resolve an AI profile only for the explicit
    # source-editing mode so automatic refresh never waits for user input.
    if "--write-docstrings" in sys.argv:
        from doc_writer import get_profiles, resolve_ai_profile
        _explicit = None
        for i, arg in enumerate(sys.argv):
            if arg == "--ai-profile" and i + 1 < len(sys.argv):
                _explicit = sys.argv[i + 1]
                break
        _chosen = resolve_ai_profile(get_profiles(), explicit=_explicit)
        if _chosen:
            os.environ["AI_PROFILE"] = _chosen

    target_dir = Path(sys.argv[1]).resolve()

    target_dir = Path(sys.argv[1]).resolve()
    if not target_dir.exists():
        print(f"Error: project not found: {target_dir}")
        return 1

    target_dir = target_dir.resolve()

    # Commands that don't need DocDNA to exist
    if "--init" in sys.argv:
        from post_docdna_enhance import setup_mr_smith
        setup_mr_smith(target_dir)
        return 0

    if "--install-hook" in sys.argv:
        install_hook(target_dir)
        return 0

    if "--remove-hook" in sys.argv:
        remove_hook(target_dir)
        return 0

    # Locate DocDNA folder
    docdna_dir = target_dir / "DocDNA"
    if not docdna_dir.exists():
        print(f"Error: DocDNA folder not found at {docdna_dir}")
        return 1

    db_path = docdna_dir / "docdna.db"
    if not db_path.exists():
        print(f"Error: docdna.db not found at {db_path}")
        return 1

    try:
        from db_docdna import DocDNADatabase
        schema_db = DocDNADatabase(db_path)
        schema_db.connect()
        schema_db.close()
    except ImportError:
        # Older generated DocDNA folders may not contain the schema helper.
        # New full builds copy it beside this script.
        pass

    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')

    # Find files to sync
    files_to_sync = []

    if "--git" in sys.argv:
        files_to_sync = files_changed_in_git(target_dir)
        if not files_to_sync:
            print("  No supported source files changed in last commit")
    elif "--all" in sys.argv:
        files_to_sync = all_source_files(target_dir)
    elif "--files" in sys.argv:
        idx = sys.argv.index("--files")
        raw_files = sys.argv[idx + 1:]
        files_to_sync = find_source_files(target_dir, raw_files)
        if not files_to_sync:
            print("Error: no valid source files specified after --files")
            conn.close()
            return 1
    else:
        # Default: look for --files or --git
        print("Error: specify --files, --git, --all, --install-hook, or --remove-hook")
        print(__doc__)
        conn.close()
        return 1

    print(f"Mr. Smith syncing {len(files_to_sync)} file(s) for {target_dir.name}...")

    synced = []
    failed = 0
    for rel_path in files_to_sync:
        full_path = target_dir / rel_path
        if not full_path.exists() and "--git" in sys.argv:
            _remove_file_records(conn, rel_path)
            synced.append(rel_path)
            print(f"  Removed deleted file from index: {rel_path}")
            continue
        if not full_path.exists():
            print(f"  ⚠️ File not found: {rel_path}")
            failed += 1
            continue
        if not is_supported_source(rel_path):
            continue
        if _is_excluded_path(rel_path):
            continue

        try:
            ok = sync_file(conn, target_dir, rel_path,
                           allow_source_edits="--write-docstrings" in sys.argv)
        except Exception as e:
            conn.rollback()
            print(f"  ⚠️ Failed to replace {rel_path}: {e}")
            ok = False
        if ok:
            synced.append(rel_path)
        else:
            failed += 1

    if synced:
        rebuild_fts(conn)
        rebuild_function_locator(conn, docdna_dir)
        rebuild_call_graph_json(docdna_dir)
        print_summary(conn, synced)

    conn.close()

    if failed:
        print(f"  ⚠️ {failed} file(s) failed to sync")
        return 1

    print("  ✅ DocDNA is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
