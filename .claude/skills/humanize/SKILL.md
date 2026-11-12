---
name: humanize
description: Rewrite a draft so it reads like the user wrote it, not a chatbot - strips AI tells (puffery, rule of three, "не лише X, а й Y", em-dash chains, bold-everywhere, closing summaries, канцелярит and English calques) and matches the user's own short, direct voice. Use on every outward draft (Slack, Jira, GitHub PR/issue/comment, email, release note) before review, and when the user asks to "олюднити"/humanize a text.
---

# Humanize

Goal: a colleague reading the text cannot tell an assistant wrote it. Meaning, facts, links and numbers stay exactly as they are; only the wording changes. Never invent details to make it sound natural.

## 1. Match the voice

- Before rewriting a Slack message, read 10-20 of the user's own recent messages in the same channel or DM (Slack search `from:<user>` or the thread) and copy their register: length, greeting or none, lowercase or not, how they ask, how they thank. Write in the language of the audience: the team's own language in Slack DMs and chats, English for Jira/GitHub.
- Drafts to colleagues: the fact and the ask in 2-3 sentences, no explanations or step lists. Every PR/ticket/run gets its URL at first mention.
- Engineers write plainly: "Підняв ліміт на фото до 20 МБ, на тесті працює. Якщо ок, викочу завтра зранку." Not a press release.

## 2. Remove the tells

Content and tone:
- Puffery and significance: «ключовий», «важливий крок», «відіграє вирішальну роль», «pivotal», «crucial», «seamless», «robust», «comprehensive», «landscape», «delve».
- Editorializing participles at the end of a sentence: «…, що підкреслює важливість…», «…, highlighting…».
- Vague attribution: «експерти вважають», «багато хто зазначає».
- Promotional or reassuring filler: «радий повідомити», «сподіваюся, це допоможе», «чудове питання», «дайте знати, якщо…» unless the user really writes that.
- Closing summaries and outlooks: «Отже, підсумовуючи…», «У майбутньому…», "In conclusion".
- Hedging stacks: «можливо, варто було б розглянути» → say it or drop it.

Structure:
- Rule of three («швидко, надійно і зручно») when there are not really three things.
- Negative parallelism: «не лише X, а й Y», «це не просто X — це Y», "not only… but also".
- Colon-reveal and rhetorical questions: «Результат? …», «Причина проста: …».
- Elegant variation: one thing has one name; do not rotate synonyms.
- Em-dash chains. One dash per message at most; prefer a comma, a full stop, or brackets.

Formatting:
- Bold only where the user bolds (almost never in Slack). No bolded lead-in on every bullet.
- Bullets only for real lists of three or more parallel items; otherwise sentences.
- No headings in chat messages, no emoji the user does not use, no Title Case Headings in English.
- Straight quotes in code and commands; «ялинки» in Ukrainian prose as the user writes them.

Ukrainian канцелярит and calques:
- «здійснити/здійснювати» → «зробити»; «у рамках» → «в», «під час»; «з метою» → «щоб»; «наразі» → «зараз»; «даний» → «цей»; «на даний момент» → «зараз»; «проводити перевірку» → «перевіряти»; «являється» → «є».
- Calques: «робить сенс» → «має сенс»; «є критичним» → «дуже важливо» or drop; «на щоденній основі» → «щодня»; «в якості» → «як»; «приймати участь» → «брати участь».

## 3. Check before handing over

- Read it aloud in the user's voice. Anything they would not say goes.
- Compare with the original: every fact, number, name, link and ask is still there; nothing new was added.
- Length went down or stayed the same.
- Return only the rewritten text, then one line listing what you changed if the lead asked for it.
