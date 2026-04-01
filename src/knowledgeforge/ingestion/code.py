"""
Code parser using tree-sitter for AST-aware code chunking.

Parses source code files into semantically meaningful chunks:
- Functions/methods with their docstrings
- Classes with per-method breakdown
- Module summaries with imports
- Heuristic parsing for non-tree-sitter files (YAML, JSON, SQL, etc.)
"""

import os
import re
import logging
from pathlib import Path
from typing import Optional

from knowledgeforge.core.models import Chunk
from knowledgeforge.config import KnowledgeForgeConfig
from knowledgeforge.ingestion.chunker import (
    split_by_tokens_with_lines,
    count_tokens,
    compute_file_hash,
    generate_chunk_id,
)

logger = logging.getLogger(__name__)

CODE_CHUNK_SIZE = 400
CODE_CHUNK_OVERLAP = 80

# Language extension mapping
LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".sh": "bash",
    ".bash": "bash",
}

# Tree-sitter language module mapping
TREE_SITTER_MODULES = {
    "python": "tree_sitter_python",
    "javascript": "tree_sitter_javascript",
    "typescript": "tree_sitter_typescript",
    "rust": "tree_sitter_rust",
    "go": "tree_sitter_go",
    "c": "tree_sitter_c",
    "cpp": "tree_sitter_cpp",
    "bash": "tree_sitter_bash",
}

# Non-tree-sitter file types (use heuristic parsing)
HEURISTIC_EXTENSIONS = {".yaml", ".yml", ".toml", ".json", ".sql"}


