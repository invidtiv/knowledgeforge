"""
Obsidian vault parser with full metadata preservation.

Parses markdown files from Obsidian vaults, extracting:
- Frontmatter metadata (tags, project, status)
- Wiki-links ([[Note]], [[Note#Heading]], [[Note|alias]])
- Inline tags (#tag, #project/subtag)
- Text embeds (![[Note]])
- Heading hierarchy and paths
- Code blocks (preserved atomically)

Creates intelligent chunks with overlap while preserving document structure.
"""

import re
import os
import logging
from pathlib import Path
from typing import Optional
import frontmatter

from knowledgeforge.core.models import Chunk
from knowledgeforge.config import KnowledgeForgeConfig
from knowledgeforge.ingestion.chunker import (
    split_by_tokens,
    count_tokens,
    compute_file_hash,
    generate_chunk_id,
    merge_small_chunks
)

logger = logging.getLogger(__name__)


class ObsidianParser:
    """Parses Obsidian vault markdown files with full metadata preservation."""

    def __init__(self, vault_path: str, config: KnowledgeForgeConfig):
        """
        Initialize the Obsidian parser.

        Args:
            vault_path: Path to the Obsidian vault directory
            config: KnowledgeForge configuration instance
        """
        self.vault_path = Path(vault_path)
        self.config = config
        self.vault_name = self.vault_path.name

        # Cache for resolved embeds to prevent infinite recursion
        self._embed_cache: dict[str, str] = {}

    def parse_file(self, file_path: str) -> list[Chunk]:
        """
        Parse a single Obsidian markdown file into chunks.

        Steps:
        1. Read file, extract frontmatter with python-frontmatter
        2. Extract wiki-links from content
        3. Extract inline tags
        4. Resolve text embeds (![[Note]])
        5. Split by heading sections (H2 primary, H3 secondary boundaries)
        6. For each section:
           a. If under max_chunk_size -> single chunk
           b. If over -> split by paragraphs with overlap
           c. Keep code blocks atomic
        7. Generate file_summary chunk
        8. Attach full metadata to every chunk

        Args:
            file_path: Path to the markdown file

        Returns:
            List of Chunk objects
        """
        try:
            file_path_obj = Path(file_path)

            # Read file content
            with open(file_path_obj, 'r', encoding='utf-8') as f:
                raw_content = f.read()

            # Extract frontmatter and content
            fm_data, content = self.extract_frontmatter(raw_content)

            # Extract metadata before processing
            wiki_links = self._extract_wiki_links(content)
            inline_tags = self._extract_inline_tags(content)

            # Combine frontmatter tags with inline tags
            fm_tags = fm_data.get('tags', [])
            if isinstance(fm_tags, str):
                fm_tags = [fm_tags]
            elif not isinstance(fm_tags, list):
                fm_tags = []
            all_tags = list(set(fm_tags + inline_tags))
            all_tags_str = ",".join(sorted(all_tags))

            # Resolve text embeds
            content = self.resolve_embeds(content)

            # Split content into heading sections
            sections = self._split_by_headings(content)

            # Compute file hash
            file_hash = compute_file_hash(file_path)

            # Get relative path from vault root
            rel_path = os.path.relpath(file_path, self.vault_path)

            chunks = []
            chunk_index = 1  # Start at 1 (0 is reserved for file_summary)

            # Process each section into chunks
            for section in sections:
                section_content = section['content']
                heading_path = section['heading_path']

                # Check if section fits in one chunk
                if count_tokens(section_content) <= self.config.max_chunk_size:
                    # Single chunk for this section
                    chunk = Chunk(
                        chunk_id=generate_chunk_id(rel_path, chunk_index),
                        content=section_content,
                        source_file=rel_path,
                        source_file_hash=file_hash,
                        chunk_index=chunk_index,
                        chunk_type="heading_section" if heading_path else "paragraph",
                        vault_name=self.vault_name,
                        heading_path=heading_path,
                        frontmatter_tags=all_tags_str,
                        frontmatter_project=fm_data.get('project', ''),
                        frontmatter_status=fm_data.get('status', ''),
                        wiki_links_out=",".join(wiki_links),
                    )
                    chunks.append(chunk)
                    chunk_index += 1
                else:
                    # Section too large, split with overlap
                    section_chunks = split_by_tokens(
                        section_content,
                        max_size=self.config.max_chunk_size,
                        overlap=self.config.chunk_overlap
                    )

                    for sc in section_chunks:
                        chunk = Chunk(
                            chunk_id=generate_chunk_id(rel_path, chunk_index),
                            content=sc,
                            source_file=rel_path,
                            source_file_hash=file_hash,
                            chunk_index=chunk_index,
                            chunk_type="heading_section" if heading_path else "paragraph",
                            vault_name=self.vault_name,
                            heading_path=heading_path,
                            frontmatter_tags=all_tags_str,
                            frontmatter_project=fm_data.get('project', ''),
                            frontmatter_status=fm_data.get('status', ''),
                            wiki_links_out=",".join(wiki_links),
                        )
                        chunks.append(chunk)
                        chunk_index += 1

            # Generate file summary chunk
            summary_content = self._make_file_summary(file_path, fm_data, content, sections)
            summary_chunk = Chunk(
                chunk_id=generate_chunk_id(rel_path, 0),
                content=summary_content,
                source_file=rel_path,
                source_file_hash=file_hash,
                chunk_index=0,
                chunk_type="file_summary",
                vault_name=self.vault_name,
                heading_path="",
                frontmatter_tags=all_tags_str,
                frontmatter_project=fm_data.get('project', ''),
                frontmatter_status=fm_data.get('status', ''),
                wiki_links_out=",".join(wiki_links),
            )

            # Insert summary at the beginning
            chunks.insert(0, summary_chunk)

            logger.debug(f"Parsed {file_path}: {len(chunks)} chunks")
            return chunks

        except Exception as e:
            logger.error(f"Error parsing file {file_path}: {e}")
            return []

    def parse_vault(self) -> list[Chunk]:
        """
        Parse all markdown files in the vault.

        Walks the vault directory, skips files matching ignore_patterns,
        parses each .md file, returns all chunks.

        Returns:
            List of all Chunk objects from all files
        """
        all_chunks = []
        files_processed = 0

        logger.info(f"Parsing Obsidian vault: {self.vault_path}")

        for root, dirs, files in os.walk(self.vault_path):
            root_path = Path(root)

            # Skip ignored directories
            dirs[:] = [d for d in dirs if not self._should_ignore(root_path / d)]

            for file in files:
                file_path = root_path / file

                # Skip if not markdown or should be ignored
                if file_path.suffix not in self.config.obsidian_extensions:
                    continue
                if self._should_ignore(file_path):
                    continue

                # Parse the file
                chunks = self.parse_file(str(file_path))
                all_chunks.extend(chunks)
                files_processed += 1

        logger.info(f"Parsed {files_processed} files, created {len(all_chunks)} chunks")
        return all_chunks

    def get_wiki_link_graph(self) -> dict[str, list[str]]:
        """
        Build wiki-link graph: {file -> [linked_files]}.

        Scans all markdown files for [[wiki-links]] and builds adjacency list.

        Returns:
            Dictionary mapping file paths to lists of linked file paths
        """
        graph: dict[str, list[str]] = {}

        logger.info("Building wiki-link graph...")

        for root, dirs, files in os.walk(self.vault_path):
            root_path = Path(root)

            # Skip ignored directories
            dirs[:] = [d for d in dirs if not self._should_ignore(root_path / d)]

            for file in files:
                file_path = root_path / file

                # Skip if not markdown or should be ignored
                if file_path.suffix not in self.config.obsidian_extensions:
                    continue
                if self._should_ignore(file_path):
                    continue

                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()

                    links = self._extract_wiki_links(content)
                    rel_path = os.path.relpath(file_path, self.vault_path)
                    graph[rel_path] = links

                except Exception as e:
                    logger.error(f"Error reading {file_path} for graph: {e}")

        logger.info(f"Built graph with {len(graph)} nodes")
        return graph

    def resolve_embeds(self, content: str) -> str:
        """
        Resolve ![[embed]] references by inlining the target note content.

        Only resolves text embeds (![[Note]] or ![[Note#Section]]).
        Skips image embeds (![[image.png]]).
        Prevents infinite recursion by tracking resolved embeds.

        Args:
            content: Original markdown content

        Returns:
            Content with embeds resolved
        """
        # Pattern for embeds: ![[Target]] or ![[Target#Section]]
        embed_pattern = r'!\[\[([^\]|#]+)(?:#([^\]|]*))?\]\]'

        resolved_embeds = set()

        def replace_embed(match):
            note_name = match.group(1).strip()
            section = match.group(2).strip() if match.group(2) else None

            # Skip images
            image_extensions = ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.pdf']
            if any(note_name.lower().endswith(ext) for ext in image_extensions):
                return match.group(0)  # Keep original

            # Prevent infinite recursion
            embed_key = f"{note_name}#{section}" if section else note_name
            if embed_key in resolved_embeds:
                logger.warning(f"Circular embed detected: {embed_key}")
                return f"[Circular embed: {embed_key}]"

            resolved_embeds.add(embed_key)

            # Find the note file
            note_path = self._find_note(note_name)
            if not note_path:
                logger.warning(f"Embed target not found: {note_name}")
                return f"[Embed not found: {note_name}]"

            try:
                # Read the embedded file
                with open(note_path, 'r', encoding='utf-8') as f:
                    embed_content = f.read()

                # Extract frontmatter (skip it for embeds)
                _, embed_text = self.extract_frontmatter(embed_content)

                # If section specified, extract only that section
                if section:
                    embed_text = self._extract_section(embed_text, section)

                return embed_text

            except Exception as e:
                logger.error(f"Error resolving embed {note_name}: {e}")
                return f"[Error resolving embed: {note_name}]"

        # Apply replacements
        max_iterations = 10  # Prevent infinite loops
        iteration = 0
        while iteration < max_iterations:
            new_content = re.sub(embed_pattern, replace_embed, content)
            if new_content == content:
                break  # No more embeds to resolve
            content = new_content
            iteration += 1

        return content

    def extract_frontmatter(self, content: str) -> tuple[dict, str]:
        """
        Extract YAML frontmatter from content.

        Returns (metadata_dict, content_without_frontmatter).
        Uses python-frontmatter library.

        Args:
            content: Raw markdown content

        Returns:
            Tuple of (frontmatter dict, content without frontmatter)
        """
        try:
            post = frontmatter.loads(content)
            return (post.metadata, post.content)
        except Exception as e:
            logger.warning(f"Error parsing frontmatter: {e}")
            return ({}, content)

    def _extract_wiki_links(self, content: str) -> list[str]:
        """
        Extract all [[wiki-links]] from content.

        Returns list of link targets (without display text).
        Pattern: [[Target]] or [[Target|Display]] or [[Target#Heading]]

        Args:
            content: Markdown content

        Returns:
            List of linked note names
        """
        pattern = r'\[\[([^\]|#]+)(?:#[^\]|]*)?\|?[^\]]*\]\]'
        matches = re.findall(pattern, content)
        # Remove duplicates and clean
        return list(set([m.strip() for m in matches]))

    def _extract_inline_tags(self, content: str) -> list[str]:
        """
        Extract inline #tags from content.

        Pattern: #tag or #project/subtag (but not inside code blocks)

        Args:
            content: Markdown content

        Returns:
            List of tags (without # prefix)
        """
        # Remove code blocks first to avoid extracting tags from code
        content_no_code = re.sub(r'```.*?```', '', content, flags=re.DOTALL)
        content_no_code = re.sub(r'`[^`]+`', '', content_no_code)

        pattern = r'(?<![^\s])#([\w/\-]+)'
        matches = re.findall(pattern, content_no_code)
        # Remove duplicates
        return list(set([m.strip() for m in matches]))

    def _split_by_headings(self, content: str) -> list[dict]:
        """
        Split content by headings into sections.

        Returns list of {"heading": str, "level": int, "content": str, "heading_path": str}
        H2 is primary boundary, H3 is secondary.
        Each section includes its heading path: "H1 > H2 > H3"

        Args:
            content: Markdown content

        Returns:
            List of section dictionaries
        """
        # Pattern to find headings
        heading_pattern = r'^(#{1,6})\s+(.+)$'

        lines = content.split('\n')
        sections = []
        current_section_lines = []
        heading_stack = []  # Stack to track heading hierarchy

        for line in lines:
            match = re.match(heading_pattern, line)

            if match:
                # Save previous section if exists
                if current_section_lines:
                    section_content = '\n'.join(current_section_lines).strip()
                    if section_content:
                        # Build heading path from stack
                        heading_path = " > ".join([h['text'] for h in heading_stack])
                        sections.append({
                            'heading': heading_stack[-1]['text'] if heading_stack else '',
                            'level': heading_stack[-1]['level'] if heading_stack else 0,
                            'content': section_content,
                            'heading_path': heading_path
                        })
                    current_section_lines = []

                # Update heading stack
                level = len(match.group(1))
                heading_text = match.group(2).strip()

                # Pop headings of same or lower level
                while heading_stack and heading_stack[-1]['level'] >= level:
                    heading_stack.pop()

                # Push new heading
                heading_stack.append({'level': level, 'text': heading_text})

                # Start new section with heading
                current_section_lines.append(line)
            else:
                current_section_lines.append(line)

        # Save last section
        if current_section_lines:
            section_content = '\n'.join(current_section_lines).strip()
            if section_content:
                heading_path = " > ".join([h['text'] for h in heading_stack])
                sections.append({
                    'heading': heading_stack[-1]['text'] if heading_stack else '',
                    'level': heading_stack[-1]['level'] if heading_stack else 0,
                    'content': section_content,
                    'heading_path': heading_path
                })

        # If no headings found, return entire content as single section
        if not sections and content.strip():
            sections.append({
                'heading': '',
                'level': 0,
                'content': content.strip(),
                'heading_path': ''
            })

        return sections

    def _should_ignore(self, path: Path) -> bool:
        """
        Check if path matches any ignore pattern.

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

    def _make_file_summary(
        self,
        file_path: str,
        fm_data: dict,
        content: str,
        sections: list[dict]
    ) -> str:
        """
        Generate a summary string for the file_summary chunk.

        Includes: filename, frontmatter fields, first paragraph, list of H2 headings.

        Args:
            file_path: Path to the file
            fm_data: Frontmatter metadata dictionary
            content: Full content (without frontmatter)
            sections: List of heading sections

        Returns:
            Summary text
        """
        summary_parts = []

        # Filename
        filename = Path(file_path).stem
        summary_parts.append(f"# {filename}\n")

        # Frontmatter fields
        if fm_data:
            summary_parts.append("**Metadata:**")
            for key, value in fm_data.items():
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                summary_parts.append(f"- {key}: {value}")
            summary_parts.append("")

        # First paragraph (extract from content)
        first_para = self._extract_first_paragraph(content)
        if first_para:
            summary_parts.append("**Summary:**")
            summary_parts.append(first_para)
            summary_parts.append("")

        # List of H2 headings
        h2_headings = [s['heading'] for s in sections if s['level'] == 2]
        if h2_headings:
            summary_parts.append("**Sections:**")
            for h2 in h2_headings:
                summary_parts.append(f"- {h2}")

        return "\n".join(summary_parts)

    def _extract_first_paragraph(self, content: str) -> str:
        """
        Extract the first meaningful paragraph from content.

        Args:
            content: Markdown content

        Returns:
            First paragraph text
        """
        # Split by double newline
        paragraphs = content.split('\n\n')
        for para in paragraphs:
            # Skip headings, empty lines, code blocks
            if para.strip() and not para.strip().startswith('#') and not para.strip().startswith('```'):
                return para.strip()
        return ""

    def _find_note(self, note_name: str) -> Optional[Path]:
        """
        Find a note file by name in the vault.

        Searches for {note_name}.md in the vault directory tree.

        Args:
            note_name: Name of the note (without extension)

        Returns:
            Path to the note file, or None if not found
        """
        # Try exact match first
        for root, dirs, files in os.walk(self.vault_path):
            root_path = Path(root)

            # Skip ignored directories
            dirs[:] = [d for d in dirs if not self._should_ignore(root_path / d)]

            for file in files:
                if file == f"{note_name}.md":
                    return root_path / file

        return None

    def _extract_section(self, content: str, section_name: str) -> str:
        """
        Extract a specific section from markdown content.

        Args:
            content: Full markdown content
            section_name: Name of the section heading

        Returns:
            Content of that section
        """
        # Find the section heading
        pattern = rf'^#+\s+{re.escape(section_name)}\s*$'
        lines = content.split('\n')
        section_lines = []
        in_section = False
        section_level = 0

        for line in lines:
            if re.match(pattern, line, re.IGNORECASE):
                in_section = True
                section_level = len(re.match(r'^(#+)', line).group(1))
                section_lines.append(line)
                continue

            if in_section:
                # Check if we hit a same-level or higher heading (end of section)
                heading_match = re.match(r'^(#+)\s+', line)
                if heading_match:
                    current_level = len(heading_match.group(1))
                    if current_level <= section_level:
                        break  # End of section

                section_lines.append(line)

        return '\n'.join(section_lines).strip() if section_lines else ""
