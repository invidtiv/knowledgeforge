"""Obsidian write-back module for promoting confirmed discoveries to the vault."""

import os
import logging
import re
from pathlib import Path
from datetime import datetime, timezone

from knowledgeforge.core.models import Discovery

logger = logging.getLogger(__name__)


class DiscoveryPromoter:
    """Writes confirmed discoveries back to the Obsidian vault as markdown notes.

    Each promoted discovery becomes a note in the configured discoveries folder:
    {vault_path}/{discoveries_folder}/{project}/{category}_{date}_{short_id}.md
    """

    def __init__(self, vault_path: str, discoveries_folder: str = "KnowledgeForge Discoveries"):
        self.vault_path = Path(vault_path)
        self.discoveries_folder = discoveries_folder
        logger.info(f"DiscoveryPromoter initialized with vault: {self.vault_path}")

    def promote(self, discovery: Discovery) -> str:
        """Write a single confirmed discovery as an Obsidian markdown note.

        Note format:
        ---
        project: {project}
        category: {category}
        severity: {severity}
        source_agent: {source_agent}
        discovered: {created_at}
        confirmed: {confirmed_at}
        tags: [knowledgeforge/discovery, project/{project}, category/{category}]
        ---

        # {Auto-generated title from content}

        ## Discovery

        {content}

        ## Context

        {context}

        ## Related Files

        - `{file1}`
        - `{file2}`

        ## Source

        Agent: {source_agent} | Session: {source_session} | Date: {created_at}

        Steps:
        1. Create directory structure: vault/discoveries_folder/project/
        2. Generate filename: {category}_{date}_{short_id}.md
        3. Auto-generate title from first line/sentence of content
        4. Write the markdown file with frontmatter
        5. Return the full path of the created note

        Args:
            discovery: A confirmed Discovery object

        Returns:
            Full path of the created Obsidian note
        """
        if not discovery.confirmed:
            logger.warning(f"Attempted to promote unconfirmed discovery: {discovery.discovery_id}")
            raise ValueError("Cannot promote unconfirmed discovery")

        # Step 1: Create directory structure
        project_name = discovery.project or "General"
        target_dir = self.vault_path / self.discoveries_folder / project_name
        target_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Created/verified directory: {target_dir}")

        # Step 2: Generate filename
        filename = self._generate_filename(discovery)
        file_path = target_dir / filename

        # Step 3: Auto-generate title
        title = self._generate_title(discovery.content)

        # Step 4: Format and write the markdown file
        note_content = self._format_note(discovery, title)

        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(note_content)

        logger.info(f"Promoted discovery {discovery.discovery_id} to {file_path}")

        # Step 5: Return full path
        return str(file_path.resolve())

    def promote_all_confirmed(self, discoveries: list[Discovery]) -> list[str]:
        """Promote all confirmed, un-promoted discoveries.

        Args:
            discoveries: List of confirmed discoveries to promote

        Returns:
            List of file paths for all created notes
        """
        promoted_paths = []

        for discovery in discoveries:
            if discovery.confirmed and not discovery.promoted_to_obsidian:
                try:
                    file_path = self.promote(discovery)
                    promoted_paths.append(file_path)
                    logger.info(f"Successfully promoted discovery {discovery.discovery_id}")
                except Exception as e:
                    logger.error(f"Failed to promote discovery {discovery.discovery_id}: {e}")

        logger.info(f"Promoted {len(promoted_paths)} discoveries out of {len(discoveries)} candidates")
        return promoted_paths

    def _generate_title(self, content: str) -> str:
        """Auto-generate a title from discovery content.

        Strategy:
        1. Take first sentence (up to first period, question mark, or newline)
        2. Truncate to 80 chars max
        3. Clean up any markdown formatting
        """
        # Take first line or first sentence
        first_line = content.split('\n')[0]

        # Find first sentence-ending punctuation
        sentence_end = re.search(r'[.!?]\s', first_line)
        if sentence_end:
            title = first_line[:sentence_end.start()].strip()
        else:
            title = first_line.strip()

        # Remove markdown formatting
        title = re.sub(r'[*_`#\[\]]', '', title)

        # Truncate to 80 chars
        if len(title) > 80:
            title = title[:77] + "..."

        # Fallback if title is empty
        if not title:
            title = "Discovery"

        return title

    def _generate_filename(self, discovery: Discovery) -> str:
        """Generate a filename for the discovery note.

        Format: {category}_{YYYYMMDD}_{short_id}.md
        short_id = first 8 chars of discovery_id
        """
        # Extract date from created_at (ISO format)
        try:
            created_dt = datetime.fromisoformat(discovery.created_at.replace('Z', '+00:00'))
            date_str = created_dt.strftime('%Y%m%d')
        except (ValueError, AttributeError):
            date_str = datetime.now(timezone.utc).strftime('%Y%m%d')

        # Get short ID
        short_id = discovery.discovery_id[:8]

        # Sanitize category for filename (alphanumeric only)
        category = re.sub(r'[^\w]', '', discovery.category)

        filename = f"{category}_{date_str}_{short_id}.md"
        return filename

    def _format_note(self, discovery: Discovery, title: str) -> str:
        """Format the complete Obsidian note with frontmatter and content.

        Use YAML frontmatter between --- delimiters.
        Include all sections: Discovery, Context, Related Files, Source.
        """
        # Build YAML frontmatter
        frontmatter_lines = ["---"]

        if discovery.project:
            frontmatter_lines.append(f"project: {discovery.project}")

        frontmatter_lines.append(f"category: {discovery.category}")
        frontmatter_lines.append(f"severity: {discovery.severity}")
        frontmatter_lines.append(f"source_agent: {discovery.source_agent}")
        frontmatter_lines.append(f"discovered: {discovery.created_at}")

        if discovery.confirmed_at:
            frontmatter_lines.append(f"confirmed: {discovery.confirmed_at}")

        # Build tags
        tags = ["knowledgeforge/discovery"]
        if discovery.project:
            tags.append(f"project/{discovery.project}")
        tags.append(f"category/{discovery.category}")
        tags.append(f"severity/{discovery.severity}")

        frontmatter_lines.append(f"tags: [{', '.join(tags)}]")
        frontmatter_lines.append("---")

        # Build note body
        note_lines = []
        note_lines.append(f"# {title}")
        note_lines.append("")
        note_lines.append("## Discovery")
        note_lines.append("")
        note_lines.append(discovery.content)
        note_lines.append("")

        # Context section (optional)
        if discovery.context:
            note_lines.append("## Context")
            note_lines.append("")
            note_lines.append(discovery.context)
            note_lines.append("")

        # Related Files section (optional)
        if discovery.related_files:
            note_lines.append("## Related Files")
            note_lines.append("")
            for file_path in discovery.related_files:
                note_lines.append(f"- `{file_path}`")
            note_lines.append("")

        # Source section
        note_lines.append("## Source")
        note_lines.append("")
        source_parts = [
            f"**Agent**: {discovery.source_agent}",
        ]
        if discovery.source_session:
            source_parts.append(f"**Session**: {discovery.source_session}")
        source_parts.append(f"**Date**: {discovery.created_at}")

        note_lines.append(" | ".join(source_parts))
        note_lines.append("")

        # Combine frontmatter and body
        full_note = "\n".join(frontmatter_lines) + "\n\n" + "\n".join(note_lines)

        return full_note