class CodeParser:
    """Parses source code files using tree-sitter for AST-aware chunking."""

    def __init__(self, config: KnowledgeForgeConfig):
        self.config = config
        self._parsers = {}  # Cache of language -> tree_sitter.Parser

    def parse_file(self, file_path: str, project_name: str) -> list[Chunk]:
        """Parse a single source code file into chunks.

        Strategy:
        1. Detect language from extension
        2. If tree-sitter supported: parse AST, extract functions/classes/methods
        3. If heuristic file (YAML/JSON/etc): use heuristic splitting
        4. Generate module_summary chunk
        5. Generate per-symbol chunks (function, class, method)

        Args:
            file_path: Absolute path to source file
            project_name: Name of the project this file belongs to

        Returns:
            List of Chunk objects representing the parsed code
        """
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
            return []

        language = self.detect_language(file_path)
        if not language:
            logger.debug(f"Unsupported file type: {file_path}")
            return []

        file_hash = compute_file_hash(file_path)

        # Use heuristic parsing for non-tree-sitter files
        ext = Path(file_path).suffix.lower()
        if ext in HEURISTIC_EXTENSIONS:
            return self._parse_heuristic(file_path, content, project_name, file_hash)

        # Try tree-sitter parsing
        parser = self.get_tree_sitter_parser(language)
        if parser:
            try:
                return self._parse_with_tree_sitter(
                    file_path, content, language, project_name, file_hash
                )
            except Exception as e:
                logger.warning(f"Tree-sitter parsing failed for {file_path}: {e}")
                # Fall back to heuristic parsing
                return self._parse_heuristic(file_path, content, project_name, file_hash)
        else:
            # No tree-sitter support, use heuristic
            return self._parse_heuristic(file_path, content, project_name, file_hash)

    def parse_project(self, project_path: str, project_name: str) -> list[Chunk]:
        """Parse all supported code files in a project directory.

        Walk directory, skip ignore_patterns, parse each file.

        Args:
            project_path: Absolute path to project root
            project_name: Name of the project

        Returns:
            List of all chunks from all parsed files
        """
        all_chunks = []
        project_root = Path(project_path)

        if not project_root.exists():
            logger.error(f"Project path does not exist: {project_path}")
            return []

        # Walk directory tree
        for root, dirs, files in os.walk(project_path):
            root_path = Path(root)

            # Skip ignored directories
            dirs[:] = [d for d in dirs if not self._should_ignore(root_path / d)]

            for file_name in files:
                file_path = root_path / file_name

                # Skip ignored files
                if self._should_ignore(file_path):
                    continue

                # Check if file extension is supported
                ext = file_path.suffix.lower()
                if ext not in self.config.code_extensions:
                    continue

                # Parse the file
                try:
                    chunks = self.parse_file(str(file_path), project_name)
                    all_chunks.extend(chunks)
                    logger.info(f"Parsed {file_path}: {len(chunks)} chunks")
                except Exception as e:
                    logger.error(f"Error parsing {file_path}: {e}")

        return all_chunks

    def detect_language(self, file_path: str) -> str:
        """Detect programming language from file extension.

        Args:
            file_path: Path to the file

        Returns:
            Language identifier (e.g., "python", "javascript") or empty string
        """
        ext = Path(file_path).suffix.lower()
        return LANGUAGE_MAP.get(ext, "")

    def get_tree_sitter_parser(self, language: str) -> Optional[object]:
        """Get or create a tree-sitter parser for a language.

        Returns None if the language grammar is not available.
        Cache parsers for reuse.

        Args:
            language: Language identifier (e.g., "python", "javascript")

        Returns:
            tree_sitter.Parser instance or None if not available
        """
        if language in self._parsers:
            return self._parsers[language]

        module_name = TREE_SITTER_MODULES.get(language)
        if not module_name:
            return None

        try:
            import tree_sitter

            # Special handling for TypeScript (has two sub-languages)
            if language == "typescript":
                import tree_sitter_typescript as tsmod

                # We'll use the TypeScript language by default
                # The parser can handle both .ts and .tsx
                lang = tree_sitter.Language(tsmod.language_typescript())
            else:
                # Dynamic import of tree-sitter language module
                mod = __import__(module_name)
                lang = tree_sitter.Language(mod.language())

            parser = tree_sitter.Parser(lang)
            self._parsers[language] = parser
            return parser
        except ImportError as e:
            logger.warning(f"Tree-sitter module {module_name} not installed: {e}")
            return None
        except Exception as e:
            logger.warning(f"Failed to load tree-sitter for {language}: {e}")
            return None

    def _parse_with_tree_sitter(
        self,
        file_path: str,
        content: str,
        language: str,
        project_name: str,
        file_hash: str,
    ) -> list[Chunk]:
        """Parse a file using tree-sitter AST.

        Extract:
        - Top-level functions (with docstrings)
        - Classes (as summary + per-method chunks)
        - Import blocks
        - Module-level constants

        Node types to look for by language:
        - Python: function_definition, class_definition, import_statement
        - JS/TS: function_declaration, class_declaration, import_statement
        - Rust: function_item, impl_item, struct_item, use_declaration
        - Go: function_declaration, method_declaration, type_declaration
        - C/C++: function_definition, struct_specifier, class_specifier

        Args:
            file_path: Absolute path to source file
            content: File content
            language: Language identifier
            project_name: Project name
            file_hash: SHA256 hash of file content

        Returns:
            List of Chunk objects
        """
        parser = self._parsers.get(language)
        if not parser:
            return []

        tree = parser.parse(content.encode())
        root = tree.root_node

        chunks: list[Chunk] = []
        symbols = []
        source_bytes = content.encode()
        source_file = file_path
        dependencies = self._extract_imports(content, language)

        chunk_index = 0
        for node in root.children:
            node_type = node.type

            if node_type in [
                "function_definition",
                "function_declaration",
                "function_item",
                "method_declaration",
            ]:
                symbol_name = self._get_node_name(node, source_bytes)
                if not symbol_name:
                    continue

                symbols.append(symbol_name)
                node_text = source_bytes[node.start_byte : node.end_byte].decode(
                    errors="replace"
                )
                docstring = self._extract_docstring(node_text, language)
                symbol_chunks = self._split_atomic_chunk(
                    node_text,
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                )

                for part_idx, part in enumerate(symbol_chunks, start=1):
                    part_name = (
                        symbol_name
                        if len(symbol_chunks) == 1
                        else f"{symbol_name}::part{part_idx}"
                    )
                    chunks.append(
                        Chunk(
                            chunk_id=generate_chunk_id(source_file, chunk_index),
                            content=part["content"],
                            file_path=source_file,
                            content_hash=file_hash,
                            chunk_index=chunk_index,
                            chunk_type="function",
                            project_name=project_name,
                            language=language,
                            symbol_name=part_name,
                            start_line=part["start_line"],
                            end_line=part["end_line"],
                            dependencies=dependencies,
                            docstring=docstring,
                        )
                    )
                    chunk_index += 1

            elif node_type in [
                "class_definition",
                "class_declaration",
                "class_specifier",
                "struct_item",
                "impl_item",
            ]:
                class_name = self._get_node_name(node, source_bytes)
                if not class_name:
                    continue

                symbols.append(class_name)
                node_text = source_bytes[node.start_byte : node.end_byte].decode(
                    errors="replace"
                )
                class_summary = self._make_class_summary(
                    node, source_bytes, language, class_name
                )
                class_chunks = self._split_atomic_chunk(
                    class_summary,
                    node.start_point[0] + 1,
                    node.end_point[0] + 1,
                )
                class_docstring = self._extract_docstring(node_text, language)

                for part_idx, part in enumerate(class_chunks, start=1):
                    part_name = (
                        class_name
                        if len(class_chunks) == 1
                        else f"{class_name}::summary_part{part_idx}"
                    )
                    chunks.append(
                        Chunk(
                            chunk_id=generate_chunk_id(source_file, chunk_index),
                            content=part["content"],
                            file_path=source_file,
                            content_hash=file_hash,
                            chunk_index=chunk_index,
                            chunk_type="class",
                            project_name=project_name,
                            language=language,
                            symbol_name=part_name,
                            start_line=part["start_line"],
                            end_line=part["end_line"],
                            dependencies=dependencies,
                            docstring=class_docstring,
                        )
                    )
                    chunk_index += 1

                for child in node.children:
                    if child.type not in [
                        "function_definition",
                        "method_definition",
                        "function_item",
                    ]:
                        continue

                    method_name = self._get_node_name(child, source_bytes)
                    if not method_name:
                        continue

                    method_text = source_bytes[
                        child.start_byte : child.end_byte
                    ].decode(errors="replace")
                    method_docstring = self._extract_docstring(method_text, language)
                    method_chunks = self._split_atomic_chunk(
                        method_text,
                        child.start_point[0] + 1,
                        child.end_point[0] + 1,
                    )

                    for part_idx, part in enumerate(method_chunks, start=1):
                        method_symbol = (
                            f"{class_name}.{method_name}"
                            if len(method_chunks) == 1
                            else f"{class_name}.{method_name}::part{part_idx}"
                        )
                        chunks.append(
                            Chunk(
                                chunk_id=generate_chunk_id(source_file, chunk_index),
                                content=part["content"],
                                file_path=source_file,
                                content_hash=file_hash,
                                chunk_index=chunk_index,
                                chunk_type="method",
                                project_name=project_name,
                                language=language,
                                symbol_name=method_symbol,
                                start_line=part["start_line"],
                                end_line=part["end_line"],
                                dependencies=dependencies,
                                docstring=method_docstring,
                            )
                        )
                        chunk_index += 1

        module_summary = self._make_module_summary(file_path, content, language, symbols)
        summary_chunks = self._split_atomic_chunk(
            module_summary,
            start_line=1,
            end_line=max(1, len(content.splitlines())),
        )
        for part in reversed(summary_chunks):
            chunks.insert(
                0,
                Chunk(
                    chunk_id=generate_chunk_id(source_file, chunk_index),
                    content=part["content"],
                    file_path=source_file,
                    content_hash=file_hash,
                    chunk_index=chunk_index,
                    chunk_type="module_summary",
                    project_name=project_name,
                    language=language,
                    symbol_name="",
                    start_line=part["start_line"],
                    end_line=part["end_line"],
                    dependencies=dependencies,
                    docstring="",
                ),
            )
            chunk_index += 1

        return chunks

    def _parse_heuristic(
        self, file_path: str, content: str, project_name: str, file_hash: str
    ) -> list[Chunk]:
        """Parse non-tree-sitter files using heuristic splitting.

        For YAML/JSON: Split by top-level keys
        For SQL: Split by statements (;)
        For TOML: Split by [sections]
        Fallback: Split by blank-line-separated blocks

        Args:
            file_path: Absolute path to file
            content: File content
            project_name: Project name
            file_hash: SHA256 hash of file content

        Returns:
            List of Chunk objects
        """
        ext = Path(file_path).suffix.lower()
        source_file = file_path
        chunks: list[Chunk] = []
        chunk_index = 0
        total_lines = max(1, len(content.splitlines()))

        if ext == ".sql":
            for statement, start_line, end_line in self._split_sql_statements(content):
                for part in self._split_atomic_chunk(statement, start_line, end_line):
                    chunks.append(
                        Chunk(
                            chunk_id=generate_chunk_id(source_file, chunk_index),
                            content=part["content"],
                            file_path=source_file,
                            content_hash=file_hash,
                            chunk_index=chunk_index,
                            chunk_type="config",
                            project_name=project_name,
                            language="sql",
                            start_line=part["start_line"],
                            end_line=part["end_line"],
                        )
                    )
                    chunk_index += 1

        elif ext in {".yaml", ".yml", ".json", ".toml"}:
            language_name = ext[1:]
            for section_text, start_line, end_line in self._split_blankline_sections(
                content
            ):
                for part in self._split_atomic_chunk(section_text, start_line, end_line):
                    chunks.append(
                        Chunk(
                            chunk_id=generate_chunk_id(source_file, chunk_index),
                            content=part["content"],
                            file_path=source_file,
                            content_hash=file_hash,
                            chunk_index=chunk_index,
                            chunk_type="config",
                            project_name=project_name,
                            language=language_name,
                            start_line=part["start_line"],
                            end_line=part["end_line"],
                        )
                    )
                    chunk_index += 1

        else:
            for part in self._split_atomic_chunk(content, 1, total_lines):
                chunks.append(
                    Chunk(
                        chunk_id=generate_chunk_id(source_file, chunk_index),
                        content=part["content"],
                        file_path=source_file,
                        content_hash=file_hash,
                        chunk_index=chunk_index,
                        chunk_type="config",
                        project_name=project_name,
                        language="text",
                        start_line=part["start_line"],
                        end_line=part["end_line"],
                    )
                )
                chunk_index += 1

        return chunks

    def _split_atomic_chunk(
        self, text: str, start_line: int, end_line: int
    ) -> list[dict]:
        """Split oversized structural chunks using 400/80 token windows.

        Returns a list of dicts with content + line ranges.
        """
        if not text.strip():
            return []

        token_count = count_tokens(text)
        if token_count <= CODE_CHUNK_SIZE:
            return [
                {
                    "content": text,
                    "start_line": int(start_line),
                    "end_line": int(end_line),
                }
            ]

        split_chunks = split_by_tokens_with_lines(
            text=text,
            max_size=CODE_CHUNK_SIZE,
            overlap=CODE_CHUNK_OVERLAP,
            start_line=start_line,
        )
        if not split_chunks:
            return [
                {
                    "content": text,
                    "start_line": int(start_line),
                    "end_line": int(end_line),
                }
            ]

        return [
            {
                "content": c.text,
                "start_line": int(c.start_line),
                "end_line": int(c.end_line),
            }
            for c in split_chunks
        ]

    def _split_sql_statements(self, content: str) -> list[tuple[str, int, int]]:
        """Split SQL content into statements with line ranges."""
        statements: list[tuple[str, int, int]] = []
        pattern = re.compile(r"[^;]+;?", re.DOTALL)

        for match in pattern.finditer(content):
            statement = match.group(0).strip()
            if not statement:
                continue
            start_line = content[: match.start()].count("\n") + 1
            end_line = content[: match.end()].count("\n") + 1
            statements.append((statement, start_line, end_line))

        return statements

    def _split_blankline_sections(self, content: str) -> list[tuple[str, int, int]]:
        """Split config-like content on blank lines while tracking line ranges."""
        lines = content.splitlines()
        if not lines:
            return []

        sections: list[tuple[str, int, int]] = []
        buffer: list[str] = []
        start_line = 1

        for idx, line in enumerate(lines, start=1):
            if not line.strip():
                if buffer:
                    sections.append(("\n".join(buffer).strip(), start_line, idx - 1))
                    buffer = []
                start_line = idx + 1
                continue

            if not buffer:
                start_line = idx
            buffer.append(line)

        if buffer:
            sections.append(("\n".join(buffer).strip(), start_line, len(lines)))

        return [section for section in sections if section[0].strip()]

    def _extract_imports(self, content: str, language: str) -> str:
        """Extract import statements from source code.
        Returns comma-separated list of imported modules.

        Python: import X, from X import Y
        JS/TS: import X from 'Y', require('Y')
        Rust: use X::Y
        Go: import "X"

        Args:
            content: Source code content
            language: Language identifier

        Returns:
            Comma-separated string of imported modules
        """
        imports = []

        if language == "python":
            # Match: import foo, from foo import bar
            import_pattern = r"^\s*(?:import|from)\s+([\w.]+)"
            for match in re.finditer(import_pattern, content, re.MULTILINE):
                imports.append(match.group(1))

        elif language in ["javascript", "typescript"]:
            # Match: import X from 'Y', require('Y')
            import_pattern = r"(?:import|require)\s*\(?['\"]([^'\"]+)['\"]"
            for match in re.finditer(import_pattern, content):
                imports.append(match.group(1))

        elif language == "rust":
            # Match: use foo::bar
            import_pattern = r"^\s*use\s+([\w:]+)"
            for match in re.finditer(import_pattern, content, re.MULTILINE):
                imports.append(match.group(1))

        elif language == "go":
            # Match: import "foo"
            import_pattern = r'^\s*import\s+["\']([^"\']+)["\']'
            for match in re.finditer(import_pattern, content, re.MULTILINE):
                imports.append(match.group(1))

        elif language in ["c", "cpp"]:
            # Match: #include <foo> or #include "foo"
            import_pattern = r'^\s*#include\s+[<"]([^>"]+)[>"]'
            for match in re.finditer(import_pattern, content, re.MULTILINE):
                imports.append(match.group(1))

        # Return unique imports as comma-separated string
        return ",".join(sorted(set(imports)))

    def _extract_docstring(self, node_text: str, language: str) -> str:
        """Extract docstring from a function/class definition.
        Returns first 200 characters.

        Python: triple-quoted string at start of function body
        JS/TS/Rust/Go: comment block before function

        Args:
            node_text: Text of the function/class node
            language: Language identifier

        Returns:
            Docstring (first 200 characters)
        """
        docstring = ""

        if language == "python":
            # Look for triple-quoted string at start
            match = re.search(
                r'^\s*(?:def|class)\s+\w+[^:]*:\s*["\']{{3}}(.*?)["\']{{3}}',
                node_text,
                re.DOTALL,
            )
            if match:
                docstring = match.group(1).strip()

        elif language in ["javascript", "typescript", "rust", "go", "c", "cpp"]:
            # Look for comment block (// or /* */) at start
            lines = node_text.split("\n")
            comment_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped.startswith("//"):
                    comment_lines.append(stripped[2:].strip())
                elif stripped.startswith("/*"):
                    # Multi-line comment
                    comment_text = stripped[2:]
                    if "*/" in comment_text:
                        comment_text = comment_text.split("*/")[0]
                    comment_lines.append(comment_text.strip())
                elif stripped.startswith("*") and comment_lines:
                    # Continuation of multi-line comment
                    comment_lines.append(stripped[1:].strip())
                elif comment_lines:
                    # End of comment block
                    break
            docstring = " ".join(comment_lines)

        # Return first 200 characters
        return docstring[:200] if docstring else ""

    def _get_node_name(self, node, source_bytes: bytes) -> str:
        """Extract the name (identifier) from a tree-sitter node.

        Args:
            node: tree-sitter node
            source_bytes: Source code as bytes

        Returns:
            Name of the symbol or empty string
        """
        # Look for child node with type "identifier" or "name"
        for child in node.children:
            if child.type in ["identifier", "name"]:
                return source_bytes[child.start_byte : child.end_byte].decode(
                    errors="replace"
                )

        # Fallback: try to extract name from node text
        node_text = source_bytes[node.start_byte : node.end_byte].decode(
            errors="replace"
        )
        # Extract first identifier-like token after keyword
        match = re.search(r"(?:def|class|fn|func|function)\s+(\w+)", node_text)
        if match:
            return match.group(1)

        return ""

    def _make_class_summary(
        self, node, source_bytes: bytes, language: str, class_name: str
    ) -> str:
        """Generate class summary: docstring + method list.

        Args:
            node: tree-sitter node for the class
            source_bytes: Source code as bytes
            language: Language identifier
            class_name: Name of the class

        Returns:
            Summary text
        """
        node_text = source_bytes[node.start_byte : node.end_byte].decode(
            errors="replace"
        )
        docstring = self._extract_docstring(node_text, language)

        # Extract method names
        methods = []
        for child in node.children:
            if child.type in ["function_definition", "method_definition", "function_item"]:
                method_name = self._get_node_name(child, source_bytes)
                if method_name:
                    methods.append(method_name)

        summary = f"Class: {class_name}\n"
        if docstring:
            summary += f"\n{docstring}\n"
        if methods:
            summary += f"\nMethods: {', '.join(methods)}\n"

        return summary

    def _make_module_summary(
        self, file_path: str, content: str, language: str, symbols: list[str]
    ) -> str:
        """Generate module summary: imports + docstring + symbol list.

        Args:
            file_path: Path to the file
            content: File content
            language: Language identifier
            symbols: List of top-level symbol names

        Returns:
            Module summary text
        """
        file_name = Path(file_path).name
        imports = self._extract_imports(content, language)

        # Extract module-level docstring
        module_docstring = ""
        if language == "python":
            # Look for module-level docstring (triple-quoted string at start)
            match = re.match(r'^\s*["\']{{3}}(.*?)["\']{{3}}', content, re.DOTALL)
            if match:
                module_docstring = match.group(1).strip()[:200]

        summary = f"Module: {file_name}\n"
        if module_docstring:
            summary += f"\n{module_docstring}\n"
        if imports:
            summary += f"\nImports: {imports}\n"
        if symbols:
            summary += f"\nSymbols: {', '.join(symbols)}\n"

        return summary

    def _should_ignore(self, path: Path) -> bool:
        """Check if path matches any ignore pattern.

        Args:
            path: Path to check

        Returns:
            True if path should be ignored
        """
        path_str = str(path)
        for pattern in self.config.ignore_patterns:
            if pattern in path_str:
                return True
        return False
