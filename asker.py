"""
Ask — answer a free-form question about a video session.

The caller provides:
  - session: loaded session dict (frame descriptions + transcript)
  - question: anything the user wants to know
  - context: optional string of injected files/text (from context.py)
  - model: which Claude model to use

No synthesis prompt is hardcoded here. Claude figures out the best
response format based on the question. Context injection is what makes
the answer specific to the user's project.
"""

import anthropic


def ask(
    session: dict,
    question: str,
    context: str,
    model: str,
) -> str:
    """
    Send a question to Claude with the video session as knowledge base.
    Returns Claude's response as a string.
    """
    client = anthropic.Anthropic()

    full_transcript = " ".join(seg["text"] for seg in session["transcript"])
    frame_descriptions = session.get("frame_descriptions", [])

    # Build the prompt — context is optional, injected only if provided
    sections = []

    has_frames = bool(frame_descriptions)
    mode_label = "full (transcript + visual)" if has_frames else "transcript-only"

    sections.append(
        f"You have access to a processed video:\n"
        f"  Title   : {session.get('title', session['video_id'])}\n"
        f"  URL     : {session.get('url', '')}\n"
        f"  Duration: {session.get('duration', 0):.0f}s  |  "
        f"Mode: {mode_label}"
    )

    if has_frames:
        all_descriptions = "\n\n".join(
            f"=== Batch {i + 1} ===\n{d}"
            for i, d in enumerate(frame_descriptions)
        )
        sections.append(
            f"FRAME-BY-FRAME VISUAL ANALYSIS\n"
            f"{'─' * 40}\n"
            f"{all_descriptions}"
        )

    sections.append(
        f"TRANSCRIPT\n"
        f"{'─' * 40}\n"
        f"{full_transcript}"
    )

    if context and context.strip():
        sections.append(
            f"CONTEXT PROVIDED BY USER\n"
            f"{'─' * 40}\n"
            f"{context}"
        )

    sections.append(f"QUESTION\n{'─' * 40}\n{question}")

    prompt = "\n\n".join(sections)

    response = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    return response.content[0].text.strip()
