You write the YouTube packaging for Who Pays Who. The voice profile is below; follow its title formats, thumbnail rules and description footer.

<voice_profile>
{{VOICE}}
</voice_profile>

<script_with_timestamps>
{{TIMED_SCRIPT}}
</script_with_timestamps>

<sources>
{{SOURCES}}
</sources>

Write:
- title_options: 3 candidate titles, each under 60 characters, each in a different format.
- title: the best of the three.
- thumbnail_text: 2 to 4 words, all caps, readable at phone size, that add a curiosity gap to the title rather than repeat it. Use a figure from the fact list (like "$29.50 OF $100") when one is surprising; never claim more than the numbers show.
- thumbnail_subject: a 40 to 70 word description of a split-screen scene: what the left "before" half shows (the cost or problem) and what the right "after" half shows (the payoff), in generic stand-ins only (a generic TV or store, cash on fire, a green growth arrow, stacks of coins). No real brands, logos, posters, show titles or people, no text.
- thumbnail_before and thumbnail_after: one short word each for the two halves' labels (like "COST" and "PROFIT"); they must match what the facts show.
- description_intro: 2 or 3 plain sentences that say what the viewer will learn. No hashtags, no emojis, no "in this video". Promise only what the fact list shows: no "decides whether" or "burns money" claims, and no saying a policy or event "changes the math" unless a fact says so.
- tags: 5 to 10 search phrases people would actually type.
- chapters: 5 to 9 chapters. Each starts at a sentence number from the script above (the first must be sentence 1) and gets a short title of 2 to 5 words. Chapters must be at least 30 seconds apart.

Output only this JSON between the marker lines:
===JSON===
{"title_options": [], "title": "", "thumbnail_text": "", "thumbnail_subject": "", "thumbnail_before": "", "thumbnail_after": "", "description_intro": "", "tags": [], "chapters": [{"sentence": 1, "title": "..."}]}
===END===
