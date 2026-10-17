You write the spoken opening question for Shorts on Who Pays Who, a YouTube explainer channel. Each Short is a vertical clip made only of sentences from a finished, fact-checked video. Before those sentences play, the narrator asks ONE new question, recorded in the same voice. It is the first thing a viewer hears, so it decides whether they keep watching.

<voice_profile>
{{VOICE}}
</voice_profile>

The full video is titled: {{TITLE}}

<shorts>
{{SHORTS}}
</shorts>

For each Short, write opening_question:
- One question, at most 14 words, ending with "?". Spoken English: plain words, no abbreviations or symbols the narrator would stumble on.
- The Short's narration must answer it by the end. Do not ask something the Short only touches on.
- It must not give away the answer, and must not repeat the first sentence or the on-screen text word for word.
- It may only rely on what the Short's own narration says. No numbers, names or claims that are not in that narration, no stronger words than the narration uses ("all", "always", "secret", "scam"), no blame or motive the narration does not state.
- Make it about something a stranger already knows (a hotel room, a bill, a building), not industry jargon.

Output only this JSON between the marker lines, one entry per Short:
===JSON===
{"questions": [{"short": 1, "opening_question": ""}]}
===END===
