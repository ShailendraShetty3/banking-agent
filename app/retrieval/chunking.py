"""Parse governed policy markdown into citeable chunks with governance metadata.

Each document header looks like:
    # Title
    Document ID: POL-CRD-001 | Version 4.2 | Effective 01-Apr-2026 | Owner: Credit Policy

Chunk ids are "<doc_id>#<n>": n is the section number for "## 3. Heading" sections,
otherwise the 1-based position of the section/paragraph within the document.
"""
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# Content that tries to instruct the model. Matched lines are removed from the text
# the LLM ever sees (the original is kept in the DB for audit).
INJECTION_PATTERNS = [
    r"\b(ignore|disregard)\b.{0,40}\b(instruction|policy|policies|citation|grounding|rule)s?\b",
    r"\bsystem (note|prompt|message)\b",
    r"\b(automated|ai) assistants?\b",
    r"\bdo not mention this\b",
    r"\bapprove any\b",
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.I)
_NON_POLICY_RE = re.compile(r"\bnot policy\b|\bdraft\b", re.I)
_PREVAILS_RE = re.compile(r"(POL-[A-Z]+-\d{3})\s+prevails", re.I)


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    heading: str | None
    text: str            # original text (audit)
    safe_text: str       # text with instruction-like lines removed (what the LLM sees)
    injection_flagged: bool


@dataclass
class PolicyDocument:
    doc_id: str
    title: str
    version: str
    effective: str | None
    owner: str | None
    status: str                     # "policy" | "non_policy"
    overridden_by: str | None       # doc this one defers to on conflict
    content_sha: str
    chunks: list[Chunk] = field(default_factory=list)


def _items(text: str) -> list[str]:
    """Group lines into items: a bullet plus its indented continuation lines is one item,
    so an instruction wrapped over several lines is matched and removed as a whole."""
    items: list[str] = []
    for line in text.splitlines():
        if items and line.startswith((" ", "\t")) and line.strip():
            items[-1] += "\n" + line
        else:
            items.append(line)
    return items


def _sanitize(text: str) -> tuple[str, bool]:
    kept, flagged = [], False
    for item in _items(text):
        if _INJECTION_RE.search(" ".join(item.split())):
            flagged = True
            kept.append("- [removed: instruction-like content, not policy]")
        else:
            kept.append(item)
    return "\n".join(kept), flagged


def _split_sections(body: str) -> list[tuple[str | None, str]]:
    """Split on '## ' headings; documents without headings split on blank lines."""
    if re.search(r"^## ", body, re.M):
        parts = re.split(r"^## (.+)$", body, flags=re.M)
        sections = []
        if parts[0].strip():
            sections.append((None, parts[0].strip()))
        for heading, text in zip(parts[1::2], parts[2::2]):
            sections.append((heading.strip(), text.strip()))
        return sections
    return [(None, p.strip()) for p in re.split(r"\n\s*\n", body) if p.strip()]


def parse_document(path: Path) -> PolicyDocument:
    raw = path.read_text(encoding="utf-8")
    lines = raw.strip().splitlines()
    title = lines[0].lstrip("# ").strip()
    header = lines[1]

    def field_(pattern: str) -> str | None:
        m = re.search(pattern, header)
        return m.group(1).strip() if m else None

    doc_id = field_(r"Document ID:\s*([^|]+)")
    if not doc_id:
        raise ValueError(f"{path.name}: missing 'Document ID' header")
    version = field_(r"Version\s+([^|]+)") or ""
    body = "\n".join(lines[2:]).strip()

    status = "non_policy" if _NON_POLICY_RE.search(version + "\n" + body) else "policy"
    prevails = [d for d in _PREVAILS_RE.findall(body) if d != doc_id]

    doc = PolicyDocument(
        doc_id=doc_id, title=title, version=version,
        effective=field_(r"Effective\s+([^|]+)"), owner=field_(r"Owner:\s*([^|]+)"),
        status=status, overridden_by=prevails[0] if prevails else None,
        content_sha=hashlib.sha256(raw.encode()).hexdigest(),
    )
    for i, (heading, text) in enumerate(_split_sections(body), start=1):
        num = re.match(r"(\d+)\.", heading or "")
        safe, flagged = _sanitize(text)
        doc.chunks.append(Chunk(
            chunk_id=f"{doc_id}#{num.group(1) if num else i}",
            doc_id=doc_id, heading=heading, text=text, safe_text=safe, injection_flagged=flagged,
        ))
    return doc


def load_corpus(corpus_dir: Path) -> list[PolicyDocument]:
    return [parse_document(p) for p in sorted(corpus_dir.glob("*.md"))]
