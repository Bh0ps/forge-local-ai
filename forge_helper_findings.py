"""Conservative helper completion checks, independent of length and tool count.

This catches acknowledgments and promises, not every semantic omission. Actual
answers can be brief, contain no tool calls, or report that no defect was found.
"""
import re


FINDINGS_INSTRUCTIONS = (
    'Finish with concrete findings, an answer, or a clearly stated lack of findings supported by the supplied evidence. '
    'Do not finish with only an acknowledgment or a promise to inspect something. '
    'Use existing evidence instead of repeating completed tools. '
    'If access or missing evidence prevents the assignment, begin with "Blocked:" and explain the specific blocker; '
    'a blocked assignment is not verified completion.')
CORRECTION = (
    'Your last response stated an intention or acknowledgment without usable findings. '
    'Continue only the original bounded assignment and return the actual answer with supporting evidence or a concrete blocker. '
    'Reuse completed tool results; do not repeat completed actions. Do not expand scope, permissions or the task. '
    + FINDINGS_INSTRUCTIONS)

_ACK = re.compile(r'(?i)^(?:(?:ok(?:ay)?|sure|understood|got it|on it|done|completed|working on it|ready)[\s,.!;:]*)+$')
_INTENT = re.compile(r"(?i)^(?:(?:sure|okay|ok|understood)[,:]?\s+)?(?:"
    r"(?:i|we)(?:\s+will|['’]ll|\s+(?:am|are)\s+going\s+to)\s+"
    r"(?:now\s+)?(?:inspect|investigate|research|review|examine|read|fetch|gather|check|look|analy[sz]e|test|report|start|proceed|return|finish|complete|work|get started)\b"
    r"|let\s+(?:me|us)\s+(?:inspect|investigate|research|review|examine|read|check|look|analy[sz]e|get started)\b)")
_FINDING = re.compile(r'(?i)\b(?:found|observed|verified|measured|because|caused|evidence shows)\b|https?://|\bline\s+\d+\b')


def completion_issue(answer,assignment=''):
    text = answer.strip()
    if not text: return 'empty_result'
    plain = re.sub(r'[*_`]', '', text).strip()
    # A literal-response assignment may legitimately ask for an acknowledgment.
    literal=re.fullmatch(r'(?is)\s*(?:please\s+)?(?:reply|respond|answer|return|say)\s+(?:(?:with|only|exactly)\s+)*["\'“‘]?(.+?)["\'”’]?[.!]?\s*',assignment)
    if literal and plain.casefold().rstrip('.!')==literal[1].casefold().rstrip('.!'): return None
    # Explicit blockers are usable reports, but cannot satisfy required work.
    if re.match(r'(?i)^blocked\s*:', plain): return 'assignment_blocked'
    if _ACK.fullmatch(plain): return 'unusable_result'
    # Do not mistake code, citations, findings, or an explanatory answer for a
    # future-tense promise. No minimum length or universal tool requirement.
    if '```' in text or _FINDING.search(plain): return None
    sentences = [part.strip() for part in re.split(r'[.!?;](?:\s+|$)', plain) if part.strip()]
    if sentences and all(_INTENT.match(part) or _ACK.fullmatch(part) for part in sentences):
        return 'unusable_result'
    return None
