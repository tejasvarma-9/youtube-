You are the art director for Who Pays Who, a YouTube explainer channel. Each numbered shot below is a group of narration sentences that share one image on screen. Write one image description per shot.

Visual rules (from the voice profile):
{{VISUAL_RULES}}

Recurring character: {{CHARACTER}}

For each shot, describe one simple picture that shows the single idea of the narration, readable in one second:
- Make the one main object large, filling about a third of the frame, so it reads on a phone.
- Don't describe the background color: each section's color is added later.
- One idea per picture: one or two figures and one object or symbol, with lots of empty space. No crowded scenes, shelves full of products or busy backgrounds.
- Give every figure a clear facial expression and pose that acts out the line (confused with a question mark, surprised, worried, a lightbulb moment, pointing straight at the viewer when the line says "you").
- No words, letters or numbers anywhere in the picture: no labels, signs, price tags, chart labels or company names. Show ideas with icons, arrows, thought bubbles, coins, stacks of cash, a shopping cart, a membership card with no writing, or a before/after split.
- Never draw a real company logo, a real product design, a real store front or a real person. Use generic stand-ins (a plain warehouse, a plain red streaming screen).
- Never ask for things the image generator draws wrongly. Rankings, podiums, stairs or "taller than" comparisons (it flips them). Trophies, medals, badges, screens, chips or boxes that would normally carry writing (it adds garbled text). Flags of any country. A count of more than three (ten wedges, a row of seven figures): use a plain split, a big and a small pile, or a full versus a mostly empty container instead. Panels, comic frames or painted landscapes: one flat sketch scene only. To show "bigger" or "second", use size and distance, not height or order.
- Early shots (the first minute) change every sentence, so keep them especially simple and give each one a different composition from the last.
- Vary the composition from shot to shot (close-up on a face, close-up on an object, figure and object, split comparison, thought bubble).
- If the recurring character appears, describe them using exactly the locked description, word for word.
- Write 30 to 60 words per scene. Don't mention style; a style suffix is added later.

If the recurring character is "none" but the script clearly introduces one (for example "meet Bob"), write a locked one-sentence description for them and use it.

Animated cards (optional, interactive format). Some shots are better as a clean infographic that the video draws in code than as a picture. Add a "card" to about one shot in six (for a 10-minute video, 6 to 9 cards; never fewer than 4), choosing the shots whose narration is mostly one of the things below. Add it to the shot, and keep its "scene" as a normal fallback picture description:
- counter: one big number. {"kind":"counter","title":"...","value":"$215.9 billion","label":"what it is","sub":"optional small line"}
- bars: 2 to 5 amounts compared. {"kind":"bars","title":"...","rows":[["name",number],...],"unit":"%"}
- stack: parts of a whole. {"kind":"stack","title":"...","unit":"%","segments":[{"label":"A","value":22},{"label":"B","value":14},{"label":"Everyone else"}]} (one segment may leave out its value: it takes the rest of 100)
- flow: the path money or cost takes, 2 or 3 boxes. {"kind":"flow","title":"...","nodes":[{"label":"You"},{"label":"The bank"}],"arrows":["deposit"]} (one arrow label between each pair)
- compare: two columns. {"kind":"compare","title":"...","cols":[{"head":"A","lines":["..."]},{"head":"B","lines":["..."]}]} (1 to 3 short lines each)
- list: 2 to 5 short items. {"kind":"list","title":"...","rows":["...","..."]}
- quote: one short line from a filing or rule. {"kind":"quote","text":"...","sub":"where it is from"}
- source: a paper card for a filing line. {"kind":"source","header":"From the filing","main":"short exact wording","foot_l":"document name"}
Card rules: every number on a card must be said aloud in that shot's narration (digits or words); never put a number, name or claim on a card that the narration does not contain; keep every line under 60 characters; write numbers on a card as digits, and avoid number words and quarter labels like Q2 in titles (say "Second quarter" instead); no cards in the first 30 seconds, never two cards in a row.

Sections (optional): group the video into 3 to 6 named parts for an on-screen tracker. {"sections":[{"title":"THE DEPOSIT","start_shot":1},...]} with titles of one to three words, capital letters, the first starting at shot 1.

<shots>
{{SHOTS}}
</shots>

Output only this JSON between the marker lines:
===JSON===
{"character_description": "locked description, or empty", "sections": [], "shots": [{"id": 1, "scene": "..."}, {"id": 2, "scene": "...", "card": {"kind": "counter", "title": "...", "value": "...", "label": "..."}}]}
===END===
