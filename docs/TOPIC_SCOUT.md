# Topic scout brief

Every Sunday the topic scout (a scheduled Claude session in the Who Pays Who project) posts three topic ideas in the project thread. Tejas replies with a number; the chosen topic's `auto` command is ready to paste.

For each idea the scout must give:

1. **Business and question:** "How X makes money" or a sharper question in the channel's style.
2. **Angle:** one insight the numbers support, checked against a primary source (a 10-K, annual report, earnings release or regulator), with the figures and links. Say it no more strongly than the source does. Prefer companies with recent results (within the last two quarters).
3. **Demand:** vidIQ search volume and competition for the main keyword, when vidIQ credits allow (the free plan has about 150 a day); otherwise say "not measured".
4. **Competition:** the two or three biggest existing videos on the same topic, and how this angle differs from them.
5. **The command:** `python -m pipeline auto "How X makes money" --angle "..."`

Rules: no topic already made or already proposed in the last 8 weeks (check project memory); about 1 in 5 ideas may be personal finance, always from the business side; no politics, no investment advice, no topic that depends on unsourced claims about living people.
